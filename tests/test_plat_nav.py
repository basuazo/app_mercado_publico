"""F-plat-nav: la plataforma es la portada; la app antigua queda en /archivo, solo admin."""

from __future__ import annotations

import re
from datetime import timedelta

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
from app.core.tiempo import ahora_utc
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import CaLinea, CompraAgil, Empresa, Licitacion, Proveedor, Usuario

_PW = "contraseña-segura-test"


@pytest.fixture()
def engine():
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
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
    return TestClient(create_app(settings, engine))


def _usuario(engine, email, rol=RolUsuario.USUARIO, con_empresa=True) -> int:
    with Session(engine) as s:
        empresa_id = None
        if con_empresa:
            e = Empresa(nombre="Emp " + email, regiones_prioritarias=[16], comunas_prioritarias=[])
            s.add(e)
            s.flush()
            empresa_id = e.id
        u = Usuario(
            email=email,
            password_hash=hash_password(_PW),
            rol=rol,
            activo=True,
            empresa_id=empresa_id,
        )
        s.add(u)
        s.commit()
        return u.id


def _sesion(settings, uid):
    token = create_session_token(settings.secret_key, uid)
    nonce = decode_session_token(settings.secret_key, token)[1]
    csrf = generate_csrf_token(settings.secret_key, nonce)
    return {COOKIE_NAME: token}, {"X-CSRF-Token": csrf}


def _ca(engine, codigo="CA-1", region=16):
    with Session(engine) as s:
        s.add(
            CompraAgil(
                codigo=codigo,
                nombre="Compra",
                estado="publicada",
                region=region,
                fecha_cierre=ahora_utc() + timedelta(days=2),
            )
        )
        s.commit()


def test_portada_con_empresa_muestra_los_tres_conteos(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    _ca(engine, "CA-1")
    _ca(engine, "CA-2")
    _ca(engine, "CA-X", region=5)  # fuera del territorio
    with Session(engine) as s:
        emp = s.get(Usuario, uid).empresa_id
        s.add(Proveedor(empresa_id=emp, nombre="P1"))
        s.add(CaLinea(ca_codigo="CA-1", n=1, texto_origen="x", estado="pendiente"))
        s.commit()
    cookies, _ = _sesion(settings, uid)
    r = client.get("/", cookies=cookies)
    assert r.status_code == 200
    assert 'data-hoy="ca-abiertas">2<' in r.text
    assert 'data-hoy="lineas-por-revisar">1<' in r.text
    assert 'data-hoy="proveedores">1 ' in r.text
    assert "Fuente: Dirección ChileCompra" in r.text


def test_portada_sin_empresa_muestra_aviso(client, engine, settings):
    uid = _usuario(engine, "u@t.cl", con_empresa=False)
    cookies, _ = _sesion(settings, uid)
    r = client.get("/", cookies=cookies)
    assert r.status_code == 200
    assert "todavía no tiene una empresa" in r.text
    assert "data-hoy=" not in r.text


def test_portada_admin_sin_empresa_muestra_hoy_con_aviso(client, engine, settings):
    uid = _usuario(engine, "a@t.cl", RolUsuario.ADMIN, con_empresa=False)
    cookies, _ = _sesion(settings, uid)
    r = client.get("/", cookies=cookies)
    assert r.status_code == 200
    assert "CA vigentes" in r.text
    assert "todavía no tiene una empresa" in r.text


def test_archivo_para_admin_es_el_feed(client, engine, settings):
    uid = _usuario(engine, "a@t.cl", RolUsuario.ADMIN, con_empresa=False)
    cookies, _ = _sesion(settings, uid)
    r = client.get("/archivo", cookies=cookies)
    assert r.status_code == 200
    assert "panel-filtros" in r.text


RUTAS_GET = [
    "/archivo",
    "/descartadas",
    "/seguidas",
    "/registro",
    "/perfiles",
    "/perfiles/nuevo/form",
    "/rubros-favoritos/form",
    "/plan-anual",
    "/plan-anual/conteo",
    "/plan-anual/export.csv",
    "/compras-agiles",
    "/compras-agiles/conteo",
    "/oportunidad/compras_agiles/CA-1/descartar-opciones",
    "/oportunidad/compras_agiles/CA-1/excluir-vista-previa",
]
RUTAS_POST = [
    "/oportunidad/compras_agiles/CA-1/guardar",
    "/oportunidad/compras_agiles/CA-1/descartar",
    "/oportunidad/compras_agiles/CA-1/seguir",
    "/oportunidad/compras_agiles/CA-1/archivar",
    "/oportunidad/compras_agiles/CA-1/descartar-y-excluir",
    "/perfiles/nuevo",
    "/perfiles/1/eliminar",
    "/perfiles/1/editar",
    "/perfiles/1/activo",
    "/rubros-favoritos/agregar",
    "/rubros-favoritos/quitar",
]


@pytest.mark.parametrize("ruta", RUTAS_GET)
def test_usuario_comun_es_redirigido_en_rutas_antiguas(client, engine, settings, ruta):
    uid = _usuario(engine, "u@t.cl")
    cookies, _ = _sesion(settings, uid)
    r = client.get(ruta, cookies=cookies, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


@pytest.mark.parametrize("ruta", RUTAS_POST)
def test_usuario_comun_recibe_403_en_acciones_antiguas(client, engine, settings, ruta):
    uid = _usuario(engine, "u@t.cl")
    cookies, headers = _sesion(settings, uid)
    r = client.post(ruta, cookies=cookies, headers=headers, follow_redirects=False)
    assert r.status_code == 403


def test_catalogo_organismos_json_solo_admin(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    cookies, _ = _sesion(settings, uid)
    assert client.get("/organismos/catalogo.json", cookies=cookies).status_code == 403


def test_ficha_ca_abierta_sin_acciones_para_usuario_con_empresa(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    _ca(engine, "CA-1")
    cookies, _ = _sesion(settings, uid)
    r = client.get("/oportunidad/compras_agiles/CA-1", cookies=cookies, follow_redirects=False)
    assert r.status_code == 200
    assert "/guardar" not in r.text
    assert "data-accion=\"descartar\"" not in r.text


def test_ficha_ca_para_admin_conserva_acciones(client, engine, settings):
    uid = _usuario(engine, "a@t.cl", RolUsuario.ADMIN)
    _ca(engine, "CA-1")
    cookies, _ = _sesion(settings, uid)
    r = client.get("/oportunidad/compras_agiles/CA-1", cookies=cookies)
    assert r.status_code == 200
    assert "/oportunidad/compras_agiles/CA-1/guardar" in r.text


def test_ficha_ca_sin_empresa_redirige(client, engine, settings):
    uid = _usuario(engine, "u@t.cl", con_empresa=False)
    _ca(engine, "CA-1")
    cookies, _ = _sesion(settings, uid)
    r = client.get("/oportunidad/compras_agiles/CA-1", cookies=cookies, follow_redirects=False)
    assert r.status_code == 303


def test_ficha_licitacion_solo_admin(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    with Session(engine) as s:
        s.add(Licitacion(codigo="LIC-1", nombre="L", estado="publicada"))
        s.commit()
    cookies, _ = _sesion(settings, uid)
    r = client.get("/oportunidad/licitaciones/LIC-1", cookies=cookies, follow_redirects=False)
    assert r.status_code == 303


def test_post_de_cuenta_sigue_abierto(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    cookies, headers = _sesion(settings, uid)
    r = client.post(
        "/cuenta/resumen",
        data={"dias_resumen": "3"},
        cookies=cookies,
        headers=headers,
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/cuenta")


def _hrefs(html: str) -> set[str]:
    return set(re.findall(r'href="([^"]+)"', html.split("</aside>")[0]))


def test_menu_usuario_con_empresa(client, engine, settings):
    uid = _usuario(engine, "u@t.cl")
    cookies, _ = _sesion(settings, uid)
    nav = _hrefs(client.get("/cuenta", cookies=cookies).text)
    assert {"/", "/catalogo", "/proveedores", "/inventario", "/proveedores/logistica", "/cuenta"} <= nav
    assert not nav & {"/archivo", "/perfiles", "/registro", "/plan-anual", "/admin/empresas", "/salud"}


def test_menu_admin_incluye_archivo(client, engine, settings):
    uid = _usuario(engine, "a@t.cl", RolUsuario.ADMIN)
    cookies, _ = _sesion(settings, uid)
    nav = _hrefs(client.get("/cuenta", cookies=cookies).text)
    esperado = {
        "/archivo", "/perfiles", "/registro", "/compras-agiles", "/plan-anual",
        "/admin/empresas", "/admin/usuarios", "/salud",
    }
    assert esperado <= nav


def test_menu_sin_empresa_no_ofrece_plataforma(client, engine, settings):
    uid = _usuario(engine, "u@t.cl", con_empresa=False)
    cookies, _ = _sesion(settings, uid)
    nav = _hrefs(client.get("/cuenta", cookies=cookies).text)
    assert "/catalogo" not in nav and "/proveedores" not in nav and "/archivo" not in nav
    assert "/inventario" not in nav
