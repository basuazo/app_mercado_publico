"""Tests F-ficha-modal (auditoría): descarte en dos botones, panel de exclusión dentro del
modal, Explorar CA con modal, encabezados de caché y avisos de cierre vencido.

Casi todo corre sobre SQLite; la validación de exclusiones (FTS) y la limpieza de matches
necesitan Postgres de dev (`DATABASE_URL`) y se saltan sin él.
"""

from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.core.db import normalizar_url_driver
from app.core.tiempo import ahora_utc
from app.models.tables import (
    CompraAgil,
    Licitacion,
    MatchFeedback,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)
from tests import test_ficha_modal as _base
from tests.test_ficha_modal import _HX, _ca, _cliente, _lic, _perfil, _usuario

# Los mismos fixtures (SQLite en memoria y Settings de prueba) que test_ficha_modal.
engine = _base.engine
settings = _base.settings

_DOS = ("Solo descartar", "Descartar y excluir términos")


def _ca_con_match(engine, uid: int, codigo: str = "CA-D1", **campos) -> None:
    _ca(engine, codigo, **campos)
    with Session(engine) as s:
        s.add(
            OportunidadMatch(
                perfil_id=_perfil(s, uid),
                fuente="compras_agiles",
                codigo_oportunidad=codigo,
                score=70,
                razones={},
            )
        )
        s.commit()


# ---------------------------------------------------------------------------
# 1. Descarte en dos botones
# ---------------------------------------------------------------------------


def test_dos_botones_con_match_en_feed_registro_modal_y_pagina(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)  # vigente, con match: sale en el feed
    _lic(engine, uid, "LIC-V", fecha_cierre=ahora_utc() - timedelta(days=3))  # vencida
    client, _ = _cliente(engine, settings, uid)

    feed = client.get("/").text
    registro = client.get("/registro?tab=vencidas").text
    modal = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX).text
    pagina = client.get("/oportunidad/licitaciones/LIC-M1").text
    for nombre, html in (("feed", feed), ("registro", registro), ("modal", modal), ("página", pagina)):
        for etiqueta in _DOS:
            assert etiqueta in html, (nombre, etiqueta)
        # Misma jerarquía visual: ambos secundarios (outline), ninguno rojo ni primario.
        for etiqueta in _DOS:
            i = html.index(etiqueta)
            boton = html[html.rindex("<button", 0, i) : i]
            assert "btn-outline-secondary" in boton and "danger" not in boton, (nombre, etiqueta)


def test_sin_match_solo_se_ofrece_solo_descartar(engine, settings):
    uid = _usuario(engine)
    _ca(engine, "CA-SIN")  # CA del explorador: sin match
    client, _ = _cliente(engine, settings, uid)
    for html in (
        client.get("/oportunidad/compras_agiles/CA-SIN", headers=_HX).text,
        client.get("/oportunidad/compras_agiles/CA-SIN").text,
    ):
        assert "Solo descartar" in html
        # (la frase también sale en el changelog de la página: se mira el botón, no el texto)
        assert "descartar-opciones" not in html


def test_panel_de_exclusion_va_dentro_del_modal_de_la_ficha(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, _ = _cliente(engine, settings, uid)
    modal = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX).text
    assert 'id="ficha-panel-excluir"' in modal
    assert 'hx-target="#ficha-panel-excluir"' in modal and "origen=modal" in modal

    panel = client.get(
        "/oportunidad/licitaciones/LIC-M1/descartar-opciones?origen=modal&desde=registro:vencidas",
        headers=_HX,
    ).text
    assert "modal-header" not in panel  # sin segundo modal
    assert 'name="origen" value="modal"' in panel and 'name="desde" value="registro:vencidas"' in panel
    assert "data-cierra-panel-excluir" in panel and "Cancelar" in panel
    assert 'hx-post="/oportunidad/licitaciones/LIC-M1/descartar-y-excluir"' in panel
    assert "Solo descartar" not in panel

    # El panel de siempre (feed y página) sigue siendo el modal "Descartar".
    clasico = client.get("/oportunidad/licitaciones/LIC-M1/descartar-opciones?origen=dashboard").text
    assert "modal-header" in clasico and "Descartar y excluir términos" in clasico
    assert "Solo descartar" not in clasico


def test_solo_descartar_desde_el_modal_saca_la_tarjeta_sin_tocar_perfiles(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, headers = _cliente(engine, settings, uid)
    with Session(engine) as s:
        antes = [(p.keywords, p.keywords_excluir) for p in s.execute(select(PerfilBusqueda)).scalars()]
    r = client.post(
        "/oportunidad/licitaciones/LIC-M1/descartar",
        data={"origen": "modal", "desde": ""},
        headers={**headers, **_HX},
    )
    assert r.status_code == 200
    assert "hx-swap-oob=\"delete:[data-oportunidad-key='licitaciones:LIC-M1']\"" in r.text
    assert 'id="ficha-panel-excluir"' in r.text
    with Session(engine) as s:
        assert [(p.keywords, p.keywords_excluir) for p in s.execute(select(PerfilBusqueda)).scalars()] == antes


# ---------------------------------------------------------------------------
# 2-3. Apertura, scroll, nav
# ---------------------------------------------------------------------------


def test_aria_label_de_la_lista_y_css_de_scroll(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, _ = _cliente(engine, settings, uid)
    modal = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX).text
    assert "anterior de la lista" in modal and "siguiente de la lista" in modal
    assert "del feed" not in modal
    css = client.get("/static/app.css").text
    assert "#modal-ficha .ficha { display: flex; flex-direction: column;" in css
    assert "min-height: 0" in css and "#modal-ficha .ficha .modal-body" in css
    js = client.get("/static/ficha_modal.js").text
    # Tras una acción que saca la tarjeta: pasa a la siguiente o cierra; nunca nav vacía.
    assert "accionPendiente" in js and "nodo.hidden = fuera" in js


# ---------------------------------------------------------------------------
# 4. Pestañas siempre fuera de banda (sin depender del contexto)
# ---------------------------------------------------------------------------


def test_pestanas_del_registro_siempre_con_swap_oob(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid, fecha_cierre=ahora_utc() - timedelta(days=3))
    client, headers = _cliente(engine, settings, uid)
    r = client.post(
        "/oportunidad/licitaciones/LIC-M1/descartar",
        data={"origen": "modal", "desde": "registro:vencidas"},
        headers={**headers, **_HX},
    )
    assert r.status_code == 200
    assert '<nav id="registro-tabs" aria-label="Secciones de Mi registro" hx-swap-oob="true">' in r.text
    # La página normal, en cambio, no lleva el atributo.
    assert 'hx-swap-oob="true">' not in client.get("/registro?tab=vencidas").text.split("<main")[1].split(
        "Vencidas recientes"
    )[0]


# ---------------------------------------------------------------------------
# 6. Explorar CA
# ---------------------------------------------------------------------------


def test_explorador_incluye_el_modal_y_sus_acciones_fuera_de_banda(engine, settings):
    uid = _usuario(engine)
    _ca(engine, "CA-EXP", fecha_cierre=ahora_utc() + timedelta(days=2))
    client, headers = _cliente(engine, settings, uid)
    html = client.get("/compras-agiles", params={"sin_favoritos": "1", "incluir_posibles": "1"}).text
    assert 'id="modal-ficha"' in html and "ficha_modal.js" in html
    assert 'data-oportunidad-key="compras_agiles:CA-EXP"' in html
    assert 'href="/oportunidad/compras_agiles/CA-EXP"\n               data-abre-ficha' in html
    assert "Solo descartar" in html

    # Guardar desde el modal abierto en el explorador: sus botones se actualizan por OOB.
    r = client.post(
        "/oportunidad/compras_agiles/CA-EXP/guardar",
        data={"origen": "modal", "desde": "explorador"},
        headers={**headers, **_HX},
    )
    assert r.status_code == 200
    assert 'id="acciones-ca-CA-EXP" hx-swap-oob="true"' in r.text
    assert "tarjeta-op__monto" not in r.text  # no mete una tarjeta del feed en la fila
    # Descartar: la fila sale.
    r = client.post(
        "/oportunidad/compras_agiles/CA-EXP/descartar",
        data={"origen": "modal", "desde": "explorador"},
        headers={**headers, **_HX},
    )
    assert "hx-swap-oob=\"delete:[data-oportunidad-key='compras_agiles:CA-EXP']\"" in r.text


# ---------------------------------------------------------------------------
# 7. Cierre vencido sale de es_vigente
# ---------------------------------------------------------------------------


def test_aviso_de_cierre_no_repite_el_chip_y_distingue_el_caso_sin_fecha(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid, fecha_cierre=ahora_utc() - timedelta(days=2))
    _ca(engine, "CA-SC", fecha_cierre=None, fecha_publicacion=ahora_utc() - timedelta(days=10))
    _ca(engine, "CA-SC-OK", fecha_cierre=None, fecha_publicacion=ahora_utc() - timedelta(days=3))
    _lic(engine, uid, "LIC-SC", fecha_cierre=None)
    client, _ = _cliente(engine, settings, uid)

    con_fecha = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX).text
    assert "Publicada · cierre vencido" in con_fecha
    aviso = con_fecha.split('<div class="alert alert-warning"')[1].split("</div>")[0]
    assert "Revisa la ficha oficial antes de preparar una oferta." in aviso
    assert "cierre vencido" not in aviso and "Figura como publicada" not in aviso

    sin_cierre = client.get("/oportunidad/compras_agiles/CA-SC", headers=_HX).text
    assert "Publicada · cierre vencido" in sin_cierre
    assert "No informa fecha de cierre y se publicó hace más de 7 días" in sin_cierre

    reciente = client.get("/oportunidad/compras_agiles/CA-SC-OK", headers=_HX).text
    assert "cierre vencido" not in reciente and 'class="alert alert-warning"' not in reciente
    # Licitación sin fecha de cierre: no se afirma nada.
    sin_fecha = client.get("/oportunidad/licitaciones/LIC-SC", headers=_HX).text
    assert "cierre vencido" not in sin_fecha


# ---------------------------------------------------------------------------
# 8. Vary / Cache-Control
# ---------------------------------------------------------------------------


def test_ficha_responde_vary_hx_request_y_el_parcial_no_se_cachea(engine, settings):
    uid = _usuario(engine)
    _lic(engine, uid)
    client, _ = _cliente(engine, settings, uid)
    pagina = client.get("/oportunidad/licitaciones/LIC-M1")
    parcial = client.get("/oportunidad/licitaciones/LIC-M1", headers=_HX)
    assert "HX-Request" in pagina.headers["vary"] and "HX-Request" in parcial.headers["vary"]
    assert parcial.headers["cache-control"] == "no-store"
    assert "no-store" not in pagina.headers.get("cache-control", "")


# ---------------------------------------------------------------------------
# Postgres: validación de exclusiones y aviso dentro del modal
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("DATABASE_URL", "")
_TIENE_PG = _DB_URL.startswith("postgresql") or _DB_URL.startswith("postgres")
_EMAILS_PG = ("ficha-pg-a@test.cl",)


@pytest.fixture()
def pg_engine():
    if not _TIENE_PG:
        pytest.skip("Requiere DATABASE_URL de Postgres")
    e = create_engine(normalizar_url_driver(_DB_URL))

    def limpiar() -> None:
        with Session(e) as s:
            s.execute(delete(Usuario).where(Usuario.email.in_(_EMAILS_PG)))
            s.execute(delete(CompraAgil).where(CompraAgil.codigo.like("FDG-%")))
            s.execute(delete(Licitacion).where(Licitacion.codigo.like("FDG-%")))
            s.commit()

    limpiar()
    yield e
    limpiar()
    e.dispose()


def _perfil_salud(engine) -> int:
    with Session(engine) as s:
        uid = _usuario_pg(s)
        p = PerfilBusqueda(
            owner_id=uid,
            nombre="Perfil salud",
            keywords=["salud"],
            keywords_excluir=[],
            regiones=[],
            fuentes=["compras_agiles"],
            activo=True,
        )
        s.add(p)
        s.flush()
        s.commit()
        return uid


def _usuario_pg(s: Session) -> int:
    from app.auth.password import hash_password
    from app.models.enums import RolUsuario

    u = Usuario(
        email=_EMAILS_PG[0], password_hash=hash_password("x"), rol=RolUsuario.USUARIO, activo=True
    )
    s.add(u)
    s.flush()
    return u.id


def test_excluir_desde_el_modal_valida_antes_de_descartar(pg_engine, settings):
    uid = _perfil_salud(pg_engine)
    _ca(pg_engine, "FDG-1", fecha_cierre=ahora_utc() + timedelta(days=2), nombre="Servicio saludable")
    with Session(pg_engine) as s:
        pid = s.execute(select(PerfilBusqueda.id).where(PerfilBusqueda.owner_id == uid)).scalar_one()
        s.add(
            OportunidadMatch(
                perfil_id=pid, fuente="compras_agiles", codigo_oportunidad="FDG-1", score=70, razones={}
            )
        )
        s.commit()
    client, headers = _cliente(pg_engine, settings, uid)
    ruta = "/oportunidad/compras_agiles/FDG-1/descartar-y-excluir"
    datos = {"perfil_id": str(pid), "origen": "modal", "desde": ""}

    # "saludable" tiene la misma raíz que la keyword "salud": se rechaza y NO se descarta.
    r = client.post(ruta, data={**datos, "palabras": ["saludable"]}, headers={**headers, **_HX})
    assert r.status_code == 200
    assert r.headers["HX-Retarget"] == "#ficha-panel-excluir"
    assert "elige otra palabra" in r.text and "data-cierra-panel-excluir" in r.text
    with Session(pg_engine) as s:
        assert s.execute(select(MatchFeedback).where(MatchFeedback.codigo_oportunidad == "FDG-1")).first() is None
        assert s.get(PerfilBusqueda, pid).keywords_excluir == []  # type: ignore[union-attr]

    # Una palabra válida: se descarta, se excluye y el modal recibe barra + aviso + salida.
    r = client.post(ruta, data={**datos, "palabras": ["zzfdgrata"]}, headers={**headers, **_HX})
    assert r.status_code == 200
    assert 'id="ficha-acciones-feedback"' in r.text and "Restaurar" in r.text
    assert 'id="ficha-panel-excluir" hx-swap-oob="true"' in r.text and "Excluiste «zzfdgrata»" in r.text
    assert "Deshacer exclusión" in r.text
    assert "hx-swap-oob=\"delete:[data-oportunidad-key='compras_agiles:FDG-1']\"" in r.text
    with Session(pg_engine) as s:
        assert s.execute(select(MatchFeedback).where(MatchFeedback.codigo_oportunidad == "FDG-1")).first() is not None
        assert s.get(PerfilBusqueda, pid).keywords_excluir == ["zzfdgrata"]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("desde", "volver"),
    [
        ("", "/"),
        ("registro:vencidas", "/registro?tab=vencidas"),
        ("registro:guardadas", "/registro?tab=guardadas"),
        ("explorador", "/compras-agiles"),
    ],
)
def test_excluir_desde_el_modal_marca_recarga_y_deshacer_vuelve_al_origen(
    pg_engine, settings, desde, volver
):
    uid = _perfil_salud(pg_engine)
    codigo = "FDG-" + str(abs(hash(desde)) % 10_000)
    _ca(pg_engine, codigo, fecha_cierre=ahora_utc() + timedelta(days=2), nombre="Servicio de aseo")
    with Session(pg_engine) as s:
        pid = s.execute(select(PerfilBusqueda.id).where(PerfilBusqueda.owner_id == uid)).scalar_one()
        s.add(
            OportunidadMatch(
                perfil_id=pid, fuente="compras_agiles", codigo_oportunidad=codigo, score=70, razones={}
            )
        )
        s.commit()
    client, headers = _cliente(pg_engine, settings, uid)
    r = client.post(
        f"/oportunidad/compras_agiles/{codigo}/descartar-y-excluir",
        data={
            "perfil_id": str(pid),
            "origen": "modal",
            "desde": desde,
            "palabras": ["zzfdg" + str(abs(hash(desde)) % 10_000)],
        },
        headers={**headers, **_HX},
    )
    assert r.status_code == 200
    # El marcador: ficha_modal.js no avanza sola, oculta la nav y recarga la lista al cerrar.
    assert "data-recargar-lista" in r.text
    assert "Deshacer exclusión" in r.text
    assert f'name="next" value="{volver}"' in r.text


def test_js_maneja_el_marcador_de_recarga(engine, settings):
    uid = _usuario(engine)
    client, _ = _cliente(engine, settings, uid)
    js = client.get("/static/ficha_modal.js").text
    assert "[data-recargar-lista]" in js and "window.location.replace(urlConFicha(null))" in js
    assert "var fuera = i < 0 || recargar;" in js


# ---------------------------------------------------------------------------
# 3. La fecha de publicación del item decide "cierre vencido" en la tarjeta
# ---------------------------------------------------------------------------


def test_ca_guardada_sin_cierre_publicada_hace_10_dias_dice_cierre_vencido_en_cerradas(engine, settings):
    from app.matching.seguimiento import guardar

    uid = _usuario(engine)
    _ca(engine, "CA-SC10", fecha_cierre=None, fecha_publicacion=ahora_utc() - timedelta(days=10))
    _ca(engine, "CA-SC3", fecha_cierre=None, fecha_publicacion=ahora_utc() - timedelta(days=3))
    with Session(engine) as s:
        guardar(s, uid, "compras_agiles", "CA-SC10")
        guardar(s, uid, "compras_agiles", "CA-SC3")
        s.commit()
    client, _ = _cliente(engine, settings, uid)
    cerradas = client.get("/registro?tab=cerradas").text
    assert "CA-SC10" in cerradas and "CA-SC3" not in cerradas
    tarjeta = cerradas.split('data-oportunidad-key="compras_agiles:CA-SC10"')[1]
    assert "Publicada · cierre vencido" in tarjeta
    assert "badge-estado--cierre_vencido" in tarjeta
    # La reciente sigue vigente: en Guardadas, como Abierta.
    guardadas = client.get("/registro?tab=guardadas").text
    tarjeta = guardadas.split('data-oportunidad-key="compras_agiles:CA-SC3"')[1]
    assert "badge-estado--familia_abierta" in tarjeta
