"""Tests F-feed-agrupado — feed agrupado por categorías (motivo/región/fuente),
reemplaza la lista plana del dashboard (F10)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.query import agrupar_oportunidades
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import CompraAgil, Licitacion, OportunidadMatch, PerfilBusqueda, Usuario

_PW = "contraseña-segura-test"


# ---------------------------------------------------------------------------
# Fixtures (mismo patrón que tests/test_feedback_routes.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401

    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    yield e


@pytest.fixture()
def settings():
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


@pytest.fixture()
def client(engine, settings):
    application = create_app(settings, engine)
    return TestClient(application, raise_server_exceptions=True)


@pytest.fixture()
def usuario(engine):
    with Session(engine) as s:
        u = Usuario(email="user@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _cookie(settings: Settings, user_id: int) -> dict[str, str]:
    token = create_session_token(settings.secret_key, user_id)
    return {COOKIE_NAME: token}


def _session(settings: Settings, user_id: int) -> tuple[dict[str, str], dict[str, str]]:
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    cookies = {COOKIE_NAME: token}
    headers = {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}
    return cookies, headers


def _crear_match_licitacion(
    engine,
    owner_id: int,
    codigo: str = "LIC-001",
    score: int = 80,
    razones: dict | None = None,
) -> None:
    """Crea una licitación + perfil + match del owner indicado."""
    with Session(engine) as s:
        s.add(
            Licitacion(
                codigo=codigo,
                nombre=f"Licitación {codigo}",
                descripcion="",
                estado="publicada",
            )
        )
        perfil = PerfilBusqueda(
            owner_id=owner_id,
            nombre="Perfil test",
            keywords=["test"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["licitaciones"],
            activo=True,
        )
        s.add(perfil)
        s.flush()
        s.add(
            OportunidadMatch(
                perfil_id=perfil.id,
                fuente="licitaciones",
                codigo_oportunidad=codigo,
                score=score,
                razones=razones if razones is not None else {},
            )
        )
        s.commit()


def _crear_match_ca(
    engine,
    owner_id: int,
    codigo: str = "CA-001",
    score: int = 80,
    region: int | None = None,
) -> None:
    """Crea una Compra Ágil + perfil + match del owner indicado (para probar
    agrupar_por='region', que solo CA popula)."""
    with Session(engine) as s:
        s.add(
            CompraAgil(
                codigo=codigo,
                nombre=f"Compra Ágil {codigo}",
                descripcion="",
                estado="publicada",
                region=region,
            )
        )
        perfil = PerfilBusqueda(
            owner_id=owner_id,
            nombre="Perfil CA",
            keywords=["test"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["compras_agiles"],
            activo=True,
        )
        s.add(perfil)
        s.flush()
        s.add(
            OportunidadMatch(
                perfil_id=perfil.id,
                fuente="compras_agiles",
                codigo_oportunidad=codigo,
                score=score,
                razones={},
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# Backend: agrupar_oportunidades (unit, sin DB)
# ---------------------------------------------------------------------------


def _item(
    codigo: str,
    score: float,
    *,
    fuente: str = "licitaciones",
    razones: dict | None = None,
    region_nombre: str | None = None,
    dias_al_cierre: float | None = None,
) -> dict:
    match = OportunidadMatch(
        perfil_id=1,
        fuente=fuente,
        codigo_oportunidad=codigo,
        score=score,
        razones=razones if razones is not None else {},
    )
    return {
        "match": match,
        "region_nombre": region_nombre,
        "dias_al_cierre": dias_al_cierre,
    }


def test_motivo_expande_en_varios_grupos():
    item = _item(
        "LIC-1",
        80,
        razones={"categorias_hit": ["1010", "1011"], "keywords_hit": ["software"]},
    )
    grupos, total_unico, total_apariciones = agrupar_oportunidades([item], "motivo")

    assert total_unico == 1
    assert total_apariciones == 3
    assert len(grupos) == 3
    labels = {g["label"] for g in grupos}
    assert "software" in labels
    # Los 2 códigos UNSPSC generan 2 grupos "rubro" distintos (aunque no
    # resuelvan a nombre legible, el código crudo basta para distinguirlos).
    assert sum(1 for g in grupos if g["tipo"] == "rubro") == 2
    assert sum(1 for g in grupos if g["tipo"] == "keyword") == 1


def test_grupo_otros_sin_motivo():
    item = _item("LIC-1", 50, razones={})
    grupos, total_unico, total_apariciones = agrupar_oportunidades([item], "motivo")

    assert total_unico == 1
    assert total_apariciones == 1
    assert len(grupos) == 1
    assert grupos[0]["label"] == "Otros"
    assert grupos[0]["tipo"] == "otros"


def test_grupo_organismo_seguido():
    item = _item("LIC-1", 50, razones={"organismo_seguido": True})
    grupos, _, _ = agrupar_oportunidades([item], "motivo")

    assert len(grupos) == 1
    assert grupos[0]["label"] == "Organismo seguido"


def test_agrupar_por_region():
    items = [
        _item("CA-1", 80, region_nombre="Valparaíso"),
        _item("CA-2", 70, region_nombre="Valparaíso"),
        _item("LIC-1", 60, region_nombre=None),
    ]
    grupos, total_unico, total_apariciones = agrupar_oportunidades(items, "region")

    assert total_unico == 3
    assert total_apariciones == 3
    por_label = {g["label"]: g for g in grupos}
    assert por_label["Valparaíso"]["count"] == 2
    assert por_label["Sin región"]["count"] == 1


def test_agrupar_por_fuente():
    items = [
        _item("LIC-1", 80, fuente="licitaciones"),
        _item("CA-1", 70, fuente="compras_agiles"),
    ]
    grupos, total_unico, total_apariciones = agrupar_oportunidades(items, "fuente")

    assert total_unico == 2
    assert total_apariciones == 2
    labels = {g["label"] for g in grupos}
    assert labels == {"Licitaciones", "Compra Ágil"}


def test_orden_grupos_por_mejor_score():
    items = [
        _item("LIC-1", 30, razones={"keywords_hit": ["bajo"]}),
        _item("LIC-2", 90, razones={"keywords_hit": ["alto"]}),
        _item("LIC-3", 60, razones={"keywords_hit": ["medio"]}),
    ]
    grupos, _, _ = agrupar_oportunidades(items, "motivo")

    assert [g["label"] for g in grupos] == ["alto", "medio", "bajo"]


def test_orden_items_dentro_de_grupo_respeta_orden_de_entrada():
    # Simula el orden ya aplicado por get_oportunidades_usuario (score desc);
    # agrupar_oportunidades NO debe reordenar dentro del grupo.
    items = [
        _item("LIC-ALTO", 90, razones={"organismo_seguido": True}),
        _item("LIC-MEDIO", 60, razones={"organismo_seguido": True}),
        _item("LIC-BAJO", 30, razones={"organismo_seguido": True}),
    ]
    grupos, _, _ = agrupar_oportunidades(items, "motivo")

    assert len(grupos) == 1
    codigos = [i["match"].codigo_oportunidad for i in grupos[0]["items"]]
    assert codigos == ["LIC-ALTO", "LIC-MEDIO", "LIC-BAJO"]


def test_cap_por_grupo_y_ver_mas():
    items = [_item(f"LIC-{i}", 80, razones={"organismo_seguido": True}) for i in range(15)]
    grupos, _, total_apariciones = agrupar_oportunidades(items, "motivo", cap_por_grupo=10)

    assert total_apariciones == 15
    assert grupos[0]["count"] == 15
    assert len(grupos[0]["items"]) == 10


def test_grupo_expandido_levanta_el_cap():
    items = [_item(f"LIC-{i}", 80, razones={"organismo_seguido": True}) for i in range(15)]
    grupos, _, _ = agrupar_oportunidades(
        items, "motivo", cap_por_grupo=10, grupo_expandido="organismo_seguido:Organismo seguido"
    )

    assert len(grupos[0]["items"]) == 15


# ---------------------------------------------------------------------------
# Ruta GET / — vista agrupada
# ---------------------------------------------------------------------------


def test_umbral_aplica_antes_de_agrupar(client, usuario, settings, engine):
    _crear_match_licitacion(engine, usuario, "LIC-BAJO", score=10, razones={"organismo_seguido": True})
    _crear_match_licitacion(engine, usuario, "LIC-ALTO", score=80, razones={"organismo_seguido": True})

    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Licitación LIC-ALTO" in r.text
    assert "Licitación LIC-BAJO" not in r.text


def test_descartar_oculta_todas_las_apariciones(client, usuario, settings, engine):
    _crear_match_licitacion(
        engine,
        usuario,
        "LIC-DOBLE",
        score=80,
        razones={"categorias_hit": ["1010", "1011"]},
    )
    cookies, headers = _session(settings, usuario)

    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.text.count("Licitación LIC-DOBLE") == 2

    r_post = client.post(
        "/oportunidad/licitaciones/LIC-DOBLE/descartar", data={}, cookies=cookies, headers=headers
    )
    assert r_post.status_code in (200, 303)

    r2 = client.get("/", cookies=_cookie(settings, usuario))
    assert "Licitación LIC-DOBLE" not in r2.text


def test_render_vista_agrupada_sin_resultados(client, usuario, settings, engine):
    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "No hay oportunidades que coincidan con tus perfiles." in r.text


def test_render_grupo_otros_por_defecto(client, usuario, settings, engine):
    _crear_match_licitacion(engine, usuario, "LIC-001", score=80, razones={})
    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Otros" in r.text
    assert "Licitación LIC-001" in r.text


def test_render_encabezado_unico_vs_apariciones(client, usuario, settings, engine):
    _crear_match_licitacion(
        engine,
        usuario,
        "LIC-DOBLE",
        score=80,
        razones={"categorias_hit": ["1010", "1011"]},
    )
    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Mostrando 1 oportunidad(es)" in r.text
    assert "2 aparición(es)" in r.text


def test_agrupar_por_region_via_query(client, usuario, settings, engine):
    _crear_match_ca(engine, usuario, "CA-1", score=80, region=5)
    _crear_match_ca(engine, usuario, "CA-2", score=70, region=None)

    r = client.get("/?agrupar_por=region", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Valparaíso" in r.text
    assert "Sin región" in r.text


def test_agrupar_por_fuente_via_query(client, usuario, settings, engine):
    _crear_match_licitacion(engine, usuario, "LIC-1", score=80)
    _crear_match_ca(engine, usuario, "CA-1", score=70)

    r = client.get("/?agrupar_por=fuente", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Licitaciones" in r.text
    assert "Compra Ágil" in r.text


def test_dashboard_cap_por_grupo_muestra_ver_mas(client, usuario, settings, engine):
    for i in range(15):
        _crear_match_licitacion(engine, usuario, f"LIC-{i}", score=80, razones={"organismo_seguido": True})

    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Ver más en este grupo (10 de 15)" in r.text
