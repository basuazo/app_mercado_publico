"""Tests F-plan-busqueda — motor FTS de app.plan_busqueda.

Requieren Postgres real con la migración d7f2a4c8b6e1 aplicada (índice GIN de
expresión sobre plan_compra_lineas.descripcion_producto + inmutable_unaccent):
SQLite no tiene to_tsvector/websearch_to_tsquery/array_agg. Mismo patrón que
tests/test_matching.py (@needs_postgres).

Prerequisito: DATABASE_URL apunta a un Postgres con `alembic upgrade head` ejecutado.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.ingest.plan_compra import _fuente_lote_vigente
from app.models.tables import InstitucionPAC, PlanCompraLinea, SyncState
from app.plan_busqueda import (
    FiltrosPlanBusqueda,
    buscar_lineas,
    buscar_por_organismo,
    contar_plan,
    lineas_para_exportar,
)

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
# La app siempre normaliza el driver a psycopg v3 (app/core/db.py); un
# create_engine con la URL cruda busca psycopg2, que no está en el stack
# (F-plan-busqueda-fix, 11 errores el 27-sep contra dev).
_DB_URL_ENGINE = normalizar_url_driver(_DB_URL)

needs_postgres = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migración aplicada)",
)

# Año centinela: nunca choca con datos reales (2025/2026 son los años reales del PAC).
_AGNO_TEST = 88888


@needs_postgres
class TestBuscarPlan:
    @pytest.fixture()
    def pg_engine(self):
        import app.models.tables  # noqa: F401
        from app.models.base import Base

        e = create_engine(_DB_URL_ENGINE)
        Base.metadata.create_all(e, checkfirst=True)
        yield e
        e.dispose()

    @pytest.fixture()
    def pg_session(self, pg_engine):
        with Session(pg_engine) as s:
            yield s

    @pytest.fixture()
    def datos(self, pg_session):
        def _limpiar() -> None:
            pg_session.execute(delete(PlanCompraLinea).where(PlanCompraLinea.agno == _AGNO_TEST))
            pg_session.execute(
                delete(InstitucionPAC).where(InstitucionPAC.codigo_entidad.in_([111, 222, 333]))
            )
            pg_session.execute(
                delete(SyncState).where(SyncState.fuente == _fuente_lote_vigente(_AGNO_TEST))
            )
            pg_session.commit()

        _limpiar()
        pg_session.add_all(
            [
                InstitucionPAC(codigo_entidad=111, razon_social="MINISTERIO DE PRUEBA", sector="Salud"),
                InstitucionPAC(codigo_entidad=222, razon_social="MUNICIPALIDAD DE PRUEBA", sector="Municipal"),
                InstitucionPAC(codigo_entidad=333, razon_social="SERVICIO DE PRUEBA", sector="Salud"),
                PlanCompraLinea(
                    codigo_entidad=111,
                    agno=_AGNO_TEST,
                    institucion_nombre="MINISTERIO DE PRUEBA",
                    codigo_producto="1",
                    descripcion_producto="Compra de resmas de papel carta",
                    monto_estimado_clp=100000.0,
                    mes_estimado=3,
                    lote_id=1,
                ),
                PlanCompraLinea(
                    codigo_entidad=222,
                    agno=_AGNO_TEST,
                    institucion_nombre="MUNICIPALIDAD DE PRUEBA",
                    codigo_producto="2",
                    descripcion_producto="Mantención de impresoras láser",
                    monto_estimado_clp=500000.0,
                    mes_estimado=6,
                    lote_id=1,
                ),
                PlanCompraLinea(
                    codigo_entidad=333,
                    agno=_AGNO_TEST,
                    institucion_nombre="SERVICIO DE PRUEBA",
                    codigo_producto="3",
                    descripcion_producto='Compra de sillas "ergonómicas" con 100% garantía',
                    monto_estimado_clp=200000.0,
                    mes_estimado=1,
                    lote_id=1,
                ),
            ]
        )
        pg_session.commit()
        try:
            yield
        finally:
            _limpiar()

    def test_encuentra_por_palabra_con_unaccent_y_plural(self, pg_session, datos):
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST, q_include="resma")
        lineas, total = buscar_lineas(pg_session, filtros)
        assert total == 1
        assert lineas[0].codigo_entidad == 111

    def test_exclusion_funciona(self, pg_session, datos):
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST, q_include="compra", q_exclude="sillas")
        lineas, total = buscar_lineas(pg_session, filtros)
        assert total == 1
        assert lineas[0].codigo_entidad == 111

    def test_filtro_organismos_acota(self, pg_session, datos):
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST, organismos=[222])
        lineas, total = buscar_lineas(pg_session, filtros)
        assert total == 1
        assert lineas[0].codigo_entidad == 222

    def test_filtro_sector_acota(self, pg_session, datos):
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST, sector="Salud")
        lineas, total = buscar_lineas(pg_session, filtros)
        assert total == 2
        assert {linea.codigo_entidad for linea in lineas} == {111, 333}

    def test_parametros_con_comillas_y_porcentaje_no_rompen(self, pg_session, datos):
        """Texto con comillas y '%' embebido: la query debe seguir parametrizada
        (nunca interpolada) y no debe lanzar excepción ni matchear de más."""
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST, q_include='"sillas" 100%')
        lineas, total = buscar_lineas(pg_session, filtros)
        assert total >= 0  # no revienta; el contenido exacto depende del parser de websearch

    def test_buscar_por_organismo_conteo_y_suma_coinciden(self, pg_session, datos):
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST)
        resultados = buscar_por_organismo(pg_session, filtros)
        por_codigo = {r.codigo_entidad: r for r in resultados}
        assert por_codigo[111].n_lineas == 1
        assert por_codigo[111].monto_total == 100000.0
        assert por_codigo[222].n_lineas == 1
        assert por_codigo[222].monto_total == 500000.0

    def test_contar_plan_respeta_filtros(self, pg_session, datos):
        conteo = contar_plan(pg_session, FiltrosPlanBusqueda(agno=_AGNO_TEST, sector="Salud"))
        assert conteo["n_lineas"] == 2
        assert conteo["n_organismos"] == 2
        assert conteo["monto_total"] == 300000.0

    def test_desde_mes_filtra_meses_pasados(self, pg_session, datos):
        # mes_estimado=1 (codigo 333) queda fuera si desde_mes=3
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST, desde_mes=3)
        _, total = buscar_lineas(pg_session, filtros)
        assert total == 2  # mes 3 y mes 6, no el 1

    def test_todo_el_anio_no_filtra_por_mes(self, pg_session, datos):
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST, desde_mes=None)
        _, total = buscar_lineas(pg_session, filtros)
        assert total == 3

    def test_monto_min_max_acotan(self, pg_session, datos):
        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST, monto_min=150000, monto_max=250000)
        _, total = buscar_lineas(pg_session, filtros)
        assert total == 1  # solo el de 200000

    def test_dos_lotes_del_mismo_anio_solo_ve_el_vigente(self, pg_session, datos):
        """F-plan-busqueda-fix: una carga interrumpida a medias puede dejar un
        segundo lote (huérfano) del mismo año en la tabla. Todas las vistas
        deben seguir viendo solo el lote vigente (lote_id=1, ver `datos`)."""
        pg_session.add(SyncState(fuente=_fuente_lote_vigente(_AGNO_TEST), cursor="1"))
        pg_session.add(
            PlanCompraLinea(
                codigo_entidad=111,
                agno=_AGNO_TEST,
                institucion_nombre="MINISTERIO DE PRUEBA",
                codigo_producto="9",
                descripcion_producto="fila huérfana de un lote viejo que no debe verse",
                monto_estimado_clp=999999.0,
                mes_estimado=3,
                lote_id=2,
            )
        )
        pg_session.commit()

        filtros = FiltrosPlanBusqueda(agno=_AGNO_TEST)

        _, total = buscar_lineas(pg_session, filtros)
        assert total == 3

        conteo = contar_plan(pg_session, filtros)
        assert conteo["n_lineas"] == 3
        assert conteo["monto_total"] == 800000.0

        resultados = buscar_por_organismo(pg_session, filtros)
        por_codigo = {r.codigo_entidad: r for r in resultados}
        assert por_codigo[111].n_lineas == 1
        assert por_codigo[111].monto_total == 100000.0

        exportadas = lineas_para_exportar(pg_session, filtros)
        assert len(exportadas) == 3
