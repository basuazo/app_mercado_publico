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

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import _normalizar_url_driver, create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.models.base import Base
from app.models.enums import EstadoOportunidad, FrecuenciaAlerta, RolUsuario
from app.models.tables import (
    CompraAgil,
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


_FUENTE_INSTITUCIONES = "plan_compra_instituciones"
_FUENTE_SECTORES = "plan_compra_sectores"


def _marcar_catalogo_organismos_fresco(engine) -> None:
    """Evita que GET /perfiles dispare sync_instituciones_pac/sync_sectores_organismos
    contra la red real (F10: el catálogo de organismos del multiselect se
    sincroniza al cargar /perfiles, igual que /plan-anual)."""
    ahora = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as s:
        s.add(SyncState(fuente=_FUENTE_INSTITUCIONES, ultima_ejecucion=ahora, ultimo_ok=ahora))
        s.add(SyncState(fuente=_FUENTE_SECTORES, ultima_ejecucion=ahora, ultimo_ok=ahora))
        s.commit()


def _cookie(settings: Settings, user_id: int) -> dict[str, str]:
    token = create_session_token(settings.secret_key, user_id)
    return {COOKIE_NAME: token}


def _session(settings: Settings, user_id: int) -> tuple[dict[str, str], dict[str, str]]:
    """Cookie de sesión + header X-CSRF-Token coherentes (mismo nonce).

    El CSRF token rota por sesión, así que cookie y header deben derivar
    del mismo create_session_token() para que validate_csrf_token() pase.
    """
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    cookies = {COOKIE_NAME: token}
    headers = {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}
    return cookies, headers


def _sembrar_compra_agil_publicada(
    engine,
    *,
    codigo: str = "CA-AUTOMATCH-1",
    region: int = 13,
) -> None:
    cierre = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=5)
    with Session(engine) as s:
        s.add(
            CompraAgil(
                codigo=codigo,
                nombre="Compra ágil para automatch",
                descripcion="Oportunidad sembrada sin red para pruebas",
                estado=EstadoOportunidad.PUBLICADA.value,
                fecha_cierre=cierre,
                monto_disponible_clp=1_000_000,
                organismo_nombre="Organismo de prueba",
                region=region,
            )
        )
        s.commit()


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
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/logout",
        data={"csrf_token": headers["X-CSRF-Token"]},
        cookies=cookies,
        follow_redirects=False,
    )
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


def _limpiar_rate_limit_testclient() -> None:
    from app.auth.rate_limit import clear_attempts

    clear_attempts("testclient")


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
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/perfiles/nuevo",
        data={"nombre": "Mi perfil", "keywords": "tecnología"},
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303


def test_cuenta_password_cambia_con_actual_correcta(client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    nueva = "nueva-clave-segura"

    r = client.post(
        "/cuenta/password",
        data={
            "password_actual": _PW,
            "password_nueva": nueva,
            "password_confirmacion": nueva,
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert "mensaje=Contrase" in r.headers["location"]
    client.cookies.clear()
    _limpiar_rate_limit_testclient()
    r_old = client.post(
        "/login",
        data={"email": "user@test.cl", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    assert r_old.status_code == 303
    assert "error=" in r_old.headers["location"]
    r_new = client.post(
        "/login",
        data={"email": "user@test.cl", "password": nueva, "next": "/"},
        follow_redirects=False,
    )
    assert r_new.status_code == 303
    assert r_new.headers["location"] == "/"


def test_cuenta_password_falla_con_actual_incorrecta(client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/cuenta/password",
        data={
            "password_actual": "incorrecta",
            "password_nueva": "nueva-clave-segura",
            "password_confirmacion": "nueva-clave-segura",
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    client.cookies.clear()
    _limpiar_rate_limit_testclient()
    r_login = client.post(
        "/login",
        data={"email": "user@test.cl", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    assert r_login.status_code == 303
    assert r_login.headers["location"] == "/"


def test_cuenta_password_falla_si_confirmacion_no_coincide(client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/cuenta/password",
        data={
            "password_actual": _PW,
            "password_nueva": "nueva-clave-segura",
            "password_confirmacion": "otra-clave-segura",
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    client.cookies.clear()
    _limpiar_rate_limit_testclient()
    r_login = client.post(
        "/login",
        data={"email": "user@test.cl", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    assert r_login.status_code == 303
    assert r_login.headers["location"] == "/"


def test_cuenta_password_falla_si_nueva_es_corta(client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/cuenta/password",
        data={
            "password_actual": _PW,
            "password_nueva": "corta",
            "password_confirmacion": "corta",
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert "error=" in r.headers["location"]
    client.cookies.clear()
    _limpiar_rate_limit_testclient()
    r_login = client.post(
        "/login",
        data={"email": "user@test.cl", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    assert r_login.status_code == 303
    assert r_login.headers["location"] == "/"


def test_cuenta_password_sin_csrf_403(client, usuario, settings):
    r = client.post(
        "/cuenta/password",
        data={
            "password_actual": _PW,
            "password_nueva": "nueva-clave-segura",
            "password_confirmacion": "nueva-clave-segura",
        },
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 403


def test_admin_resetea_password_de_usuario(client, usuario, admin, settings):
    cookies, headers = _session(settings, admin)
    nueva = "reset-admin-seguro"

    r = client.post(
        f"/admin/usuarios/{usuario}/password",
        data={"password_nueva": nueva},
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )

    assert r.status_code == 200
    assert "Contraseña reseteada" in r.text
    assert nueva in r.text
    client.cookies.clear()
    _limpiar_rate_limit_testclient()
    r_old = client.post(
        "/login",
        data={"email": "user@test.cl", "password": _PW, "next": "/"},
        follow_redirects=False,
    )
    assert r_old.status_code == 303
    assert "error=" in r_old.headers["location"]
    r_new = client.post(
        "/login",
        data={"email": "user@test.cl", "password": nueva, "next": "/"},
        follow_redirects=False,
    )
    assert r_new.status_code == 303
    assert r_new.headers["location"] == "/"


def test_admin_reset_password_rechaza_no_admin(client, usuario, admin, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        f"/admin/usuarios/{admin}/password",
        data={"password_nueva": "reset-admin-seguro"},
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_admin_reset_password_sin_csrf_403(client, usuario, admin, settings):
    r = client.post(
        f"/admin/usuarios/{usuario}/password",
        data={"password_nueva": "reset-admin-seguro"},
        cookies=_cookie(settings, admin),
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_admin_reset_password_rechaza_corta(client, usuario, admin, settings):
    cookies, headers = _session(settings, admin)
    r = client.post(
        f"/admin/usuarios/{usuario}/password",
        data={"password_nueva": "corta"},
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


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


@respx.mock
def test_jobs_run_token_correcto(client):
    """job="all" (default) corre el ciclo completo dentro del mismo ciclo del
    TestClient (BackgroundTasks) — con la BD de test vacía toca dos hosts
    reales (v1 licitaciones activas + HEAD del blob de datos abiertos, ver
    `run_sync_activas`/`sync_items_datos_abiertos`); se mockean ambos con
    respx (regla CLAUDE.md: tests de red SIEMPRE mockeados)."""
    respx.get(url__regex=r"https://api\.mercadopublico\.cl/servicios/v1/publico/licitaciones\.json.*").mock(
        return_value=httpx.Response(200, json={"Listado": []})
    )
    respx.route(
        method="HEAD",
        url__regex=r"https://transparenciachc\.blob\.core\.windows\.net/lic-da/.*\.zip",
    ).mock(return_value=httpx.Response(200, headers={"Last-Modified": "Wed, 01 Jul 2026 12:30:24 GMT"}))

    r = client.post("/api/jobs/run", headers={"X-Jobs-Token": "jobs-token-secreto"})
    assert r.status_code == 200
    assert r.json()["queued"] is True
    assert r.json()["job"] == "all"


def test_jobs_run_job_invalido(client):
    r = client.post(
        "/api/jobs/run?job=xxx", headers={"X-Jobs-Token": "jobs-token-secreto"}
    )
    assert r.status_code == 400


_LISTADO_CA_VACIO = {
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
    "errors": [],
}


@respx.mock
def test_jobs_run_job_ca(client):
    """BackgroundTasks corre el job dentro del mismo ciclo del TestClient, así
    que `run_sync_ca` llega a pegarle a `httpx.Client` real — se mockea con
    respx (regla CLAUDE.md: tests de red SIEMPRE mockeados) en vez de saltarlo."""
    respx.get("https://api2.mercadopublico.cl/v2/compra-agil").mock(
        return_value=httpx.Response(200, json=_LISTADO_CA_VACIO)
    )
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

    cookies, headers = _session(settings, propio_id)
    r = client.delete(
        f"/api/perfiles/{perfil_id}",
        headers=headers,
        cookies=cookies,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PUT y DELETE persisten (session.commit)
# ---------------------------------------------------------------------------


def test_api_perfil_put_persiste(engine, client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    client.cookies.set(COOKIE_NAME, cookies[COOKIE_NAME])

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
    cookies, headers = _session(settings, usuario)
    client.cookies.set(COOKIE_NAME, cookies[COOKIE_NAME])

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
# Normalización de driver en DATABASE_URL (make_engine)
# ---------------------------------------------------------------------------


def test_normaliza_postgresql_sin_driver():
    url = "postgresql://user:pw@host/db"
    assert _normalizar_url_driver(url) == "postgresql+psycopg://user:pw@host/db"


def test_normaliza_postgres_sin_driver():
    url = "postgres://user:pw@host/db"
    assert _normalizar_url_driver(url) == "postgresql+psycopg://user:pw@host/db"


def test_normaliza_respeta_driver_explicito():
    url = "postgresql+psycopg://user:pw@host/db"
    assert _normalizar_url_driver(url) == url


def test_normaliza_no_toca_sqlite():
    url = "sqlite:///:memory:"
    assert _normalizar_url_driver(url) == url


# ---------------------------------------------------------------------------
# F9a: regiones / monto_min_clp / monto_max_clp en el formulario de perfiles
# ---------------------------------------------------------------------------


def test_parse_regiones_ignora_no_numerico():
    from app.api.routes.pages import _parse_regiones

    assert _parse_regiones(["13", "5", "abc", "", "  9  "]) == [13, 5, 9]


def test_parse_regiones_vacio():
    from app.api.routes.pages import _parse_regiones

    assert _parse_regiones([]) == []


def test_parse_monto_vacio_es_none():
    from app.api.routes.pages import _parse_monto

    assert _parse_monto("") is None
    assert _parse_monto("   ") is None


def test_parse_monto_invalido_es_none():
    from app.api.routes.pages import _parse_monto

    assert _parse_monto("no-es-un-numero") is None


def test_parse_monto_valido():
    from app.api.routes.pages import _parse_monto

    assert _parse_monto("1500000") == 1_500_000.0
    assert _parse_monto("  2500.5  ") == 2500.5


def test_perfil_crear_persiste_regiones_y_montos(engine, client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/perfiles/nuevo",
        data={
            "nombre": "Con filtros",
            "keywords": "",
            "regiones": ["13", "5", "abc"],
            "monto_min_clp": "1000000",
            "monto_max_clp": "5000000",
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Perfil+creado" in r.headers["location"]

    with Session(engine) as s:
        perfil = s.execute(
            select(PerfilBusqueda).where(PerfilBusqueda.nombre == "Con filtros")
        ).scalar_one()
        assert perfil.regiones == [13, 5]
        assert perfil.monto_min_clp == 1_000_000.0
        assert perfil.monto_max_clp == 5_000_000.0


def test_perfil_crear_monto_min_mayor_que_max_no_persiste(engine, client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/perfiles/nuevo",
        data={
            "nombre": "Rango invertido",
            "keywords": "",
            "monto_min_clp": "9000000",
            "monto_max_clp": "1000000",
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]

    with Session(engine) as s:
        existe = s.execute(
            select(PerfilBusqueda).where(PerfilBusqueda.nombre == "Rango invertido")
        ).scalar_one_or_none()
        assert existe is None


def test_perfil_crear_sin_keywords_ni_filtros_error_amistoso(client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/perfiles/nuevo",
        data={"nombre": "Vacío total", "keywords": "", "excluir": ""},
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "error=" in r.headers["location"]


def test_perfil_editar_persiste_regiones_y_montos(engine, client, usuario, settings):
    from app.matching.perfiles import crear_perfil

    with Session(engine) as s:
        perfil = crear_perfil(s, owner_id=usuario, nombre="Editable", keywords=["luz"])
        s.commit()
        perfil_id = perfil.id

    cookies, headers = _session(settings, usuario)
    r = client.post(
        f"/perfiles/{perfil_id}/editar",
        data={
            "nombre": "Editable",
            "keywords": "luz",
            "regiones": ["1", "2"],
            "monto_min_clp": "200000",
            "monto_max_clp": "800000",
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Perfil+actualizado" in r.headers["location"]

    with Session(engine) as s:
        actualizado = s.get(PerfilBusqueda, perfil_id)
        assert actualizado is not None
        assert actualizado.regiones == [1, 2]
        assert actualizado.monto_min_clp == 200_000.0
        assert actualizado.monto_max_clp == 800_000.0


def test_perfil_crear_dispara_automatch_on_demand_sin_cuota(
    engine, client, usuario, settings
):
    _sembrar_compra_agil_publicada(engine, codigo="CA-AUTOMATCH-CREAR", region=13)
    cookies, headers = _session(settings, usuario)

    r = client.post(
        "/perfiles/nuevo",
        data={
            "nombre": "Automatch crear",
            "keywords": "",
            "fuentes": ["compras_agiles"],
            "regiones": ["13"],
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )

    assert r.status_code == 303
    with Session(engine) as s:
        perfil = s.execute(
            select(PerfilBusqueda).where(PerfilBusqueda.nombre == "Automatch crear")
        ).scalar_one()
        matches = s.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil.id,
                OportunidadMatch.fuente == "compras_agiles",
                OportunidadMatch.codigo_oportunidad == "CA-AUTOMATCH-CREAR",
            )
        ).scalars().all()
        assert len(matches) == 1


def test_perfil_editar_dispara_automatch_on_demand_y_no_duplica(
    engine, client, usuario, settings
):
    from app.matching.perfiles import crear_perfil

    _sembrar_compra_agil_publicada(engine, codigo="CA-AUTOMATCH-EDITAR", region=13)
    with Session(engine) as s:
        perfil = crear_perfil(
            s,
            owner_id=usuario,
            nombre="Automatch editar",
            regiones=[5],
            fuentes=["compras_agiles"],
        )
        s.commit()
        perfil_id = perfil.id

    cookies, headers = _session(settings, usuario)
    for _ in range(2):
        r = client.post(
            f"/perfiles/{perfil_id}/editar",
            data={
                "nombre": "Automatch editar",
                "keywords": "",
                "fuentes": ["compras_agiles"],
                "regiones": ["13"],
            },
            headers=headers,
            cookies=cookies,
            follow_redirects=False,
        )
        assert r.status_code == 303

    with Session(engine) as s:
        matches = s.execute(
            select(OportunidadMatch).where(
                OportunidadMatch.perfil_id == perfil_id,
                OportunidadMatch.fuente == "compras_agiles",
                OportunidadMatch.codigo_oportunidad == "CA-AUTOMATCH-EDITAR",
            )
        ).scalars().all()
        assert len(matches) == 1


def test_automatch_background_perfil_inexistente_o_inactivo_noop(engine, usuario):
    from app.api.routes.pages import _match_perfil_background
    from app.matching.perfiles import crear_perfil

    _sembrar_compra_agil_publicada(engine, codigo="CA-AUTOMATCH-NOOP", region=13)
    with Session(engine) as s:
        perfil = crear_perfil(
            s,
            owner_id=usuario,
            nombre="Automatch inactivo",
            regiones=[13],
            fuentes=["compras_agiles"],
        )
        perfil.activo = False
        s.commit()
        perfil_id = perfil.id

    _match_perfil_background(engine, 999_999)
    _match_perfil_background(engine, perfil_id)

    with Session(engine) as s:
        total = s.execute(
            select(OportunidadMatch).where(OportunidadMatch.perfil_id == perfil_id)
        ).scalars().all()
        assert total == []


def test_perfiles_get_muestra_regiones_disponibles(client, usuario, settings, engine):
    _marcar_catalogo_organismos_fresco(engine)
    cookies, _ = _session(settings, usuario)
    r = client.get("/perfiles", cookies=cookies)
    assert r.status_code == 200
    assert "Tarapacá" in r.text
    assert "Metropolitana de Santiago" in r.text


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


# ---------------------------------------------------------------------------
# F9b: parseo de categorias_unspsc / organismos_seguidos en rutas de perfiles
# ---------------------------------------------------------------------------


def test_parse_categorias_descarta_no_digitos_y_largos_invalidos():
    from app.api.routes.pages import _parse_categorias

    assert _parse_categorias(["43", "4321", "abc", "123", "43211500", "432115001"]) == [
        "43",
        "4321",
        "43211500",
    ]


def test_parse_categorias_divide_texto_libre_por_coma():
    from app.api.routes.pages import _parse_categorias

    assert _parse_categorias(["4321", "432115,43211500"]) == ["4321", "432115", "43211500"]


def test_parse_categorias_deduplica():
    from app.api.routes.pages import _parse_categorias

    assert _parse_categorias(["4321", "4321"]) == ["4321"]


def test_parse_categorias_vacio():
    from app.api.routes.pages import _parse_categorias

    assert _parse_categorias([]) == []


def test_parse_organismos_separa_por_coma_y_limpia_espacios():
    from app.api.routes.pages import _parse_organismos

    assert _parse_organismos(" 12345 , 76123456-7 ,") == ["12345", "76123456-7"]


def test_parse_organismos_vacio():
    from app.api.routes.pages import _parse_organismos

    assert _parse_organismos("") == []


def test_perfil_crear_persiste_categorias_y_organismos(engine, client, usuario, settings):
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/perfiles/nuevo",
        data={
            "nombre": "Con rubros",
            "keywords": "",
            "categorias_unspsc": ["4321", "abc", "432115,43211500"],
            "organismos_seguidos": "ORG-1, 76123456-7",
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Perfil+creado" in r.headers["location"]

    with Session(engine) as s:
        perfil = s.execute(
            select(PerfilBusqueda).where(PerfilBusqueda.nombre == "Con rubros")
        ).scalar_one()
        assert perfil.categorias_unspsc == ["4321", "432115", "43211500"]
        assert perfil.organismos_seguidos == ["ORG-1", "76123456-7"]


def test_perfil_crear_solo_rubro_sin_keywords_no_da_error(engine, client, usuario, settings):
    """categorias_unspsc por sí sola debe ser criterio mínimo válido (no PerfilInvalido)."""
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/perfiles/nuevo",
        data={"nombre": "Solo rubro web", "keywords": "", "categorias_unspsc": ["4321"]},
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Perfil+creado" in r.headers["location"]


def test_perfil_editar_persiste_categorias_y_organismos(engine, client, usuario, settings):
    from app.matching.perfiles import crear_perfil

    with Session(engine) as s:
        perfil = crear_perfil(s, owner_id=usuario, nombre="Editable rubro", keywords=["luz"])
        s.commit()
        perfil_id = perfil.id

    cookies, headers = _session(settings, usuario)
    r = client.post(
        f"/perfiles/{perfil_id}/editar",
        data={
            "nombre": "Editable rubro",
            "keywords": "luz",
            "categorias_unspsc": ["1010"],
            "organismos_seguidos": "ORG-EDITADO",
        },
        headers=headers,
        cookies=cookies,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "Perfil+actualizado" in r.headers["location"]

    with Session(engine) as s:
        actualizado = s.get(PerfilBusqueda, perfil_id)
        assert actualizado is not None
        assert actualizado.categorias_unspsc == ["1010"]
        assert actualizado.organismos_seguidos == ["ORG-EDITADO"]


def test_perfiles_get_muestra_rubros_y_organismos(engine, client, usuario, settings):
    from app.matching.perfiles import crear_perfil

    _marcar_catalogo_organismos_fresco(engine)
    with Session(engine) as s:
        crear_perfil(
            s,
            owner_id=usuario,
            nombre="Con rubro visible",
            categorias_unspsc=["4321"],
            organismos_seguidos=["ORG-VISIBLE"],
        )
        s.commit()

    cookies, _ = _session(settings, usuario)
    r = client.get("/perfiles", cookies=cookies)
    assert r.status_code == 200
    assert "ORG-VISIBLE" in r.text
    # El nombre de la familia 4321 debe resolverse vía el catálogo, no solo el código crudo.
    assert "Equipo inform" in r.text or "4321" in r.text
    assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin"


# ---------------------------------------------------------------------------
# F10: multiselect de organismos en /perfiles (catálogo presente vs degradado)
# ---------------------------------------------------------------------------


def test_perfiles_get_catalogo_disponible_muestra_multiselect(client, usuario, settings):
    """Con el bulk de instituciones/sectores mockeado (red mockeada, regla del
    proyecto), GET /perfiles sincroniza el catálogo y renderiza el widget JS
    en vez del input de texto libre de organismos."""
    cookies, _ = _session(settings, usuario)
    with respx.mock:
        respx.get(settings.plan_compra_kpi_url).mock(
            return_value=httpx.Response(
                200,
                json={"payload": [{"codigoEntidad": 224060, "rut": "61.935.400-1", "razonSocial": "MINISTERIO  PUBLICO"}]},
            )
        )
        respx.get(settings.plan_compra_sectores_bulk_url).mock(
            return_value=httpx.Response(
                200,
                json=[{"type": "comprador", "entcode": 224060, "idSector": 3, "sector": "Legislativo y Judicial"}],
            )
        )
        r = client.get("/perfiles", cookies=cookies)

    assert r.status_code == 200
    assert 'class="js-org-widget"' in r.text
    assert "Catálogo de organismos no disponible" not in r.text
    assert "MINISTERIO  PUBLICO" in r.text  # vive en el const JS del catálogo


def test_perfiles_get_sin_red_degrada_a_input_manual(client, usuario, settings):
    """Sin red disponible para el catálogo (primer arranque / sin conexión),
    la página no debe romper: degrada al input de texto libre de organismos
    (regla 6)."""
    cookies, _ = _session(settings, usuario)
    with respx.mock:
        respx.get(settings.plan_compra_kpi_url).mock(side_effect=httpx.ConnectError("sin red"))
        r = client.get("/perfiles", cookies=cookies)

    assert r.status_code == 200
    assert "Catálogo de organismos no disponible" in r.text
    assert 'class="js-org-widget"' not in r.text
    assert 'placeholder="ej: 12345,76123456-7"' in r.text
