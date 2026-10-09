"""Tests F-match-1 — feed sin duplicados, orden "Mejor match", búsqueda sin tildes,
región de licitaciones y fechas de publicación en hora de Chile. SQLite en memoria."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.api.presentacion import fecha_chile
from app.api.query import (
    FiltrosFeed,
    _aplicar_filtros,
    get_oportunidades_usuario,
)
from app.core.tiempo import ahora_utc
from app.models.tables import (
    Licitacion,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

_PW_HASH = "$2b$12$fakehashforteststhatislong.enough.xyz"


@pytest.fixture()
def session():
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(e)
    with Session(e) as s:
        yield s
    e.dispose()


def _usuario(s: Session) -> Usuario:
    u = Usuario(email="feed1@test.cl", password_hash=_PW_HASH, activo=True)
    s.add(u)
    s.flush()
    return u


def _perfil(s: Session, u: Usuario, nombre: str) -> PerfilBusqueda:
    p = PerfilBusqueda(owner_id=u.id, nombre=nombre, keywords=["k"], activo=True)
    s.add(p)
    s.flush()
    return p


def _lic(s: Session, codigo: str, nombre: str, dias: float, **campos: Any) -> Licitacion:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": nombre,
        "descripcion": "",
        "estado": "publicada",
        "fecha_cierre": ahora_utc() + timedelta(days=dias),
    }
    base.update(campos)
    lic = Licitacion(**base)
    s.add(lic)
    s.flush()
    return lic


def _match(s: Session, perfil: PerfilBusqueda, codigo: str, score: float, **razones: Any) -> OportunidadMatch:
    m = OportunidadMatch(
        perfil_id=perfil.id,
        fuente="licitaciones",
        codigo_oportunidad=codigo,
        score=score,
        razones={"keywords_hit": ["k"], **razones},
        fecha_match=ahora_utc(),
    )
    s.add(m)
    s.flush()
    return m


def test_dos_perfiles_que_calzan_la_misma_oportunidad_dan_un_item(session: Session):
    u = _usuario(session)
    a, b = _perfil(session, u, "Aseo"), _perfil(session, u, "Limpieza")
    _lic(session, "L-1", "Servicio de aseo", 5)
    _match(session, a, "L-1", 55)
    _match(session, b, "L-1", 73)

    res = get_oportunidades_usuario(session, u.id, min_score=0)

    assert res.total == 1 and len(res.items) == 1
    item = res.items[0]
    assert item["score"] == 73  # el de mayor relevancia
    assert item["perfiles"] == ["Limpieza", "Aseo"]
    assert res.facetas["fuente"] == {"licitaciones": 1}
    # La faceta de perfiles cuenta la oportunidad en cada uno de sus perfiles.
    assert res.facetas["perfil"] == {str(a.id): 1, str(b.id): 1}


def test_filtrar_por_un_perfil_encuentra_la_oportunidad_aunque_el_mejor_match_sea_de_otro(session: Session):
    u = _usuario(session)
    a, b = _perfil(session, u, "Aseo"), _perfil(session, u, "Limpieza")
    _lic(session, "L-1", "Servicio de aseo", 5)
    _match(session, a, "L-1", 55)
    _match(session, b, "L-1", 73)

    res = get_oportunidades_usuario(session, u.id, perfil_ids=[a.id])

    assert res.total == 1


def test_orden_mejor_match_relevancia_y_luego_cierre_mas_proximo_con_null_al_final(session: Session):
    u = _usuario(session)
    p = _perfil(session, u, "P")
    _lic(session, "L-ALTA", "alta", 20)
    _lic(session, "L-B-LEJOS", "b lejos", 9)
    _lic(session, "L-B-CERCA", "b cerca", 3)
    _match(session, p, "L-ALTA", 80)
    _match(session, p, "L-B-LEJOS", 60)
    _match(session, p, "L-B-CERCA", 60)

    res = get_oportunidades_usuario(session, u.id, orden="score")

    assert [i["codigo"] for i in res.items] == ["L-ALTA", "L-B-CERCA", "L-B-LEJOS"]


def test_texto_sin_tildes_ni_mayusculas_sobre_nombre_y_organismo(session: Session):
    u = _usuario(session)
    p = _perfil(session, u, "P")
    _lic(session, "L-1", "Reparación de techumbre", 5, organismo_nombre="Municipalidad de Ñuñoa")
    _lic(session, "L-2", "Compra de papel", 5, organismo_nombre="Hospital Regional")
    _match(session, p, "L-1", 60)
    _match(session, p, "L-2", 60)

    assert [i["codigo"] for i in get_oportunidades_usuario(session, u.id, texto="reparacion").items] == ["L-1"]
    assert [i["codigo"] for i in get_oportunidades_usuario(session, u.id, texto="REPARACIÓN").items] == ["L-1"]
    # Por organismo
    assert [i["codigo"] for i in get_oportunidades_usuario(session, u.id, texto="nunoa").items] == ["L-1"]
    assert [i["codigo"] for i in get_oportunidades_usuario(session, u.id, texto="hospital").items] == ["L-2"]


def test_region_filtra_licitaciones_y_la_no_informada_pasa(session: Session):
    u = _usuario(session)
    p = _perfil(session, u, "P")
    _lic(session, "L-RM", "uno", 5, region=13)
    _lic(session, "L-SUR", "dos", 5, region=10)
    _lic(session, "L-NULL", "tres", 5)
    for cod in ("L-RM", "L-SUR", "L-NULL"):
        _match(session, p, cod, 60)

    res = get_oportunidades_usuario(session, u.id, region=13)

    assert {i["codigo"] for i in res.items} == {"L-RM", "L-NULL"}
    assert res.facetas["region"] == {"13": 1, "10": 1, "sin_region": 1}


def test_los_filtros_puros_siguen_funcionando_con_items_sin_matches(session: Session):
    """Un item con solo `match` (tests y guardadas sin dedupe) se filtra igual."""
    u = _usuario(session)
    p = _perfil(session, u, "P")
    m = _match(session, p, "L-1", 60)
    item = {"match": m, "nombre": "x", "organismo": None, "region": None, "monto": None, "fecha_cierre": None}

    assert _aplicar_filtros([item], FiltrosFeed(perfiles=frozenset({p.id}))) == [item]
    assert _aplicar_filtros([item], FiltrosFeed(perfiles=frozenset({p.id + 1}))) == []


def test_fecha_de_publicacion_en_hora_de_chile():
    # 22:00 del 15-jul en Chile (UTC-4 en invierno) = 02:00 del 16-jul UTC guardado naive.
    publicada_utc = datetime(2026, 7, 16, 2, 0)
    assert fecha_chile(publicada_utc) == "15/07/2026"
    # Verano (UTC-3): 22:00 del 15-ene en Chile = 01:00 del 16-ene UTC.
    assert fecha_chile(datetime(2026, 1, 16, 1, 0)) == "15/01/2026"
    # Un borde de día de Chile (00:00 local) conserva su día.
    assert fecha_chile(datetime(2026, 7, 15, 4, 0)) == "15/07/2026"
    assert fecha_chile(None) == "No informada"
