"""Tests F-plan — ruta HTML /plan-anual: render, auth, año inválido, sin datos."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token
from app.core.settings import Settings
from app.models.base import Base
from app.models.enums import EstadoPlanificacionPAC, RolUsuario
from app.models.tables import InstitucionPAC, PlanCompraLinea, PlanCompraSync, SyncState, Usuario

_PW = "contraseña-segura-test"
_FUENTE_INSTITUCIONES = "plan_compra_instituciones"


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


def _marcar_catalogo_instituciones_fresco(engine) -> None:
    """Evita que la ruta dispare sync_instituciones_pac contra la red real:
    deja el catálogo "fresco" según TTL sin necesidad de mockear HTTP."""
    with Session(engine) as s:
        s.add(
            SyncState(
                fuente=_FUENTE_INSTITUCIONES,
                ultima_ejecucion=datetime.now(UTC).replace(tzinfo=None),
                ultimo_ok=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        s.commit()


def _cachear_plan_ok(engine, codigo_entidad: int, agno: int, n_lineas: int = 2) -> None:
    with Session(engine) as s:
        s.add(InstitucionPAC(codigo_entidad=codigo_entidad, razon_social="MINISTERIO PUBLICO", rut="61.935.400-1"))
        s.add(
            PlanCompraSync(
                codigo_entidad=codigo_entidad,
                agno=agno,
                estado="ok",
                n_filas=n_lineas,
                fetched_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        for i in range(n_lineas):
            s.add(
                PlanCompraLinea(
                    codigo_entidad=codigo_entidad,
                    agno=agno,
                    institucion_nombre="MINISTERIO PUBLICO",
                    codigo_producto=str(i),
                    descripcion_producto=f"Compra de prueba {i}",
                    cantidad_estimada=1.0,
                    monto_unitario_clp=1000.0,
                    monto_estimado_clp=1000.0,
                    mes_estimado=3,
                    trimestre_estimado=1,
                    estado_planificacion=EstadoPlanificacionPAC.PUBLICADO.value,
                )
            )
        s.commit()


def _cachear_sin_plan(engine, codigo_entidad: int, agno: int) -> None:
    with Session(engine) as s:
        s.add(InstitucionPAC(codigo_entidad=codigo_entidad, razon_social="GOBERNACION SIN PLAN", rut=""))
        s.add(
            PlanCompraSync(
                codigo_entidad=codigo_entidad,
                agno=agno,
                estado="sin_plan",
                n_filas=0,
                fetched_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_plan_anual_sin_sesion_redirige(client):
    r = client.get("/plan-anual", follow_redirects=False)
    assert r.status_code == 302


# ---------------------------------------------------------------------------
# Render básico
# ---------------------------------------------------------------------------


def test_plan_anual_render_ok_sin_seleccion(client, usuario, settings, engine):
    _marcar_catalogo_instituciones_fresco(engine)
    r = client.get("/plan-anual", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Plan Anual de Compra" in r.text
    assert "Fuente: Dirección ChileCompra" in r.text


def test_plan_anual_busca_institucion_muestra_sugerencias(client, usuario, settings, engine):
    _marcar_catalogo_instituciones_fresco(engine)
    with Session(engine) as s:
        s.add(InstitucionPAC(codigo_entidad=224060, razon_social="MINISTERIO  PUBLICO", rut="61.935.400-1"))
        s.commit()

    r = client.get("/plan-anual?institucion=MINISTERIO", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "MINISTERIO  PUBLICO" in r.text


def test_plan_anual_busqueda_sin_resultados(client, usuario, settings, engine):
    _marcar_catalogo_instituciones_fresco(engine)
    r = client.get("/plan-anual?institucion=NO+EXISTE+ESTO", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "No se encontraron instituciones" in r.text


# ---------------------------------------------------------------------------
# Institución seleccionada
# ---------------------------------------------------------------------------


def test_plan_anual_institucion_seleccionada_muestra_lineas(client, usuario, settings, engine):
    _marcar_catalogo_instituciones_fresco(engine)
    _cachear_plan_ok(engine, 224060, 2026, n_lineas=2)

    r = client.get("/plan-anual?codigo_entidad=224060&agno=2026", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "MINISTERIO PUBLICO" in r.text
    assert "Compra de prueba 0" in r.text
    assert "Compra de prueba 1" in r.text
    assert "2 línea" in r.text


def test_plan_anual_sin_plan_publicado_muestra_mensaje(client, usuario, settings, engine):
    _marcar_catalogo_instituciones_fresco(engine)
    _cachear_sin_plan(engine, 7055, 2024)

    r = client.get("/plan-anual?codigo_entidad=7055&agno=2024", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Sin plan publicado este año" in r.text


def test_plan_anual_agno_invalido_usa_default_sin_error(client, usuario, settings, engine):
    _marcar_catalogo_instituciones_fresco(engine)
    r = client.get("/plan-anual?codigo_entidad=224060&agno=no-es-un-anio", cookies=_cookie(settings, usuario))
    assert r.status_code == 200


def test_plan_anual_codigo_entidad_invalido_no_rompe(client, usuario, settings, engine):
    _marcar_catalogo_instituciones_fresco(engine)
    r = client.get("/plan-anual?codigo_entidad=no-es-un-codigo", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Plan Anual de Compra" in r.text


def test_plan_anual_pagina_fuera_de_rango_se_acota(client, usuario, settings, engine):
    _marcar_catalogo_instituciones_fresco(engine)
    _cachear_plan_ok(engine, 224060, 2026, n_lineas=2)

    r = client.get("/plan-anual?codigo_entidad=224060&agno=2026&pagina=999", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Compra de prueba 0" in r.text
