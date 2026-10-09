"""Tests F-match-1 (Postgres): exclusión solo por título, organismos seguidos y tope de
candidatos aplicado después de región y monto."""

from __future__ import annotations

import os
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.auth.password import hash_password
from app.core.db import normalizar_url_driver
from app.core.tiempo import ahora_utc
from app.matching import engine as eng
from app.matching.engine import (
    contar_limpieza,
    criterio_perfil,
    limpiar_matches_perfil,
    match_perfil,
)
from app.matching.text import build_exclude_tsquery, build_tsquery
from app.models.enums import RolUsuario
from app.models.tables import (
    CaProducto,
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    LicitacionItem,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_POSTGRES = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
pytestmark = pytest.mark.skipif(
    not _TIENE_POSTGRES,
    reason="Requiere DATABASE_URL apuntando a Postgres (con migraciones aplicadas)",
)

_EMAIL = "match1-a@test.cl"
_COD_ENTIDAD = 987_654  # código de organismo de prueba en instituciones_pac
_TOKEN = "zzmatchuno"  # palabra clave que solo existe en los datos de este archivo


@pytest.fixture(scope="module")
def pg_engine():
    e = create_engine(normalizar_url_driver(_DB_URL))
    yield e
    e.dispose()


def _limpiar(engine) -> None:
    with Session(engine) as s:
        s.execute(delete(Usuario).where(Usuario.email == _EMAIL))
        s.execute(delete(Licitacion).where(Licitacion.codigo.like("MM1-%")))
        s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("MM1-%")))
        s.execute(delete(InstitucionPAC).where(InstitucionPAC.codigo_entidad == _COD_ENTIDAD))
        s.commit()


@pytest.fixture()
def limpio(pg_engine):
    _limpiar(pg_engine)
    yield pg_engine
    _limpiar(pg_engine)


def _usuario(s: Session) -> int:
    u = Usuario(email=_EMAIL, password_hash=hash_password("x-contraseña-test"), rol=RolUsuario.USUARIO, activo=True)
    s.add(u)
    s.flush()
    return u.id


def _perfil(s: Session, **campos: Any) -> PerfilBusqueda:
    base: dict[str, Any] = {
        "owner_id": _usuario(s),
        "nombre": "Perfil match-1",
        "keywords": [_TOKEN],
        "keywords_excluir": [],
        "regiones": [],
        "categorias_unspsc": [],
        "organismos_seguidos": [],
        "fuentes": ["licitaciones", "compras_agiles"],
        "activo": True,
    }
    base.update(campos)
    p = PerfilBusqueda(**base)
    s.add(p)
    s.flush()
    return p


def _lic(s: Session, codigo: str, nombre: str, *, descripcion: str = "", items: tuple[str, ...] = (), **campos: Any) -> None:
    ahora = ahora_utc()
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": nombre,
        "descripcion": descripcion,
        "estado": "publicada",
        "fecha_cierre": ahora + timedelta(days=10),
    }
    base.update(campos)
    s.add(Licitacion(**base))
    s.flush()
    for it in items:
        s.add(LicitacionItem(licitacion_codigo=codigo, codigo_producto="", nombre=it))
    s.flush()


def _ca(s: Session, codigo: str, nombre: str, *, productos: tuple[str, ...] = (), **campos: Any) -> None:
    ahora = ahora_utc()
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": nombre,
        "descripcion": "",
        "estado": "publicada",
        "region": 13,
        "total_ofertas": 0,
        "monto_disponible_clp": 500_000.0,
        "fecha_publicacion": ahora - timedelta(days=1),
        "fecha_cierre": ahora + timedelta(days=10),
    }
    base.update(campos)
    s.add(CompraAgil(**base))
    s.flush()
    for p in productos:
        s.add(CaProducto(ca_codigo=codigo, codigo_producto="", nombre=p, descripcion=""))
    s.flush()


def _recall_lic(s: Session, excluir: list[str]) -> set[str]:
    q = build_tsquery([_TOKEN])
    qx = build_exclude_tsquery(excluir)
    return {c.codigo for c in eng._candidatos_licitaciones(s, ahora_utc(), q, qx)}


def _recall_ca(s: Session, excluir: list[str]) -> set[str]:
    q = build_tsquery([_TOKEN])
    qx = build_exclude_tsquery(excluir)
    return {c.codigo for c in eng._candidatos_ca(s, ahora_utc(), q, qx)}


def _codigos_match(s: Session, perfil_id: int) -> set[str]:
    return set(
        s.execute(
            select(OportunidadMatch.codigo_oportunidad).where(OportunidadMatch.perfil_id == perfil_id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Exclusión solo por título (§1.7)
# ---------------------------------------------------------------------------


def test_licitacion_con_la_palabra_excluida_solo_en_un_item_entra(limpio):
    with Session(limpio) as s:
        _lic(s, "MM1-L1", f"{_TOKEN} programa adulto mayor", items=("Agua embotellada",))
        s.commit()
        assert "MM1-L1" in _recall_lic(s, ["agua"])


def test_licitacion_con_la_palabra_excluida_solo_en_la_descripcion_entra(limpio):
    with Session(limpio) as s:
        _lic(s, "MM1-L2", f"{_TOKEN} programa adulto mayor", descripcion="incluye agua y colaciones")
        s.commit()
        assert "MM1-L2" in _recall_lic(s, ["agua"])


def test_licitacion_con_la_palabra_excluida_en_el_titulo_sale(limpio):
    with Session(limpio) as s:
        _lic(s, "MM1-L3", f"Reparación sistema de aguas centro adulto mayor {_TOKEN}")
        s.commit()
        # raíz común agua/aguas
        assert "MM1-L3" not in _recall_lic(s, ["agua"])


def test_exclusion_sin_tildes_saca_el_titulo_con_tilde(limpio):
    with Session(limpio) as s:
        _lic(s, "MM1-L4", f"Reparación de techumbre {_TOKEN}")
        s.commit()
        assert "MM1-L4" not in _recall_lic(s, ["reparacion"])


def test_ca_con_la_palabra_excluida_solo_en_un_producto_entra(limpio):
    with Session(limpio) as s:
        _ca(s, "MM1-C1", f"{_TOKEN} insumos centro", productos=("Agua purificada",))
        _ca(s, "MM1-C2", f"Reparación sistema de aguas {_TOKEN}")
        s.commit()
        recall = _recall_ca(s, ["agua"])
    assert "MM1-C1" in recall
    assert "MM1-C2" not in recall


def test_limpieza_sigue_el_mismo_criterio_y_no_borra_un_match_con_la_palabra_solo_en_un_item(limpio):
    with Session(limpio) as s:
        perfil = _perfil(s, keywords_excluir=["agua"])
        _lic(s, "MM1-LK", f"{_TOKEN} programa adulto mayor", items=("Agua embotellada",))
        _lic(s, "MM1-LB", f"Reparación sistema de aguas {_TOKEN}")
        _ca(s, "MM1-CK", f"{_TOKEN} insumos", productos=("Agua purificada",))
        for fuente, cod in (("licitaciones", "MM1-LK"), ("licitaciones", "MM1-LB"), ("compras_agiles", "MM1-CK")):
            s.add(OportunidadMatch(perfil_id=perfil.id, fuente=fuente, codigo_oportunidad=cod, score=50, razones={}))
        s.flush()

        assert contar_limpieza(s, criterio_perfil(s, perfil)) == 1
        assert limpiar_matches_perfil(s, perfil) == 1
        assert _codigos_match(s, perfil.id) == {"MM1-LK", "MM1-CK"}


def test_vista_previa_de_excluir_cuenta_solo_titulos(limpio):
    with Session(limpio) as s:
        perfil = _perfil(s)
        _lic(s, "MM1-LK", f"{_TOKEN} programa adulto mayor", items=("Agua embotellada",))
        _lic(s, "MM1-LB", f"Reparación sistema de aguas {_TOKEN}")
        for cod in ("MM1-LK", "MM1-LB"):
            s.add(OportunidadMatch(perfil_id=perfil.id, fuente="licitaciones", codigo_oportunidad=cod, score=50, razones={}))
        s.flush()
        assert contar_limpieza(s, criterio_perfil(s, perfil, ["agua"])) == 1


# ---------------------------------------------------------------------------
# Organismos seguidos (§1.2)
# ---------------------------------------------------------------------------


def test_ca_de_organismo_seguido_calza_via_rut_aunque_venga_con_o_sin_puntos(limpio):
    with Session(limpio) as s:
        s.add(InstitucionPAC(codigo_entidad=_COD_ENTIDAD, razon_social="ORG PRUEBA MM1", rut="76.123.456-7"))
        s.flush()
        perfil = _perfil(s, keywords=[], organismos_seguidos=[str(_COD_ENTIDAD)], fuentes=["compras_agiles"])
        _ca(s, "MM1-CA", "cosa neutra uno", organismo_rut="76123456-7")
        _ca(s, "MM1-CB", "cosa neutra dos", organismo_rut="76.123.456-7")
        _ca(s, "MM1-CC", "cosa neutra tres", organismo_rut="99.999.999-9")
        s.commit()

        match_perfil(perfil, s)
        assert {"MM1-CA", "MM1-CB"} <= _codigos_match(s, perfil.id)
        assert "MM1-CC" not in _codigos_match(s, perfil.id)
        m = s.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id, OportunidadMatch.codigo_oportunidad == "MM1-CB"
            )
        ).scalar_one()
        assert m.razones["organismo_seguido"] is True
        assert m.score == 40.0


def test_limpieza_no_borra_una_ca_de_organismo_seguido(limpio):
    with Session(limpio) as s:
        s.add(InstitucionPAC(codigo_entidad=_COD_ENTIDAD, razon_social="ORG PRUEBA MM1", rut="76.123.456-7"))
        s.flush()
        perfil = _perfil(s, keywords=[], organismos_seguidos=[str(_COD_ENTIDAD)], fuentes=["compras_agiles"])
        _ca(s, "MM1-CA", "cosa neutra uno", organismo_rut="76123456-7")
        s.add(OportunidadMatch(perfil_id=perfil.id, fuente="compras_agiles", codigo_oportunidad="MM1-CA", score=40, razones={}))
        s.flush()
        assert limpiar_matches_perfil(s, perfil) == 0


def test_licitacion_de_organismo_seguido_calza_por_codigo_organismo(limpio):
    with Session(limpio) as s:
        perfil = _perfil(s, keywords=[], organismos_seguidos=[str(_COD_ENTIDAD)], fuentes=["licitaciones"])
        _lic(s, "MM1-LO", "cosa neutra", codigo_organismo=str(_COD_ENTIDAD))
        _lic(s, "MM1-LX", "cosa neutra", codigo_organismo="1")
        s.commit()

        match_perfil(perfil, s)
        codigos = _codigos_match(s, perfil.id)
    assert "MM1-LO" in codigos
    assert "MM1-LX" not in codigos


# ---------------------------------------------------------------------------
# Tope de candidatos después de región y monto (§1.3)
# ---------------------------------------------------------------------------


def test_el_tope_de_candidatos_se_aplica_despues_de_region_y_monto(limpio):
    ahora = ahora_utc()
    with Session(limpio) as s:
        # Cinco CA de otra región que cierran ANTES: sin el filtro en SQL llenarían el tope.
        for i in range(5):
            _ca(s, f"MM1-F{i}", f"{_TOKEN} fuera {i}", region=7, fecha_cierre=ahora + timedelta(days=1))
        _ca(s, "MM1-DENTRO", f"{_TOKEN} dentro", region=13, fecha_cierre=ahora + timedelta(days=9))
        _ca(s, "MM1-BARATA", f"{_TOKEN} barata", region=13, monto_disponible_clp=10.0, fecha_cierre=ahora + timedelta(days=2))
        s.commit()
        q = build_tsquery([_TOKEN])
        with patch.object(eng, "_MAX_CANDIDATOS", 2):
            codigos = [
                c.codigo
                for c in eng._candidatos_ca(s, ahora, q, None, regiones=[13], monto_min=1000.0)
            ]
    assert codigos == ["MM1-DENTRO"]


def test_licitacion_sin_region_pasa_el_filtro_y_lleva_la_razon(limpio):
    with Session(limpio) as s:
        perfil = _perfil(s, regiones=[13], fuentes=["licitaciones"])
        _lic(s, "MM1-R13", f"{_TOKEN} uno", region=13)
        _lic(s, "MM1-R7", f"{_TOKEN} dos", region=7)
        _lic(s, "MM1-RN", f"{_TOKEN} tres")
        s.commit()

        match_perfil(perfil, s)
        assert _codigos_match(s, perfil.id) == {"MM1-R13", "MM1-RN"}
        sin = s.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id, OportunidadMatch.codigo_oportunidad == "MM1-RN"
            )
        ).scalar_one()
        assert sin.razones["region_no_informada"] is True
