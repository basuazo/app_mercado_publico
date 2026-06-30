"""Tests F-competencia — vista en la ficha (resumen/detalle) y RUT propio en /perfiles."""

from __future__ import annotations

from datetime import UTC, datetime

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
from app.models.enums import EstadoOportunidad, FrecuenciaAlerta, RolUsuario
from app.models.tables import (
    Licitacion,
    OfertaCompetencia,
    OportunidadMatch,
    PerfilBusqueda,
    SyncState,
    Usuario,
)

_PW = "contraseña-segura-test"
_FUENTE_INSTITUCIONES = "plan_compra_instituciones"
_FUENTE_SECTORES = "plan_compra_sectores"


def _marcar_catalogo_organismos_fresco(engine) -> None:
    """Evita que GET /perfiles (incl. vía redirect tras POST) dispare
    sync_instituciones_pac/sync_sectores_organismos contra la red real (F10)."""
    ahora = datetime.now(UTC).replace(tzinfo=None)
    with Session(engine) as s:
        s.add(SyncState(fuente=_FUENTE_INSTITUCIONES, ultima_ejecucion=ahora, ultimo_ok=ahora))
        s.add(SyncState(fuente=_FUENTE_SECTORES, ultima_ejecucion=ahora, ultimo_ok=ahora))
        s.commit()


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


def _crear_lic_con_match(
    engine, owner_id: int, codigo: str = "LIC-001", estado: str = EstadoOportunidad.ADJUDICADA.value
) -> None:
    with Session(engine) as s:
        s.add(Licitacion(codigo=codigo, nombre="Lic test", descripcion="", estado=estado))
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


def _agregar_ofertas(engine, codigo: str = "LIC-001") -> None:
    with Session(engine) as s:
        s.add_all(
            [
                OfertaCompetencia(
                    licitacion_codigo=codigo,
                    codigo_item="ITEM-1",
                    rut_proveedor="1-9",
                    nombre_proveedor="Prov Perdedor",
                    monto_unitario=100,
                    monto_linea_adjudicada=0,
                    cantidad=0,
                    seleccionada=False,
                ),
                OfertaCompetencia(
                    licitacion_codigo=codigo,
                    codigo_item="ITEM-1",
                    rut_proveedor="2-7",
                    nombre_proveedor="Prov Ganador",
                    monto_unitario=90,
                    monto_linea_adjudicada=90,
                    cantidad=1,
                    seleccionada=True,
                ),
            ]
        )
        s.commit()


# ---------------------------------------------------------------------------
# Ficha: sección de análisis de competencia
# ---------------------------------------------------------------------------


def test_ficha_sin_ofertas_no_muestra_seccion(client, usuario, settings, engine):
    _crear_lic_con_match(engine, usuario)
    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Análisis de competencia" not in r.text


def test_ficha_con_ofertas_muestra_resumen_y_detalle(client, usuario, settings, engine):
    _crear_lic_con_match(engine, usuario)
    _agregar_ofertas(engine)
    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Análisis de competencia" in r.text
    assert "Adjudicatario" in r.text
    assert "Prov Ganador" in r.text
    # El detalle por ítem (sección opcional) sí incluye también a los no ganadores.
    assert "Ver detalle por ítem" in r.text
    assert "Prov Perdedor" in r.text


def test_ficha_no_adjudicada_no_muestra_seccion_aunque_haya_ofertas(client, usuario, settings, engine):
    _crear_lic_con_match(engine, usuario, "LIC-002", estado=EstadoOportunidad.PUBLICADA.value)
    # No debería ocurrir en la práctica (capturar_competencia exige adjudicada),
    # pero la vista debe ser defensiva igual.
    _agregar_ofertas(engine, "LIC-002")
    r = client.get("/oportunidad/licitaciones/LIC-002", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "Análisis de competencia" not in r.text


def test_ficha_resalta_rut_propio(client, usuario, settings, engine):
    _crear_lic_con_match(engine, usuario)
    _agregar_ofertas(engine)
    with Session(engine) as s:
        u = s.get(Usuario, usuario)
        assert u is not None
        u.rut_proveedor = "2-7"
        s.commit()

    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "table-success" in r.text


def test_ficha_sin_rut_propio_no_resalta(client, usuario, settings, engine):
    _crear_lic_con_match(engine, usuario)
    _agregar_ofertas(engine)
    r = client.get("/oportunidad/licitaciones/LIC-001", cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    assert "table-success" not in r.text


# ---------------------------------------------------------------------------
# /perfiles/rut-proveedor
# ---------------------------------------------------------------------------


def test_guardar_rut_proveedor(client, usuario, settings, engine):
    _marcar_catalogo_organismos_fresco(engine)
    cookies, headers = _session(settings, usuario)
    r = client.post(
        "/perfiles/rut-proveedor",
        data={"rut_proveedor": "76.123.456-7"},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303

    r2 = client.get("/perfiles", cookies=cookies)
    assert "76.123.456-7" in r2.text


def test_guardar_rut_proveedor_vacio_limpia_el_campo(client, usuario, settings, engine):
    _marcar_catalogo_organismos_fresco(engine)
    cookies, headers = _session(settings, usuario)
    client.post("/perfiles/rut-proveedor", data={"rut_proveedor": "76.123.456-7"}, cookies=cookies, headers=headers)
    client.post("/perfiles/rut-proveedor", data={"rut_proveedor": "   "}, cookies=cookies, headers=headers)

    with Session(engine) as s:
        u = s.get(Usuario, usuario)
        assert u is not None
        assert u.rut_proveedor is None


def test_guardar_rut_proveedor_sin_csrf_403(client, usuario, settings):
    r = client.post(
        "/perfiles/rut-proveedor",
        data={"rut_proveedor": "x", "csrf_token": "invalido"},
        cookies=_cookie(settings, usuario),
    )
    assert r.status_code == 403
