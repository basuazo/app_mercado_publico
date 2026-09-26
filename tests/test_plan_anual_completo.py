"""Tests F-plan-busqueda — ingesta del año completo (job `plan-anual`).

Sin FTS acá (eso vive en tests/test_plan_busqueda.py, needs_postgres): esto
prueba sync_plan_anual_completo/get_plan/anio_completo_cargado, que son
INSERT/DELETE simples y corren igual de bien en SQLite.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
import respx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clients.plan_compra import url_pac_completo
from app.core.settings import Settings
from app.ingest.plan_compra import anio_completo_cargado, get_plan, sync_plan_anual_completo
from app.models.enums import EstadoPlanificacionPAC
from app.models.tables import PlanCompraLinea

_HEADER = (
    "institucion_nombre;rut_institucion;codigo_producto;descripcion_producto;"
    "cantidad_estimada;monto_unitario_clp;monto_estimado_clp;mes_estimado;"
    "trimestre_estimado;estado_planificacion\n"
)


def _fila(inst: str, rut: str, prod: str, desc: str, cant="1", munit="100.0", mest="100.0", mes="3", trim="1", estado="Publicado") -> str:
    return f"{inst};{rut};{prod};{desc};{cant};{munit};{mest};{mes};{trim};{estado}\n"


def _zip_completo(filas: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pacorganismos_2026.csv", (_HEADER + filas).encode("utf-8-sig"))
    return buf.getvalue()


_VALID_ENV = {
    "MP_TICKET": "ticket-test-plan-anual",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-plan-anual-32bytesxx",
    "JOBS_TOKEN": "token-test-plan-anual-jobsxxxxx",
}


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _VALID_ENV.items():
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


def _mock_head(url: str, last_modified: str) -> None:
    respx.head(url).mock(return_value=httpx.Response(200, headers={"Last-Modified": last_modified}))


def _mock_get(url: str, zip_bytes: bytes) -> None:
    respx.get(url).mock(return_value=httpx.Response(200, content=zip_bytes))


class TestSyncPlanAnualCompleto:
    def test_primera_corrida_carga_todas_las_filas(self, session, settings):
        url = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        zip_bytes = _zip_completo(
            _fila("MINISTERIO PUBLICO", "224060", "1", "Resmas de papel carta")
            + _fila("MUNICIPALIDAD X", "999", "2", "Sillas de oficina")
        )
        with respx.mock:
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(url, zip_bytes)
            resultado = sync_plan_anual_completo(session, settings, agno=2026)

        assert resultado["actualizado"] == 1
        assert resultado["filas"] == 2
        filas = session.execute(select(PlanCompraLinea)).scalars().all()
        assert len(filas) == 2
        assert {f.codigo_entidad for f in filas} == {224060, 999}
        assert all(f.lote_id is not None for f in filas)

    def test_last_modified_sin_cambios_no_descarga_de_nuevo(self, session, settings):
        url = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        zip_bytes = _zip_completo(_fila("X", "1", "1", "algo"))
        with respx.mock:
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(url, zip_bytes)
            sync_plan_anual_completo(session, settings, agno=2026)

        with respx.mock:
            # Solo HEAD mockeado: si el código intentara el GET, respx fallaría.
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            resultado = sync_plan_anual_completo(session, settings, agno=2026)

        assert resultado["actualizado"] == 0
        assert resultado["filas"] == 0

    def test_last_modified_cambia_reemplaza_sin_duplicar(self, session, settings):
        url = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        with respx.mock:
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(url, _zip_completo(_fila("VIEJO", "1", "1", "descripcion vieja")))
            sync_plan_anual_completo(session, settings, agno=2026)

        with respx.mock:
            _mock_head(url, "Fri, 30 Oct 2026 00:00:00 GMT")
            _mock_get(url, _zip_completo(_fila("NUEVO", "2", "2", "descripcion nueva")))
            resultado = sync_plan_anual_completo(session, settings, agno=2026)

        assert resultado["actualizado"] == 1
        filas = session.execute(select(PlanCompraLinea)).scalars().all()
        assert len(filas) == 1
        assert filas[0].institucion_nombre == "NUEVO"

    def test_reemplaza_tambien_cache_on_demand_del_mismo_anio(self, session, settings):
        """Una fila cacheada on-demand (lote_id NULL) del mismo año se reemplaza
        también cuando corre el job de año completo — pasa a ser autoritativa."""
        session.add(
            PlanCompraLinea(
                codigo_entidad=555,
                agno=2026,
                institucion_nombre="ON DEMAND VIEJO",
                codigo_producto="9",
                descripcion_producto="cacheado antes del job",
                estado_planificacion=EstadoPlanificacionPAC.PUBLICADO.value,
                lote_id=None,
            )
        )
        session.commit()

        url = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        with respx.mock:
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(url, _zip_completo(_fila("NUEVO", "2", "2", "descripcion nueva")))
            sync_plan_anual_completo(session, settings, agno=2026)

        filas = session.execute(select(PlanCompraLinea)).scalars().all()
        assert len(filas) == 1
        assert filas[0].institucion_nombre == "NUEVO"

    def test_fila_con_codigo_entidad_invalido_se_descarta_sin_romper(self, session, settings):
        url = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        zip_bytes = _zip_completo(
            _fila("SIN CODIGO VALIDO", "no-es-un-entero", "1", "algo")
            + _fila("OK", "123", "2", "esto si sirve")
        )
        with respx.mock:
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(url, zip_bytes)
            resultado = sync_plan_anual_completo(session, settings, agno=2026)

        assert resultado["filas"] == 1
        assert resultado["descartadas"] == 1
        filas = session.execute(select(PlanCompraLinea)).scalars().all()
        assert len(filas) == 1
        assert filas[0].institucion_nombre == "OK"

    def test_sin_archivo_publicado_403_no_rompe(self, session, settings):
        url = url_pac_completo(2027, settings.plan_compra_pac_base_url)
        with respx.mock:
            respx.head(url).mock(return_value=httpx.Response(403))
            resultado = sync_plan_anual_completo(session, settings, agno=2027)

        assert resultado == {"actualizado": 0, "filas": 0}
        assert session.execute(select(PlanCompraLinea)).scalars().all() == []


class TestAnioCompletoCargado:
    def test_false_sin_sync_state(self, session):
        assert anio_completo_cargado(session, 2026) is False

    def test_true_tras_sync_exitoso(self, session, settings):
        url = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        with respx.mock:
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(url, _zip_completo(_fila("X", "1", "1", "algo")))
            sync_plan_anual_completo(session, settings, agno=2026)

        assert anio_completo_cargado(session, 2026) is True
        assert anio_completo_cargado(session, 2025) is False


class TestGetPlanConAnioCompleto:
    def test_anio_completo_no_llama_a_la_red(self, session, settings):
        url = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        with respx.mock:
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(
                url,
                _zip_completo(_fila("MINISTERIO PUBLICO", "224060", "1", "Resmas de papel")),
            )
            sync_plan_anual_completo(session, settings, agno=2026)

        with respx.mock:
            # Nada mockeado: get_plan no debe intentar red para este año.
            resultado = get_plan(session, settings, 224060, 2026)

        assert resultado.estado == "ok"
        assert len(resultado.lineas) == 1
        assert resultado.lineas[0].descripcion_producto == "Resmas de papel"

    def test_anio_completo_institucion_sin_lineas_da_sin_plan(self, session, settings):
        url = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        with respx.mock:
            _mock_head(url, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(url, _zip_completo(_fila("X", "1", "1", "algo")))
            sync_plan_anual_completo(session, settings, agno=2026)

        with respx.mock:
            resultado = get_plan(session, settings, 999999, 2026)

        assert resultado.estado == "sin_plan"

    def test_anio_anterior_sigue_on_demand(self, session, settings):
        """El año completo cargado NO afecta a otros años: siguen on-demand."""
        url_2026 = url_pac_completo(2026, settings.plan_compra_pac_base_url)
        with respx.mock:
            _mock_head(url_2026, "Mon, 28 Sep 2026 00:00:00 GMT")
            _mock_get(url_2026, _zip_completo(_fila("X", "1", "1", "algo")))
            sync_plan_anual_completo(session, settings, agno=2026)

        url_institucion_2025 = "https://pac-files.da.mercadopublico.cl/2025/pacorganismos_2025_224060.zip"
        with respx.mock:
            respx.get(url_institucion_2025).mock(
                return_value=httpx.Response(
                    200,
                    content=_build_pac_zip_institucion(_fila("MINISTERIO PUBLICO", "224060", "1", "algo 2025")),
                )
            )
            resultado = get_plan(session, settings, 224060, 2025)

        assert resultado.estado == "ok"
        assert len(resultado.lineas) == 1


def _build_pac_zip_institucion(csv_filas: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("pacorganismos_2025_224060.csv", (_HEADER + csv_filas).encode("utf-8-sig"))
    return buf.getvalue()
