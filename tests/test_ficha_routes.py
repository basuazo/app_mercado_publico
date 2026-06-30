"""Tests F10 parte 3 — ficha de detalle rediseñada: rubro en ítems y
botones de feedback (me-sirve/descartar/deshacer-descarte) reusando las
rutas creadas en F10 parte 2."""

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
from app.models.base import Base
from app.models.enums import FrecuenciaAlerta, RolUsuario
from app.models.tables import (
    CaProducto,
    CompraAgil,
    Licitacion,
    LicitacionItem,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

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


def _crear_match_lic_con_items(engine, owner_id: int, codigo: str = "LIC-001") -> None:
    with Session(engine) as s:
        lic = Licitacion(codigo=codigo, nombre="Licitación test", descripcion="", estado="publicada")
        s.add(lic)
        s.add(
            LicitacionItem(
                licitacion_codigo=codigo, codigo_producto="43201500", nombre="Notebooks", cantidad=10, unidad="UN"
            )
        )
        s.add(
            LicitacionItem(
                licitacion_codigo=codigo, codigo_producto="", nombre="Servicio sin clasificar", cantidad=1, unidad="UN"
            )
        )
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


def _crear_match_compra_agil(engine, owner_id: int, codigo: str = "CA-001") -> None:
    with Session(engine) as s:
        ca = CompraAgil(codigo=codigo, nombre="Compra ágil test", descripcion="", estado="publicada")
        s.add(ca)
        s.add(CaProducto(ca_codigo=codigo, codigo_producto="", nombre="Producto sin UNSPSC", cantidad=1, unidad="UN"))
        perfil = PerfilBusqueda(
            owner_id=owner_id,
            nombre="Perfil test",
            keywords=["test"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["compras_agiles"],
            frecuencia_alerta=FrecuenciaAlerta.INMEDIATA,
            activo=True,
        )
        s.add(perfil)
        s.flush()
        s.add(
            OportunidadMatch(
                perfil_id=perfil.id, fuente="compras_agiles", codigo_oportunidad=codigo, score=60, razones=[]
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# Rubro en ítems
# ---------------------------------------------------------------------------


def test_ficha_items_muestra_rubro_cuando_hay_codigo_producto(client, usuario, settings, engine):
    _crear_match_lic_con_items(engine, usuario)
    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Notebooks" in r.text
    assert "Componentes para tecnología de la información" in r.text


def test_ficha_items_muestra_guion_cuando_no_hay_codigo_producto(client, usuario, settings, engine):
    _crear_match_lic_con_items(engine, usuario)
    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Servicio sin clasificar" in r.text


def test_ficha_render_compra_agil_no_se_rompe(client, usuario, settings, engine):
    _crear_match_compra_agil(engine, usuario)
    r = client.get("/oportunidad/compras_agiles/CA-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Producto sin UNSPSC" in r.text


# ---------------------------------------------------------------------------
# Botones de feedback en la ficha
# ---------------------------------------------------------------------------


def test_ficha_muestra_botones_me_sirve_y_descartar(client, usuario, settings, engine):
    _crear_match_lic_con_items(engine, usuario)
    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "/oportunidad/licitaciones/LIC-001/me-sirve" in r.text
    assert "/oportunidad/licitaciones/LIC-001/descartar" in r.text


def test_ficha_refleja_me_sirve_activo(client, usuario, settings, engine):
    _crear_match_lic_con_items(engine, usuario)
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/me-sirve", data={}, cookies=cookies, headers=headers)

    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert 'btn btn-success">Me sirve' in r.text


def test_ficha_refleja_descartada_con_boton_restaurar(client, usuario, settings, engine):
    _crear_match_lic_con_items(engine, usuario)
    cookies, headers = _session(settings, usuario)
    client.post("/oportunidad/licitaciones/LIC-001/descartar", data={}, cookies=cookies, headers=headers)

    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Descartada" in r.text
    assert "/oportunidad/licitaciones/LIC-001/deshacer-descarte" in r.text


def test_ficha_me_sirve_via_htmx_origen_ficha_devuelve_partial_no_vacio(client, usuario, settings, engine):
    _crear_match_lic_con_items(engine, usuario)
    cookies, headers = _session(settings, usuario)
    headers_htmx = {**headers, "HX-Request": "true"}
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/me-sirve",
        data={"origen": "ficha"},
        cookies=cookies,
        headers=headers_htmx,
    )
    assert r.status_code == 200
    assert "ficha-acciones-feedback" in r.text
    assert r.text.strip() != ""


def test_ficha_descartar_via_htmx_origen_ficha_no_devuelve_vacio(client, usuario, settings, engine):
    """A diferencia del dashboard, descartar desde la ficha NO debe vaciar la
    respuesta (no tiene sentido ocultar la página completa) — debe reflejar
    el nuevo estado (Descartada + Restaurar)."""
    _crear_match_lic_con_items(engine, usuario)
    cookies, headers = _session(settings, usuario)
    headers_htmx = {**headers, "HX-Request": "true"}
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/descartar",
        data={"origen": "ficha"},
        cookies=cookies,
        headers=headers_htmx,
    )
    assert r.status_code == 200
    assert r.text.strip() != ""
    assert "Descartada" in r.text


def test_dashboard_descartar_via_htmx_sigue_vacio_sin_origen(client, usuario, settings, engine):
    """Regresión: sin `origen` (caso dashboard/default) descartar sigue
    devolviendo 200 vacío para ocultar la tarjeta."""
    _crear_match_lic_con_items(engine, usuario)
    cookies, headers = _session(settings, usuario)
    headers_htmx = {**headers, "HX-Request": "true"}
    r = client.post(
        "/oportunidad/licitaciones/LIC-001/descartar",
        data={},
        cookies=cookies,
        headers=headers_htmx,
    )
    assert r.status_code == 200
    assert r.text == ""
