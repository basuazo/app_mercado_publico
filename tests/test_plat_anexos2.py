"""F-plat-anexos-2: prioridad del anexo sobre la API, carga masiva por código, API externa con token,
cola vigentes/históricas, enlace a la ficha, modelo de respaldo y descarga directa (apagada).

Red siempre mockeada (respx). Anexos sintéticos (XLSX generados acá).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.clients import openrouter as orc
from app.clients.base import RateLimiter
from app.core.tiempo import ahora_utc
from app.mercado import adjudicacion as adj
from app.mercado import anexos, oferta
from app.mercado import anexos_carga as carga
from app.mercado import catalogo as cat
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaAnexo,
    CaLinea,
    CompraAgil,
    Empresa,
    LlmUso,
    ProductoCanonico,
    Proveedor,
    SyncState,
)
from tests.test_plat2 import (
    _ANEXO,
    _DOS_LINEAS,
    _URL_OR,
    _ca,
    _lineas,
    _respuesta,
    _sesion,
    _settings,
    _sin_espera,
    _usuario,
)

# Fixtures como las de F-plat-2 (tests/test_plat2.py), definidas acá para no re-importarlas.


@pytest.fixture()
def engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    yield e


@pytest.fixture()
def settings():
    return _settings(
        openrouter_api_key="sk-or-v1-clave-de-prueba", llm_modelo_extraccion="prueba/vision"
    )


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture()
def cliente(settings, engine):
    c = orc.OpenRouterClient(settings, engine, dormir=_sin_espera)
    c._limiter = RateLimiter(1000.0)
    return c


@pytest.fixture()
def client(engine, settings):
    from fastapi.testclient import TestClient

    return TestClient(create_app(settings, engine), raise_server_exceptions=True)


@pytest.fixture()
def mundo(engine):
    with Session(engine) as s:
        e1 = Empresa(nombre="E1", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
        e2 = Empresa(nombre="E2", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
        s.add_all([e1, e2])
        s.flush()
        ids = {"con": _usuario(s, "con@t.cl", e1.id), "sin": _usuario(s, "sin@t.cl")}
        cat.sembrar_canasta(s)
        _ca(s, "1-1-COT26", productos=_ANEXO, n_documentos=1)
        _ca(s, "5-1-COT26", productos=_ANEXO, region=5)
        s.add(Proveedor(empresa_id=e1.id, nombre="Uno", rubros=[], despacha=False, activo=True))
        s.commit()
        cat.procesar_catalogo(s, [16], inicial=True)
    return ids


def _xlsx_con(*filas: str) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Producto", "Cantidad"])
    for f in filas:
        ws.append([f, 3])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _producto(session: Session, nombre: str = "Hallulla") -> int:
    return session.execute(
        select(ProductoCanonico.id).where(ProductoCanonico.nombre == nombre)
    ).scalar_one()


def _caso_d(session: Session) -> CompraAgil:
    """CA con una línea pobre de la API ya confirmada (`api|ok`) y un anexo leído (`dudosa`)."""
    cat.sembrar_canasta(session)
    ca = _ca(session, productos=[("50181901", "Pan", "pan batido de campo", 10, "Kilogramo")])
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    (pan,) = _lineas(session)
    cat.confirmar_linea(session, pan, producto_id=_producto(session), formato_id=None, usuario_id=1)
    session.commit()
    return ca


# ---------------------------------------------------------------------------
# §2.1 Prioridad de fuentes: anexo > ficha > API
# ---------------------------------------------------------------------------


@respx.mock
def test_caso_d_confirmar_linea_del_anexo_reemplaza_las_de_la_api(session, cliente):
    ca = _caso_d(session)
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    res = anexos.procesar_anexo_ca(
        session, ca, _xlsx_con("Arroz"), "anexo.xlsx", usuario_id=None, extractor=cliente
    )
    session.commit()
    api, arroz, te = _lineas(session)
    assert (api.origen, api.estado) == ("api", "ok")  # todavía conviven: nada confirmado
    assert res.anexo.fuente == "manual"
    cat.agregar_linea_manual(
        session, ca, producto_id=_producto(session), formato_id=None, cantidad=2, usuario_id=1
    )
    session.commit()
    cat.confirmar_linea(
        session, arroz, producto_id=_producto(session), formato_id=None, usuario_id=1
    )
    session.commit()
    session.expire_all()
    estados = {(ln.origen, ln.estado) for ln in _lineas(session)}
    assert ("api", "reemplazada") in estados and ("api", "ok") not in estados
    assert ("manual", "ok") in estados  # la manual nunca se toca
    session.refresh(ca)
    assert ca.detalle_lineas == "incompleto"  # queda «Té» dudosa; la reemplazada no cuenta
    cat.quitar_linea_manual(session, next(ln for ln in _lineas(session) if ln.id == te.id))
    session.commit()
    session.refresh(ca)
    assert ca.detalle_lineas == "completo"  # ok + manual; la reemplazada se ignora

    # Una reemplazada no se puede confirmar, y el job del catálogo no la revive.
    reemplazada = next(ln for ln in _lineas(session) if ln.estado == cat.REEMPLAZADA)
    with pytest.raises(cat.CatalogoError):
        cat.confirmar_linea(
            session, reemplazada, producto_id=_producto(session), formato_id=None, usuario_id=1
        )
    session.rollback()
    cat.procesar_catalogo(session, [16], inicial=True)
    session.expire_all()
    assert session.get(CaLinea, reemplazada.id).estado == cat.REEMPLAZADA


def test_recalcular_con_solo_reemplazadas_es_sin_detalle(session):
    ca = _caso_d(session)
    cat.reemplazar_lineas_api(session, ca.codigo)
    cat.recalcular_detalle_lineas(session, [ca.codigo])
    session.commit()
    session.refresh(ca)
    assert ca.detalle_lineas == "sin_detalle"


def test_mesa_y_oferta_no_ven_las_reemplazadas(session):
    ca = _caso_d(session)
    cat.agregar_linea_manual(
        session, ca, producto_id=_producto(session), formato_id=None, cantidad=1, usuario_id=1,
        reemplaza_api=True,
    )  # fmt: skip
    session.commit()
    assert [ln.origen for ln in adj._lineas_ca(session, ca)] == ["manual"]
    assert [ln.origen for ln in oferta.lineas_de(session, ca.codigo)] == ["manual"]


def test_caso_b_linea_manual_con_y_sin_casilla(client, settings, mundo, engine):
    with Session(engine) as s:
        cat.sembrar_canasta(s)
        _ca(s, "7-1-COT26", productos=[("50181901", "Pan", "pan batido de campo", 10, "Kg")])
        s.commit()
        cat.procesar_catalogo(s, [16], inicial=True)
        pid = _producto(s)
    c, h = _sesion(settings, mundo["con"])
    r = client.post(
        "/catalogo/ca/7-1-COT26/lineas", data={"producto_id": pid, "cantidad": "3"},
        cookies=c, headers=h, follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303
    with Session(engine) as s:
        assert {ln.estado for ln in _lineas(s, "7-1-COT26") if ln.origen == "api"} != {
            cat.REEMPLAZADA
        }
    r = client.post(
        "/catalogo/ca/7-1-COT26/lineas",
        data={"producto_id": pid, "cantidad": "4", "reemplaza_api": "1"},
        cookies=c, headers=h, follow_redirects=False,
    )  # fmt: skip
    assert r.status_code == 303
    with Session(engine) as s:
        api = [ln for ln in _lineas(s, "7-1-COT26") if ln.origen == "api"]
        assert api and {ln.estado for ln in api} == {cat.REEMPLAZADA}
    r = client.get("/catalogo/ca/7-1-COT26", cookies=c)
    assert "reemplazada(s) por el anexo" in r.text and "Abrir en Mercado Público" in r.text


def test_anexo_resuelto_reemplaza_las_de_la_api(session):
    ca = _caso_d(session)
    anexos.resolver_anexo(session, ca)
    session.commit()
    assert {ln.estado for ln in _lineas(session)} == {cat.REEMPLAZADA}


@respx.mock
def test_segundo_documento_convive_y_relectura_del_mismo_reemplaza(session, cliente):
    ca = _caso_d(session)
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    anexos.procesar_anexo_ca(session, ca, _xlsx_con("a"), "Anexo 1.xlsx", usuario_id=None,
                             extractor=cliente)  # fmt: skip
    session.commit()
    anexos.procesar_anexo_ca(session, ca, _xlsx_con("b"), "Anexo 2.xlsx", usuario_id=None,
                             extractor=cliente)  # fmt: skip
    session.commit()
    assert sum(1 for ln in _lineas(session) if ln.anexo_id) == 4  # 2 + 2: conviven
    anexos.procesar_anexo_ca(session, ca, _xlsx_con("c"), "Anexo 1.xlsx", usuario_id=None,
                             extractor=cliente)  # fmt: skip
    session.commit()
    assert sum(1 for ln in _lineas(session) if ln.anexo_id) == 4  # relectura del Anexo 1


@respx.mock
def test_mismo_archivo_no_se_procesa_dos_veces(session, cliente):
    ca = _caso_d(session)
    ruta = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    data = _xlsx_con("arroz")
    primero = anexos.procesar_anexo_ca(session, ca, data, "a.xlsx", usuario_id=None,
                                       extractor=cliente)  # fmt: skip
    session.commit()
    segundo = anexos.procesar_anexo_ca(session, ca, data, "otro nombre.xlsx", usuario_id=None,
                                       extractor=cliente)  # fmt: skip
    session.commit()
    assert segundo.duplicado and segundo.anexo.id == primero.anexo.id
    assert ruta.call_count == 1
    assert len(list(session.execute(select(CaAnexo)).scalars())) == 1


# ---------------------------------------------------------------------------
# §2.2 Carga masiva por código
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("nombre", "codigo"),
    [
        ("1234-56-COT26__Anexo 1.pdf", "1234-56-COT26"),
        ("carpeta/1234-56-cot26__x.pdf", "1234-56-COT26"),
        ("1234-56-COT26 anexo técnico.pdf", "1234-56-COT26"),
        ("1234-56-COT26.xlsx", "1234-56-COT26"),
        ("Anexo 1234-56-COT26.pdf", None),
        ("../malo__x.pdf", "MALO"),
        ("con espacio__x.pdf", None),
        ("", None),
    ],
)
def test_codigo_de_nombre(nombre, codigo):
    assert anexos.codigo_de_nombre(nombre) == codigo


def test_vista_previa_estados(session):
    cat.sembrar_canasta(session)
    _ca(session, "1-1-COT26", productos=_ANEXO, n_documentos=1)
    _ca(session, "2-1-COT26", productos=_ANEXO, anexo_resuelto_en=datetime(2026, 11, 1))
    _ca(session, "5-1-COT26", productos=_ANEXO, region=5)
    session.add(
        CaAnexo(
            ca_codigo="1-1-COT26",
            nombre_original="x",
            mime=anexos.PDF,
            tamano=1,
            sha256="a" * 64,
            extraccion_estado="hecha",
        )  # fmt: skip
    )
    session.commit()
    v = [
        carga.fila_vista(session, [16], "1-1-COT26__a.pdf", 10),
        carga.fila_vista(session, [16], "1-1-COT26__b.pdf", 10, "A" * 64),
        carga.fila_vista(session, [16], "2-1-COT26__c.pdf", 10),
        carga.fila_vista(session, [16], "5-1-COT26__d.pdf", 10),  # otra región: no se revela
        carga.fila_vista(session, [16], "9-9-COT26__e.pdf", 10),
        carga.fila_vista(session, [16], "sin código.pdf", 10),
        carga.fila_vista(session, [16], "1-1-COT26__f.pdf", 11 * 1024 * 1024),
    ]
    assert [f.estado for f in v] == [
        carga.NUEVA, carga.DUPLICADO, carga.YA_RESUELTA, carga.NO_ASOCIADO, carga.NO_ASOCIADO,
        carga.NO_ASOCIADO, carga.INVALIDO,
    ]  # fmt: skip
    assert v[0].ca_nombre == "CA de prueba"
    assert v[3].detalle == v[4].detalle  # inexistente y fuera de alcance: mismo mensaje


@respx.mock
def test_carga_masiva_por_rutas(client, settings, mundo, engine):
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    c, h = _sesion(settings, mundo["con"])
    assert client.get("/catalogo/anexos/carga", cookies=c).status_code == 200
    data = _xlsx_con("arroz")
    sha = hashlib.sha256(data).hexdigest()
    r = client.post(
        "/catalogo/anexos/carga/vista-previa",
        data={"nombres": ["1-1-COT26__pedido.xlsx", "otro.pdf"], "tamanos": [len(data), 5],
              "sha256": [sha, ""]},
        cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 200 and 'data-procesables="1"' in r.text and "No asociado" in r.text
    r = client.post(
        "/catalogo/anexos/carga/archivo",
        files={"archivo": ("1-1-COT26__pedido.xlsx", data, "application/octet-stream")},
        data={"indice": "0"}, cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 200 and "Leído" in r.text and "Revisar 2 fila(s)" in r.text
    with Session(engine) as s:
        (a,) = s.execute(select(CaAnexo)).scalars()
        assert (a.fuente, a.subido_por) == ("masiva", mundo["con"])
        assert a.empresa_id is not None
        assert {ln.estado for ln in _lineas(s) if ln.anexo_id} == {"dudosa"}
    # La vista previa ya lo ve duplicado; procesarlo de nuevo no llama al modelo.
    r = client.post(
        "/catalogo/anexos/carga/vista-previa",
        data={"nombres": ["1-1-COT26__pedido.xlsx"], "tamanos": [len(data)], "sha256": [sha]},
        cookies=c, headers=h,
    )  # fmt: skip
    assert "Ya cargado" in r.text
    # Sin CSRF: 403.
    r = client.post("/catalogo/anexos/carga/vista-previa", data={"nombres": ["x"]}, cookies=c)
    assert r.status_code == 403


def test_carga_masiva_exige_empresa(client, settings, mundo):
    c, _h = _sesion(settings, mundo["sin"])
    assert client.get("/catalogo/anexos/carga", cookies=c).status_code == 403


def _zip(miembros: dict[str, bytes], infos: list[zipfile.ZipInfo] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for nombre, data in miembros.items():
            z.writestr(nombre, data)
        for info in infos or []:
            z.writestr(info, b"%PDF-1.4 x")
    return buf.getvalue()


def test_zip_con_miembro_bomba_se_rechaza_entero(client, settings, mundo):
    c, h = _sesion(settings, mundo["con"])
    bomba = _zip({"1-1-COT26__a.pdf": b"0" * 3_000_000})
    r = client.post(
        "/catalogo/anexos/carga/zip", files={"zip": ("lote.zip", bomba, "application/zip")},
        data={"accion": "vista"}, cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 200 and "sospechoso" in r.text


def test_zip_marca_rutas_raras_y_enlaces(session):
    _ca(session, "1-1-COT26", productos=_ANEXO)
    session.commit()
    enlace = zipfile.ZipInfo("1-1-COT26__enlace.pdf")
    enlace.external_attr = (stat.S_IFLNK | 0o777) << 16
    datos = _zip(
        {"../1-1-COT26__fuera.pdf": b"%PDF-1.4", "__MACOSX/._x": b"x", "carpeta/": b""},
        [enlace],
    )
    filas = carga.vista_zip(session, [16], io.BytesIO(datos))
    motivos = {f.nombre: f.detalle for f in filas}
    assert motivos == {
        "1-1-COT26__fuera.pdf": "Ruta no permitida",
        "1-1-COT26__enlace.pdf": "Es un enlace, no un archivo",
    }


@respx.mock
def test_zip_se_procesa_por_tramos_y_el_presupuesto_se_agota_a_mitad(session, engine):
    s = _settings(
        openrouter_api_key="k" * 20, llm_modelo_extraccion="prueba/vision",
        llm_presupuesto_mes_usd=0.001,
    )  # fmt: skip
    extractor = orc.OpenRouterClient(s, engine, dormir=_sin_espera)
    extractor._limiter = RateLimiter(1000.0)
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS, costo=0.001))
    cat.sembrar_canasta(session)
    _ca(session, "1-1-COT26", productos=_ANEXO, n_documentos=2)
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    datos = _zip({
        "1-1-COT26__a.xlsx": _xlsx_con("arroz"),
        "1-1-COT26__b.xlsx": _xlsx_con("azúcar"),
        "9-9-COT26__c.xlsx": _xlsx_con("té"),
    })  # fmt: skip
    t = iter([0.0, 99.0, 99.0, 99.0])
    filas, siguiente = carga.procesar_zip(
        session, [16], io.BytesIO(datos), 0, extractor=extractor, usuario_id=None,
        empresa_id=None, max_paginas_imagen=1, tiempo_s=20, reloj=lambda: next(t),
    )  # fmt: skip
    assert [f.estado for f in filas] == ["hecha"] and siguiente == 1
    filas, siguiente = carga.procesar_zip(
        session, [16], io.BytesIO(datos), 1, extractor=extractor, usuario_id=None,
        empresa_id=None, max_paginas_imagen=1,
    )  # fmt: skip
    assert [f.estado for f in filas] == ["sin_presupuesto", carga.NO_ASOCIADO]
    assert siguiente is None
    # Reenviar el ZIP entero: lo cargado sale duplicado, sin llamar al modelo.
    llamadas = respx.calls.call_count
    filas, _ = carga.procesar_zip(
        session, [16], io.BytesIO(datos), 0, extractor=extractor, usuario_id=None,
        empresa_id=None, max_paginas_imagen=1,
    )  # fmt: skip
    # El leído sale duplicado; el que quedó `sin_presupuesto` se vuelve a leer (F-plat-lectura §3:
    # misma fila, sin duplicar), y como el presupuesto sigue agotado queda igual.
    assert [f.duplicado for f in filas[:2]] == [True, False]
    assert filas[1].estado == "sin_presupuesto" and respx.calls.call_count == llamadas
    assert len(list(session.execute(select(CaAnexo)).scalars())) == 2
    assert {a.fuente for a in session.execute(select(CaAnexo)).scalars()} == {"masiva"}


# ---------------------------------------------------------------------------
# §2.3 API para automatización externa
# ---------------------------------------------------------------------------


def _token(engine, nombre: str = "E1") -> str:
    with Session(engine) as s:
        e = s.execute(select(Empresa).where(Empresa.nombre == nombre)).scalar_one()
        token = carga.generar_token(e)
        s.commit()
        assert e.token_anexos_hash == hashlib.sha256(token.encode()).hexdigest()
        return token


@respx.mock
def test_api_subir_anexo_con_token(client, mundo, engine, monkeypatch):
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    token = _token(engine)
    comparaciones: list[Any] = []
    real = carga.hmac.compare_digest

    def _espia(a: Any, b: Any) -> bool:
        comparaciones.append(1)
        return real(a, b)

    monkeypatch.setattr(carga.hmac, "compare_digest", _espia)
    data = _xlsx_con("arroz")
    r = client.post(
        "/api/anexos/1-1-COT26", files={"archivo": ("a.xlsx", data)},
        headers={"X-Anexos-Token": token},
    )  # fmt: skip
    assert r.status_code == 201 and comparaciones
    cuerpo = r.json()
    assert cuerpo["estado"] == "hecha" and cuerpo["duplicado"] is False and cuerpo["anexo_id"]
    r = client.post(
        "/api/anexos/1-1-COT26", files={"archivo": ("b.xlsx", data)},
        headers={"X-Anexos-Token": token},
    )  # fmt: skip
    assert r.status_code == 200 and r.json()["duplicado"] is True
    with Session(engine) as s:
        (a,) = s.execute(select(CaAnexo)).scalars()
        assert a.fuente == "api_externa" and a.subido_por is None and a.empresa_id is not None
    # Otra región (la empresa prioriza la 16): 404 sin decir más.
    r = client.post(
        "/api/anexos/5-1-COT26", files={"archivo": ("a.xlsx", data)},
        headers={"X-Anexos-Token": token},
    )  # fmt: skip
    assert r.status_code == 404
    # Archivo no aceptado: 422 con el motivo.
    r = client.post(
        "/api/anexos/1-1-COT26", files={"archivo": ("a.exe", b"MZ\x90")},
        headers={"X-Anexos-Token": token},
    )  # fmt: skip
    assert r.status_code == 422


def test_api_token_invalido_o_revocado_no_revela_la_ca(client, mundo, engine):
    token = _token(engine)
    malos = [
        client.post("/api/anexos/1-1-COT26", files={"archivo": ("a.pdf", b"%PDF")},
                    headers={"X-Anexos-Token": "otro"}),
        client.post("/api/anexos/9-9-COT99", files={"archivo": ("a.pdf", b"%PDF")},
                    headers={"X-Anexos-Token": "otro"}),
        client.post("/api/anexos/1-1-COT26", files={"archivo": ("a.pdf", b"%PDF")}),
        client.get("/api/anexos/pendientes", headers={"X-Anexos-Token": "otro"}),
    ]  # fmt: skip
    assert {r.status_code for r in malos} == {401} and len({r.text for r in malos}) == 1
    with Session(engine) as s:
        e = s.execute(select(Empresa).where(Empresa.nombre == "E1")).scalar_one()
        carga.revocar_token(e)
        s.commit()
    r = client.get("/api/anexos/pendientes", headers={"X-Anexos-Token": token})
    assert r.status_code == 401


def test_api_token_de_empresa_inactiva_no_sirve(client, mundo, engine):
    token = _token(engine)
    with Session(engine) as s:
        s.execute(select(Empresa).where(Empresa.nombre == "E1")).scalar_one().activa = False
        s.commit()
    assert (
        client.get("/api/anexos/pendientes", headers={"X-Anexos-Token": token}).status_code == 401
    )


def test_api_rate_limit_propio(client, mundo, engine, monkeypatch):
    monkeypatch.setattr(carga, "LIMITE_API_POR_MIN", 2)
    fijo = datetime(2026, 11, 6, 12, 0, 30)
    monkeypatch.setattr("app.api.routes.anexos.ahora_utc", lambda: fijo)
    token = _token(engine)
    codigos = [
        client.get("/api/anexos/pendientes", headers={"X-Anexos-Token": token}).status_code
        for _ in range(3)
    ]
    assert codigos == [200, 200, 429]
    r = client.get("/api/anexos/pendientes", headers={"X-Anexos-Token": token})
    assert r.headers["Retry-After"] == "30"
    with Session(engine) as s:
        assert any(
            f.fuente.startswith("anexos_api:") for f in s.execute(select(SyncState)).scalars()
        )


def test_api_pendientes_por_regiones_de_la_empresa(client, mundo, engine):
    futuro = ahora_utc() + timedelta(days=2)
    with Session(engine) as s:
        docs = [{"id": "d1", "nombre": "Anexo 1.pdf"}]
        _ca(s, "3-1-COT26", productos=_ANEXO, n_documentos=1, estado="publicada",
            fecha_cierre=futuro, documentos=docs)  # fmt: skip
        _ca(s, "4-1-COT26", productos=_ANEXO, n_documentos=1, estado="publicada",
            fecha_cierre=futuro + timedelta(days=1), region=5, documentos=docs)  # fmt: skip
        _ca(s, "6-1-COT26", productos=_ANEXO, n_documentos=1, estado="publicada",
            fecha_cierre=futuro - timedelta(days=1), documentos=docs)  # fmt: skip
        s.add(CaAnexo(ca_codigo="6-1-COT26", nombre_original="x", mime=anexos.PDF, tamano=1,
                      sha256="b" * 64, extraccion_estado="hecha"))  # fmt: skip
        s.commit()
        cat.procesar_catalogo(s, [16, 5], inicial=True)
    token = _token(engine)
    r = client.get("/api/anexos/pendientes", headers={"X-Anexos-Token": token})
    assert r.status_code == 200
    pend = r.json()["pendientes"]
    assert [p["codigo"] for p in pend] == ["3-1-COT26"]  # ni otra región ni la que ya tiene anexo
    assert pend[0]["documentos"] == [{"nombre": "Anexo 1.pdf"}]
    assert set(pend[0]) == {"codigo", "nombre", "fecha_cierre", "documentos"}
    assert r.json()["fuente"] == "Dirección ChileCompra"


def test_admin_genera_token_una_vez_y_lo_revoca(client, settings, mundo, engine):
    with Session(engine) as s:
        admin = _usuario(s, "admin@t.cl", rol=RolUsuario.ADMIN)
        eid = s.execute(select(Empresa.id).where(Empresa.nombre == "E1")).scalar_one()
        s.commit()
    c, h = _sesion(settings, admin)
    r = client.post(f"/admin/empresas/{eid}/token-anexos", data={"accion": "generar"},
                    cookies=c, headers=h)  # fmt: skip
    assert r.status_code == 200 and "no se vuelve a mostrar" in r.text
    with Session(engine) as s:
        guardado = s.get(Empresa, eid).token_anexos_hash
    m = re.search(r'text-break">([^<]+)</code>', r.text)
    assert m is not None
    token = m.group(1).strip()  # el en claro solo está en esta respuesta
    assert hashlib.sha256(token.encode()).hexdigest() == guardado
    assert r.headers.get("Cache-Control") == "no-store"
    pagina = client.get(f"/admin/empresas/{eid}", cookies=c).text
    assert "activo" in pagina and token not in pagina
    r = client.post(f"/admin/empresas/{eid}/token-anexos", data={"accion": "revocar"},
                    cookies=c, headers=h, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303
    with Session(engine) as s:
        assert s.get(Empresa, eid).token_anexos_hash is None
    # Un usuario sin rol admin no puede.
    c2, h2 = _sesion(settings, mundo["con"])
    r = client.post(f"/admin/empresas/{eid}/token-anexos", data={"accion": "generar"},
                    cookies=c2, headers=h2, follow_redirects=False)  # fmt: skip
    assert r.status_code in (303, 403)


# ---------------------------------------------------------------------------
# §2.4 Cola: vigentes primero, muestra de históricas
# ---------------------------------------------------------------------------


def test_cola_vigentes_y_muestra_de_historicas(client, settings, mundo, engine, monkeypatch):
    monkeypatch.setattr(cat, "MUESTRA_HISTORICAS", 2)
    with Session(engine) as s:
        for i, monto in enumerate((100.0, 300.0, 200.0), start=1):
            _ca(s, f"8{i}-1-COT26", productos=_ANEXO, monto=monto)
        _ca(s, "90-1-COT26", productos=_ANEXO, fecha_cierre=ahora_utc() + timedelta(days=1))
        s.commit()
        cat.procesar_catalogo(s, [16], inicial=True)
        conteo = cat.conteo_por_revisar(s, [16], ahora_utc())
    # 1-1-COT26 (de `mundo`, monto 400.000) y 82 (300) entran a la muestra; 83 y 81 no.
    assert conteo == {"vigentes": 1, "historicas_muestra": 2, "historicas_resto_cas": 2}
    c, _ = _sesion(settings, mundo["con"])
    r = client.get("/catalogo?tab=pendientes", cookies=c)
    assert "90-1-COT26" in r.text and "82-1-COT26" not in r.text and "Vigentes (1)" in r.text
    r = client.get("/catalogo?tab=pendientes&bloque=historicas", cookies=c)
    assert "82-1-COT26" in r.text and "1-1-COT26" in r.text and "81-1-COT26" not in r.text
    assert "2 Compra(s) Ágil(es) cerrada(s) más" in r.text
    assert "buscador.mercadopublico.cl/ficha?code=82-1-COT26" in r.text
    r = client.get("/", cookies=c)
    assert "vigentes: 1 · históricas (muestra): 2" in r.text


# ---------------------------------------------------------------------------
# §2.6 Modelo de respaldo
# ---------------------------------------------------------------------------


@pytest.fixture()
def con_respaldo(engine):
    s = _settings(
        openrouter_api_key="k" * 20, llm_modelo_extraccion="prueba/vision",
        llm_modelo_respaldo="prueba/respaldo",
    )  # fmt: skip
    c = orc.OpenRouterClient(s, engine, dormir=_sin_espera)
    c._limiter = RateLimiter(1000.0)
    return c


def _por_modelo(fallo: httpx.Response):
    def _responder(request: httpx.Request) -> httpx.Response:
        modelo = json.loads(request.content)["model"]
        if modelo == "prueba/vision":
            return fallo
        r = _respuesta(_DOS_LINEAS)
        cuerpo = r.json() | {"model": "prueba/respaldo"}
        return httpx.Response(200, json=cuerpo)

    return _responder


@respx.mock
def test_respaldo_responde_si_el_principal_da_500(con_respaldo, session):
    respx.post(_URL_OR).mock(side_effect=_por_modelo(httpx.Response(500, text="caído")))
    res = con_respaldo.extraer_lineas("texto del anexo")
    assert res.respaldo and res.modelo == "prueba/respaldo" and len(res.lineas) == 2
    ca = _caso_d(session)
    r = anexos.procesar_anexo_ca(session, ca, _xlsx_con("x"), "a.xlsx", usuario_id=None,
                                 extractor=con_respaldo)  # fmt: skip
    session.commit()
    assert r.anexo.extraccion_respaldo is True and r.anexo.extraccion_modelo == "prueba/respaldo"


@respx.mock
def test_respaldo_si_el_principal_no_devuelve_json(con_respaldo):
    malo = httpx.Response(200, json={"model": "prueba/vision", "choices": [
        {"message": {"content": "no es json"}}], "usage": {"cost": 0.001}})  # fmt: skip
    respx.post(_URL_OR).mock(side_effect=_por_modelo(malo))
    res = con_respaldo.extraer_lineas("otro texto")
    assert res.respaldo and not res.invalida and res.costo_usd == pytest.approx(0.003)
    assert respx.calls.call_count == 3  # principal + su reintento + respaldo


@respx.mock
def test_sin_respaldo_el_error_sigue_igual(cliente):
    respx.post(_URL_OR).mock(return_value=httpx.Response(500, text="caído"))
    with pytest.raises(orc.LLMError):
        cliente.extraer_lineas("texto")
    assert cliente.modelo_respaldo == ""


def test_salud_cuenta_anexos_por_modelo_y_no_mezcla_la_cuota(session, settings):
    from app.api.salud_data import _anexos_salud

    _ca(session, "1-1-COT26", productos=_ANEXO)
    for i, (modelo, respaldo) in enumerate([("m1", False), ("m2", True), ("m2", True)]):
        session.add(CaAnexo(ca_codigo="1-1-COT26", nombre_original="x", mime=anexos.PDF,
                            tamano=1, sha256=str(i) * 64, extraccion_estado="hecha",
                            extraccion_modelo=modelo, extraccion_respaldo=respaldo,
                            fuente="masiva"))  # fmt: skip
    session.add(LlmUso(mes=orc._mes_chile(), requests=3, tokens_in=1, tokens_out=1,
                       costo_usd=0.5, estimado=False))  # fmt: skip
    session.commit()
    ax = _anexos_salud(session, settings)
    assert ax["respaldo_total"] == 2 and ax["por_fuente"] == {"masiva": 3}
    assert {m["modelo"]: m["anexos"] for m in ax["por_modelo"]} == {"m1": 1, "m2": 2}
    assert ax["gasto_mes_usd"] == pytest.approx(0.5)


# §2.7 Descarga directa: desde F-plat-anexos-auto es el job `mercado-anexos`; sus tests viven en
# tests/test_plat_anexos_auto.py.


def test_documentos_del_detalle_se_guardan():
    from types import SimpleNamespace

    from app.clients.types import DocumentoCA
    from app.ingest.mercado import documentos_json

    det = SimpleNamespace(documentos=[DocumentoCA(id="d1", nombre="Anexo.pdf")])
    assert documentos_json(det) == [{"id": "d1", "nombre": "Anexo.pdf"}]  # type: ignore[arg-type]
