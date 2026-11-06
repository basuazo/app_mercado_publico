"""F-plat-anexos-boton: `necesita_anexo`, lista «CA que necesitan anexo», bloque en «Hoy»,
`GET /api/anexos/pendientes` con el mismo criterio y comprobaciones estáticas de `extension/`.

Sin red ni navegador. La lógica pura de la extensión se prueba con `node --test extension/lib.test.js`
(ver `extension/README.md`).
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.tiempo import ahora_utc
from app.mercado import anexos_pendientes as ap
from app.mercado.anexos_pendientes import LineaInfo, motivo_anexo, necesita_anexo
from app.models.tables import CaAnexo, CaLinea, Empresa
from tests.test_plat2 import _ANEXO, _ca, _sesion, _usuario
from tests.test_plat_anexos2 import _token
from tests.test_plat_anexos2 import client as client  # fixtures compartidos con anexos-2
from tests.test_plat_anexos2 import engine as engine
from tests.test_plat_anexos2 import settings as settings

EXTENSION = Path(__file__).resolve().parents[1] / "extension"
_OTRO = [("44121500", "Lápiz", "lápiz grafito", 5, "u")]


# ---------------------------------------------------------------------------
# necesita_anexo (pura)
# ---------------------------------------------------------------------------


def _n(**kw):
    base = dict(n_documentos=1, vigente=True, alimentos=True, resuelta=False, lineas=[])
    base.update(kw)
    return necesita_anexo(**base)


API_OK = LineaInfo("api", "ok", "Arroz grado 1", 20)
API_PEND = LineaInfo("api", "pendiente", "Alimentos varios", 1)
PLACEHOLDER = LineaInfo("anexo", "pendiente", "sin detalle (anexo)", 1)


@pytest.mark.parametrize(
    ("kw", "esperado"),
    [
        ({}, True),  # sin líneas de la API
        ({"lineas": [PLACEHOLDER]}, True),  # placeholder «ver anexo»
        ({"lineas": [API_PEND]}, True),  # caso D: líneas de la API sin reconocer
        ({"lineas": [LineaInfo("api", "dudosa", "Cosas", 1)]}, True),
        ({"lineas": [LineaInfo("api", "ok", "Detalle en archivo adjunto", 1)]}, True),
        ({"lineas": [LineaInfo("api", "ok", "Alimentos según anexo", None)]}, True),
        ({"lineas": [API_OK]}, False),  # la API trae el detalle
        ({"lineas": [API_OK, API_PEND]}, False),  # alguna ok: no es pobre
        ({"lineas": [LineaInfo("api", "ok", "20 cajas según anexo", 20)]}, False),  # compra chica
        ({"lineas": [API_PEND, LineaInfo("manual", "ok", "Arroz", 3)]}, False),  # ya cubierta
        ({"lineas": [API_PEND, LineaInfo("anexo", "ok", "Arroz", 3)]}, False),
        ({"lineas": [LineaInfo("api", "reemplazada", "x", 1)]}, True),  # no cuenta
        ({"lineas": [API_PEND, LineaInfo("anexo", "dudosa", "Arroz", 3)]}, True),  # sin confirmar
        ({"n_documentos": 0}, False),
        ({"n_documentos": None}, False),
        ({"vigente": False}, False),
        ({"alimentos": False}, False),
        ({"resuelta": True}, False),
    ],
)
def test_necesita_anexo_casos(kw, esperado):
    assert _n(**kw) is esperado


def test_motivo_dice_por_que():
    assert motivo_anexo(n_documentos=1, vigente=True, alimentos=True, resuelta=False,
                        lineas=[]) == ap.MOTIVO_SIN_LINEAS  # fmt: skip
    assert motivo_anexo(n_documentos=1, vigente=True, alimentos=True, resuelta=False,
                        lineas=[PLACEHOLDER]) == ap.MOTIVO_PLACEHOLDER  # fmt: skip
    assert motivo_anexo(n_documentos=1, vigente=True, alimentos=True, resuelta=False,
                        lineas=[API_PEND]) == ap.MOTIVO_POBRES  # fmt: skip
    assert motivo_anexo(n_documentos=0, vigente=True, alimentos=True, resuelta=False,
                        lineas=[]) is None  # fmt: skip


# ---------------------------------------------------------------------------
# Consulta, lista web, «Hoy» y API
# ---------------------------------------------------------------------------


def _vigente(s: Session, codigo: str, dias: int = 2, productos=_ANEXO, **kw):
    kw.setdefault("n_documentos", 1)
    kw.setdefault("documentos", [{"id": "d1", "nombre": "Anexo 1.pdf"}])
    return _ca(s, codigo, productos=productos, estado="publicada",
               fecha_cierre=ahora_utc() + timedelta(days=dias), **kw)  # fmt: skip


def _linea(s: Session, codigo: str, n: int, origen: str, estado: str, texto: str = "x"):
    s.add(CaLinea(ca_codigo=codigo, n=n, origen=origen, estado=estado, texto_origen=texto,
                  cantidad=1.0))  # fmt: skip


@pytest.fixture()
def escenario(engine):
    """Una CA por caso: lo que debe y no debe salir en la lista."""
    with Session(engine) as s:
        e1 = Empresa(nombre="E1", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
        s.add(e1)
        s.flush()
        ids = {"con": _usuario(s, "con@t.cl", e1.id), "sin": _usuario(s, "sin@t.cl")}
        _vigente(s, "10-1-COT26", dias=3)  # sin líneas
        _vigente(s, "11-1-COT26", dias=1)  # caso D: línea de la API sin reconocer
        _linea(s, "11-1-COT26", 0, "api", "pendiente", "Alimentos varios")
        _vigente(s, "12-1-COT26", dias=4)  # la API trae el detalle: no
        _linea(s, "12-1-COT26", 0, "api", "ok", "Arroz grado 1")
        _vigente(s, "13-1-COT26", dias=5)  # ya tiene un anexo cargado: no
        s.add(CaAnexo(ca_codigo="13-1-COT26", nombre_original="x", mime="application/pdf",
                      tamano=1, sha256="a" * 64, extraccion_estado="hecha"))  # fmt: skip
        _vigente(s, "14-1-COT26", dias=6, productos=_OTRO)  # no es de alimentos: no
        _vigente(s, "15-1-COT26", dias=7, n_documentos=0)  # sin documentos: no
        _vigente(s, "16-1-COT26", dias=8, region=5)  # otra región: no
        _vigente(s, "17-1-COT26", dias=9, anexo_resuelto_en=ahora_utc())  # resuelta: no
        _ca(s, "18-1-COT26", productos=_ANEXO, n_documentos=1, estado="publicada",
            fecha_cierre=ahora_utc() - timedelta(days=1))  # cerrada: no  # fmt: skip
        s.commit()
    return ids


def test_pendientes_aplica_el_criterio_y_ordena_por_cierre(escenario, engine):
    with Session(engine) as s:
        lista = ap.pendientes(s, [16], ahora_utc())
    assert [p.codigo for p in lista] == ["11-1-COT26", "10-1-COT26"]  # por cierre
    assert lista[0].motivo == ap.MOTIVO_POBRES and lista[1].motivo == ap.MOTIVO_SIN_LINEAS
    assert lista[0].documentos == ["Anexo 1.pdf"]
    with Session(engine) as s:
        assert ap.pendientes(s, [], ahora_utc()) == []
        assert [p.codigo for p in ap.pendientes(s, [16], ahora_utc(), limite=1)] == ["11-1-COT26"]


def test_pagina_pendientes_lista_y_boton_abrir_fichas(client, settings, escenario):
    c, _ = _sesion(settings, escenario["con"])
    r = client.get("/catalogo/anexos/pendientes", cookies=c)
    assert r.status_code == 200
    assert r.text.index("11-1-COT26") < r.text.index("10-1-COT26")
    for fuera in ("12-1-COT26", "13-1-COT26", "14-1-COT26", "15-1-COT26", "16-1-COT26"):
        assert fuera not in r.text
    assert "buscador.mercadopublico.cl/ficha?code=11-1-COT26" in r.text
    assert 'rel="noopener"' in r.text and "data-ficha" in r.text
    assert 'id="abrir-fichas"' in r.text and 'value="10"' in r.text  # N por defecto
    assert "anexos_pendientes.js" in r.text and "Fuente: Dirección ChileCompra" in r.text


def test_pagina_pendientes_exige_sesion_y_empresa(client, settings, escenario):
    assert client.get("/catalogo/anexos/pendientes", follow_redirects=False).status_code in (
        302, 303, 307, 401,
    )  # fmt: skip
    c, _ = _sesion(settings, escenario["sin"])
    assert client.get("/catalogo/anexos/pendientes", cookies=c).status_code == 403


def test_hoy_muestra_cuantas_esperan_anexo(client, settings, escenario):
    c, _ = _sesion(settings, escenario["con"])
    r = client.get("/", cookies=c)
    assert r.status_code == 200
    assert re.search(r'data-hoy="anexos-pendientes">2 Compras Ágiles esperan anexo', r.text)


def test_api_pendientes_usa_el_mismo_criterio(client, escenario, engine):
    token = _token(engine)
    r = client.get("/api/anexos/pendientes", headers={"X-Anexos-Token": token})
    assert r.status_code == 200
    assert [p["codigo"] for p in r.json()["pendientes"]] == ["11-1-COT26", "10-1-COT26"]
    assert set(r.json()["pendientes"][0]) == {"codigo", "nombre", "fecha_cierre", "documentos"}


def test_enlace_en_el_catalogo(client, settings, escenario):
    c, _ = _sesion(settings, escenario["con"])
    assert "/catalogo/anexos/pendientes" in client.get("/catalogo?tab=pendientes", cookies=c).text


def test_las_reemplazadas_no_cubren_la_ca(engine):
    with Session(engine) as s:
        _vigente(s, "20-1-COT26")
        _linea(s, "20-1-COT26", 0, "api", "reemplazada", "Arroz")
        s.commit()
        assert [p.codigo for p in ap.pendientes(s, [16], ahora_utc())] == ["20-1-COT26"]
        assert s.execute(select(CaLinea.estado)).scalar_one() == "reemplazada"


# ---------------------------------------------------------------------------
# extension/ (comprobaciones estáticas: regla 1 y regla 9)
# ---------------------------------------------------------------------------


def _fuentes_extension() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in EXTENSION.iterdir()
        if p.suffix in {".js", ".html", ".json"} and not p.name.endswith(".test.js")
    }


def test_manifest_acotado_a_la_ficha_y_sin_permisos_de_mas():
    m = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))
    assert m["manifest_version"] == 3
    assert m["permissions"] == ["storage"]
    assert "host_permissions" not in m  # los permisos de sitio se piden por origen, al guardar
    assert "<all_urls>" not in json.dumps(m)
    for cs in m["content_scripts"]:
        assert cs["matches"] == ["https://buscador.mercadopublico.cl/ficha*"]
    for js in (m["background"]["service_worker"], *(f for cs in m["content_scripts"] for f in cs["js"])):
        assert (EXTENSION / js).is_file()


def test_extension_no_usa_el_servicio_de_adjuntos_ni_trae_secretos():
    for nombre, texto in _fuentes_extension().items():
        assert "adjunto.mercadopublico" not in texto, nombre  # no llama a su servicio ni su clave
        assert "adjuntos-compra-agil" not in texto, nombre
        assert "eval(" not in texto and "new Function" not in texto, nombre
        assert not re.search(r"""(token|ticket)\s*[:=]\s*["'][A-Za-z0-9_-]{16,}["']""", texto, re.I), nombre
    # El token solo lo lee el service worker y la página de opciones, nunca el content script.
    fuentes = _fuentes_extension()
    assert "rastrillo_token" not in fuentes["content.js"]
    assert "rastrillo_token" not in fuentes["hook_main.js"]
    assert "Fuente: Dirección ChileCompra" in fuentes["content.js"]


# ---------------------------------------------------------------------------
# Auto-confirmación (F-plat-auto-revision): la CA resuelta sola no vuelve a la lista
# ---------------------------------------------------------------------------


def test_ca_resuelta_por_auto_confirmacion_no_necesita_anexo(engine):
    """Todas las líneas del anexo `ok`/`auto` → «anexo resuelto» y las de la API `reemplazada`."""
    with Session(engine) as s:
        _vigente(s, "30-1-COT26", anexo_resuelto_en=ahora_utc())
        _linea(s, "30-1-COT26", 0, "api", "reemplazada", "Alimentos varios")
        _linea(s, "30-1-COT26", 1000, "anexo", "ok", "Arroz")
        s.add(CaAnexo(ca_codigo="30-1-COT26", nombre_original="x", mime="application/pdf",
                      tamano=1, sha256="c" * 64, extraccion_estado="hecha"))  # fmt: skip
        s.flush()
        s.execute(CaLinea.__table__.update().values(confirmada_por="auto"))
        s.commit()
        assert ap.pendientes(s, [16], ahora_utc()) == []


def test_auto_confirmacion_parcial_tampoco_vuelve_a_la_lista(engine):
    """Algunas líneas auto-confirmadas y otras `dudosa` (van a la cola de excepciones): el anexo
    ya se subió, así que pedirlo de nuevo no tiene sentido."""
    with Session(engine) as s:
        _vigente(s, "31-1-COT26")
        _linea(s, "31-1-COT26", 0, "api", "pendiente", "Alimentos varios")
        _linea(s, "31-1-COT26", 1000, "anexo", "ok", "Arroz")
        _linea(s, "31-1-COT26", 1001, "anexo", "dudosa", "Cosa rara")
        s.commit()
        assert ap.pendientes(s, [16], ahora_utc()) == []  # sin fila de anexo: lo para la línea ok
    assert not _n(lineas=[API_PEND, LineaInfo("anexo", "ok", "Arroz", 3),
                          LineaInfo("anexo", "dudosa", "Cosa", 1)])  # fmt: skip


def test_reemplazadas_no_cuentan_ni_como_pobres_ni_como_ok():
    # Solo `reemplazada` de la API + una línea manual ok: resuelta por la persona.
    assert not _n(lineas=[LineaInfo("api", "reemplazada", "x", 1), LineaInfo("manual", "ok", "A", 2)])
    # Una `reemplazada` con texto «según anexo» no dispara «remite al adjunto».
    assert _n(lineas=[LineaInfo("api", "reemplazada", "según anexo", 1)])  # sin activas → sin líneas
    assert motivo_anexo(n_documentos=1, vigente=True, alimentos=True, resuelta=False,
                        lineas=[LineaInfo("api", "reemplazada", "según anexo", 1),
                                API_OK]) is None  # fmt: skip
