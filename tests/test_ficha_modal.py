"""Tests F-ficha-modal — la ficha como parcial (HX-Request) y modal del feed y de Mi registro.

SQLite en memoria: nada acá depende de Postgres.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.api.presentacion import cierre_vencido, presentacion_estado_con_cierre
from app.api.routes.pages import _url_feed
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.matching.seguimiento import guardar
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaProducto,
    CompraAgil,
    Licitacion,
    LicitacionItem,
    OfertaCompetencia,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

_PW = "contraseña-segura-test"
_HX = {"HX-Request": "true"}


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401

    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    return e


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


def _usuario(engine, email: str = "ficha-a@test.cl") -> int:
    with Session(engine) as s:
        u = Usuario(email=email, password_hash=hash_password(_PW), rol=RolUsuario.USUARIO, activo=True)
        s.add(u)
        s.commit()
        return u.id


def _cliente(engine, settings: Settings, user_id: int) -> tuple[TestClient, dict[str, str]]:
    client = TestClient(create_app(settings, engine))
    token = create_session_token(settings.secret_key, user_id)
    client.cookies.set(COOKIE_NAME, token)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    _, nonce = decoded
    return client, {"X-CSRF-Token": generate_csrf_token(settings.secret_key, nonce)}


def _perfil(s: Session, owner_id: int) -> int:
    p = PerfilBusqueda(
        owner_id=owner_id,
        nombre="Perfil ficha",
        keywords=["test"],
        keywords_excluir=[],
        regiones=[],
        fuentes=["licitaciones", "compras_agiles"],
        activo=True,
    )
    s.add(p)
    s.flush()
    return p.id


def _lic(
    engine, user_id: int | None, codigo: str = "LIC-M1", *, con_match: bool = True, **campos: Any
) -> None:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": f"Licitación {codigo}",
        "descripcion": "",
        "estado": "publicada",
        "fecha_cierre": ahora_utc() + timedelta(days=5),
    }
    base.update(campos)
    with Session(engine) as s:
        s.add(Licitacion(**base))
        s.flush()
        if user_id is not None and con_match:
            s.add(
                OportunidadMatch(
                    perfil_id=_perfil(s, user_id),
                    fuente="licitaciones",
                    codigo_oportunidad=codigo,
                    score=80,
                    razones={},
                )
            )
        s.commit()


def _ca(engine, codigo: str = "CA-M1", **campos: Any) -> None:
    base: dict[str, Any] = {
        "codigo": codigo,
        "nombre": f"Compra {codigo}",
        "estado": "publicada",
        "region": 13,
        "organismo_nombre": "ORG MODAL",
        "fecha_publicacion": ahora_utc() - timedelta(days=1),
        "fecha_cierre": ahora_utc() + timedelta(days=2),
    }
    base.update(campos)
    with Session(engine) as s:
        s.add(CompraAgil(**base))
        s.commit()


# ---------------------------------------------------------------------------
# Parcial vs página completa, ownership
# ---------------------------------------------------------------------------


def test_con_hx_request_devuelve_solo_el_parcial(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, _ = _cliente(engine, settings, uid)
    r = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX)
    assert r.status_code == 200
    assert '<nav class="navbar' not in r.text and "<html" not in r.text
    assert 'data-ficha-key="licitaciones:LIC-M1"' in r.text
    assert 'data-ficha-nav="prev"' in r.text and 'data-ficha-nav="next"' in r.text
    # En el modal no hay "Volver": se cierra con la × o Esc.
    assert ">Volver</a>" not in r.text
    assert 'data-bs-dismiss="modal"' in r.text


def test_sin_hx_request_sigue_la_pagina_completa_con_volver(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, _ = _cliente(engine, settings, uid)
    r = client.get("/oportunidad/licitaciones/LIC-M1")
    assert r.status_code == 200
    assert "<html" in r.text and '<nav class="navbar' in r.text
    assert ">Volver</a>" in r.text
    assert 'data-ficha-nav="prev"' not in r.text


def test_sin_acceso_404_en_ambos_modos(engine, settings):
    ajeno = _usuario(engine, "ficha-b@test.cl")
    _lic(engine, ajeno)  # el match es de otro usuario
    propio = _usuario(engine)
    client, _ = _cliente(engine, settings, propio)
    assert client.get("/oportunidad/licitaciones/LIC-M1").status_code == 404
    assert client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX).status_code == 404


def test_fuente_indebida_404_por_htmx(engine, settings):
    uid = _usuario(engine)
    client, _ = _cliente(engine, settings, uid)
    assert client.get("/oportunidad/ordenes/X-1", headers=_HX).status_code == 404


def test_atribucion_chilecompra_una_sola_vez(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, _ = _cliente(engine, settings, uid)
    r = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX)
    assert r.text.count("Fuente: Dirección ChileCompra") == 1


# ---------------------------------------------------------------------------
# Pestañas y avisos
# ---------------------------------------------------------------------------


def test_pestanas_solo_las_que_tienen_datos(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, _ = _cliente(engine, settings, uid)
    html = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX).text
    assert "ficha-tab-items" not in html and "ficha-tab-comp" not in html and "ficha-tab-desc" not in html

    _lic(engine, uid, "LIC-M2", descripcion="Descripción de prueba")
    with Session(engine) as s:
        s.add(LicitacionItem(licitacion_codigo="LIC-M2", nombre="Resma", cantidad=2, unidad="UN"))
        s.commit()
    html = client.get("/oportunidad/licitaciones/LIC-M2", headers=_HX).text
    assert "ficha-tab-items" in html and "ficha-tab-desc" in html
    assert "Ítems (1)" in html
    assert "ficha-tab-comp" not in html
    # La pestaña inicial es Ítems si hay.
    assert 'id="ficha-tab-items" type="button" role="tab"' in html
    assert 'class="nav-link active" id="ficha-tab-items"' in html
    assert 'role="tablist"' in html and 'role="tabpanel"' in html


def test_pestana_competencia_en_licitacion_adjudicada(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid, estado="adjudicada", fecha_cierre=ahora_utc() - timedelta(days=3))
    with Session(engine) as s:
        s.add(
            OfertaCompetencia(
                licitacion_codigo="LIC-M1",
                codigo_item="ITEM-1",
                rut_proveedor="2-7",
                nombre_proveedor="Prov Ganador",
                monto_unitario=90,
                monto_linea_adjudicada=90,
                cantidad=1,
                seleccionada=True,
            )
        )
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    html = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX).text
    assert "ficha-tab-comp" in html and "Competencia (1)" in html and "Prov Ganador" in html


def test_ca_sin_descripcion_ni_productos_avisa_el_detalle_faltante(engine, settings):
    uid = _usuario(engine)
    _ca(engine)
    client, _ = _cliente(engine, settings, uid)
    html = client.get("/oportunidad/compras_agiles/CA-M1", headers=_HX).text
    assert "Todavía sin descripción ni productos" in html
    assert "La app aún no descarga el detalle de esta Compra Ágil" in html

    _ca(engine, "CA-M2", descripcion="Con descripción")
    html = client.get("/oportunidad/compras_agiles/CA-M2", headers=_HX).text
    assert "Todavía sin descripción ni productos" not in html
    with Session(engine) as s:
        s.add(CaProducto(ca_codigo="CA-M1", nombre="Toner", cantidad=1, unidad="UN"))
        s.commit()
    assert "Todavía sin descripción ni productos" not in client.get(
        "/oportunidad/compras_agiles/CA-M1", headers=_HX
    ).text


def test_cierre_vencido_funcion_pura():
    ahora = ahora_utc()
    lic, ca = "licitaciones", "compras_agiles"
    pasado, futuro = ahora - timedelta(days=1), ahora + timedelta(days=1)
    assert cierre_vencido("publicada", pasado, lic, ahora) is True
    assert cierre_vencido("publicada", futuro, lic, ahora) is False
    # Un estado terminal con cierre pasado no es "cierre vencido": ya se ve como lo que es.
    assert cierre_vencido("adjudicada", pasado, lic, ahora) is False
    # Sale de `es_vigente`: una CA sin cierre publicada hace más de 7 días también.
    assert cierre_vencido("publicada", None, ca, ahora, ahora - timedelta(days=10)) is True
    assert cierre_vencido("publicada", None, ca, ahora, ahora - timedelta(days=3)) is False
    # Sin fecha con la que juzgar no se afirma nada (regla 6).
    assert cierre_vencido("publicada", None, lic, ahora) is False
    assert cierre_vencido("publicada", None, ca, ahora, None) is False
    badge = presentacion_estado_con_cierre("publicada", pasado, lic, ahora)
    assert badge["etiqueta"] == "Publicada · cierre vencido"
    assert presentacion_estado_con_cierre("publicada", futuro, lic, ahora)["etiqueta"] == "Abierta"


def test_ficha_y_tarjeta_dicen_cierre_vencido_no_abierta(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid, fecha_cierre=ahora_utc() - timedelta(days=2))
    with Session(engine) as s:
        guardar(s, uid, "licitaciones", "LIC-M1")
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    ficha = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX).text
    assert "cierre vencido" in ficha and "Abierta" not in ficha
    tarjeta = client.get("/registro?tab=cerradas").text
    assert "Publicada · cierre vencido" in tarjeta
    assert "badge-estado--cierre_vencido" in tarjeta
    assert "badge-estado--familia_abierta" not in tarjeta


# ---------------------------------------------------------------------------
# Acciones dentro del modal
# ---------------------------------------------------------------------------


def test_guardar_desde_el_modal_devuelve_barra_y_tarjeta_oob(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, headers = _cliente(engine, settings, uid)
    r = client.post(
        "/oportunidad/licitaciones/LIC-M1/guardar",
        data={"origen": "modal", "desde": ""},
        headers={**headers, **_HX},
    )
    assert r.status_code == 200
    assert 'id="ficha-acciones-feedback"' in r.text and 'aria-pressed="true"' in r.text
    assert "hx-swap-oob=\"outerHTML:[data-oportunidad-key='licitaciones:LIC-M1']\"" in r.text
    assert "tarjeta-op" in r.text


def test_descartar_desde_el_modal_mantiene_la_barra_y_ofrece_restaurar(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, headers = _cliente(engine, settings, uid)
    r = client.post(
        "/oportunidad/licitaciones/LIC-M1/descartar",
        data={"origen": "modal", "desde": ""},
        headers={**headers, **_HX},
    )
    assert r.status_code == 200
    assert "Descartada" in r.text and "Restaurar" in r.text
    assert "tarjeta-op" not in r.text  # la tarjeta sale del feed (script del dashboard + toast)
    r = client.post(
        "/oportunidad/licitaciones/LIC-M1/deshacer-descarte",
        data={"origen": "modal", "desde": ""},
        headers={**headers, **_HX},
    )
    assert r.status_code == 200 and "Restaurar" not in r.text and "tarjeta-op" in r.text


def test_archivar_con_htmx_no_redirige_y_sin_htmx_si(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    with Session(engine) as s:
        guardar(s, uid, "licitaciones", "LIC-M1")
        s.commit()
    client, headers = _cliente(engine, settings, uid)
    base = "/oportunidad/licitaciones/LIC-M1"
    r = client.post(
        f"{base}/archivar", data={"origen": "modal"}, headers={**headers, **_HX}, follow_redirects=False
    )
    assert r.status_code == 200 and "Archivada en Mi registro" in r.text and "Desarchivar" in r.text
    r = client.post(
        f"{base}/desarchivar", data={"origen": "modal"}, headers={**headers, **_HX}, follow_redirects=False
    )
    assert r.status_code == 200 and "Archivar" in r.text
    r = client.post(
        f"{base}/archivar", data={"next": "/registro?tab=guardadas"}, headers=headers, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/registro?tab=guardadas"
    # CSRF igual que antes.
    r = client.post(f"{base}/archivar", data={"csrf_token": "malo"}, follow_redirects=False)
    assert r.status_code == 403


def test_modal_ownership_en_acciones(engine, settings):
    ajeno = _usuario(engine, "ficha-b@test.cl")
    _lic(engine, ajeno)
    propio = _usuario(engine)
    client, headers = _cliente(engine, settings, propio)
    r = client.post(
        "/oportunidad/licitaciones/LIC-M1/guardar",
        data={"origen": "modal"},
        headers={**headers, **_HX},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Feed y Mi registro: apertura, URL y pestañas
# ---------------------------------------------------------------------------


def test_url_feed_no_arrastra_ficha():
    url = _url_feed({"texto": "aseo", "ficha": "licitaciones:LIC-1"}, orden="cierre")
    assert "ficha" not in url and "texto=aseo" in url and "orden=cierre" in url


def test_feed_con_ficha_en_la_url_incluye_el_modal_sin_propagarla(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, _ = _cliente(engine, settings, uid)
    html = client.get("/?ficha=licitaciones:LIC-M1&texto=Licitaci").text
    assert 'id="modal-ficha"' in html and "modal-xl" in html and "modal-fullscreen-md-down" in html
    assert 'src="/static/ficha_modal.js' in html
    assert "ficha=" not in html.replace("ficha_modal", "")
    # Título y "Ver ficha" conservan su href real y los abre el listener delegado de
    # ficha_modal.js: sin hx-get/hx-trigger (htmx cancela el clic antes de evaluar el filtro,
    # y Ctrl/Cmd/Shift-clic dejaría de abrir la página).
    assert html.count('href="/oportunidad/licitaciones/LIC-M1"') == 2
    assert 'data-abre-ficha aria-haspopup="dialog"' in html
    assert 'hx-get="/oportunidad/licitaciones/LIC-M1"' not in html
    assert 'hx-get="/oportunidad/licitaciones/LIC-M1?' not in html
    assert "click[button" not in html
    js = client.get("/static/ficha_modal.js").text
    assert "evt.button !== 0 || evt.ctrlKey || evt.metaKey || evt.shiftKey || evt.altKey" in js


def test_modal_desde_vencidas_saca_la_tarjeta_y_actualiza_pestanas(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid, fecha_cierre=ahora_utc() - timedelta(days=3))
    client, headers = _cliente(engine, settings, uid)

    pagina = client.get("/registro?tab=vencidas&texto=licit&ficha=licitaciones:LIC-M1").text
    assert 'id="modal-ficha"' in pagina
    assert 'data-abre-ficha' in pagina and 'hx-get="/oportunidad/licitaciones/LIC-M1"' not in pagina
    # Las pestañas y los filtros no arrastran `ficha`.
    tabs = pagina.split('id="registro-tabs"')[1].split("</nav>")[0]
    assert 'href="/registro?tab=cerradas"' in tabs and "ficha" not in tabs
    assert "Vencidas recientes (1)" in pagina

    # El parcial recuerda de qué pestaña viene.
    parcial = client.get("/oportunidad/licitaciones/LIC-M1?desde=registro:vencidas", headers=_HX).text
    assert 'name="desde" value="registro:vencidas"' in parcial

    # Guardar desde el modal: la tarjeta sale de Vencidas y las pestañas se recuentan.
    r = client.post(
        "/oportunidad/licitaciones/LIC-M1/guardar",
        data={"origen": "modal", "desde": "registro:vencidas"},
        headers={**headers, **_HX},
    )
    assert r.status_code == 200
    assert "hx-swap-oob=\"delete:[data-oportunidad-key='licitaciones:LIC-M1']\"" in r.text
    assert 'id="registro-tabs"' in r.text and 'hx-swap-oob="true"' in r.text
    assert "Vencidas recientes (0)" in r.text and "Cerradas (1)" in r.text
    # Y la pestaña que quedó es la misma, con el mismo filtro.
    assert "Vencidas recientes (0)" in client.get("/registro?tab=vencidas&texto=licit").text
    # Un `desde` inventado se ignora.
    parcial = client.get("/oportunidad/licitaciones/LIC-M1?desde=http://evil", headers=_HX).text
    assert "evil" not in parcial
