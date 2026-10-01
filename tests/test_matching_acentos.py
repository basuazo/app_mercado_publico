"""Tests F-acentos — las palabras con tilde de los perfiles calzan como sin tilde.

El test de guardia no necesita BD; los de FTS requieren Postgres con la
migración aplicada (@needs_postgres).
"""

from __future__ import annotations

import os
import re
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

import app.plan_busqueda as plan
from app.core.db import normalizar_url_driver
from app.matching import engine as eng
from app.matching.engine import match_perfil
from app.matching.perfiles import crear_perfil
from app.models.tables import (
    CaProducto,
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    LicitacionItem,
    OportunidadMatch,
    PlanCompraLinea,
    Usuario,
)
from app.plan_busqueda import FiltrosPlanBusqueda, buscar_lineas
from tests.fixtures.dataset_matching import AHORA

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
needs_postgres = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migración aplicada)",
)

_EMAIL = "acentos_owner@test.com"
_PW_HASH = "$2b$12$fakehashforteststhatislong.enough.xyz"
_AGNO = 88887
_COD_ENTIDAD = 444


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql)


def test_fragmentos_fts_usan_unaccent():
    fragmentos = {
        "_FTS_LIC_INCLUDE": eng._FTS_LIC_INCLUDE,
        "_FTS_LIC_EXCLUDE": eng._FTS_LIC_EXCLUDE,
        "_FTS_CA_INCLUDE": eng._FTS_CA_INCLUDE,
        "_FTS_CA_EXCLUDE": eng._FTS_CA_EXCLUDE,
        "_HITS_LIC_SQL": str(eng._HITS_LIC_SQL.text),
        "_HITS_CA_SQL": str(eng._HITS_CA_SQL.text),
        "plan._FTS_INCLUDE": plan._FTS_INCLUDE,
        "plan._FTS_EXCLUDE": plan._FTS_EXCLUDE,
        "plan._TS_RANK": plan._TS_RANK,
    }
    for nombre, sql in fragmentos.items():
        n = _norm(sql)
        assert "websearch_to_tsquery('spanish', :" not in n, nombre
        assert "websearch_to_tsquery('spanish', kw." not in n, nombre
        assert "inmutable_unaccent(" in n, nombre


@needs_postgres
class TestAcentosPG:
    @pytest.fixture()
    def pg_session(self):
        import app.models.tables  # noqa: F401
        from app.models.base import Base

        e = create_engine(normalizar_url_driver(_DB_URL))
        Base.metadata.create_all(e, checkfirst=True)
        with Session(e) as s:
            yield s
        e.dispose()

    @pytest.fixture()
    def ds(self, pg_session):
        s = pg_session

        def _limpiar() -> None:
            s.rollback()
            for u in s.execute(select(Usuario).where(Usuario.email == _EMAIL)).scalars():
                s.delete(u)
            for lic in s.execute(
                select(Licitacion).where(Licitacion.codigo.like("ACENTOS-%"))
            ).scalars():
                s.delete(lic)
            for ca in s.execute(
                select(CompraAgil).where(CompraAgil.codigo.like("ACENTOS-%"))
            ).scalars():
                s.delete(ca)
            s.execute(delete(PlanCompraLinea).where(PlanCompraLinea.agno == _AGNO))
            s.execute(delete(InstitucionPAC).where(InstitucionPAC.codigo_entidad == _COD_ENTIDAD))
            s.commit()

        _limpiar()
        s.add(Usuario(email=_EMAIL, password_hash=_PW_HASH, activo=True))
        cierre = AHORA + timedelta(days=10)
        for codigo, nombre in [
            ("ACENTOS-L-REP", "Servicio de reparación de bombas"),
            ("ACENTOS-L-REPSIN", "Servicio de reparacion de bombas"),
            ("ACENTOS-L-FERRET", "Compra de insumos varios"),
            ("ACENTOS-L-MATFER", "Materiales de ferretería"),
            ("ACENTOS-L-MATOF", "Materiales de oficina"),
            ("ACENTOS-L-CONS", "Construcción de sede social"),
        ]:
            s.add(
                Licitacion(
                    codigo=codigo,
                    nombre=nombre,
                    descripcion="",
                    estado="publicada",
                    fecha_cierre=cierre,
                )
            )
        for codigo, nombre in [
            ("ACENTOS-C-REP", "Reparación de techumbre"),
            ("ACENTOS-C-FERRET", "Compra de insumos varios"),
            ("ACENTOS-C-MATFER", "Materiales de ferretería"),
            ("ACENTOS-C-MATOF", "Materiales de oficina"),
            ("ACENTOS-C-CONS", "Construcción de sede social"),
        ]:
            s.add(
                CompraAgil(
                    codigo=codigo,
                    nombre=nombre,
                    descripcion="",
                    estado="publicada",
                    region=13,
                    total_ofertas=0,
                    monto_disponible_clp=300_000.0,
                    fecha_cierre=cierre,
                )
            )
        s.flush()
        s.add(
            LicitacionItem(
                licitacion_codigo="ACENTOS-L-FERRET",
                codigo_producto="X",
                nombre="Artículos de ferretería",
            )
        )
        s.add(
            CaProducto(
                ca_codigo="ACENTOS-C-FERRET",
                codigo_producto="X",
                nombre="Insumos de ferretería",
                descripcion="",
            )
        )
        s.add(InstitucionPAC(codigo_entidad=_COD_ENTIDAD, razon_social="ACENTOS PRUEBA", sector="Salud"))
        for i, desc in enumerate(["Reparación de vehículos", "Compra de ferretería para reparación"]):
            s.add(
                PlanCompraLinea(
                    codigo_entidad=_COD_ENTIDAD,
                    agno=_AGNO,
                    institucion_nombre="ACENTOS PRUEBA",
                    codigo_producto=str(i),
                    descripcion_producto=desc,
                    monto_estimado_clp=1000.0,
                    mes_estimado=3,
                    lote_id=1,
                )
            )
        s.commit()
        try:
            yield s.execute(select(Usuario).where(Usuario.email == _EMAIL)).scalar_one()
        finally:
            _limpiar()

    def _matches(self, s, user, **perfil_kw):
        perfil = crear_perfil(s, user.id, "Acentos", **perfil_kw)
        s.flush()
        match_perfil(perfil, s, ahora=AHORA)
        s.flush()
        filas = s.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.codigo_oportunidad.like("ACENTOS-%"),
            )
        ).scalars()
        return {m.codigo_oportunidad: m for m in filas}

    def test_incluye_con_tilde_en_nombre(self, ds, pg_session):
        m = self._matches(pg_session, ds, keywords=["reparación"])
        for cod in ("ACENTOS-L-REP", "ACENTOS-C-REP"):
            assert cod in m
            assert m[cod].razones["keywords_hit"] == ["reparación"]
            assert m[cod].razones["campo_hit"] == "nombre"
        # Invariante F9c: ningún match sin keywords_hit
        assert all(x.razones["keywords_hit"] for x in m.values())

    def test_incluye_con_tilde_en_producto(self, ds, pg_session):
        m = self._matches(pg_session, ds, keywords=["ferretería"])
        for cod in ("ACENTOS-L-FERRET", "ACENTOS-C-FERRET"):
            assert cod in m
            assert m[cod].razones["campo_hit"] == "producto"

    def test_excluye_con_tilde(self, ds, pg_session):
        m = self._matches(pg_session, ds, keywords=["materiales"], keywords_excluir=["ferretería"])
        assert "ACENTOS-L-MATOF" in m and "ACENTOS-C-MATOF" in m
        assert "ACENTOS-L-MATFER" not in m and "ACENTOS-C-MATFER" not in m

    def test_sin_tilde_y_con_tilde_son_equivalentes(self, ds, pg_session):
        m = self._matches(pg_session, ds, keywords=["reparacion"])
        assert "ACENTOS-L-REP" in m and "ACENTOS-C-REP" in m
        m2 = self._matches(pg_session, ds, keywords=["reparación"])
        assert "ACENTOS-L-REPSIN" in m2

    def test_regresion_construccion(self, ds, pg_session):
        m = self._matches(pg_session, ds, keywords=["construcción"])
        assert "ACENTOS-L-CONS" in m and "ACENTOS-C-CONS" in m

    def test_plan_anual_include_y_exclude(self, ds, pg_session):
        f = FiltrosPlanBusqueda(agno=_AGNO, q_include="reparación")
        _, total = buscar_lineas(pg_session, f)
        assert total == 2
        f = FiltrosPlanBusqueda(agno=_AGNO, q_include="reparación", q_exclude="ferretería")
        lineas, total = buscar_lineas(pg_session, f)
        assert total == 1
        assert lineas[0].descripcion_producto == "Reparación de vehículos"
