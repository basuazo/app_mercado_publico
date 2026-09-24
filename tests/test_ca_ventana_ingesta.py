"""Tests F-ca-ventana — ingesta incremental de Compra Ágil en ventanas y parser v2.

Nada pega a la red: el cliente v2 es un falso que responde por ventana y
página, o el cliente real detrás de respx. El reloj se congela con freezegun
(ahora_utc usa datetime.now).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from freezegun import freeze_time
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.clients.base import (
    MPConcurrencyError,
    MPRateLimitError,
    MPServerError,
    reset_rate_limiter_compartido,
)
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.clients.types import (
    CompraAgilBasica,
    PaginacionV2,
    RespuestaListadoV2,
    parse_fecha_iso,
    parse_fecha_v2,
)
from app.core.settings import Settings
from app.ingest.compra_agil import CompraAgilIngestaError, sync_incremental
from app.models.tables import CompraAgil, SyncState

_AHORA = datetime(2026, 9, 22, 15, 0, 0)  # UTC naive, como ahora_utc()
_LIMITE = _AHORA - timedelta(minutes=10)
_CINCO = timedelta(minutes=5)

_ENV = {
    "MP_TICKET": "ticket-test-ca-ventana",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-ca-ventana-32bytesxxxx",
    "JOBS_TOKEN": "token-test-ca-ventana-xxxxxxxxxxx",
}


def _settings(monkeypatch: pytest.MonkeyPatch, **extra: object) -> Settings:
    for k, v in {**_ENV, **{k.upper(): str(v) for k, v in extra.items()}}.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


def _con_cursor(session: Session, cursor: datetime) -> None:
    session.add(SyncState(fuente="compra_agil", cursor=cursor.isoformat()))
    session.commit()


def _cursor(engine) -> datetime | None:
    with Session(engine) as s:
        state = s.get(SyncState, "compra_agil")
        return datetime.fromisoformat(state.cursor) if state and state.cursor else None


def _ca(codigo: str, cambio: datetime | None = None) -> CompraAgilBasica:
    return CompraAgilBasica(
        codigo=codigo,
        nombre=f"CA {codigo}",
        estado="publicada",
        fecha_publicacion=None,
        fecha_cierre=None,
        fecha_ultimo_cambio=cambio,
        monto_clp=None,
        region=13,
        organismo_nombre=None,
        organismo_rut=None,
        total_ofertas=0,
    )


def codigo(desde: datetime | None, pagina: int) -> str:
    return f"{'frio' if desde is None else f'{desde:%m%d-%H%M}'}-P{pagina}"


class _ApiFalsa:
    """Cliente v2 falso: numera las ventanas en el orden en que se abren
    (página 1 de un par desde/hasta nuevo) y responde por (ventana, página).

    - `paginas`: páginas por ventana (1 por defecto), o función (desde, hasta)
      → páginas, para simular volumen según el ancho; cada página trae un ítem
      cuyo código sale de la ventana real y la página (ver `codigo`), salvo
      `vacias=True`. Así, repetir una ventana devuelve los mismos códigos.
    - `fallar`: {(ventana, página): excepción} para simular un corte.
    - `total`: función (desde, hasta) → total_resultados informado, o None.
    - `al_abrir`: callback(ventana) al pedir la página 1 de cada ventana.
    """

    def __init__(
        self,
        paginas: Any = 1,
        vacias: bool = False,
        fallar: dict[tuple[int, int], BaseException] | None = None,
        total: Any = None,
        al_abrir: Any = None,
    ) -> None:
        self.paginas = paginas
        self.vacias = vacias
        self.fallar = fallar or {}
        self.total = total
        self.al_abrir = al_abrir
        self.llamadas: list[dict[str, Any]] = []
        self._ventanas: list[tuple[Any, Any]] = []

    def ventanas(self) -> list[tuple[datetime, datetime]]:
        return list(self._ventanas)

    def listar_compra_agil(self, **kw: Any) -> RespuestaListadoV2:
        self.llamadas.append(kw)
        par = (kw.get("cambio_desde"), kw.get("cambio_hasta"))
        pagina = kw["numero_pagina"]
        if pagina == 1 and (not self._ventanas or self._ventanas[-1] != par):
            self._ventanas.append(par)
            if self.al_abrir:
                self.al_abrir(len(self._ventanas))
        ventana = len(self._ventanas)
        exc = self.fallar.get((ventana, pagina))
        if exc is not None:
            raise exc
        paginas = self.paginas(*par) if callable(self.paginas) else self.paginas
        items = [] if self.vacias else [_ca(codigo(par[0], pagina), par[1])]
        total = self.total(*par) if self.total else len(items) * paginas
        return RespuestaListadoV2(
            items=items,
            paginacion=PaginacionV2(
                total_paginas=paginas,
                total_resultados=total,
                numero_pagina=pagina,
                tamano_pagina=kw.get("tamano_pagina", 20),
            ),
        )


# ---------------------------------------------------------------------------
# Parser v2: todas las fechas en hora de Chile, la Z es falsa
# ---------------------------------------------------------------------------


class TestParseFechaV2:
    def test_la_z_se_ignora_en_septiembre_utc_menos_3(self) -> None:
        assert parse_fecha_v2("2026-09-21T15:00:00.740Z") == datetime(2026, 9, 21, 18, 0, 0, 740000)

    def test_la_z_se_ignora_en_julio_utc_menos_4(self) -> None:
        assert parse_fecha_v2("2026-07-21T15:00:00.740Z") == datetime(2026, 7, 21, 19, 0, 0, 740000)

    def test_con_espacio_y_sin_segundos(self) -> None:
        assert parse_fecha_v2("2026-09-21 14:59") == datetime(2026, 9, 21, 17, 59)

    def test_el_cambio_de_horario_del_6_sep(self) -> None:
        # Chile adelanta el reloj el domingo 6-sep-2026 a las 00:00 (UTC-4 → UTC-3).
        assert parse_fecha_v2("2026-09-05T12:00:00Z") == datetime(2026, 9, 5, 16, 0)
        assert parse_fecha_v2("2026-09-07T12:00:00Z") == datetime(2026, 9, 7, 15, 0)

    def test_valores_vacios_o_no_texto(self) -> None:
        assert parse_fecha_v2(None) is None
        assert parse_fecha_v2("") is None
        assert parse_fecha_v2(123) is None

    def test_parse_fecha_iso_sigue_respetando_la_z(self) -> None:
        """No se tocó el parser genérico: la corrección es solo de la v2."""
        assert parse_fecha_iso("2026-09-21T15:00:00.740Z") == datetime(
            2026, 9, 21, 15, 0, 0, 740000
        )

    @respx.mock
    def test_el_cliente_v2_lee_todas_sus_fechas_en_hora_de_chile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = _settings(monkeypatch)
        reset_rate_limiter_compartido()
        eng = create_engine("sqlite:///:memory:")
        item = {
            "codigo": "3136-19-COT26",
            "nombre": "x",
            "estado": "publicada",
            "fechas": {
                "fecha_publicacion": "2026-09-21T09:00:00Z",
                "fecha_cierre": "2026-09-23 13:00",
                "fecha_ultimo_cambio": "2026-09-21T15:00:00.740Z",
            },
        }
        respx.get("https://api2.mercadopublico.cl/v2/compra-agil").mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": "OK",
                    "payload": {"convocatorias": [item], "paginacion": {"total_paginas": 1}},
                },
            )
        )
        try:
            ca = (
                MercadoPublicoV2Client(settings, eng)
                .listar_compra_agil(estados=["publicada"])
                .items[0]
            )
        finally:
            eng.dispose()
            reset_rate_limiter_compartido()

        assert ca.fecha_ultimo_cambio == datetime(2026, 9, 21, 18, 0, 0, 740000)
        assert ca.fecha_publicacion == datetime(2026, 9, 21, 12, 0)
        assert ca.fecha_cierre == datetime(2026, 9, 23, 16, 0)


# ---------------------------------------------------------------------------
# Ventanas
# ---------------------------------------------------------------------------


@freeze_time(_AHORA)
class TestVentanas:
    def test_30_h_de_atraso_en_ventanas_de_6_h(self, monkeypatch, session, engine) -> None:
        settings = _settings(monkeypatch, ca_ventana_horas=6)
        cursor0 = _AHORA - timedelta(hours=30)
        _con_cursor(session, cursor0)
        cursores_al_abrir: list[datetime | None] = []
        api = _ApiFalsa(al_abrir=lambda _v: cursores_al_abrir.append(_cursor(engine)))

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        ventanas = api.ventanas()
        assert len(ventanas) in (5, 6)
        # Cada ventana parte 5 min antes del final de la anterior y mide 6 h,
        # salvo la última, recortada a ahora − 10 min.
        cursor = cursor0
        for i, (desde, hasta) in enumerate(ventanas):
            assert desde == cursor - _CINCO
            assert hasta == min(desde + timedelta(hours=6), _LIMITE)
            # El cursor ya avanzó (y se commiteó) al abrir cada ventana.
            assert cursores_al_abrir[i] == cursor
            cursor = hasta
        assert _cursor(engine) == _LIMITE
        assert resultado["ventanas_completadas"] == len(ventanas)
        assert resultado["cursor_final"] == _LIMITE.isoformat()
        assert resultado["atraso_horas"] == pytest.approx(10 / 60, abs=0.01)
        assert resultado["requests_usadas"] == len(ventanas)
        assert resultado["ventanas_partidas"] == 0
        assert resultado["nuevas"] == len(ventanas)

    def test_siempre_manda_cambio_hasta_y_el_tamano_de_pagina(self, monkeypatch, session) -> None:
        settings = _settings(monkeypatch, ca_tamano_pagina=20)
        _con_cursor(session, _AHORA - timedelta(hours=5))
        api = _ApiFalsa(paginas=2)

        sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert api.llamadas
        for kw in api.llamadas:
            assert kw["cambio_hasta"] is not None
            assert kw["tamano_pagina"] == 20
            assert kw["estados"] == ["cerrada", "proveedor_seleccionado", "publicada"]
        assert [kw["numero_pagina"] for kw in api.llamadas[:2]] == [1, 2]

    def test_ventanas_vacias_igual_hacen_avanzar_el_cursor(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch)
        _con_cursor(session, _AHORA - timedelta(hours=7))
        api = _ApiFalsa(vacias=True)

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert _cursor(engine) == _LIMITE
        assert resultado["ventanas_completadas"] == len(api.ventanas()) >= 3
        assert resultado["nuevas"] == 0

    def test_hasta_nunca_pasa_de_ahora_menos_10_min(self, monkeypatch, session, engine) -> None:
        settings = _settings(monkeypatch, ca_ventana_horas=6)
        _con_cursor(session, _AHORA - timedelta(hours=1))
        api = _ApiFalsa()

        sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert all(kw["cambio_hasta"] <= _LIMITE for kw in api.llamadas)
        assert api.ventanas() == [(_AHORA - timedelta(hours=1) - _CINCO, _LIMITE)]
        assert _cursor(engine) == _LIMITE

    def test_al_dia_no_pide_nada_y_termina_ok(self, monkeypatch, session, engine) -> None:
        """Con el cursor en ahora − 10 min no queda ventana que avance el cursor:
        el solapamiento no debe hacer que se repita la misma ventana."""
        settings = _settings(monkeypatch)
        _con_cursor(session, _LIMITE)
        api = _ApiFalsa()

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert api.llamadas == []
        assert resultado["ventanas_completadas"] == 0
        assert _cursor(engine) == _LIMITE
        with Session(engine) as s:
            assert s.get(SyncState, "compra_agil").ultimo_ok == _AHORA

    def test_tope_de_requests_se_revisa_antes_de_abrir_cada_ventana(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch, ca_max_requests_por_corrida=2)
        cursor0 = _AHORA - timedelta(hours=30)
        _con_cursor(session, cursor0)
        api = _ApiFalsa()

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert len(api.ventanas()) == 2
        assert resultado["ventanas_completadas"] == 2
        assert resultado["requests_usadas"] == 2
        assert _cursor(engine) == api.ventanas()[1][1]
        assert resultado["atraso_horas"] > 25

    def test_una_ventana_abierta_se_termina_aunque_pase_el_tope(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch, ca_max_requests_por_corrida=2)
        _con_cursor(session, _AHORA - timedelta(hours=30))
        api = _ApiFalsa(paginas=3)

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert len(api.ventanas()) == 1
        assert resultado["requests_usadas"] == 3
        assert _cursor(engine) == api.ventanas()[0][1]


# ---------------------------------------------------------------------------
# Tope de 10 000 resultados: partir la ventana
# ---------------------------------------------------------------------------


@freeze_time(_AHORA)
class TestTopeDeResultados:
    def test_la_ventana_en_el_tope_se_parte_a_la_mitad(self, monkeypatch, session, engine) -> None:
        # Tope 3: la ventana 1 gasta 2 (tope + mitad), la 2 se abre con 2 < 3 y
        # también se parte; al terminarla van 4 y la corrida corta.
        settings = _settings(monkeypatch, ca_max_requests_por_corrida=3)
        cursor0 = _AHORA - timedelta(hours=10)
        _con_cursor(session, cursor0)
        desde0 = cursor0 - _CINCO
        desde1 = desde0 + timedelta(hours=1) - _CINCO

        # Más de 1 h de ancho → tope; 1 h o menos → 500 resultados.
        def total(desde: datetime, hasta: datetime) -> int:
            return 10_000 if hasta - desde > timedelta(hours=1) else 500

        api = _ApiFalsa(total=total)

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        primeras = [(kw["cambio_desde"], kw["cambio_hasta"]) for kw in api.llamadas[:2]]
        assert primeras == [
            (desde0, desde0 + timedelta(hours=2)),
            (desde0, desde0 + timedelta(hours=1)),
        ]
        # La primera ventana completada es la mitad; la siguiente parte de ahí.
        assert [(kw["cambio_desde"], kw["cambio_hasta"]) for kw in api.llamadas[2:]] == [
            (desde1, desde1 + timedelta(hours=2)),
            (desde1, desde1 + timedelta(hours=1)),
        ]
        assert resultado["ventanas_partidas"] == 2
        assert resultado["ventanas_completadas"] == 2
        assert resultado["requests_usadas"] == 4
        assert _cursor(engine) == desde1 + timedelta(hours=1)

    def test_si_sigue_en_el_tope_bajo_10_min_error_claro(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch)
        cursor0 = _AHORA - timedelta(hours=10)
        _con_cursor(session, cursor0)
        api = _ApiFalsa(total=lambda _d, _h: 10_000)

        with pytest.raises(CompraAgilIngestaError, match="tope de 10000"):
            sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert _cursor(engine) == cursor0
        # 2 h → 1 h → 30 min → 15 min, y 7,5 min ya no se intenta.
        anchos = [kw["cambio_hasta"] - kw["cambio_desde"] for kw in api.llamadas]
        assert anchos == [timedelta(minutes=m) for m in (120, 60, 30, 15)]
        with Session(engine) as s:
            assert s.execute(select(func.count()).select_from(CompraAgil)).scalar() == 0

    def test_en_el_tope_con_muchas_paginas_sigue_lanzando(
        self, monkeypatch, session, engine
    ) -> None:
        """El tope blando por volumen no debe tapar el tope duro de 10 000."""
        settings = _settings(monkeypatch)
        cursor0 = _AHORA - timedelta(hours=10)
        _con_cursor(session, cursor0)
        api = _ApiFalsa(paginas=1000, total=lambda _d, _h: 10_000)

        with pytest.raises(CompraAgilIngestaError, match="tope de 10000"):
            sync_incremental(session, api, settings)  # type: ignore[arg-type]
        assert _cursor(engine) == cursor0


# ---------------------------------------------------------------------------
# Volumen: partir la ventana según total_paginas, antes de paginarla
# ---------------------------------------------------------------------------


def _por_minuto(ritmo: Any) -> Any:
    """Páginas proporcionales al ancho: `ritmo(desde)` páginas por minuto."""

    def paginas(desde: datetime, hasta: datetime) -> int:
        return max(1, math.ceil(ritmo(desde) * (hasta - desde).total_seconds() / 60))

    return paginas


def _aperturas(api: _ApiFalsa) -> list[tuple[datetime, datetime]]:
    return [
        (kw["cambio_desde"], kw["cambio_hasta"]) for kw in api.llamadas if kw["numero_pagina"] == 1
    ]


def _min(n: float) -> timedelta:
    return timedelta(minutes=n)


@freeze_time(_AHORA)
class TestVolumen:
    def test_hora_punta_se_parte_a_30_y_15_antes_de_paginar(
        self, monkeypatch, session, engine
    ) -> None:
        # 1,2 páginas/min (~canario 4): 1 h → 72, 30 min → 36, 15 min → 18 ≤ 20.
        settings = _settings(
            monkeypatch,
            ca_ventana_horas=1,
            ca_max_paginas_por_ventana=20,
            ca_max_requests_por_corrida=1,
        )
        cursor0 = _AHORA - timedelta(hours=3)
        _con_cursor(session, cursor0)
        cursores_al_abrir: list[datetime | None] = []
        api = _ApiFalsa(
            paginas=_por_minuto(lambda _d: 1.2),
            al_abrir=lambda _v: cursores_al_abrir.append(_cursor(engine)),
        )

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        desde0 = cursor0 - _CINCO
        # Tres páginas 1 (60 → 30 → 15 min) y recién ahí las páginas 2..18.
        pedidas = [
            (kw["cambio_hasta"] - kw["cambio_desde"], kw["numero_pagina"]) for kw in api.llamadas
        ]
        assert pedidas[:3] == [(_min(60), 1), (_min(30), 1), (_min(15), 1)]
        assert pedidas[3:] == [(_min(15), p) for p in range(2, 19)]
        assert all(kw["cambio_desde"] == desde0 for kw in api.llamadas)
        # El cursor avanzó al cerrar la sub-ventana de 15 min.
        assert _cursor(engine) == desde0 + _min(15)
        assert all(c == cursor0 for c in cursores_al_abrir)
        assert resultado["ventanas_partidas_volumen"] == 2
        assert resultado["ventanas_partidas"] == 0
        assert resultado["ancho_final_min"] == 15
        assert resultado["requests_usadas"] == 3 + 17
        assert resultado["cortado_por_error"] is None

    def test_el_cursor_avanza_al_cerrar_cada_sub_ventana(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch, ca_ventana_horas=1, ca_max_requests_por_corrida=50)
        cursor0 = _AHORA - timedelta(hours=3)
        _con_cursor(session, cursor0)
        cursores_al_abrir: list[datetime | None] = []
        api = _ApiFalsa(
            paginas=_por_minuto(lambda _d: 1.2),
            al_abrir=lambda _v: cursores_al_abrir.append(_cursor(engine)),
        )

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        # Ventana 1: 3 páginas 1 + 17; ventanas 2 y 3: sin partir, 18 cada una.
        assert resultado["ventanas_completadas"] == 3
        assert resultado["requests_usadas"] == 20 + 18 + 18
        aperturas = _aperturas(api)
        assert [h - d for d, h in aperturas] == [_min(m) for m in (60, 30, 15, 15, 15)]
        # Cada sub-ventana arranca 5 min antes del final de la anterior, con el
        # cursor ya commiteado en ese final.
        cierres = [cursor0 - _CINCO + _min(15)]
        for d, h in aperturas[3:]:
            assert d == cierres[-1] - _CINCO
            cierres.append(h)
        assert _cursor(engine) == cierres[-1]
        assert cursores_al_abrir == [cursor0, cursor0, cursor0, cierres[0], cierres[1]]

    def test_el_ancho_se_mantiene_y_se_duplica_con_poco_volumen(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch, ca_ventana_horas=1, ca_max_requests_por_corrida=1000)
        cursor0 = _AHORA - timedelta(hours=4)
        _con_cursor(session, cursor0)
        corte = _AHORA - timedelta(hours=3)
        # Hora punta hasta `corte` (1,2 pág/min); después, 0,1 pág/min.
        api = _ApiFalsa(paginas=_por_minuto(lambda d: 1.2 if d < corte else 0.1))

        resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        # Solo las ventanas que se paginaron (la última página 1 de cada desde).
        paginadas: dict[datetime, timedelta] = {}
        for d, h in _aperturas(api):
            paginadas[d] = h - d
        anchos = [paginadas[d] for d in sorted(paginadas)]
        # Punta: 15 min se mantiene (18 páginas ≥ 10, no se duplica).
        n_punta = sum(1 for d in paginadas if d < corte)
        assert anchos[:n_punta] == [_min(15)] * n_punta
        # Poco volumen: 15 → 30 → 60 y ahí se queda, sin pasar de ca_ventana_horas.
        assert anchos[n_punta : n_punta + 3] == [_min(15), _min(30), _min(60)]
        assert max(anchos) == _min(60)
        assert all(a == _min(60) for a in anchos[n_punta + 2 : -1])
        assert resultado["ancho_final_min"] == 60
        # Solo se partió la primera ventana (60 → 30 → 15), ninguna después.
        assert resultado["ventanas_partidas_volumen"] == 2
        assert _cursor(engine) == _LIMITE

    def test_en_el_minimo_pagina_igual_sin_error(
        self, monkeypatch, session, engine, caplog
    ) -> None:
        settings = _settings(monkeypatch, ca_ventana_horas=1, ca_max_requests_por_corrida=1)
        cursor0 = _AHORA - timedelta(hours=3)
        _con_cursor(session, cursor0)
        api = _ApiFalsa(paginas=50)

        with caplog.at_level(logging.WARNING, logger="app.ingest.compra_agil"):
            resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        desde0 = cursor0 - _CINCO
        assert [h - d for d, h in _aperturas(api)] == [_min(60), _min(30), _min(15)]
        assert len(api.llamadas) == 3 + 49
        assert _cursor(engine) == desde0 + _min(15)
        assert resultado["ventanas_completadas"] == 1
        assert resultado["ventanas_partidas_volumen"] == 2
        assert resultado["cortado_por_error"] is None
        avisos = [r for r in caplog.records if "ancho mínimo" in r.getMessage()]
        assert len(avisos) == 1 and avisos[0].levelno == logging.WARNING

    def test_cada_corrida_parte_en_ca_ventana_horas(self, monkeypatch, session, engine) -> None:
        settings = _settings(monkeypatch, ca_ventana_horas=1, ca_max_requests_por_corrida=1)
        _con_cursor(session, _AHORA - timedelta(hours=5))
        primera = _ApiFalsa(paginas=_por_minuto(lambda _d: 1.2))
        sync_incremental(session, primera, settings)  # type: ignore[arg-type]
        segunda = _ApiFalsa(paginas=_por_minuto(lambda _d: 1.2))
        sync_incremental(session, segunda, settings)  # type: ignore[arg-type]

        primera_apertura = _aperturas(segunda)[0]
        assert primera_apertura[1] - primera_apertura[0] == _min(60)


# ---------------------------------------------------------------------------
# Cortes: el cursor queda en la última ventana completa
# ---------------------------------------------------------------------------


@freeze_time(_AHORA)
class TestCortes:
    @pytest.mark.parametrize(
        "exc",
        [
            MPConcurrencyError("429/10500 persistente", retry_after_seconds=900),
            MPRateLimitError("Cuota agotada (429)", retry_after_seconds=3600),
        ],
        ids=["429-10500", "429-diario"],
    )
    def test_429_con_ventanas_cerradas_sigue_siendo_error(
        self, monkeypatch, session, engine, exc
    ) -> None:
        """El cierre parcial es solo para 5xx/timeout: un 429 lo decide la regla 3."""
        settings = _settings(monkeypatch)
        _con_cursor(session, _AHORA - timedelta(hours=30))
        api = _ApiFalsa(paginas=2, fallar={(3, 2): exc})

        with pytest.raises(type(exc)):
            sync_incremental(session, api, settings)  # type: ignore[arg-type]

        ventanas = api.ventanas()
        assert len(ventanas) == 3
        assert _cursor(engine) == ventanas[1][1]  # final de la ventana 2
        with Session(engine) as s:
            codigos = set(s.execute(select(CompraAgil.codigo)).scalars())
            state = s.get(SyncState, "compra_agil")
        # Lo de la ventana a medias quedó commiteado por página.
        (d1, _), (d2, _), (d3, _) = ventanas
        assert codigos == {
            codigo(d1, 1),
            codigo(d1, 2),
            codigo(d2, 1),
            codigo(d2, 2),
            codigo(d3, 1),
        }
        # Se registró el intento (ultimo_ok viene de las ventanas 1 y 2, que cerraron).
        assert state.ultima_ejecucion == _AHORA

    def test_reejecutar_tras_un_cierre_parcial_retoma_sin_duplicar(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch, ca_ventana_horas=6)
        _con_cursor(session, _AHORA - timedelta(hours=12))
        corte = _ApiFalsa(paginas=2, fallar={(2, 2): MPServerError("504", status_code=504)})
        parcial = sync_incremental(session, corte, settings)  # type: ignore[arg-type]
        assert parcial["cortado_por_error"] == "MPServerError"
        cursor_tras_corte = _cursor(engine)
        assert cursor_tras_corte == corte.ventanas()[0][1]

        # La repetición vuelve a pedir la ventana 2 desde su inicio: el falso
        # devuelve los mismos códigos para la misma (ventana, página).
        api = _ApiFalsa(paginas=2)
        final = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert api.ventanas()[0][0] == cursor_tras_corte - _CINCO
        assert final["cortado_por_error"] is None
        with Session(engine) as s:
            codigos = list(s.execute(select(CompraAgil.codigo)).scalars())
        assert len(codigos) == len(set(codigos))
        assert _cursor(engine) == _LIMITE

    @pytest.mark.parametrize(
        "exc",
        [
            MPServerError("Error del servidor (504)", status_code=504),
            MPServerError("Timeout de red", status_code=0),
        ],
        ids=["504", "timeout"],
    )
    def test_transitorio_en_la_ventana_2_cierra_ok_parcial(
        self, monkeypatch, session, engine, caplog, exc
    ) -> None:
        settings = _settings(monkeypatch)
        _con_cursor(session, _AHORA - timedelta(hours=30))
        api = _ApiFalsa(paginas=2, fallar={(2, 2): exc})

        with caplog.at_level(logging.WARNING, logger="app.ingest.compra_agil"):
            resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

        ventanas = api.ventanas()
        assert len(ventanas) == 2  # no siguió con la ventana 3
        fin_v1 = ventanas[0][1]
        assert _cursor(engine) == fin_v1
        assert resultado["cortado_por_error"] == "MPServerError"
        assert resultado["ventanas_completadas"] == 1
        assert resultado["cursor_final"] == fin_v1.isoformat()
        assert resultado["atraso_horas"] == pytest.approx(
            (_AHORA - fin_v1).total_seconds() / 3600, abs=0.01
        )
        # Solo el tipo, nunca el mensaje de la excepción.
        assert "Error del servidor" not in str(resultado) and "Timeout" not in str(resultado)
        assert any("avance parcial" in r.getMessage() for r in caplog.records)
        with Session(engine) as s:
            codigos = set(s.execute(select(CompraAgil.codigo)).scalars())
            state = s.get(SyncState, "compra_agil")
        (d1, _), (d2, _) = ventanas
        assert codigos == {codigo(d1, 1), codigo(d1, 2), codigo(d2, 1)}
        assert state.ultima_ejecucion == _AHORA

    def test_el_parcial_no_mueve_ultimo_ok_mas_alla_de_la_ultima_ventana(
        self, monkeypatch, session, engine
    ) -> None:
        """ultimo_ok queda en el cierre de la ventana 1; ultima_ejecucion, en el corte."""
        settings = _settings(monkeypatch)
        _con_cursor(session, _AHORA - timedelta(hours=30))
        with freeze_time(_AHORA) as reloj:

            def al_abrir(ventana: int) -> None:
                if ventana == 2:
                    reloj.tick(timedelta(minutes=7))

            api = _ApiFalsa(
                paginas=2,
                fallar={(2, 2): MPServerError("504", status_code=504)},
                al_abrir=al_abrir,
            )
            sync_incremental(session, api, settings)  # type: ignore[arg-type]

        with Session(engine) as s:
            state = s.get(SyncState, "compra_agil")
        assert state.ultimo_ok == _AHORA
        assert state.ultima_ejecucion == _AHORA + timedelta(minutes=7)

    def test_transitorio_en_la_ventana_1_es_error_sin_avanzar(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch)
        cursor0 = _AHORA - timedelta(hours=30)
        _con_cursor(session, cursor0)
        api = _ApiFalsa(paginas=2, fallar={(1, 2): MPServerError("504", status_code=504)})

        with pytest.raises(MPServerError):
            sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert _cursor(engine) == cursor0
        with Session(engine) as s:
            state = s.get(SyncState, "compra_agil")
        assert state.ultimo_ok is None and state.ultima_ejecucion == _AHORA

    def test_transitorio_en_la_pagina_1_de_la_ventana_1_es_error(
        self, monkeypatch, session, engine
    ) -> None:
        settings = _settings(monkeypatch)
        cursor0 = _AHORA - timedelta(hours=30)
        _con_cursor(session, cursor0)
        api = _ApiFalsa(fallar={(1, 1): MPServerError("504", status_code=504)})

        with pytest.raises(MPServerError):
            sync_incremental(session, api, settings)  # type: ignore[arg-type]
        assert _cursor(engine) == cursor0

    def test_commit_fallido_no_avanza_el_cursor_y_corta(self, monkeypatch, session, engine) -> None:
        """Bug heredado: antes la página se logueaba como descartada y la corrida
        seguía, avanzando el cursor por encima del hueco."""
        settings = _settings(monkeypatch)
        cursor0 = _AHORA - timedelta(hours=30)
        _con_cursor(session, cursor0)
        api = _ApiFalsa(paginas=2)

        from app.core import db_retry

        real = db_retry.commit_con_retry
        llamadas = 0

        def commit(session: Session, aplicar: Any, *, contexto: str) -> bool:
            nonlocal llamadas
            llamadas += 1
            if llamadas == 2:  # página 2 de la ventana 1
                session.rollback()
                return False
            return real(session, aplicar, contexto=contexto)

        with (
            patch("app.ingest.compra_agil.commit_con_retry", side_effect=commit),
            pytest.raises(CompraAgilIngestaError, match="commit falló"),
        ):
            sync_incremental(session, api, settings)  # type: ignore[arg-type]

        assert _cursor(engine) == cursor0
        assert len(api.llamadas) == 2  # no siguió con la ventana siguiente

    def test_aviso_de_saltos_si_hay_menos_codigos_que_el_total(
        self, monkeypatch, session, caplog
    ) -> None:
        settings = _settings(monkeypatch)
        _con_cursor(session, _AHORA - timedelta(hours=1))
        api = _ApiFalsa(paginas=2, total=lambda _d, _h: 7)

        with caplog.at_level(logging.WARNING, logger="app.ingest.compra_agil"):
            sync_incremental(session, api, settings)  # type: ignore[arg-type]

        avisos = [r.getMessage() for r in caplog.records if "posible salto" in r.getMessage()]
        assert len(avisos) == 1
        assert "vio 2 códigos únicos" in avisos[0] and "informó 7" in avisos[0]

    def test_sin_aviso_si_calzan(self, monkeypatch, session, caplog) -> None:
        settings = _settings(monkeypatch)
        _con_cursor(session, _AHORA - timedelta(hours=1))
        with caplog.at_level(logging.WARNING, logger="app.ingest.compra_agil"):
            sync_incremental(session, _ApiFalsa(paginas=2), settings)  # type: ignore[arg-type]
        assert not [r for r in caplog.records if "posible salto" in r.getMessage()]


# ---------------------------------------------------------------------------
# Arranque en frío: igual que antes de F-ca-ventana
# ---------------------------------------------------------------------------


@freeze_time(_AHORA)
def test_arranque_en_frio_pide_lo_mismo_que_antes(monkeypatch, session, engine) -> None:
    settings = _settings(monkeypatch)
    api = _ApiFalsa(paginas=2)

    resultado = sync_incremental(session, api, settings)  # type: ignore[arg-type]

    esperado = {
        "cambio_desde": None,
        "estados": ["cerrada", "proveedor_seleccionado", "publicada"],
        "tamano_pagina": 50,
    }
    assert [kw["numero_pagina"] for kw in api.llamadas] == [1, 2]
    for kw in api.llamadas:
        assert "cambio_hasta" not in kw
        assert {k: kw[k] for k in esperado} == esperado
    assert resultado == {"nuevas": 2, "actualizadas": 0, "descartadas": 0}
