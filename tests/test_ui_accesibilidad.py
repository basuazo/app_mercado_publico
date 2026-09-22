"""Tests de render de F-ui-fixes: querystring codificado y estructura accesible.

Todos offline: SQLite en memoria y TestClient, sin tocar la red.
"""

from __future__ import annotations

import re

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
from app.models.enums import RolUsuario
from app.models.tables import Usuario

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
    return TestClient(create_app(settings, engine), raise_server_exceptions=True)


@pytest.fixture()
def usuario(engine):
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


def _cookie(settings: Settings, user_id: int) -> dict[str, str]:
    return {COOKIE_NAME: create_session_token(settings.secret_key, user_id)}


# ---------------------------------------------------------------------------
# 1.2 — el texto de búsqueda se codifica en los enlaces de filtro
# ---------------------------------------------------------------------------


def test_texto_con_ampersand_se_codifica_en_los_enlaces(client, settings, usuario) -> None:
    r = client.get("/", params={"texto": "aseo & mantención"}, cookies=_cookie(settings, usuario))
    assert r.status_code == 200
    html = r.text

    # El & del usuario no puede quedar crudo: partiría el querystring.
    assert "texto=aseo%20%26%20mantenci%C3%B3n" in html
    assert "texto=aseo & mantención" not in html
    assert "texto=aseo &amp; mantención" not in html


def test_enlaces_de_orden_y_agrupacion_conservan_el_texto_codificado(
    client, settings, usuario
) -> None:
    r = client.get("/", params={"texto": "aseo & mantención"}, cookies=_cookie(settings, usuario))
    html = r.text

    # Desde F-feed-ui-2 el orden de los parámetros lo fija `_ORDEN_PARAMS`, así
    # que el enlace no empieza necesariamente por el parámetro buscado: se
    # localiza el href completo y se revisa ahí dentro.
    enlaces = re.findall(r'(?:href|value)="(/\?[^"]*)"', html)
    # `min_score` salió de esta lista en F-feed-ui-2: dejó de ser un enlace y
    # pasó a ser un radio del panel, que el navegador codifica solo.
    for parametro in ("orden=cierre", "agrupar_por=region"):
        conservan = [e for e in enlaces if parametro in e]
        assert conservan, f"no se encontró el enlace con {parametro}"
        for enlace in conservan:
            assert "%26" in enlace, f"{parametro} perdió la codificación: {enlace}"


# ---------------------------------------------------------------------------
# Bloque 3 — estructura accesible en base.html
# ---------------------------------------------------------------------------


def test_navbar_colapsa_en_movil(client, settings, usuario) -> None:
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert 'class="navbar-toggler"' in html
    assert 'data-bs-toggle="collapse"' in html
    assert 'data-bs-target="#nav-principal"' in html
    assert 'aria-controls="nav-principal"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-label="Abrir navegación"' in html
    assert 'class="collapse navbar-collapse" id="nav-principal"' in html


def test_landmark_main_y_enlace_de_salto(client, settings, usuario) -> None:
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert '<main id="contenido"' in html
    assert 'href="#contenido">Saltar al contenido</a>' in html
    assert "visually-hidden-focusable" in html
    # El salto debe venir antes del nav para ser lo primero que recibe foco.
    assert html.index("Saltar al contenido") < html.index("<nav")


def test_region_de_anuncios_aria_live(client, settings, usuario) -> None:
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert 'id="anuncios"' in html
    assert 'role="status"' in html
    assert 'aria-live="polite"' in html
    assert html.count('id="anuncios"') == 1


def test_pagina_actual_marcada_en_la_navegacion(client, settings, usuario) -> None:
    html = client.get("/perfiles", cookies=_cookie(settings, usuario)).text
    i = html.index('href="/perfiles"')
    enlace = html[html.rindex("<a", 0, i) : html.index("</a>", i)]
    assert 'aria-current="page"' in enlace
    assert "active" in enlace

    # En otra página el mismo enlace no debe declararse actual.
    html_seguidas = client.get("/seguidas", cookies=_cookie(settings, usuario)).text
    j = html_seguidas.index('href="/perfiles"')
    enlace_perfiles = html_seguidas[
        html_seguidas.rindex("<a", 0, j) : html_seguidas.index("</a>", j)
    ]
    assert 'aria-current="page"' not in enlace_perfiles


def test_salir_ya_no_usa_el_rojo_de_peligro(client, settings, usuario) -> None:
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    i = html.index(">Salir<")
    boton = html[html.rindex("<button", 0, i) : i]
    assert "btn-outline-danger" not in boton


def test_clase_num_disponible_para_columnas_numericas(client, settings, usuario) -> None:
    """Desde F-feed-ui-1 la clase vive en el archivo estático, no inline."""
    html = client.get("/", cookies=_cookie(settings, usuario)).text
    assert '<link rel="stylesheet" href="/static/app.css?v=' in html

    css = client.get("/static/app.css")
    assert css.status_code == 200
    assert ".num { font-variant-numeric: tabular-nums; }" in css.text


def _crear_match(engine, owner_id: int, score: float, codigo: str = "LIC-UI-1") -> None:
    from app.models.tables import Licitacion, LicitacionItem, OportunidadMatch, PerfilBusqueda

    with Session(engine) as s:
        s.add(
            Licitacion(
                codigo=codigo,
                nombre="Licitación de prueba",
                descripcion="",
                estado="publicada",
                monto_clp=12500000,
            )
        )
        s.add(
            LicitacionItem(
                licitacion_codigo=codigo,
                codigo_producto="43201500",
                nombre="Notebooks",
                cantidad=1500,
                unidad="UN",
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
                razones=[],
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# 2.1 / 2.2 / 3.4 / 3.6 — ficha de detalle
# ---------------------------------------------------------------------------


def test_score_65_es_banda_alta_en_la_ficha(client, settings, usuario, engine) -> None:
    """65 pasa el preset "Alta relevancia" (60): el badge no puede salir ámbar."""
    _crear_match(engine, usuario, score=65)
    html = client.get("/oportunidad/licitaciones/LIC-UI-1", cookies=_cookie(settings, usuario)).text
    assert "badge bg-success fs-5 num" in html
    assert "badge bg-warning text-dark fs-5" not in html


def test_score_bajo_el_corte_medio_es_banda_baja(client, settings, usuario, engine) -> None:
    _crear_match(engine, usuario, score=20, codigo="LIC-UI-2")
    html = client.get("/oportunidad/licitaciones/LIC-UI-2", cookies=_cookie(settings, usuario)).text
    assert "badge bg-secondary fs-5 num" in html


def test_montos_y_cantidades_en_formato_chileno(client, settings, usuario, engine) -> None:
    _crear_match(engine, usuario, score=65)
    html = client.get("/oportunidad/licitaciones/LIC-UI-1", cookies=_cookie(settings, usuario)).text
    assert "$12.500.000" in html
    assert "$12,500,000" not in html
    assert "1.500" in html


def test_encabezados_de_tabla_con_scope(client, settings, usuario, engine) -> None:
    _crear_match(engine, usuario, score=65)
    html = client.get("/oportunidad/licitaciones/LIC-UI-1", cookies=_cookie(settings, usuario)).text
    assert '<th scope="col">Descripción</th>' in html
    assert "<th>" not in html


def test_toggle_me_sirve_expone_su_estado(client, settings, usuario, engine) -> None:
    _crear_match(engine, usuario, score=65)
    html = client.get("/oportunidad/licitaciones/LIC-UI-1", cookies=_cookie(settings, usuario)).text
    assert 'data-accion="me-sirve"' in html
    assert 'aria-pressed="false"' in html
    # El mecanismo de anuncio y de foco cuelga del contenedor que HTMX reemplaza.
    assert "data-anuncio=" in html


def test_nota_de_alcance_del_filtro_de_region(client, settings, usuario) -> None:
    html = client.get("/perfiles", cookies=_cookie(settings, usuario)).text
    plano = " ".join(html.split())  # la nota va envuelta en varias líneas
    assert "El filtro de región aplica solo a Compra Ágil" in plano
    assert "Las licitaciones no traen región en la fuente oficial" in plano
