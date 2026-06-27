"""Tests F-seguir — rutas HTML: seguir/archivar/dejar de seguir, /seguidas, IDOR, CSRF."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.matching.seguimiento import obtener_seguimiento
from app.models.base import Base
from app.models.enums import FrecuenciaAlerta, RolUsuario
from app.models.tables import Licitacion, OportunidadMatch, PerfilBusqueda, Usuario

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


def _crear_match_propio(engine, owner_id: int, codigo: str = "LIC-001") -> None:
    """Crea una licitación + perfil + match del owner indicado, condición para
    poder seguirla (check_oportunidad_access exige acceso vía match)."""
    with Session(engine) as s:
        s.add(Licitacion(codigo=codigo, nombre="Licitación test", descripcion="", estado="publicada"))
        perfil = PerfilBusqueda(
            owner_id=owner_id,
            nombre="Perfil test",
            keywords=["test"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["licitaciones"],
            frecuencia_alerta=FrecuenciaAlerta.INMEDIATA,
            activo=True,
        )
        s.add(perfil)
        s.flush()
        s.add(
            OportunidadMatch(
                perfil_id=perfil.id, fuente="licitaciones", codigo_oportunidad=codigo, score=80, razones=[]
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# Seguir
# ---------------------------------------------------------------------------


def test_seguir_sin_csrf_403(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/seguir",
        data={"csrf_token": "invalido"},
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 403


def test_seguir_con_csrf_crea_seguimiento(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/seguir",
        data={},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        seg = obtener_seguimiento(s, usuario, "licitaciones", "LIC-001")
        assert seg is not None
        assert seg.estado_visto == "publicada"


def test_seguir_oportunidad_inaccesible_404(client, usuario, settings, engine):
    """Sin match propio, check_oportunidad_access falla → 404, no se crea seguimiento."""
    with Session(engine) as s:
        s.add(Licitacion(codigo="LIC-AJENA", nombre="Ajena", descripcion="", estado="publicada"))
        s.commit()
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/oportunidad/licitaciones/LIC-AJENA/seguir",
        data={},
        cookies=cookies,
        headers=headers,
    )
    assert r.status_code == 404
    with Session(engine) as s:
        assert obtener_seguimiento(s, usuario, "licitaciones", "LIC-AJENA") is None


def test_seguir_no_duplica_via_ruta(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    cookies, headers = _session(settings, usuario)
    for _ in range(2):
        client.post("/oportunidad/licitaciones/LIC-001/seguir", data={}, cookies=cookies, headers=headers)
    with Session(engine) as s:
        from sqlalchemy import select

        from app.models.tables import OportunidadSeguida

        filas = list(s.execute(select(OportunidadSeguida)).scalars())
        assert len(filas) == 1


# ---------------------------------------------------------------------------
# Archivar / desarchivar / dejar de seguir
# ---------------------------------------------------------------------------


def test_archivar_y_desarchivar(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/seguir", data={}, cookies=cookies, headers=headers)

    r = client.post(
        "/oportunidad/licitaciones/LIC-001/archivar", data={}, cookies=cookies, headers=headers, follow_redirects=False
    )
    assert r.status_code == 303
    with Session(engine) as s:
        seg = obtener_seguimiento(s, usuario, "licitaciones", "LIC-001")
        assert seg is not None and seg.archivada is True

    r = client.post(
        "/oportunidad/licitaciones/LIC-001/desarchivar",
        data={},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        seg = obtener_seguimiento(s, usuario, "licitaciones", "LIC-001")
        assert seg is not None and seg.archivada is False


def test_dejar_de_seguir(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/seguir", data={}, cookies=cookies, headers=headers)

    r = client.post(
        "/oportunidad/licitaciones/LIC-001/dejar-de-seguir",
        data={},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    with Session(engine) as s:
        assert obtener_seguimiento(s, usuario, "licitaciones", "LIC-001") is None


def test_archivar_inexistente_404(client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post("/oportunidad/licitaciones/NOPE/archivar", data={}, cookies=cookies, headers=headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# IDOR: un usuario no puede archivar/dejar de seguir lo de otro
# ---------------------------------------------------------------------------


def test_archivar_seguimiento_ajeno_404(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario, "LIC-001")
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/seguir", data={}, cookies=cookies, headers=headers)

    with Session(engine) as s:
        otro = Usuario(email="otro@test.cl", password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(otro)
        s.commit()
        s.refresh(otro)
        otro_id = otro.id

    cookies_otro, headers_otro = _session(settings, otro_id)
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/archivar", data={}, cookies=cookies_otro, headers=headers_otro
    )
    assert r.status_code == 404

    # el seguimiento del propietario original sigue intacto (no archivado)
    with Session(engine) as s:
        seg = obtener_seguimiento(s, usuario, "licitaciones", "LIC-001")
        assert seg is not None and seg.archivada is False


# ---------------------------------------------------------------------------
# /seguidas
# ---------------------------------------------------------------------------


def test_seguidas_get_lista_activas(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/seguir", data={}, cookies=cookies, headers=headers)

    r = client.get("/seguidas", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "LIC-001" in r.text or "Licitación test" in r.text
    assert "Fuente: Dirección ChileCompra" in r.text


def test_seguidas_get_oculta_archivadas_por_defecto(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/seguir", data={}, cookies=cookies, headers=headers)
    client.post("/oportunidad/licitaciones/LIC-001/archivar", data={}, cookies=cookies, headers=headers)

    r = client.get("/seguidas", cookies=_cookie(settings, usuario))
    assert "No tienes oportunidades seguidas activas" in r.text

    r2 = client.get("/seguidas?archivadas=1", cookies=_cookie(settings, usuario))
    assert "Licitación test" in r2.text


def test_seguidas_sin_sesion_redirige(client):
    r = client.get("/seguidas", follow_redirects=False)
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# Botones en la ficha
# ---------------------------------------------------------------------------


def test_ficha_muestra_boton_seguir_si_no_sigue(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "/oportunidad/licitaciones/LIC-001/seguir" in r.text
    assert "/oportunidad/licitaciones/LIC-001/dejar-de-seguir" not in r.text


def test_ficha_muestra_botones_archivar_y_dejar_de_seguir_si_sigue(client, usuario, settings, engine):
    _crear_match_propio(engine, usuario)
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/seguir", data={}, cookies=cookies, headers=headers)

    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "/oportunidad/licitaciones/LIC-001/archivar" in r.text
    assert "/oportunidad/licitaciones/LIC-001/dejar-de-seguir" in r.text
