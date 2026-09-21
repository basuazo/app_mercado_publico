"""Tests F-fecha-cierre — la hora real de cierre y el bug que perdía licitaciones.

El bug: `parse_fecha_v1` cortaba el ISO a 10 caracteres, `_fecha_a_dt` expandía a
medianoche de INICIO de día y el matching comparaba esa medianoche contra un
"ahora" en UTC. Resultado: una licitación que cerraba hoy a las 15:00 dejaba de
ser candidata desde las 21:00 del día ANTERIOR (hora de Chile).

Nada aquí pega a la red: lo que sale de un cliente HTTP va por respx.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.mp_v2 import MercadoPublicoV2Client, _iso_para_la_api
from app.clients.types import parse_fecha_iso, parse_fecha_v1_dt
from app.core.settings import Settings
from app.core.tiempo import (
    TZ_CHILE,
    a_naive_como_la_api,
    a_utc_naive,
    ahora_utc,
    borde_del_dia_utc_naive,
)
from app.ingest.licitaciones import upsert_basica
from app.matching.engine import _candidatos_licitaciones
from app.models.tables import Licitacion

_V1_BASE = "https://api.mercadopublico.cl/servicios/v1/publico/"
_V2_LISTADO = "https://api2.mercadopublico.cl/v2/compra-agil"

# 24-sep-2026: jueves, Chile en horario de verano (UTC-3). El día en que la
# licitación de ejemplo cierra a las 15:00.
_CIERRE_CHILE = datetime(2026, 9, 24, 15, 0, 0, tzinfo=TZ_CHILE)
_MANANA_CHILE = datetime(2026, 9, 24, 10, 0, 0, tzinfo=TZ_CHILE)
_TARDE_CHILE = datetime(2026, 9, 24, 16, 0, 0, tzinfo=TZ_CHILE)


def _utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mem_engine():
    engine = create_engine("sqlite:///:memory:")
    yield engine
    engine.dispose()


@pytest.fixture()
def settings_fake(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("MP_TICKET", "ticket-de-test-fecha-cierre")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("SECRET_KEY", "clave-de-test-32bytesxxxxxxxxxx")
    monkeypatch.setenv("JOBS_TOKEN", "token-de-test-jobs-abcdefgh1234")
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture()
def session():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s
    engine.dispose()


def _v1_client(settings_fake, mem_engine) -> MercadoPublicoV1Client:
    from app.clients.base import BaseClient, QuotaTracker, RateLimiter

    c = MercadoPublicoV1Client.__new__(MercadoPublicoV1Client)
    rl = RateLimiter(rps=100.0)
    qt = QuotaTracker(mem_engine, budget=100)
    c._ticket = settings_fake.mp_ticket
    c._client = BaseClient(ticket=settings_fake.mp_ticket, rate_limiter=rl, quota=qt)
    return c


def _v2_client(settings_fake, mem_engine) -> MercadoPublicoV2Client:
    from app.clients.base import BaseClient, QuotaTracker, RateLimiter

    c = MercadoPublicoV2Client.__new__(MercadoPublicoV2Client)
    rl = RateLimiter(rps=100.0)
    qt = QuotaTracker(mem_engine, budget=100)
    c._ticket = settings_fake.mp_ticket
    c._client = BaseClient(
        ticket=settings_fake.mp_ticket,
        rate_limiter=rl,
        quota=qt,
        default_headers={"ticket": settings_fake.mp_ticket},
    )
    return c


def _listado_activas(fecha_cierre: str) -> dict[str, object]:
    return {
        "Cantidad": 1,
        "Listado": [
            {
                "CodigoExterno": "1234-5-L126",
                "Nombre": "Suministro de material eléctrico",
                "CodigoEstado": 5,
                "FechaCierre": fecha_cierre,
                "FechaPublicacion": "2026-09-01T10:00:00",
                "Tipo": "L1",
                "CodigoOrganismo": "6945",
            }
        ],
    }


# ---------------------------------------------------------------------------
# 1. parse_fecha_v1_dt — la hora deja de perderse
# ---------------------------------------------------------------------------


class TestParseFechaV1Dt:
    def test_iso_con_hora_sin_offset_se_lee_como_hora_de_chile(self):
        """El slice de 10 caracteres tiraba la hora. Ahora se conserva y se convierte."""
        dt = parse_fecha_v1_dt("2026-09-24T15:00:00")
        assert dt == _utc_naive(_CIERRE_CHILE)
        assert dt is not None
        assert dt.hour == 18  # 15:00 en Chile (UTC-3 en septiembre) = 18:00 UTC

    def test_iso_con_offset_explicito_se_respeta(self):
        assert parse_fecha_v1_dt("2026-09-24T15:00:00-03:00") == datetime(2026, 9, 24, 18, 0)
        assert parse_fecha_v1_dt("2026-09-24T15:00:00Z") == datetime(2026, 9, 24, 15, 0)
        assert parse_fecha_v1_dt("2026-09-24T15:00:00+00:00") == datetime(2026, 9, 24, 15, 0)

    def test_iso_con_milisegundos(self):
        assert parse_fecha_v1_dt("2026-09-24T15:00:00.123") == _utc_naive(
            _CIERRE_CHILE.replace(microsecond=123000)
        )

    def test_ddmmaaaa_de_cierre_va_al_FIN_del_dia_chileno(self):
        """Una licitación que cierra "el 24" está abierta durante todo el 24."""
        dt = parse_fecha_v1_dt("24092026", fin_de_dia=True)
        assert dt == _utc_naive(datetime(2026, 9, 24, 23, 59, 59, tzinfo=TZ_CHILE))
        assert dt is not None
        assert dt > _utc_naive(_CIERRE_CHILE)

    def test_ddmmaaaa_de_publicacion_va_al_INICIO_del_dia_chileno(self):
        dt = parse_fecha_v1_dt("24092026")
        assert dt == _utc_naive(datetime(2026, 9, 24, 0, 0, 0, tzinfo=TZ_CHILE))

    def test_iso_solo_fecha_tambien_usa_el_borde_del_dia(self):
        assert parse_fecha_v1_dt("2026-09-24", fin_de_dia=True) == _utc_naive(
            datetime(2026, 9, 24, 23, 59, 59, tzinfo=TZ_CHILE)
        )
        assert parse_fecha_v1_dt("2026-09-24") == _utc_naive(
            datetime(2026, 9, 24, 0, 0, 0, tzinfo=TZ_CHILE)
        )

    @pytest.mark.parametrize(
        "basura", [None, "", "   ", "corta", "99999999", "32092026", "2026-13-45", 12345, []]
    )
    def test_basura_devuelve_none_y_no_rompe(self, basura):
        """Regla 6: lo que no calza devuelve None, nunca una excepción."""
        assert parse_fecha_v1_dt(basura) is None


# ---------------------------------------------------------------------------
# 2. parse_fecha_iso — el offset deja de descartarse
# ---------------------------------------------------------------------------


class TestParseFechaIso:
    def test_la_Z_ya_no_se_descarta(self):
        """Antes `rstrip("Z")` guardaba como naive algo marcado como UTC."""
        assert parse_fecha_iso("2026-09-24T15:00:00Z") == datetime(2026, 9, 24, 15, 0)

    def test_offset_explicito_se_convierte_a_utc(self):
        assert parse_fecha_iso("2026-09-24T15:00:00-03:00") == datetime(2026, 9, 24, 18, 0)
        assert parse_fecha_iso("2026-09-24T12:00:00+02:00") == datetime(2026, 9, 24, 10, 0)

    def test_sin_offset_se_interpreta_como_hora_de_chile(self):
        assert parse_fecha_iso("2026-09-24T15:00:00") == _utc_naive(_CIERRE_CHILE)

    def test_milisegundos_con_y_sin_z(self):
        assert parse_fecha_iso("2026-09-24T15:00:00.500Z") == datetime(
            2026, 9, 24, 15, 0, 0, 500000
        )
        assert parse_fecha_iso("2026-09-24T15:00:00.500") == _utc_naive(
            _CIERRE_CHILE.replace(microsecond=500000)
        )

    def test_solo_fecha_respeta_fin_de_dia(self):
        assert parse_fecha_iso("2026-09-24", fin_de_dia=True) == _utc_naive(
            datetime(2026, 9, 24, 23, 59, 59, tzinfo=TZ_CHILE)
        )

    @pytest.mark.parametrize("basura", [None, "", "no-es-fecha", 42, {}])
    def test_basura_devuelve_none(self, basura):
        assert parse_fecha_iso(basura) is None


# ---------------------------------------------------------------------------
# 3. app/core/tiempo — la única conversión de husos
# ---------------------------------------------------------------------------


class TestTiempo:
    def test_ahora_utc_es_naive(self):
        assert ahora_utc().tzinfo is None

    def test_a_utc_naive_ida_y_vuelta(self):
        """a_naive_como_la_api deshace exactamente lo que hace a_utc_naive."""
        original = datetime(2026, 9, 24, 15, 0, 0)
        assert a_naive_como_la_api(a_utc_naive(original)) == original

    def test_a_utc_naive_descarta_el_tzinfo(self):
        assert a_utc_naive(_CIERRE_CHILE).tzinfo is None

    def test_borde_del_dia_fin_es_posterior_al_inicio(self):
        d = _CIERRE_CHILE.date()
        assert borde_del_dia_utc_naive(d, fin_de_dia=True) > borde_del_dia_utc_naive(
            d, fin_de_dia=False
        )


# ---------------------------------------------------------------------------
# 4. El cliente v1 entrega la hora real (respx, cero red)
# ---------------------------------------------------------------------------


class TestClienteV1:
    @respx.mock
    def test_activas_conserva_la_hora_del_iso(self, settings_fake, mem_engine):
        respx.get(_V1_BASE + "licitaciones.json").mock(
            return_value=httpx.Response(200, json=_listado_activas("2026-09-24T15:00:00"))
        )
        result = _v1_client(settings_fake, mem_engine).licitaciones_activas()
        assert result[0].fecha_cierre == _utc_naive(_CIERRE_CHILE)

    @respx.mock
    def test_activas_con_ddmmaaaa_cierra_al_final_del_dia(self, settings_fake, mem_engine):
        respx.get(_V1_BASE + "licitaciones.json").mock(
            return_value=httpx.Response(200, json=_listado_activas("24092026"))
        )
        result = _v1_client(settings_fake, mem_engine).licitaciones_activas()
        assert result[0].fecha_cierre == _utc_naive(
            datetime(2026, 9, 24, 23, 59, 59, tzinfo=TZ_CHILE)
        )


# ---------------------------------------------------------------------------
# 5. El cursor de Compra Ágil no se corre de huso (round-trip con la API)
# ---------------------------------------------------------------------------


class TestCursorCompraAgil:
    def test_cambio_desde_vuelve_a_la_api_en_el_mismo_huso_en_que_llego(self):
        """Si el cursor saliera como UTC naive, la ingesta incremental perdería cambios."""
        como_lo_manda_la_api = "2026-09-24T15:00:00"
        guardado = parse_fecha_iso(como_lo_manda_la_api)
        assert guardado is not None
        assert _iso_para_la_api(guardado) == como_lo_manda_la_api

    @respx.mock
    def test_la_request_lleva_cambio_desde_sin_desplazar(self, settings_fake, mem_engine):
        ruta = respx.get(_V2_LISTADO).mock(
            return_value=httpx.Response(
                200,
                json={
                    "success": "OK",
                    "payload": {
                        "convocatorias": [],
                        "paginacion": {
                            "total_paginas": 1,
                            "total_resultados": 0,
                            "numero_pagina": 1,
                            "tamano_pagina": 50,
                        },
                    },
                },
            )
        )
        cursor = parse_fecha_iso("2026-09-24T15:00:00")
        assert cursor is not None
        _v2_client(settings_fake, mem_engine).listar_compra_agil(cambio_desde=cursor)
        assert ruta.calls.last.request.url.params["cambio_desde"] == "2026-09-24T15:00:00"


# ---------------------------------------------------------------------------
# 6. El test que prueba que el bug murió
# ---------------------------------------------------------------------------


class TestSigueSiendoCandidataSuUltimoDia:
    """Una licitación que cierra HOY a las 15:00 (Chile) debe seguir siendo
    candidata a las 10:00 de ESE MISMO día.

    Antes del arreglo la fila quedaba con `2026-09-24 00:00` naive; leída como
    UTC eso son las 21:00 del 23 en Chile, así que a las 10:00 del 24 la
    licitación ya estaba fuera del feed y fuera de las alertas — justo el día
    que importa.
    """

    @respx.mock
    def _ingestar(self, session, settings_fake, mem_engine, fecha_cierre_cruda: str) -> None:
        respx.get(_V1_BASE + "licitaciones.json").mock(
            return_value=httpx.Response(200, json=_listado_activas(fecha_cierre_cruda))
        )
        for item in _v1_client(settings_fake, mem_engine).licitaciones_activas():
            upsert_basica(session, item)
        session.commit()

    def test_con_hora_real_sigue_siendo_candidata_a_las_10(
        self, session, settings_fake, mem_engine
    ):
        self._ingestar(session, settings_fake, mem_engine, "2026-09-24T15:00:00")

        lic = session.get(Licitacion, "1234-5-L126")
        assert lic is not None
        assert lic.estado == "publicada"
        assert lic.fecha_cierre == _utc_naive(_CIERRE_CHILE)

        candidatos = _candidatos_licitaciones(session, _utc_naive(_MANANA_CHILE), None, None)
        assert [c.codigo for c in candidatos] == ["1234-5-L126"]

    def test_solo_con_fecha_sigue_siendo_candidata_a_las_10(
        self, session, settings_fake, mem_engine
    ):
        """Mismo resultado cuando la fuente no manda hora: el borde es el FIN del día."""
        self._ingestar(session, settings_fake, mem_engine, "24092026")

        candidatos = _candidatos_licitaciones(session, _utc_naive(_MANANA_CHILE), None, None)
        assert [c.codigo for c in candidatos] == ["1234-5-L126"]

    def test_pasada_la_hora_de_cierre_deja_de_ser_candidata(
        self, session, settings_fake, mem_engine
    ):
        """El arreglo no es "siempre candidata": a las 16:00 ya cerró."""
        self._ingestar(session, settings_fake, mem_engine, "2026-09-24T15:00:00")

        candidatos = _candidatos_licitaciones(session, _utc_naive(_TARDE_CHILE), None, None)
        assert candidatos == []

    def test_el_valor_viejo_si_habria_quedado_fuera(self, session, settings_fake, mem_engine):
        """Fija el bug: con la medianoche que fabricaba `_fecha_a_dt` no era candidata.

        Sin esta comprobación, los tests de arriba podrían pasar por cualquier
        motivo. Acá se escribe a mano el valor que guardaba el código anterior
        —`datetime(2026, 9, 24)`, medianoche de INICIO de día, naive— y se
        confirma que a las 10:00 del 24 en Chile ya estaba fuera del feed.
        """
        self._ingestar(session, settings_fake, mem_engine, "2026-09-24T15:00:00")
        lic = session.get(Licitacion, "1234-5-L126")
        assert lic is not None
        lic.fecha_cierre = datetime(2026, 9, 24)  # lo que guardaba _fecha_a_dt
        session.commit()

        candidatos = _candidatos_licitaciones(session, _utc_naive(_MANANA_CHILE), None, None)
        assert candidatos == []
