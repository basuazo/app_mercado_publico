"""Tests F10 parte 2 — dashboard rediseñado: feed excluye descartadas,
/descartadas, toggle me-sirve/descartar/deshacer-descarte, orden, IDOR."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.query import get_oportunidades_usuario
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.matching.feedback import obtener_feedback
from app.models.base import Base
from app.models.enums import RolUsuario, ValorFeedback
from app.models.tables import Licitacion, MatchFeedback, OportunidadMatch, PerfilBusqueda, Usuario

_PW = "contraseña-segura-test"


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


def _crear_match_propio(
    engine,
    owner_id: int,
    codigo: str = "LIC-001",
    score: int = 80,
    fecha_cierre=None,
) -> None:
    """Crea una licitación + perfil + match del owner indicado."""
    with Session(engine) as s:
        s.add(
            Licitacion(
                codigo=codigo,
                nombre=f"Licitación {codigo}",
                descripcion="",
                estado="publicada",
                fecha_cierre=fecha_cierre,
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
                perfil_id=perfil.id, fuente="licitaciones", codigo_oportunidad=codigo, score=score, razones=[]
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# Feed excluye descartadas
# ---------------------------------------------------------------------------


def test_feed_excluye_descartadas(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)

    with Session(engine) as s:
        items, total, _ = get_oportunidades_usuario(s, usuario)
        assert total == 1

    client.post("/oportunidad/licitaciones/LIC-001/descartar", data={}, cookies=cookies, headers=headers)

    with Session(engine) as s:
        items, total, _ = get_oportunidades_usuario(s, usuario)
        assert total == 0

    r = client.get("/", cookies=_cookie(settings, usuario))
    assert "Licitación LIC-001" not in r.text


def test_descartadas_get_las_lista(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/descartar", data={}, cookies=cookies, headers=headers)

    r = client.get("/descartadas", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Licitación LIC-001" in r.text


def test_deshacer_descarte_reincorpora_al_feed(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/descartar", data={}, cookies=cookies, headers=headers)

    with Session(engine) as s:
        _, total, _ = get_oportunidades_usuario(s, usuario)
        assert total == 0

    r = client.post(
        "/oportunidad/licitaciones/LIC-001/deshacer-descarte",
        data={},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303

    with Session(engine) as s:
        _, total, _ = get_oportunidades_usuario(s, usuario)
        assert total == 1
        assert obtener_feedback(s, usuario, "licitaciones", "LIC-001") is None


def test_deshacer_descarte_inexistente_404(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/deshacer-descarte", data={}, cookies=cookies, headers=headers
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Me sirve: toggle, idempotente
# ---------------------------------------------------------------------------


def test_me_sirve_marca_y_alterna(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)

    client.post("/oportunidad/licitaciones/LIC-001/me-sirve", data={}, cookies=cookies, headers=headers)
    with Session(engine) as s:
        fb = obtener_feedback(s, usuario, "licitaciones", "LIC-001")
        assert fb is not None and fb.valor == ValorFeedback.SIRVE.value

    # alternar de nuevo lo borra (vuelve a neutro)
    client.post("/oportunidad/licitaciones/LIC-001/me-sirve", data={}, cookies=cookies, headers=headers)
    with Session(engine) as s:
        assert obtener_feedback(s, usuario, "licitaciones", "LIC-001") is None


def test_me_sirve_no_duplica_filas(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/me-sirve", data={}, cookies=cookies, headers=headers)
    client.post("/oportunidad/licitaciones/LIC-001/me-sirve", data={}, cookies=cookies, headers=headers)
    client.post("/oportunidad/licitaciones/LIC-001/me-sirve", data={}, cookies=cookies, headers=headers)
    with Session(engine) as s:
        filas = list(s.execute(select(MatchFeedback)).scalars())
        assert len(filas) <= 1


def test_descartar_sobre_me_sirve_reemplaza_valor(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/me-sirve", data={}, cookies=cookies, headers=headers)
    client.post("/oportunidad/licitaciones/LIC-001/descartar", data={}, cookies=cookies, headers=headers)
    with Session(engine) as s:
        fb = obtener_feedback(s, usuario, "licitaciones", "LIC-001")
        assert fb is not None and fb.valor == ValorFeedback.DESCARTE.value
        filas = list(s.execute(select(MatchFeedback)).scalars())
        assert len(filas) == 1


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_me_sirve_sin_csrf_403(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/me-sirve",
        data={"csrf_token": "invalido"},
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 403


def test_descartar_sin_csrf_403(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/descartar",
        data={"csrf_token": "invalido"},
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# IDOR: un usuario no puede dar feedback sobre matches de otro
# ---------------------------------------------------------------------------


def test_me_sirve_sobre_match_ajeno_404(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    with Session(engine) as s:
        otro = Usuario(email="otro@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(otro)
        s.commit()
        s.refresh(otro)
        otro_id = otro.id

    cookies_otro, headers_otro = _session(settings, otro_id)
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/me-sirve", data={}, cookies=cookies_otro, headers=headers_otro
    )
    assert r.status_code == 404
    with Session(engine) as s:
        assert obtener_feedback(s, otro_id, "licitaciones", "LIC-001") is None


def test_descartar_sobre_match_ajeno_404(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    with Session(engine) as s:
        otro = Usuario(email="otro2@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(otro)
        s.commit()
        s.refresh(otro)
        otro_id = otro.id

    cookies_otro, headers_otro = _session(settings, otro_id)
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/descartar", data={}, cookies=cookies_otro, headers=headers_otro
    )
    assert r.status_code == 404
    # el match del dueño original no quedó descartado
    with Session(engine) as s:
        assert obtener_feedback(s, usuario, "licitaciones", "LIC-001") is None


# ---------------------------------------------------------------------------
# Orden: score vs cierre
# ---------------------------------------------------------------------------


def test_orden_score_vs_cierre(client, usuario, settings, engine):
    from datetime import datetime

    _crear_match_propio(engine, usuario, "LIC-ALTO", score=90, fecha_cierre=datetime(2030, 1, 1))
    _crear_match_propio(engine, usuario, "LIC-BAJO", score=30, fecha_cierre=datetime(2026, 1, 1))

    with Session(engine) as s:
        items_score, _, _ = get_oportunidades_usuario(s, usuario, orden="score")
        assert [i["match"].codigo_oportunidad for i in items_score] == ["LIC-ALTO", "LIC-BAJO"]

        items_cierre, _, _ = get_oportunidades_usuario(s, usuario, orden="cierre")
        assert [i["match"].codigo_oportunidad for i in items_cierre] == ["LIC-BAJO", "LIC-ALTO"]


def test_orden_cierre_nulos_al_final(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-SIN-FECHA", score=90, fecha_cierre=None)
    _crear_match_propio(engine, usuario, "LIC-CON-FECHA", score=10)

    with Session(engine) as s:
        from datetime import datetime

        m = s.execute(
            select(OportunidadMatch).where(OportunidadMatch.codigo_oportunidad == "LIC-CON-FECHA")
        ).scalar_one()
        lic = s.get(Licitacion, "LIC-CON-FECHA")
        assert lic is not None
        lic.fecha_cierre = datetime(2026, 1, 1)
        s.commit()
        del m

    with Session(engine) as s:
        items, _, _ = get_oportunidades_usuario(s, usuario, orden="cierre")
        assert [i["match"].codigo_oportunidad for i in items] == ["LIC-CON-FECHA", "LIC-SIN-FECHA"]


def test_dashboard_orden_param_en_query(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    r = client.get("/?orden=cierre", cookies=_cookie(settings, usuario))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Seguir rápido desde la lista sigue funcionando
# ---------------------------------------------------------------------------


def test_seguir_rapido_desde_dashboard(client, usuario, settings, engine):
    from app.matching.seguimiento import obtener_seguimiento

    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/seguir",
        data={"next": "/"},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        assert obtener_seguimiento(s, usuario, "licitaciones", "LIC-001") is not None


def test_seguir_via_htmx_devuelve_tarjeta_parcial(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    headers_htmx = {**headers, "HX-Request": "true"}
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/seguir",
        data={},
        cookies=cookies,
        headers=headers_htmx,
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "Siguiendo" in r.text


def test_descartar_via_htmx_devuelve_200_vacio(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    headers_htmx = {**headers, "HX-Request": "true"}
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/descartar",
        data={},
        cookies=cookies,
        headers=headers_htmx,
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.text == ""


# ---------------------------------------------------------------------------
# Render del dashboard con tarjetas nuevas
# ---------------------------------------------------------------------------


def test_dashboard_render_tarjeta_con_acciones(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001", score=90)
    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Licitación LIC-001" in r.text
    assert "/oportunidad/licitaciones/LIC-001/me-sirve" in r.text
    assert "/oportunidad/licitaciones/LIC-001/descartar" in r.text
    assert "/oportunidad/licitaciones/LIC-001/seguir" in r.text


def test_dashboard_banner_descartadas(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/descartar", data={}, cookies=cookies, headers=headers)

    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Umbral de relevancia del feed (F-feed-umbral)
# ---------------------------------------------------------------------------


def test_min_score_filtra_bajo_el_piso(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-ALTO", score=80)
    _crear_match_propio(engine, usuario, "LIC-BAJO", score=10)

    with Session(engine) as s:
        items, total, total_sin_relevancia = get_oportunidades_usuario(s, usuario, min_score=40)
        assert total == 1
        assert [i["match"].codigo_oportunidad for i in items] == ["LIC-ALTO"]
        assert total_sin_relevancia == 2


def test_min_score_cero_muestra_todo(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-ALTO", score=80)
    _crear_match_propio(engine, usuario, "LIC-BAJO", score=10)

    with Session(engine) as s:
        items, total, total_sin_relevancia = get_oportunidades_usuario(s, usuario, min_score=0)
        assert total == 2
        assert total_sin_relevancia == 2
        assert {i["match"].codigo_oportunidad for i in items} == {"LIC-ALTO", "LIC-BAJO"}


def test_min_score_conteo_de_ocultas(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-1", score=80)
    _crear_match_propio(engine, usuario, "LIC-2", score=60)
    _crear_match_propio(engine, usuario, "LIC-3", score=20)

    with Session(engine) as s:
        _, total, total_sin_relevancia = get_oportunidades_usuario(s, usuario, min_score=50)
        assert total == 2
        assert total_sin_relevancia == 3
        assert total_sin_relevancia - total == 1


def test_min_score_no_rompe_orden_por_score(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-MEDIO", score=55)
    _crear_match_propio(engine, usuario, "LIC-ALTO", score=90)
    _crear_match_propio(engine, usuario, "LIC-BAJO", score=10)

    with Session(engine) as s:
        items, _, _ = get_oportunidades_usuario(s, usuario, min_score=50, orden="score")
        assert [i["match"].codigo_oportunidad for i in items] == ["LIC-ALTO", "LIC-MEDIO"]


def test_dashboard_aplica_default_de_settings(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-ALTO", score=80)
    _crear_match_propio(engine, usuario, "LIC-BAJO", score=10)
    assert settings.feed_min_score_default == 40

    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Licitación LIC-ALTO" in r.text
    assert "Licitación LIC-BAJO" not in r.text
    assert "oculta" in r.text
    assert "ver todas" in r.text


def test_dashboard_respeta_override_min_score_por_query(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-ALTO", score=80)
    _crear_match_propio(engine, usuario, "LIC-BAJO", score=10)

    r = client.get("/?min_score=0", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Licitación LIC-ALTO" in r.text
    assert "Licitación LIC-BAJO" in r.text
    assert "oculta" not in r.text


def test_dashboard_render_control_de_relevancia(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001", score=80)
    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Alta relevancia" in r.text
    assert "Todas" in r.text
    assert "min_score=" in r.text


def test_dashboard_usa_total_filtrado_por_relevancia(client, usuario, settings, engine):
    for i in range(25):
        _crear_match_propio(engine, usuario, f"LIC-ALTO-{i}", score=80)
    _crear_match_propio(engine, usuario, "LIC-BAJO", score=10)

    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Mostrando 25 oportunidad(es)" in r.text
    assert "1 oculta(s) por baja relevancia" in r.text
