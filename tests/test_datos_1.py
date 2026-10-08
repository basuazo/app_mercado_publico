"""Tests F-datos-1: organismo, región y fechas de licitaciones, fechas de CA, 429
diario persistido, reserva de cuota para `ca`, errores de transporte y el paso
`rellenar-organismo`.

Red mockeada con respx; base SQLite en memoria (no hace falta nada propio de
Postgres: el GREATEST del 429 diario tiene su gemelo MAX en SQLite).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.base import (
    BaseClient,
    MPRateLimitError,
    MPServerError,
    MPTransportError,
    QuotaExceededError,
    QuotaTracker,
    RateLimiter,
)
from app.clients.mp_v1 import (
    _parse_licitacion_basica,
    _parse_licitacion_detalle,
    region_desde_nombre,
)
from app.clients.types import CompraAgilBasica, LicitacionBasica, LicitacionDetalle
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.ingest.compra_agil import upsert_ca_basica
from app.ingest.licitaciones import upsert_basica
from app.models.seeds import REGIONES
from app.models.tables import CompraAgil, Licitacion

_VALID_ENV = {
    "MP_TICKET": "ticket-test-datos-1",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-datos-1-32bytesxxxxxx",
    "JOBS_TOKEN": "token-test-datos-1-jobs-xxxxxxxx",
}
_URL = "https://api.mercadopublico.cl/servicios/v1/publico/licitaciones.json"

_DIA = lambda tz: datetime(2026, 10, 8, 13, 0, tzinfo=tz)  # noqa: E731
_NOCHE = lambda tz: datetime(2026, 10, 8, 1, 30, tzinfo=tz)  # noqa: E731


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _VALID_ENV.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def engine() -> Iterator[Any]:
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


class _Reloj:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, segundos: float) -> None:
        self.t += max(0.0, segundos)


# ---------------------------------------------------------------------------
# D1 / D11 · Parseo v1 con la forma observada en la sonda claves-lic
# ---------------------------------------------------------------------------

# Recorte fiel de Listado[0] de la sonda (08-oct-2026): primer nivel null y los
# datos bajo Comprador y Fechas.
_DETALLE_REAL: dict[str, Any] = {
    "Cantidad": 1,
    "Listado": [
        {
            "CodigoExterno": "2775-118-LE26",
            "Nombre": "Adquisición de prueba",
            "CodigoEstado": 5,
            "Descripcion": "Descripción de prueba",
            "FechaCierre": None,
            "Estado": "Publicada",
            "Comprador": {
                "CodigoOrganismo": "100555",
                "NombreOrganismo": "I MUNICIPALIDAD DE RENGO",
                "RutUnidad": "69.081.200-2",
                "CodigoUnidad": "3764",
                "NombreUnidad": "SECPLAC",
                "RegionUnidad": "Región del Libertador General Bernardo O´Higgins",
            },
            "CodigoTipo": 1,
            "Tipo": "LE",
            "Moneda": "CLP",
            "Fechas": {
                "FechaCreacion": "2026-10-07T00:00:00",
                "FechaCierre": "2026-10-13T15:01:00",
                "FechaPublicacion": "2026-10-07T19:32:09.92",
                "FechaAdjudicacion": "2026-10-20T16:23:00",
            },
            "Items": {"Cantidad": 0, "Listado": []},
        }
    ],
}


class TestParseoV1:
    def test_organismo_region_y_fechas_bajo_comprador_y_fechas(self) -> None:
        det = _parse_licitacion_detalle(_DETALLE_REAL)

        assert det.codigo_organismo == "100555"
        assert det.organismo_nombre == "I MUNICIPALIDAD DE RENGO"
        assert det.region == 6
        assert det.tipo == "LE"
        # Hora de Chile (UTC−3 en octubre) → naive UTC.
        assert det.fecha_publicacion == datetime(2026, 10, 7, 22, 32, 9, 920000)
        assert det.fecha_cierre == datetime(2026, 10, 13, 18, 1)

    def test_el_primer_nivel_manda_si_existe(self) -> None:
        item = {
            "CodigoExterno": "X-1",
            "Nombre": "n",
            "CodigoOrganismo": "999",
            "FechaCierre": "2026-10-20T15:00:00",
            "FechaPublicacion": "2026-10-01T10:00:00",
            "Fechas": {
                "FechaCierre": "2026-10-13T15:01:00",
                "FechaPublicacion": "2026-10-07T19:32:09",
            },
        }
        b = _parse_licitacion_basica(item)

        assert b.fecha_cierre == datetime(2026, 10, 20, 18, 0)
        assert b.fecha_publicacion == datetime(2026, 10, 1, 13, 0)
        assert b.codigo_organismo == "999"  # respaldo del primer nivel

    def test_listado_de_activas_sin_comprador_da_none(self) -> None:
        b = _parse_licitacion_basica(
            {"CodigoExterno": "X-2", "Nombre": "n", "FechaCierre": "2026-10-20T15:00:00"}
        )

        assert b.codigo_organismo is None
        assert b.organismo_nombre is None
        assert b.region is None
        assert b.tipo is None

    def test_region_desconocida_da_none_sin_romper_y_avisa_una_vez(self, caplog) -> None:
        item = dict(_DETALLE_REAL["Listado"][0])
        item["Comprador"] = {"CodigoOrganismo": "1", "RegionUnidad": "Región de Narnia"}

        with caplog.at_level("WARNING", logger="app.clients.mp_v1"):
            a = _parse_licitacion_detalle({"Listado": [item]})
            b = _parse_licitacion_detalle({"Listado": [item]})

        assert a.region is None and b.region is None
        assert a.codigo_organismo == "1"
        assert caplog.text.count("Narnia") == 1


class TestRegionDesdeNombre:
    @pytest.mark.parametrize(("codigo", "nombre"), REGIONES)
    def test_los_16_nombres_oficiales(self, codigo: int, nombre: str) -> None:
        assert region_desde_nombre(nombre) == codigo
        assert region_desde_nombre(f"Región de {nombre}") == codigo

    @pytest.mark.parametrize(
        ("nombre", "codigo"),
        [
            ("Región Metropolitana de Santiago", 13),
            ("Región del Libertador General Bernardo O´Higgins", 6),
            ("Región del Libertador General Bernardo O'Higgins", 6),
            ("Región de Ñuble", 16),
            ("REGION DE NUBLE", 16),
            ("Región del Biobío", 8),
            ("Región del Bío-Bío", 8),
            ("Región de Aysén del General Carlos Ibáñez del Campo", 11),
            ("Región de Magallanes y de la Antártica Chilena", 12),
            ("Región de la Araucanía", 9),
            ("  Región de Los Ríos  ", 14),
        ],
    )
    def test_variantes(self, nombre: str, codigo: int) -> None:
        assert region_desde_nombre(nombre) == codigo

    @pytest.mark.parametrize("valor", [None, "", "   ", 13])
    def test_vacios_y_no_texto(self, valor: object) -> None:
        # 13 no es un nombre: "13" no calza con ninguna clave.
        assert region_desde_nombre(valor) is None


# ---------------------------------------------------------------------------
# D1 · upsert_basica no pisa con None
# ---------------------------------------------------------------------------


def _basica(codigo: str, **kw: Any) -> LicitacionBasica:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": "Licitación",
        "estado": 5,
        "fecha_publicacion": None,
        "fecha_cierre": datetime(2026, 11, 1, 18, 0),
        "tipo": None,
        "codigo_organismo": None,
    }
    base.update(kw)
    return LicitacionBasica(**base)


class TestUpsertBasica:
    def test_el_listado_sin_tipo_ni_organismo_no_pisa(self, engine) -> None:
        with Session(engine) as s:
            upsert_basica(
                s,
                _basica(
                    "L-1",
                    tipo="LE",
                    codigo_organismo="100555",
                    organismo_nombre="MUNI",
                    region=6,
                ),
            )
            s.commit()
            upsert_basica(s, _basica("L-1"))  # como llega del listado de activas
            s.commit()
            lic = s.get(Licitacion, "L-1")
            assert lic is not None
            assert (lic.tipo, lic.codigo_organismo, lic.organismo_nombre, lic.region) == (
                "LE",
                "100555",
                "MUNI",
                6,
            )

    def test_una_licitacion_nueva_los_escribe(self, engine) -> None:
        with Session(engine) as s:
            _, nueva = upsert_basica(
                s, _basica("L-2", tipo="LP", codigo_organismo="7", organismo_nombre="X", region=13)
            )
            s.commit()
            lic = s.get(Licitacion, "L-2")
            assert nueva is True
            assert lic is not None
            assert (lic.tipo, lic.codigo_organismo, lic.organismo_nombre, lic.region) == (
                "LP",
                "7",
                "X",
                13,
            )

    def test_un_valor_nuevo_si_reemplaza(self, engine) -> None:
        with Session(engine) as s:
            upsert_basica(s, _basica("L-3", tipo="LE", region=6))
            s.commit()
            upsert_basica(s, _basica("L-3", tipo="LP", region=7))
            s.commit()
            lic = s.get(Licitacion, "L-3")
            assert lic is not None
            assert (lic.tipo, lic.region) == ("LP", 7)


# ---------------------------------------------------------------------------
# D4 · upsert_ca_basica no pisa con None
# ---------------------------------------------------------------------------


def _ca(codigo: str, **kw: Any) -> CompraAgilBasica:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": "CA",
        "estado": "publicada",
        "fecha_publicacion": None,
        "fecha_cierre": None,
        "fecha_ultimo_cambio": None,
        "monto_clp": None,
        "region": None,
        "organismo_nombre": None,
        "organismo_rut": None,
        "total_ofertas": 0,
    }
    base.update(kw)
    return CompraAgilBasica(**base)


class TestUpsertCaBasica:
    def test_none_no_pisa_fechas_region_rut_ni_monto(self, engine) -> None:
        completo = _ca(
            "CA-1",
            fecha_publicacion=datetime(2026, 10, 1, 13),
            fecha_cierre=datetime(2026, 10, 9, 18),
            fecha_ultimo_cambio=datetime(2026, 10, 2, 12),
            monto_clp=500_000.0,
            region=13,
            organismo_nombre="ORG",
            organismo_rut="61.000.000-0",
            total_ofertas=4,
        )
        with Session(engine) as s:
            upsert_ca_basica(s, completo)
            s.commit()
            upsert_ca_basica(s, _ca("CA-1", total_ofertas=2))
            s.commit()
            ca = s.get(CompraAgil, "CA-1")
            assert ca is not None
            assert ca.fecha_publicacion == datetime(2026, 10, 1, 13)
            assert ca.fecha_cierre == datetime(2026, 10, 9, 18)
            assert ca.fecha_ultimo_cambio == datetime(2026, 10, 2, 12)
            assert ca.monto_disponible_clp == 500_000.0
            assert ca.region == 13
            assert ca.organismo_nombre == "ORG"
            assert ca.organismo_rut == "61.000.000-0"
            assert ca.total_ofertas == 4  # se queda con el mayor

    def test_lo_que_trae_si_actualiza_y_ofertas_suben(self, engine) -> None:
        with Session(engine) as s:
            upsert_ca_basica(s, _ca("CA-2", region=13, total_ofertas=1))
            s.commit()
            upsert_ca_basica(
                s, _ca("CA-2", region=5, fecha_cierre=datetime(2026, 10, 9, 18), total_ofertas=3)
            )
            s.commit()
            ca = s.get(CompraAgil, "CA-2")
            assert ca is not None
            assert (ca.region, ca.fecha_cierre, ca.total_ofertas) == (
                5,
                datetime(2026, 10, 9, 18),
                3,
            )


# ---------------------------------------------------------------------------
# R1 · 429 diario persistido, por día calendario de Chile
# ---------------------------------------------------------------------------


def _cliente(quota: QuotaTracker) -> BaseClient:
    return BaseClient(ticket="t", rate_limiter=RateLimiter(rps=1000.0), quota=quota)


class TestTopeDiarioPersistido:
    @respx.mock
    def test_otra_corrida_del_mismo_dia_no_emite_y_al_dia_siguiente_si(self, engine) -> None:
        # 23:50 del 8 en Chile: en UTC ya es el 9. Manda el día de Chile.
        hoy = datetime(2026, 10, 8, 23, 50, tzinfo=TZ_CHILE)
        ruta = respx.get(_URL).mock(return_value=httpx.Response(429, json={"Codigo": 999}))

        with pytest.raises(MPRateLimitError):
            _cliente(QuotaTracker(engine, budget=100, now_fn=lambda: hoy))._request("GET", _URL)
        assert ruta.call_count == 1

        # "Otra corrida": tracker nuevo, misma base, mismo día de Chile.
        mas_tarde = hoy + timedelta(minutes=5)
        otra = QuotaTracker(engine, budget=100, now_fn=lambda: mas_tarde)
        with pytest.raises(QuotaExceededError):
            otra.check_budget()
        with pytest.raises(QuotaExceededError):
            _cliente(otra)._request("GET", _URL)
        assert ruta.call_count == 1  # no salió nada

        # Con reserva también corta (el contador subió al presupuesto completo).
        with pytest.raises(QuotaExceededError):
            QuotaTracker(engine, budget=100, reserva=30, now_fn=lambda: mas_tarde).check_budget()

        manana = datetime(2026, 10, 9, 0, 1, tzinfo=TZ_CHILE)
        QuotaTracker(engine, budget=100, now_fn=lambda: manana).check_budget()

    @respx.mock
    def test_el_10500_no_marca_el_dia(self, engine, monkeypatch) -> None:
        monkeypatch.setattr("app.clients.base.time.sleep", lambda _s: None)
        monkeypatch.setattr(RateLimiter, "enfriar", lambda *_a, **_k: None)
        respx.get(_URL).mock(return_value=httpx.Response(429, json={"Codigo": 10500}))
        q = QuotaTracker(engine, budget=100)

        with pytest.raises(MPRateLimitError):
            _cliente(q)._request("GET", _URL)

        assert q.remaining() == 96  # 4 intentos contados, nada más

    def test_marcar_no_baja_un_contador_mayor(self, engine) -> None:
        q = QuotaTracker(engine, budget=100)
        q.consume(150)
        q.marcar_agotado()
        assert QuotaTracker(engine, budget=200).remaining() == 50


# ---------------------------------------------------------------------------
# R2 · Reserva de cuota para `ca`
# ---------------------------------------------------------------------------


class TestReservaCa:
    def test_con_reserva_corta_en_budget_menos_reserva(self, engine) -> None:
        con = QuotaTracker(engine, budget=10, reserva=4)
        sin = QuotaTracker(engine, budget=10)
        con.consume(6)

        with pytest.raises(QuotaExceededError):
            con.check_budget()
        sin.check_budget()
        assert sin.remaining() == 4

    def test_make_clients_reserva_salvo_para_ca(self, settings, engine) -> None:
        from app.ingest import orchestrator

        v1, v2 = orchestrator._make_clients(settings, engine)
        assert v1._client._quota._reserva == settings.cuota_reserva_ca == 2500
        assert v2._client._quota._reserva == 2500
        _, v2_ca = orchestrator._make_clients(settings, engine, reserva_ca=False)
        assert v2_ca._client._quota._reserva == 0

    def test_run_sync_ca_crea_clientes_sin_reserva(self, settings, engine) -> None:
        from app.ingest import orchestrator

        with (
            patch.object(
                orchestrator, "_make_clients", return_value=(MagicMock(), MagicMock())
            ) as mk,
            patch.object(orchestrator, "sync_incremental", return_value={}),
        ):
            orchestrator.run_sync_ca(settings, engine)
        assert mk.call_args.kwargs == {"reserva_ca": False}


# ---------------------------------------------------------------------------
# R3 · Errores de transporte
# ---------------------------------------------------------------------------


class TestTransporte:
    @pytest.fixture()
    def reloj(self, monkeypatch) -> _Reloj:
        r = _Reloj()
        monkeypatch.setattr("app.clients.base.time.monotonic", r)
        monkeypatch.setattr("app.clients.base.time.sleep", r.sleep)
        return r

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("rechazada"),
            httpx.RemoteProtocolError("cortada"),
            httpx.ReadError("cortada"),
        ],
        ids=["connect", "protocol", "read"],
    )
    @respx.mock
    def test_enfria_reintenta_y_al_agotarse_lanza_server_error_0(self, engine, reloj, exc) -> None:
        ruta = respx.get(_URL).mock(side_effect=exc)
        limiter = RateLimiter(rps=1000.0)
        c = BaseClient(ticket="t", rate_limiter=limiter, quota=QuotaTracker(engine, budget=100))

        inicio = reloj.t
        with pytest.raises(MPTransportError) as info:
            c._request("GET", _URL)

        assert isinstance(info.value, MPServerError)
        assert info.value.status_code == 0
        assert ruta.call_count == 3  # mismos intentos que el timeout
        assert reloj.t - inicio >= 120.0  # dos enfriamientos de 60 s entre intentos

    @respx.mock
    def test_reintenta_y_si_vuelve_devuelve_los_datos(self, engine, reloj) -> None:
        respx.get(_URL).mock(
            side_effect=[httpx.ConnectError("x"), httpx.Response(200, json={"ok": 1})]
        )
        c = _cliente(QuotaTracker(engine, budget=100))
        assert c._request("GET", _URL) == {"ok": 1}

    @respx.mock
    def test_sin_reintento_de_transitorios_falla_al_primero(self, engine, reloj) -> None:
        ruta = respx.get(_URL).mock(side_effect=httpx.ConnectError("x"))
        c = _cliente(QuotaTracker(engine, budget=100))
        with pytest.raises(MPTransportError):
            c._request("GET", _URL, reintentar_transitorios=False)
        assert ruta.call_count == 1

    def test_detalles_match_no_suma_fallo_por_transporte(self, settings, engine) -> None:
        from app.ingest.orchestrator import run_detalles_match

        cola = [("compras_agiles", "CA-1"), ("compras_agiles", "CA-2")]
        with (
            patch("app.ingest.orchestrator._cola_detalles_match", return_value=(cola, 0)),
            patch("app.ingest.orchestrator._make_clients", return_value=(MagicMock(), MagicMock())),
            patch(
                "app.ingest.orchestrator._bajar_detalle",
                side_effect=MPTransportError("Error de transporte (ConnectError)"),
            ),
            patch("app.ingest.orchestrator._registrar_fallo_detalle") as registrar,
        ):
            r = run_detalles_match(settings, engine, reloj=_Reloj(), now_fn=_DIA)

        registrar.assert_not_called()
        assert r["detalles_intentados"] == 2
        assert r["detalles_fallidos"] == 2
        assert "detalles_interrumpidos" not in r


# ---------------------------------------------------------------------------
# rellenar-organismo
# ---------------------------------------------------------------------------


def _det(codigo: str, organismo: str | None = "100555") -> LicitacionDetalle:
    return LicitacionDetalle(
        codigo=codigo,
        nombre="Licitación",
        estado=5,
        fecha_publicacion=None,
        fecha_cierre=None,
        tipo="LE",
        codigo_organismo=organismo,
        organismo_nombre="MUNI" if organismo else None,
        region=6 if organismo else None,
    )


@pytest.fixture()
def licitaciones(engine) -> None:
    ahora = ahora_utc()
    filas = [
        # (codigo, cierre, organismo, estado)
        ("V-2", ahora + timedelta(days=2), None, "publicada"),
        ("V-1", ahora + timedelta(days=1), None, "publicada"),
        ("V-3", ahora + timedelta(days=3), None, "publicada"),
        ("CON-ORG", ahora + timedelta(days=1), "777", "publicada"),
        ("CERRADA", ahora - timedelta(days=1), None, "publicada"),
        ("ADJ", ahora + timedelta(days=1), None, "adjudicada"),
        ("SIN-CIERRE", None, None, "publicada"),
    ]
    with Session(engine) as s:
        for codigo, cierre, org, estado in filas:
            s.add(
                Licitacion(
                    codigo=codigo,
                    nombre="x",
                    estado=estado,
                    fecha_cierre=cierre,
                    codigo_organismo=org,
                    detalle_obtenido=True,
                )
            )
        s.commit()


class TestRellenarOrganismo:
    def _correr(self, settings, engine, v1: Any, reloj: _Reloj | None = None, now_fn=_NOCHE):
        from app.ingest.orchestrator import run_rellenar_organismo

        with patch("app.ingest.orchestrator._make_clients", return_value=(v1, MagicMock())):
            return run_rellenar_organismo(settings, engine, reloj=reloj or _Reloj(), now_fn=now_fn)

    def test_solo_vigentes_sin_organismo_por_cierre_y_guarda(
        self, settings, engine, licitaciones
    ) -> None:
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c, **_k: _det(c)

        r = self._correr(settings, engine, v1)

        pedidos = [c.args[0] for c in v1.licitacion_detalle.call_args_list]
        assert pedidos == ["V-1", "V-2", "V-3"]
        for c in v1.licitacion_detalle.call_args_list:
            assert c.kwargs == {"reintentar_transitorios": False}
        assert r["candidatas"] == 3 and r["rellenados"] == 3
        with Session(engine) as s:
            lic = s.get(Licitacion, "V-1")
            assert lic is not None
            assert (lic.codigo_organismo, lic.organismo_nombre, lic.region) == ("100555", "MUNI", 6)
            # El detalle no traía cierre: no se pisó.
            assert lic.fecha_cierre is not None

    def test_respeta_el_tope_de_requests(self, settings, engine, licitaciones) -> None:
        settings.relleno_organismo_max = 2
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c, **_k: _det(c)

        r = self._correr(settings, engine, v1)

        assert v1.licitacion_detalle.call_count == 2
        assert r["intentados"] == 2

    def test_respeta_el_tope_de_tiempo(self, settings, engine, licitaciones) -> None:
        settings.relleno_organismo_minutos = 10
        reloj = _Reloj()

        def _detalle(c: str, **_k: Any) -> LicitacionDetalle:
            reloj.t += 6 * 60
            return _det(c)

        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = _detalle

        r = self._correr(settings, engine, v1, reloj=reloj)

        assert v1.licitacion_detalle.call_count == 2  # 0 y 6 min; a los 12 corta
        assert r["cortado_por_tiempo"] is True

    def test_fuera_de_la_ventana_nocturna_no_hace_nada(self, settings, engine, licitaciones) -> None:
        v1 = MagicMock()
        r = self._correr(settings, engine, v1, now_fn=_DIA)

        v1.licitacion_detalle.assert_not_called()
        assert r == {"omitido_fuera_de_ventana": True}

    def test_un_error_sigue_y_uno_del_canal_corta(self, settings, engine, licitaciones) -> None:
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = [
            MPServerError("504", status_code=504),
            _det("V-2"),
            MPRateLimitError("tope", retry_after_seconds=60),
        ]

        with pytest.raises(MPRateLimitError):
            self._correr(settings, engine, v1)

        with Session(engine) as s:
            v_1, v_2 = s.get(Licitacion, "V-1"), s.get(Licitacion, "V-2")
            assert v_1 is not None and v_1.codigo_organismo is None
            assert v_2 is not None and v_2.codigo_organismo == "100555"

    def test_es_idempotente(self, settings, engine, licitaciones) -> None:
        v1 = MagicMock()
        v1.licitacion_detalle.side_effect = lambda c, **_k: _det(c)
        self._correr(settings, engine, v1)
        v1.licitacion_detalle.reset_mock()

        r = self._correr(settings, engine, v1)

        v1.licitacion_detalle.assert_not_called()
        assert r["candidatas"] == 0

    def test_el_ciclo_nocturno_lo_corre_antes_de_detalles_match(self, settings, engine) -> None:
        from app.ingest import orchestrator

        orden: list[str] = []
        with (
            patch.object(orchestrator, "CacheZipsDA", MagicMock()),
            patch.object(
                orchestrator,
                "_run_with_lock",
                side_effect=lambda nombre, *_a, **_k: orden.append(nombre),
            ),
        ):
            orchestrator._ciclo_nocturno(settings, engine, now_fn=_NOCHE)

        assert orden[-2:] == ["rellenar-organismo", "detalles-match"]


# ---------------------------------------------------------------------------
# Migración
# ---------------------------------------------------------------------------


def test_migracion_agrega_y_revierte_columnas_e_indice(monkeypatch) -> None:
    import importlib.util
    from pathlib import Path

    ruta = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "a4e7c2d9f1b3_licitaciones_organismo_region.py"
    )
    spec = importlib.util.spec_from_file_location("mig_a4e7c2d9f1b3", ruta)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    llamadas: list[tuple[str, ...]] = []

    class FakeOp:
        def add_column(self, table: str, column: Any) -> None:
            llamadas.append(("add", table, column.name, str(column.type)))

        def create_index(self, nombre: str, table: str, cols: list[str]) -> None:
            llamadas.append(("index", nombre, table, *cols))

        def drop_index(self, nombre: str, table_name: str) -> None:
            llamadas.append(("drop_index", nombre, table_name))

        def drop_column(self, table: str, column_name: str) -> None:
            llamadas.append(("drop", table, column_name))

    monkeypatch.setattr(migration, "op", FakeOp())
    migration.upgrade()
    migration.downgrade()

    assert migration.down_revision == "c9e4b2f7a1d8"
    assert llamadas == [
        ("add", "licitaciones", "organismo_nombre", "VARCHAR(500)"),
        ("add", "licitaciones", "region", "INTEGER"),
        ("index", "ix_licitaciones_region", "licitaciones", "region"),
        ("drop_index", "ix_licitaciones_region", "licitaciones"),
        ("drop", "licitaciones", "region"),
        ("drop", "licitaciones", "organismo_nombre"),
    ]


# ---------------------------------------------------------------------------
# Visualización: nombre del organismo y región de la licitación
# ---------------------------------------------------------------------------


class TestVisualizacion:
    def _lic(self, **kw: Any) -> Licitacion:
        base: dict[str, Any] = {"codigo": "L-V", "nombre": "x", "estado": "publicada"}
        base.update(kw)
        return Licitacion(**base)

    def test_la_tarjeta_muestra_nombre_y_region_sin_filtrar_por_region(self) -> None:
        from app.api.query import _construir_item

        item = _construir_item(
            None,
            self._lic(codigo_organismo="100555", organismo_nombre="MUNI RENGO", region=6),
            feedback_valor=None,
            guardada=False,
            ahora=ahora_utc(),
        )

        assert item["organismo"] == "MUNI RENGO"
        assert item["region_nombre"] == "Libertador General Bernardo O'Higgins"
        # Filtro y faceta de región para licitaciones: F-match-1.
        assert item["region"] is None

    def test_sin_nombre_cae_al_codigo(self) -> None:
        from app.api.query import _construir_item

        item = _construir_item(
            None,
            self._lic(codigo_organismo="100555"),
            feedback_valor=None,
            guardada=False,
            ahora=ahora_utc(),
        )

        assert item["organismo"] == "100555"
        assert item["region_nombre"] is None

    def test_el_correo_usa_el_nombre(self, engine) -> None:
        from app.alerts.email import _datos_oportunidad

        with Session(engine) as s:
            s.add(self._lic(codigo_organismo="100555", organismo_nombre="MUNI RENGO", region=6))
            s.commit()
            datos = _datos_oportunidad(s, "licitaciones", "L-V")

        assert datos["organismo"] == "MUNI RENGO"
        assert datos["region"] == 6


# ---------------------------------------------------------------------------
# D3 · CA desiertas y canceladas
# ---------------------------------------------------------------------------


@respx.mock
def test_el_listado_de_ca_pide_desierta_y_cancelada(settings, engine) -> None:
    from app.clients.mp_v2 import MercadoPublicoV2Client
    from app.ingest.compra_agil import sync_incremental

    vacia = {
        "success": "OK",
        "payload": {
            "convocatorias": [],
            "paginacion": {"total_paginas": 0, "total_resultados": 0},
        },
    }
    ruta = respx.get("https://api2.mercadopublico.cl/v2/compra-agil").mock(
        return_value=httpx.Response(200, json=vacia)
    )
    with Session(engine) as s:
        sync_incremental(s, MercadoPublicoV2Client(settings, engine), settings)

    estado = ruta.calls[0].request.url.params["estado"]
    assert estado.split(",") == [
        "cancelada",
        "cerrada",
        "desierta",
        "proveedor_seleccionado",
        "publicada",
    ]


def test_una_ca_desierta_del_listado_actualiza_el_estado(engine) -> None:
    from app.clients.types import PaginacionV2, RespuestaListadoV2
    from app.ingest.compra_agil import _procesar_pagina, _Totales

    with Session(engine) as s:
        upsert_ca_basica(s, _ca("CA-D", fecha_cierre=datetime(2026, 10, 9, 18)))
        s.commit()
        resp = RespuestaListadoV2(
            items=[_ca("CA-D", estado="desierta")],
            paginacion=PaginacionV2(
                total_paginas=1, total_resultados=1, numero_pagina=1, tamano_pagina=20
            ),
        )
        totales = _Totales()
        _procesar_pagina(s, resp, totales, contexto="test")
        ca = s.get(CompraAgil, "CA-D")
        assert ca is not None
        assert ca.estado == "desierta"
        assert ca.fecha_cierre == datetime(2026, 10, 9, 18)
        assert totales.descartadas == 0
