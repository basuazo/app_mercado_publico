"""Tests F6 — auth, dashboard, API REST.

Cobertura:
- Login / logout
- Rate limit de login
- Redirect a /login si no hay sesión
- IDOR: oportunidad ajena devuelve 404, no 403
- CSRF: POST sin token → 403, con header X-CSRF-Token → OK
- /api/jobs/run: token correcto → 200, token incorrecto → 401
- /api/salud: sin secretos en respuesta
- /api/salud/ping: público, sin auth
- Perfiles: solo propios (IDOR devuelve 404)
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import create_session_token
from app.core.settings import Settings
from app.models.base import Base
from app.models.enums import FrecuenciaAlerta, RolUsuario
from app.models.tables import (
    OportunidadMatch,
    PerfilBusqueda,
    SyncState,
    Usuario,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    # Pass the same engine so the app uses the same DB as the test fixtures
    application = create_app(settings, engine)
    return TestClient(application, raise_server_exceptions=True)


@pytest.fixture()
def usuario(engine):
    """Crea usuario normal."""
    with Session(engine) as s:
        u = Usuario(
            email="user@test.cl",
            password_hash=hash_password(_PW),
            rol=RolUsuario.USUARIO,
            activo=True,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


@pytest.fixture()
def admin(engine):
    """Crea usuario admin."""
    with Session(engine) as s:
        u = Usuario(
            email="admin@test.cl",
            password_hash=hash_password(_PW),
            rol=RolUsuario.ADMIN,
            activo=True,
        )
        s.add(u)
        s.commit()
        s.refresh(u)
        return u.id


def _cookie(settings: Settings, user_id: int) -> dict[str, str]:
    from app.auth.session import COOKIE_NAME
    token = create_session_token(settings.secret_key, user_id)
    return {COOKIE_NAME: token}


def _csrf_header(settings: Settings, user_id: int) -> dict[str, str]:
    return {"X-CSRF-Token": generate_csrf_token(settings.secret_key, user_id)}


# ---------------------------------------------------------------------------
# Login / logout
# ---------------------------------------------------------------------------


def test_login_ok(client, usuario, settings):
    r = client.post(
        "/login",
        data={"email": "user@test.cl", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/"
    from app.auth.session import COOKIE_NAME
    assert COOKIE_NAME in r.cookies


def test_login_bad_password(client, usuario):
    r = client.post(
        "/login",
        data={"email": "user@test.cl", "password": "mala", "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_login_usuario_inactivo(engine, client, settings):
    with Session(engine) as s:
        u = Usuario(
            email="inactivo@test.cl",
            password_hash=hash_password(_PW),
            rol=RolUsuario.USUARIO,
            activo=False,
        )
        s.add(u)
        s.commit()

    r = client.post(
        "/login",
        data={"email": "inactivo@test.cl", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_logout_borra_cookie(client, usuario, settings):
    cookies = _cookie(settings, usuario)
    csrf = _csrf_header(settings, usuario)
    r = client.post("/logout", data={"csrf_token": csrf["X-CSRF-Token"]}, cookies=cookies, follow_redirects=False)
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# Rate limit de login
# ---------------------------------------------------------------------------


def test_rate_limit_despues_de_5_intentos(client, usuario):
    for _ in range(5):
        client.post(
            "/login",
            data={"email": "user@test.cl", "password": "mal"},
            follow_redirects=False,
        )
    r = client.post(
        "/login",
        data={"email": "user@test.cl", "password": _PW},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Demasiados" in r.headers["location"]


# ---------------------------------------------------------------------------
# Redirect a /login sin sesión
# ---------------------------------------------------------------------------


def test_dashboard_sin_sesion_redirige(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]


def test_perfiles_sin_sesion_redirige(client):
    r = client.get("/perfiles", follow_redirects=False)
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# Dashboard con sesión
# ---------------------------------------------------------------------------


def test_dashboard_con_sesion(client, usuario, settings):
    r = client.get("/", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Fuente: Dirección ChileCompra" in r.text


# ---------------------------------------------------------------------------
# IDOR: oportunidad ajena devuelve 404
# ---------------------------------------------------------------------------


def test_oportunidad_ajena_404(engine, client, settings):
    with Session(engine) as s:
        otro = Usuario(
            email="otro@test.cl",
            password_hash=hash_password(_PW),
            rol=RolUsuario.USUARIO,
            activo=True,
        )
        s.add(otro)
        s.flush()
        perfil = PerfilBusqueda(
            owner_id=otro.id,
            nombre="ajeno",
            keywords=["tecnología"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["licitaciones"],
            frecuencia_alerta=FrecuenciaAlerta.INMEDIATA,
            activo=True,
        )
        s.add(perfil)
        s.flush()
        match = OportunidadMatch(
            perfil_id=perfil.id,
            fuente="licitaciones",
            codigo_oportunidad="LIC-999",
            score=80,
            razones=[],
        )
        s.add(match)
        s.commit()
        user_id_propio = _crear_usuario_normal(s, "propio@test.cl")
        s.commit()

    from app.auth.session import COOKIE_NAME
    client.cookies.set(COOKIE_NAME, create_session_token(settings.secret_key, user_id_propio))
    r = client.get("/oportunidad/licitaciones/LIC-999")
    assert r.status_code == 404


def _crear_usuario_normal(session: Session, email: str) -> int:
    u = Usuario(
        email=email,
        password_hash=hash_password(_PW),
        rol=RolUsuario.USUARIO,
        activo=True,
    )
    session.add(u)
    session.flush()
    return u.id


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


def test_crear_perfil_sin_csrf_403(client, usuario, settings):
    r = client.post(
        "/perfiles/nuevo",
        data={"nombre": "test", "keywords": "tecnología", "csrf_token": "invalido"},
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 403


def test_crear_perfil_con_csrf_header_ok(client, usuario, settings):
    r = client.post(
        "/perfiles/nuevo",
        data={"nombre": "Mi perfil", "keywords": "tecnología"},
        headers=_csrf_header(settings, usuario),
        cookies=_cookie(settings, usuario),
        follow_redirects=False,
    )
    assert r.status_code == 303


# ---------------------------------------------------------------------------
# /api/salud/ping — público
# ---------------------------------------------------------------------------


def test_ping_publico(client):
    r = client.get("/api/salud/ping")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# /api/jobs/run — solo X-Jobs-Token
# ---------------------------------------------------------------------------


def test_jobs_run_token_correcto(client):
    r = client.post("/api/jobs/run", headers={"X-Jobs-Token": "jobs-token-secreto"})
    assert r.status_code == 200
    assert r.json()["queued"] is True
    assert r.json()["job"] == "all"


def test_jobs_run_job_invalido(client):
    r = client.post(
        "/api/jobs/run?job=xxx", headers={"X-Jobs-Token": "jobs-token-secreto"}
    )
    assert r.status_code == 400


def test_jobs_run_job_ca(client):
    r = client.post(
        "/api/jobs/run?job=ca", headers={"X-Jobs-Token": "jobs-token-secreto"}
    )
    assert r.status_code == 200
    assert r.json()["queued"] is True
    assert r.json()["job"] == "ca"


def test_jobs_run_token_incorrecto(client):
    r = client.post("/api/jobs/run", headers={"X-Jobs-Token": "incorrecto"})
    assert r.status_code == 401


def test_jobs_run_sin_token(client):
    r = client.post("/api/jobs/run")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# /api/salud — sin secretos (admin only)
# ---------------------------------------------------------------------------


def test_api_salud_no_secretos(engine, client, admin, settings):
    with Session(engine) as s:
        s.add(SyncState(fuente="test", requests_usadas_hoy=0))
        s.commit()

    r = client.get(
        "/api/salud",
        cookies=_cookie(settings, admin),
    )
    assert r.status_code == 200
    body = r.text
    assert "TICKET_TEST" not in body
    assert "secret-test-key" not in body
    assert "jobs-token-secreto" not in body


def test_api_salud_usuario_normal_403(client, usuario, settings):
    r = client.get("/api/salud", cookies=_cookie(settings, usuario))
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# /api/perfiles — IDOR devuelve 404
# ---------------------------------------------------------------------------


def test_api_perfil_ajeno_404(engine, client, settings):
    with Session(engine) as s:
        otro_id = _crear_usuario_normal(s, "otro2@test.cl")
        s.flush()
        perfil = PerfilBusqueda(
            owner_id=otro_id,
            nombre="ajeno",
            keywords=["x"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["licitaciones"],
            frecuencia_alerta=FrecuenciaAlerta.INMEDIATA,
            activo=True,
        )
        s.add(perfil)
        s.flush()
        perfil_id = perfil.id
        propio_id = _crear_usuario_normal(s, "propio2@test.cl")
        s.commit()

    r = client.delete(
        f"/api/perfiles/{perfil_id}",
        headers={**_csrf_header(settings, propio_id)},
        cookies=_cookie(settings, propio_id),
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PUT y DELETE persisten (session.commit)
# ---------------------------------------------------------------------------


def test_api_perfil_put_persiste(engine, client, usuario, settings):
    from app.auth.session import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, create_session_token(settings.secret_key, usuario))
    headers = _csrf_header(settings, usuario)

    # Crear
    r = client.post(
        "/api/perfiles",
        json={"nombre": "Original", "keywords": ["tecnología"]},
        headers=headers,
    )
    assert r.status_code == 201
    perfil_id = r.json()["id"]

    # Actualizar
    r = client.put(
        f"/api/perfiles/{perfil_id}",
        json={"nombre": "Actualizado", "keywords": ["software"]},
        headers=headers,
    )
    assert r.status_code == 200
    assert r.json()["nombre"] == "Actualizado"

    # Verificar que persiste en GET
    r = client.get("/api/perfiles", headers=headers)
    assert r.status_code == 200
    nombres = [p["nombre"] for p in r.json()]
    assert "Actualizado" in nombres
    assert "Original" not in nombres


def test_api_perfil_delete_persiste(engine, client, usuario, settings):
    from app.auth.session import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, create_session_token(settings.secret_key, usuario))
    headers = _csrf_header(settings, usuario)

    # Crear
    r = client.post(
        "/api/perfiles",
        json={"nombre": "Para borrar", "keywords": ["aseo"]},
        headers=headers,
    )
    assert r.status_code == 201
    perfil_id = r.json()["id"]

    # Eliminar
    r = client.delete(f"/api/perfiles/{perfil_id}", headers=headers)
    assert r.status_code == 204

    # Verificar que ya no aparece
    r = client.get("/api/perfiles", headers=headers)
    ids = [p["id"] for p in r.json()]
    assert perfil_id not in ids


# ---------------------------------------------------------------------------
# Logout con CSRF inválido
# ---------------------------------------------------------------------------


def test_logout_csrf_invalido(client, usuario, settings):
    from app.auth.session import COOKIE_NAME

    client.cookies.set(COOKIE_NAME, create_session_token(settings.secret_key, usuario))
    r = client.post("/logout", data={"csrf_token": ""}, follow_redirects=False)
    # Debe redirigir a /login con error, sin eliminar la cookie
    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    assert COOKIE_NAME in client.cookies  # cookie sigue presente


# ---------------------------------------------------------------------------
# Lifespan: scheduler se apaga; ping sin auth
# ---------------------------------------------------------------------------


def test_lifespan_scheduler_arranca_y_apaga(engine, settings):
    """El lifespan arranca el BackgroundScheduler y lo apaga al salir."""
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    application = create_app(settings, engine)
    with TestClient(application) as tc:
        # Scheduler debe estar activo dentro del contexto
        assert application.state.scheduler.running
        r = tc.get("/api/salud/ping")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    # Fuera del contexto el scheduler debe haberse detenido
    assert not application.state.scheduler.running


def test_ping_sin_auth_ni_datos(engine, settings):
    """/api/salud/ping responde 200 sin autenticación y sin datos en la BD."""
    from fastapi.testclient import TestClient

    from app.api.main import create_app

    application = create_app(settings, engine)
    with TestClient(application) as tc:
        r = tc.get("/api/salud/ping")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# A1 security fixes
# ---------------------------------------------------------------------------


def test_logger_enmascara_secretos_adicionales(monkeypatch):
    """SMTP_PASSWORD, BREVO_API_KEY, DATABASE_URL y ADMIN_PASSWORD deben quedar como ***."""
    import logging

    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secreto-123")
    monkeypatch.setenv("BREVO_API_KEY", "brevo-key-456")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@host/db")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-pw-789")

    from app.core.logging import _SecretFilter

    f = _SecretFilter()
    f._reload()

    for valor in ("smtp-secreto-123", "brevo-key-456", "postgresql://user:pw@host/db", "admin-pw-789"):
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg=f"credencial: {valor}", args=(), exc_info=None,
        )
        f.filter(record)
        assert "***" in record.msg, f"El secreto '{valor}' no fue enmascarado"
        assert valor not in record.msg, f"El secreto '{valor}' quedó expuesto"


def test_bcrypt_cost_12():
    """hash_password debe generar hashes con coste 12 ($2b$12$)."""
    from app.auth.password import hash_password

    h = hash_password("cualquier-contraseña")
    assert h.startswith("$2b$12$"), f"Coste inesperado en el hash: {h[:10]}"


def test_login_redirect_double_slash(client, usuario):
    """next=//evil.com debe redirigir a / y no a //evil.com."""
    from app.auth.rate_limit import clear_attempts

    clear_attempts("testclient")  # evita contaminación del rate limiter entre tests

    r = client.post(
        "/login",
        data={"email": "user@test.cl", "password": _PW, "next": "//evil.com"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert not location.startswith("//"), f"Open redirect no mitigado: {location}"
    assert location == "/"


def test_security_headers_presentes(client):
    """El middleware debe añadir X-Content-Type-Options, X-Frame-Options y Referrer-Policy."""
    r = client.get("/api/salud/ping")
    assert r.headers.get("x-content-type-options") == "nosniff"
    assert r.headers.get("x-frame-options") == "DENY"
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
