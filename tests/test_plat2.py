"""F-plat-2: anexos de Compra Ágil → líneas con extracción asistida (OpenRouter) y revisión humana.

Red siempre mockeada (respx). Anexos sintéticos: XLSX y DOCX generados acá, y dos PDF de fixture
sin datos reales (`tests/fixtures/anexos/`: con texto y solo imagen).
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import LargeBinary, create_engine, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.auth.session import COOKIE_NAME, create_session_token, decode_session_token
from app.clients import openrouter as orc
from app.clients.base import BaseClient, QuotaTracker, RateLimiter
from app.clients.mp_v2 import _parse_ca_detalle
from app.clients.types import DocumentoCA, LineaExtraida
from app.core.logging import _SecretFilter
from app.core.settings import Settings
from app.mercado import anexos
from app.mercado import catalogo as cat
from app.models.base import Base
from app.models.enums import RolUsuario
from app.models.tables import (
    CaAnexo,
    CaLinea,
    CaProducto,
    CompraAgil,
    CostoHistorial,
    Empresa,
    LlmCache,
    LlmUso,
    ProductoCanonico,
    Proveedor,
    ProveedorProducto,
    Usuario,
)

FIXTURES = Path(__file__).parent / "fixtures" / "anexos"
_URL_OR = "https://openrouter.ai/api/v1/chat/completions"
_KEY = "sk-or-v1-clave-de-prueba-0123456789abcdef"
_PW = "contraseña-segura-test"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    yield e


def _settings(**extra: Any) -> Settings:
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
        **extra,
    )


@pytest.fixture()
def settings():
    return _settings(openrouter_api_key=_KEY, llm_modelo_extraccion="prueba/vision")


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


def _sin_espera(_s: float) -> None:
    return None


@pytest.fixture()
def cliente(settings, engine):
    c = orc.OpenRouterClient(settings, engine, dormir=_sin_espera)
    c._limiter = RateLimiter(1000.0)  # sin esperas de 1 s entre requests en los tests
    return c


def _respuesta(lineas: list[dict[str, Any]] | str, costo: float | None = 0.001) -> httpx.Response:
    contenido = lineas if isinstance(lineas, str) else json.dumps({"lineas": lineas})
    usage: dict[str, Any] = {"prompt_tokens": 1200, "completion_tokens": 300}
    if costo is not None:
        usage["cost"] = costo
    return httpx.Response(
        200,
        json={
            "model": "prueba/vision",
            "choices": [{"message": {"content": contenido}}],
            "usage": usage,
        },
    )


_DOS_LINEAS = [
    {"texto": "Arroz grado 1", "cantidad": 20, "unidad": "bolsa", "formato": "bolsa 1 kg"},
    {"texto": "Té negro", "cantidad": 5, "unidad": "caja", "formato": "caja 100 bolsitas"},
]


def _ca(session, codigo="1-1-COT26", productos=(), region=16, monto=400000.0, **kw):
    session.add(
        CompraAgil(
            codigo=codigo, nombre="CA de prueba", region=region, monto_disponible_clp=monto, **kw
        )
    )
    session.flush()
    for cod, nom, desc, cant, uni in productos:
        session.add(
            CaProducto(
                ca_codigo=codigo, codigo_producto=cod, nombre=nom, descripcion=desc,
                cantidad=cant, unidad=uni,
            )
        )  # fmt: skip
    session.flush()
    return session.get(CompraAgil, codigo)


_ANEXO = [("50192100", "Alimentos", "ALIMENTOS SEGUN ANEXO ADJUNTO", 1, "Unidad")]


def _lineas(session, codigo="1-1-COT26"):
    return list(
        session.execute(
            select(CaLinea).where(CaLinea.ca_codigo == codigo).order_by(CaLinea.n)
        ).scalars()
    )


def _xlsx() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "pedido"
    ws.append(["Producto", "Cantidad", "Unidad"])
    ws.append(["Arroz 1 kg", 20, "bolsa"])
    ws.append([None, None, None])
    ws.append(["Aceite 1 L", 12.0, "unidad"])
    ws2 = wb.create_sheet("otra")
    ws2.append(["Azúcar 1 kg", 8, "bolsa"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _docx() -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph("Pedido de alimentos de prueba")
    t = d.add_table(rows=2, cols=3)
    for i, v in enumerate(["Producto", "Cantidad", "Unidad"]):
        t.cell(0, i).text = v
    for i, v in enumerate(["Harina 1 kg", "10", "bolsa"]):
        t.cell(1, i).text = v
    d.add_paragraph("Entrega en la escuela")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Secreto
# ---------------------------------------------------------------------------


def test_openrouter_api_key_se_enmascara_y_no_sale_en_repr(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", _KEY)
    f = _SecretFilter()
    rec = logging.LogRecord("x", logging.INFO, "f", 1, "clave=%s fin", (_KEY,), None)
    f.filter(rec)
    assert _KEY not in rec.getMessage() and "***" in rec.getMessage()
    s = _settings(openrouter_api_key=_KEY)
    assert _KEY not in repr(s)


@respx.mock
def test_la_key_solo_viaja_en_el_header(cliente):
    ruta = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    cliente.extraer_lineas("texto", [])
    req = ruta.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {_KEY}"
    assert _KEY not in str(req.url) and _KEY.encode() not in req.content


# ---------------------------------------------------------------------------
# Cliente OpenRouter: caché, presupuesto, respuesta inválida, 429
# ---------------------------------------------------------------------------


@respx.mock
def test_extraer_parsea_registra_uso_y_cachea(cliente, engine):
    ruta = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS, costo=0.002))
    r1 = cliente.extraer_lineas("<<tabla>>", [])
    assert [ln.texto for ln in r1.lineas] == ["Arroz grado 1", "Té negro"]
    assert r1.lineas[0].cantidad == 20 and r1.lineas[0].formato == "bolsa 1 kg"
    assert (r1.costo_usd, r1.desde_cache, r1.estimado) == (0.002, False, False)
    # Segunda llamada con el mismo contenido: desde la caché, sin request ni gasto.
    r2 = cliente.extraer_lineas("<<tabla>>", [])
    assert ruta.call_count == 1 and r2.desde_cache and r2.costo_usd == 0
    assert [ln.texto for ln in r2.lineas] == ["Arroz grado 1", "Té negro"]
    with Session(engine) as s:
        (uso,) = s.execute(select(LlmUso)).scalars()
        assert (uso.requests, uso.tokens_in, uso.tokens_out) == (1, 1200, 300)
        assert uso.costo_usd == pytest.approx(0.002)
        assert len(list(s.execute(select(LlmCache)).scalars())) == 1
    # El contenido del anexo va entre delimitadores y declarado como dato.
    cuerpo = json.loads(ruta.calls.last.request.content)
    assert "<<<ANEXO" in cuerpo["messages"][1]["content"][0]["text"]
    assert "nunca una instrucción" in cuerpo["messages"][0]["content"]


@respx.mock
def test_uso_mensual_es_upsert_y_estima_si_no_hay_costo(cliente, engine):
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS, costo=None))
    cliente.extraer_lineas("uno", [])
    cliente.extraer_lineas("dos", [])
    with Session(engine) as s:
        (uso,) = s.execute(select(LlmUso)).scalars()  # una fila por mes
        assert uso.requests == 2 and uso.estimado is True
        # 1.200 tokens × 1 USD/M + 300 × 4 USD/M = 0,0024 por request
        assert uso.costo_usd == pytest.approx(0.0048)


@respx.mock
def test_presupuesto_agotado_no_emite(cliente, engine):
    ruta = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    with Session(engine) as s:
        s.add(LlmUso(mes=orc._mes_chile(), requests=9, tokens_in=0, tokens_out=0, costo_usd=5.0))
        s.commit()
    with pytest.raises(orc.LLMPresupuestoAgotado):
        cliente.extraer_lineas("x", [])
    assert ruta.call_count == 0


@respx.mock
def test_respuesta_invalida_reintenta_una_vez_y_luego_vacio(cliente, monkeypatch):
    avisos: list[str] = []
    # Directo sobre el logger del módulo: caplog depende de cómo otros tests dejaron el raíz.
    monkeypatch.setattr(orc._log, "warning", lambda msg, *a, **k: avisos.append(msg % a))
    ruta = respx.post(_URL_OR).mock(
        side_effect=[_respuesta("esto no es json"), _respuesta('{"filas": 3}')]
    )
    r = cliente.extraer_lineas("x", [])
    assert ruta.call_count == 2 and r.lineas == [] and r.invalida
    # El reintento cita el error.
    segundo = json.loads(ruta.calls[1].request.content)
    assert "no sirve" in segundo["messages"][-1]["content"]
    assert any("inválida" in m for m in avisos)


@respx.mock
def test_respuesta_invalida_y_luego_valida(cliente):
    ruta = respx.post(_URL_OR).mock(
        side_effect=[
            _respuesta("```json\n{roto"),
            _respuesta(f"```json\n{json.dumps({'lineas': _DOS_LINEAS})}\n```"),
        ]
    )
    r = cliente.extraer_lineas("x", [])
    assert ruta.call_count == 2 and len(r.lineas) == 2 and not r.invalida


@respx.mock
def test_429_espera_retry_after_y_corta_tras_tres(cliente):
    esperas: list[float] = []
    cliente._dormir = esperas.append
    respx.post(_URL_OR).mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "7"}), _respuesta(_DOS_LINEAS)]
    )
    assert len(cliente.extraer_lineas("x", []).lineas) == 2
    assert esperas == [7.0]
    respx.post(_URL_OR).mock(return_value=httpx.Response(429))
    esperas.clear()
    with pytest.raises(orc.LLMError):
        cliente.extraer_lineas("otro", [])
    assert esperas == [30.0, 30.0, 30.0]


def test_clave_de_cache_distinta_para_dos_escaneados_distintos():
    a = orc.clave_cache("m", "lineas", "", [b"imagen-uno"])
    b = orc.clave_cache("m", "lineas", "", [b"imagen-dos"])
    assert a != b
    assert a == orc.clave_cache("m", "lineas", None, [b"imagen-uno"])
    assert a != orc.clave_cache("otro-modelo", "lineas", "", [b"imagen-uno"])
    assert a != orc.clave_cache("m", "precios", "", [b"imagen-uno"])


def test_sin_key_o_sin_modelo_el_cliente_esta_apagado(engine):
    assert not orc.OpenRouterClient(_settings(llm_modelo_extraccion="m"), engine).habilitado
    assert not orc.OpenRouterClient(_settings(openrouter_api_key=_KEY), engine).habilitado


# ---------------------------------------------------------------------------
# Archivo: firma, tamaño y texto por tipo
# ---------------------------------------------------------------------------


def test_firma_del_archivo_no_la_extension():
    assert anexos.detectar_mime((FIXTURES / "texto.pdf").read_bytes()) == anexos.PDF
    assert anexos.detectar_mime(_xlsx()) == anexos.XLSX
    assert anexos.detectar_mime(_docx()) == anexos.DOCX
    assert anexos.detectar_mime(b"\x89PNG\r\n\x1a\n...") == anexos.PNG
    assert anexos.detectar_mime(b"\xff\xd8\xff\xe0...") == anexos.JPEG
    exe = b"MZ\x90\x00" + b"\x00" * 100  # un .exe renombrado .pdf
    with pytest.raises(anexos.AnexoInvalido):
        anexos.validar_archivo(exe)
    with pytest.raises(anexos.AnexoInvalido, match="vacío"):
        anexos.validar_archivo(b"")
    with pytest.raises(anexos.AnexoInvalido, match="10 MB"):
        anexos.validar_archivo(b"%PDF-" + b"0" * anexos.MAX_BYTES)
    assert anexos.detectar_mime(b"PK\x03\x04basura") is None  # zip roto
    assert anexos.detectar_mime(b"\xd0\xcf\x11\xe0") is None  # .xls viejo: se pide .xlsx


def test_texto_de_xlsx_docx_y_pdf_con_texto():
    x = anexos.extraer_texto(_xlsx(), anexos.XLSX)
    assert "Arroz 1 kg | 20 | bolsa" in x.texto and "Aceite 1 L | 12 | unidad" in x.texto
    assert "[hoja otra]" in x.texto and x.error is None
    d = anexos.extraer_texto(_docx(), anexos.DOCX)
    lineas = d.texto.splitlines()
    assert lineas[0] == "Pedido de alimentos de prueba"
    assert "Harina 1 kg | 10 | bolsa" in lineas and lineas[-1] == "Entrega en la escuela"
    p = anexos.extraer_texto((FIXTURES / "texto.pdf").read_bytes(), anexos.PDF)
    assert p.paginas == 1 and not p.escaneado and not p.imagenes
    assert "Arroz grado 1 bolsa 1 kg | 20 | bolsa" in p.texto


def test_pdf_escaneado_va_como_imagen_con_render_mockeado():
    llamadas: list[Any] = []

    def render(pagina: Any) -> bytes:
        llamadas.append(pagina)
        return b"\x89PNG-falso"

    r = anexos.extraer_texto((FIXTURES / "escaneado.pdf").read_bytes(), anexos.PDF, render=render)
    assert r.escaneado and r.paginas == 1 and r.imagenes == [(b"\x89PNG-falso", anexos.PNG)]
    assert len(llamadas) == 1 and r.paginas_omitidas == 0
    # Con tope 0 de páginas no se envía ninguna: error claro, sin excepción.
    r0 = anexos.extraer_texto(
        (FIXTURES / "escaneado.pdf").read_bytes(), anexos.PDF, max_paginas_imagen=0, render=render
    )
    assert r0.error and not r0.imagenes


def test_pdf_escaneado_render_real():
    r = anexos.extraer_texto((FIXTURES / "escaneado.pdf").read_bytes(), anexos.PDF)
    assert r.escaneado and r.imagenes[0][0].startswith(b"\x89PNG")


# --- Guardas de memoria (Render: 512 MB) ---------------------------------------------


def _zip_docx(miembros: dict[str, bytes], metodo: int = 8) -> bytes:
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=metodo) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        for nombre, contenido in miembros.items():
            z.writestr(nombre, contenido)
    return buf.getvalue()


def test_zip_que_se_expande_mas_alla_del_tope_se_rechaza():
    bomba = _zip_docx({"word/document.xml": b"\0" * (60 * 1024 * 1024)})
    assert len(bomba) < anexos.MAX_BYTES  # pasa el tope de 10 MB: solo lo para la guarda
    with pytest.raises(anexos.AnexoInvalido, match="50 MB"):
        anexos.validar_archivo(bomba)
    # Quien llama sin validar (scripts) recibe un error, no la expansión.
    r = anexos.extraer_texto(bomba, anexos.DOCX)
    assert r.error and "50 MB" in r.error


def test_zip_con_razon_de_compresion_sospechosa_se_rechaza():
    raro = _zip_docx({"xl/sheet1.xml": b"\0" * (2 * 1024 * 1024)})  # ~1.000:1, solo 2 MB
    with pytest.raises(anexos.AnexoInvalido, match="sospechoso"):
        anexos.validar_archivo(raro)


def test_tope_total_del_zip_sin_compresion(monkeypatch):
    import os
    import zipfile

    monkeypatch.setattr(anexos, "MAX_ZIP_EXPANDIDO", 1024 * 1024)
    grande = _zip_docx({"word/document.xml": os.urandom(2 * 1024 * 1024)}, zipfile.ZIP_STORED)
    with pytest.raises(anexos.AnexoInvalido, match="expande"):
        anexos.validar_archivo(grande)


def test_docx_y_xlsx_normales_pasan_las_guardas():
    assert anexos.validar_archivo(_docx()) == anexos.DOCX
    assert anexos.validar_archivo(_xlsx()) == anexos.XLSX


def test_dpi_de_render_acota_el_lado_mayor():
    assert anexos.dpi_render(612, 792) == 150  # carta: 1.650 px a 150 dpi, bajo el tope
    assert anexos.dpi_render(14400, 14400) == pytest.approx(10.0)  # 200 pulgadas → 2.000 px
    assert anexos.dpi_render(0, 0) == 150


def _pdf_mediabox_enorme() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    # 400 px a 2 dpi = 200 pulgadas = 14.400 pt por lado (a 150 dpi serían 30.000 px).
    Image.new("RGB", (400, 400), "white").save(buf, format="PDF", resolution=2)
    return buf.getvalue()


def test_pagina_con_mediabox_enorme_no_pasa_de_2000_px():
    from PIL import Image

    data = _pdf_mediabox_enorme()
    r = anexos.extraer_texto(data, anexos.PDF)  # camino pdfplumber, render real
    assert r.escaneado and r.imagenes
    assert max(Image.open(io.BytesIO(r.imagenes[0][0])).size) <= anexos.MAX_LADO_PX
    r2 = anexos._pdf_con_pdfium(data, 5, "x")  # camino de respaldo
    assert r2.imagenes
    assert max(Image.open(io.BytesIO(r2.imagenes[0][0])).size) <= anexos.MAX_LADO_PX


def test_pdf_malformado_es_error_sin_excepcion():
    r = anexos.extraer_texto(b"%PDF-1.4\nesto no es un pdf\n%%EOF", anexos.PDF)
    assert r.error and not r.hay_contenido


def test_imagen_suelta_va_al_modelo():
    r = anexos.extraer_texto(b"\xff\xd8\xff\xe0jpeg", anexos.JPEG)
    assert r.imagenes == [(b"\xff\xd8\xff\xe0jpeg", anexos.JPEG)] and r.escaneado


def test_ninguna_columna_guarda_el_binario():
    cols = CaAnexo.__table__.columns
    assert not any(isinstance(c.type, LargeBinary) for c in cols)
    assert not {"archivo", "contenido", "binario", "datos"} & {c.name for c in cols}


def test_documentos_del_detalle_se_parsean_defensivo():
    det = _parse_ca_detalle(
        {
            "codigo": "1-1-COT26",
            "documentos": [{"id": 77, "nombre": "anexo.pdf"}, {"nombre": "sin id"}, "x"],
        }
    )
    assert det.n_documentos == 3
    assert det.documentos == [DocumentoCA(id="77", nombre="anexo.pdf")]


# ---------------------------------------------------------------------------
# Servicio: anexo → líneas dudosa
# ---------------------------------------------------------------------------


def _con_placeholder(session):
    cat.sembrar_canasta(session)
    ca = _ca(
        session,
        productos=[
            ("50181901", "Pan", "pan batido de campo", 10, "Kilogramo"),
            *_ANEXO,
        ],
        n_documentos=1,
    )
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    pan, placeholder = _lineas(session)
    cat.confirmar_linea(
        session,
        pan,
        producto_id=session.execute(
            select(ProductoCanonico.id).where(ProductoCanonico.nombre == "Hallulla")
        ).scalar_one(),
        formato_id=None,
        usuario_id=1,
    )
    session.commit()
    assert (placeholder.origen, placeholder.estado) == ("anexo", "pendiente")
    session.refresh(ca)
    assert ca.detalle_lineas == "sin_detalle"
    return ca


@respx.mock
def test_anexo_reemplaza_placeholder_no_pisa_ok_y_deja_dudosa(session, cliente):
    ca = _con_placeholder(session)
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    res = anexos.procesar_anexo_ca(
        session, ca, (FIXTURES / "texto.pdf").read_bytes(), "anexo.pdf",
        usuario_id=None, extractor=cliente,
    )  # fmt: skip
    session.commit()
    assert res.anexo.extraccion_estado == "hecha" and len(res.lineas) == 2
    assert res.anexo.texto_extraido and res.anexo.sha256 and res.anexo.mime == anexos.PDF
    lineas = _lineas(session)
    ok, *nuevas = lineas
    assert (ok.estado, ok.origen, ok.n) == ("ok", "api", 1)  # la confirmada no se toca
    assert [(ln.origen, ln.estado) for ln in nuevas] == [("anexo", "dudosa")] * 2
    assert all(ln.anexo_id == res.anexo.id and ln.n >= cat.N_MANUAL_DESDE for ln in nuevas)
    assert nuevas[0].texto_origen == "Arroz grado 1 bolsa 1 kg"  # el formato va en el texto
    assert nuevas[0].producto_id is not None  # sugerencia de las reglas: igual queda dudosa
    session.refresh(ca)
    assert ca.detalle_lineas == "incompleto" and ca.anexo_resuelto_en is not None

    # El reproceso del catálogo no recrea el placeholder ni borra las líneas del anexo.
    cat.procesar_catalogo(session, [16], inicial=True)
    session.expire_all()
    assert [(ln.origen, ln.estado) for ln in _lineas(session)] == [
        ("api", "ok"), ("anexo", "dudosa"), ("anexo", "dudosa"),
    ]  # fmt: skip

    # Confirmar una línea del anexo: corrige la cantidad y sigue siendo de origen anexo.
    arroz = _lineas(session)[1]
    cat.confirmar_linea(
        session, arroz, producto_id=arroz.producto_id, formato_id=None, usuario_id=1, cantidad=25
    )
    session.commit()
    assert (arroz.estado, arroz.origen, arroz.cantidad) == ("ok", "anexo", 25)
    for ln in _lineas(session)[2:]:
        cat.quitar_linea_manual(session, ln)  # «Eliminar fila»
    session.commit()
    session.refresh(ca)
    assert ca.detalle_lineas == "completo"


def test_cantidad_solo_se_corrige_en_lineas_de_anexo(session):
    ca = _con_placeholder(session)
    pan = _lineas(session)[0]
    with pytest.raises(cat.CatalogoError):
        cat.confirmar_linea(
            session, pan, producto_id=pan.producto_id, formato_id=None, usuario_id=1, cantidad=3
        )
    assert ca is not None


def test_sin_modelo_queda_sin_presupuesto_y_no_toca_lineas(session):
    ca = _con_placeholder(session)
    res = anexos.procesar_anexo_ca(
        session, ca, _xlsx(), "pedido.xlsx", usuario_id=None, extractor=None
    )
    session.commit()
    assert res.anexo.extraccion_estado == "sin_presupuesto" and res.lineas == []
    assert "a mano" in res.aviso
    assert [ln.estado for ln in _lineas(session)] == ["ok", "pendiente"]  # placeholder intacto
    session.refresh(ca)
    assert ca.detalle_lineas == "sin_detalle" and ca.anexo_resuelto_en is None


@respx.mock
def test_presupuesto_agotado_en_la_subida_es_manual(session, cliente, engine):
    ca = _con_placeholder(session)
    session.add(LlmUso(mes=orc._mes_chile(), requests=1, tokens_in=0, tokens_out=0, costo_usd=9))
    session.commit()
    ruta = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    res = anexos.procesar_anexo_ca(
        session, ca, _xlsx(), "p.xlsx", usuario_id=None, extractor=cliente
    )
    assert res.anexo.extraccion_estado == "sin_presupuesto" and ruta.call_count == 0


def test_pdf_malformado_en_la_subida_es_error(session):
    ca = _con_placeholder(session)
    res = anexos.procesar_anexo_ca(
        session, ca, b"%PDF-1.4\nroto", "roto.pdf", usuario_id=None, extractor=None
    )
    assert res.anexo.extraccion_estado == "error" and "a mano" in res.aviso


@respx.mock
def test_respuesta_invalida_en_la_subida_es_error_y_no_borra_placeholder(session, cliente):
    ca = _con_placeholder(session)
    respx.post(_URL_OR).mock(return_value=_respuesta("nada"))
    res = anexos.procesar_anexo_ca(
        session, ca, _xlsx(), "p.xlsx", usuario_id=None, extractor=cliente
    )
    assert res.anexo.extraccion_estado == "error"
    assert [ln.estado for ln in _lineas(session)] == ["ok", "pendiente"]


def test_anexo_resuelto_borra_placeholders_y_el_job_no_los_recrea(session):
    ca = _con_placeholder(session)
    cat.agregar_linea_manual(
        session, ca, producto_id=_lineas(session)[0].producto_id, formato_id=None,
        cantidad=3, usuario_id=1,
    )  # fmt: skip
    session.commit()
    session.refresh(ca)
    assert ca.detalle_lineas == "sin_detalle"  # una línea manual no resuelve el anexo sola
    antes = ca.actualizado_en
    assert anexos.resolver_anexo(session, ca) == 1
    session.commit()
    session.refresh(ca)
    assert ca.detalle_lineas == "completo" and ca.anexo_resuelto_en is not None
    assert ca.actualizado_en == antes  # resolver el anexo no cuenta como «la CA cambió»
    cat.procesar_catalogo(session, [16], inicial=True)
    session.expire_all()
    assert [ln.origen for ln in _lineas(session)] == ["api", "manual"]


def test_bandera_solo_el_placeholder_es_sin_detalle(session):
    ca = _ca(session)
    session.add_all(
        [
            CaAnexo(
                ca_codigo=ca.codigo,
                mime=anexos.PDF,
                tamano=1,
                sha256="x",
                extraccion_estado="hecha",
            ),
        ]
    )
    session.flush()
    anexo_id = session.execute(select(CaAnexo.id)).scalar_one()
    session.add(
        CaLinea(ca_codigo=ca.codigo, n=1000, texto_origen="arroz", texto_norm="arroz",
                origen="anexo", estado="dudosa", anexo_id=anexo_id)
    )  # fmt: skip
    session.flush()
    cat.recalcular_detalle_lineas(session, [ca.codigo])
    session.refresh(ca)
    assert ca.detalle_lineas == "incompleto"  # «líneas por revisar», no «sin detalle»
    session.add(
        CaLinea(ca_codigo=ca.codigo, n=1, texto_origen="segun anexo", texto_norm="segun anexo",
                origen="anexo", estado="pendiente")
    )  # fmt: skip
    session.flush()
    cat.recalcular_detalle_lineas(session, [ca.codigo])
    session.refresh(ca)
    assert ca.detalle_lineas == "sin_detalle"


# ---------------------------------------------------------------------------
# BaseClient: descarga binaria
# ---------------------------------------------------------------------------


@respx.mock
def test_request_bytes_cuenta_cuota_y_404_es_none(engine):
    q = QuotaTracker(engine, 100)
    c = BaseClient(ticket="T", rate_limiter=RateLimiter(1000.0), quota=q)
    url = "https://api2.mercadopublico.cl/v2/compra-agil/X/documentos/1"
    respx.get(url).mock(
        side_effect=[httpx.Response(200, content=b"%PDF-1.4 x"), httpx.Response(404, json={})]
    )
    assert c._request_bytes("GET", url) == b"%PDF-1.4 x"
    assert c._request_bytes("GET", url) is None
    assert q.remaining() == 98


# ---------------------------------------------------------------------------
# §1.6: descarga desde la API, detrás de `documentos_api_habilitado`
# ---------------------------------------------------------------------------


class _V2Falso:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.detalles: list[str] = []
        self.descargas: list[tuple[str, str]] = []

    def detalle_compra_agil(self, codigo: str, **_kw: Any) -> Any:
        self.detalles.append(codigo)

        class _D:
            documentos = [DocumentoCA(id="d1", nombre="pedido.xlsx")]

        return _D()

    def descargar_documento(self, codigo: str, documento_id: str) -> bytes | None:
        self.descargas.append((codigo, documento_id))
        return self.data


def _ca_vigente_con_anexo(session, codigo="1-1-COT26"):
    cat.sembrar_canasta(session)
    _ca(
        session, codigo, productos=_ANEXO, n_documentos=1, estado="publicada",
        fecha_cierre=datetime.now() + timedelta(days=2),
    )  # fmt: skip
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)


@respx.mock
def test_bajar_anexos_procesa_y_no_repite(session, engine, settings):
    from app.ingest.mercado import bajar_anexos, cola_anexos

    _ca_vigente_con_anexo(session)
    _ca(session, "2-1-COT26", productos=[("44121500", "Lápiz", "lápiz", 5, "u")], n_documentos=2,
        estado="publicada", fecha_cierre=datetime.now() + timedelta(days=2))  # fmt: skip
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)
    # Solo la CA con el placeholder entra (la de lápices es `sin_detalle` sin alimentos).
    assert cola_anexos(session, [16], datetime.now(), 10) == ["1-1-COT26"]
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    v2 = _V2Falso(_xlsx())
    resultado: dict[str, Any] = {}
    bajar_anexos(settings, engine, v2, [16], resultado)  # type: ignore[arg-type]
    assert resultado["anexos_bajados"] == 1 and resultado["anexos_procesados"] == 1
    assert v2.detalles == ["1-1-COT26"] and v2.descargas == [("1-1-COT26", "d1")]
    session.expire_all()
    (a,) = session.execute(select(CaAnexo)).scalars()
    assert a.subido_por is None and a.extraccion_estado == "hecha"
    # Ya tiene anexo: la corrida siguiente no lo vuelve a bajar.
    assert cola_anexos(session, [16], datetime.now(), 10) == []
    bajar_anexos(settings, engine, v2, [16], {})  # type: ignore[arg-type]
    assert len(v2.descargas) == 1


def test_bajar_anexos_sin_llm_no_pide_nada(session, engine):
    from app.ingest.mercado import bajar_anexos

    _ca_vigente_con_anexo(session)
    v2 = _V2Falso(_xlsx())
    resultado: dict[str, Any] = {}
    bajar_anexos(_settings(), engine, v2, [16], resultado)  # type: ignore[arg-type]
    assert resultado == {"anexos_omitidos": "sin_llm"} and not v2.descargas and not v2.detalles


def test_archivo_rechazado_queda_registrado(session, engine, settings):
    from app.ingest.mercado import bajar_anexos, cola_anexos

    _ca_vigente_con_anexo(session)
    v2 = _V2Falso(b"MZ ejecutable")
    bajar_anexos(settings, engine, v2, [16], {})  # type: ignore[arg-type]
    session.expire_all()
    (a,) = session.execute(select(CaAnexo)).scalars()
    assert a.extraccion_estado == "error" and a.mime == "desconocido"
    assert cola_anexos(session, [16], datetime.now(), 10) == []


@pytest.mark.parametrize("habilitado", [False, True])
def test_mercado_ca_respeta_documentos_api_habilitado(monkeypatch, engine, habilitado):
    from app.ingest import mercado

    llamadas: list[Any] = []
    monkeypatch.setattr(mercado, "_sync_en_ventanas", lambda *a, **k: {"ventanas": 0})
    monkeypatch.setattr(mercado, "cola_vigentes", lambda *a, **k: [])
    monkeypatch.setattr(mercado, "cola_mercado_ca", lambda *a, **k: [])
    monkeypatch.setattr(mercado, "bajar_anexos", lambda *a, **k: llamadas.append(a))
    s = _settings(documentos_api_habilitado=habilitado)
    mercado.sync_mercado_ca(s, engine, object())  # type: ignore[arg-type]
    assert bool(llamadas) is habilitado


# ---------------------------------------------------------------------------
# Rutas: subida, confirmar, eliminar, «Anexo resuelto», ownership
# ---------------------------------------------------------------------------


def _usuario(session, email, empresa_id=None, rol=RolUsuario.USUARIO) -> int:
    u = Usuario(
        email=email, password_hash=hash_password(_PW), rol=rol, activo=True, empresa_id=empresa_id
    )
    session.add(u)
    session.flush()
    return u.id


def _sesion(settings, user_id):
    token = create_session_token(settings.secret_key, user_id)
    decoded = decode_session_token(settings.secret_key, token)
    assert decoded is not None
    return {COOKIE_NAME: token}, {
        "X-CSRF-Token": generate_csrf_token(settings.secret_key, decoded[1])
    }


@pytest.fixture()
def client(engine, settings):
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
        p1 = Proveedor(
            empresa_id=e1.id, nombre="Distribuidora Uno", rubros=[], despacha=False, activo=True
        )
        p2 = Proveedor(
            empresa_id=e2.id, nombre="Otra Empresa", rubros=[], despacha=False, activo=True
        )
        s.add_all([p1, p2])
        s.commit()
        cat.procesar_catalogo(s, [16], inicial=True)
        ids["prov"], ids["prov_otra"] = p1.id, p2.id
    return ids


def _subir(
    client, cookies, headers, codigo="1-1-COT26", data=None, nombre="pedido.xlsx", htmx=True
):
    return client.post(
        f"/oportunidad/compras_agiles/{codigo}/anexo",
        files={
            "archivo": (nombre, data if data is not None else _xlsx(), "application/octet-stream")
        },
        cookies=cookies,
        headers={**headers, **({"HX-Request": "true"} if htmx else {})},
        follow_redirects=False,
    )


@respx.mock
def test_subir_por_htmx_devuelve_tabla_editable(client, settings, mundo, engine):
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    c, h = _sesion(settings, mundo["con"])
    r = _subir(client, c, h)
    assert r.status_code == 200
    assert "Arroz grado 1" in r.text and "Confirmar todas" in r.text and "Eliminar fila" in r.text
    assert 'name="cantidad"' in r.text and "Agregar fila" in r.text
    with Session(engine) as s:
        (a,) = s.execute(select(CaAnexo)).scalars()
        assert a.subido_por == mundo["con"] and a.extraccion_estado == "hecha"
        lineas = [ln for ln in _lineas(s) if ln.anexo_id]
        assert len(lineas) == 2 and {ln.estado for ln in lineas} == {"dudosa"}

    # Confirmar fila con cantidad corregida (HTMX: la fila se repinta).
    ln = lineas[0]
    r = client.post(
        f"/catalogo/lineas/{ln.id}/confirmar",
        data={"producto_id": ln.producto_id, "formato_id": "", "cantidad": "21,5", "vista": "anexo"},
        cookies=c, headers={**h, "HX-Request": "true"},
    )  # fmt: skip
    assert r.status_code == 200 and f'id="linea-{ln.id}"' in r.text
    # Eliminar fila: 200 con solo el aviso (nunca 204).
    r = client.post(
        f"/catalogo/lineas/{lineas[1].id}/quitar", cookies=c, headers={**h, "HX-Request": "true"}
    )
    assert r.status_code == 200 and "<tr" not in r.text
    with Session(engine) as s:
        assert s.get(CaLinea, ln.id).cantidad == 21.5
        assert s.get(CaLinea, lineas[1].id) is None
        assert s.get(CompraAgil, "1-1-COT26").detalle_lineas == "completo"


def test_subir_sin_htmx_redirige_y_sin_modelo_es_manual(engine, mundo):
    s = _settings()  # sin key ni modelo
    client = TestClient(create_app(s, engine))
    c, h = _sesion(s, mundo["con"])
    r = _subir(client, c, h, htmx=False)
    assert r.status_code == 303 and "/catalogo/ca/1-1-COT26" in r.headers["location"]
    with Session(engine) as ses:
        (a,) = ses.execute(select(CaAnexo)).scalars()
        assert a.extraccion_estado == "sin_presupuesto"
    pagina = client.get("/catalogo/ca/1-1-COT26", cookies=c)
    assert "Manual (sin lectura automática)" in pagina.text and "Anexo resuelto" in pagina.text


def test_subir_rechaza_firma_y_exige_csrf_empresa_y_region(client, settings, mundo, engine):
    c, h = _sesion(settings, mundo["con"])
    r = _subir(client, c, h, data=b"MZ\x90\x00 binario", nombre="malo.pdf")
    assert r.status_code == 200 and "no aceptado" in r.text
    r = client.post(
        "/oportunidad/compras_agiles/1-1-COT26/anexo",
        files={"archivo": ("a.xlsx", _xlsx())}, cookies=c,
    )  # fmt: skip
    assert r.status_code == 403  # sin CSRF
    assert _subir(client, c, h, codigo="5-1-COT26").status_code == 404  # región no activa
    assert client.post(
        "/oportunidad/licitaciones/1-1-COT26/anexo", files={"archivo": ("a", b"x")},
        cookies=c, headers=h,
    ).status_code == 404  # fmt: skip
    c2, h2 = _sesion(settings, mundo["sin"])
    assert _subir(client, c2, h2).status_code == 403  # sin empresa
    with Session(engine) as s:
        assert s.execute(select(CaAnexo)).first() is None


@respx.mock
def test_confirmar_todas_y_anexo_resuelto(client, settings, mundo, engine):
    respx.post(_URL_OR).mock(
        return_value=_respuesta([*_DOS_LINEAS, {"texto": "zzz producto raro", "cantidad": 1}])
    )
    c, h = _sesion(settings, mundo["con"])
    _subir(client, c, h)
    r = client.post(
        "/catalogo/ca/1-1-COT26/anexo/confirmar-todas", cookies=c, headers=h, follow_redirects=False
    )
    assert r.status_code == 303 and "sin%20producto" in r.headers["location"]
    with Session(engine) as s:
        estados = sorted(ln.estado for ln in _lineas(s) if ln.anexo_id)
        assert estados == ["dudosa", "ok", "ok"]  # la sin producto espera
    # La CA de la otra región no es accesible.
    assert (
        client.post("/catalogo/ca/5-1-COT26/anexo-resuelto", cookies=c, headers=h).status_code
        == 404
    )


def test_anexo_resuelto_por_ruta(client, settings, mundo, engine):
    c, h = _sesion(settings, mundo["con"])
    r = client.post(
        "/catalogo/ca/1-1-COT26/anexo-resuelto", cookies=c, headers=h, follow_redirects=False
    )
    assert r.status_code == 303
    with Session(engine) as s:
        assert _lineas(s) == []
        assert s.get(CompraAgil, "1-1-COT26").anexo_resuelto_en is not None


def test_ficha_ofrece_subir_anexo(client, settings, mundo):
    c, _ = _sesion(settings, mundo["con"])
    r = client.get("/oportunidad/compras_agiles/1-1-COT26", cookies=c)
    assert r.status_code == 200 and "Subir anexo" in r.text and 'name="archivo"' in r.text


def test_subida_de_mas_de_1_mb_llega_y_la_de_mas_de_10_mb_se_rechaza(
    client, settings, mundo, engine
):
    c, h = _sesion(settings, mundo["con"])
    # 3 MB que dicen ser PDF pero no se pueden leer: llega entera (Starlette no corta) y queda error.
    r = _subir(client, c, h, data=b"%PDF-1.4\n" + b"0" * (3 * 1024 * 1024), nombre="grande.pdf")
    assert r.status_code == 200 and "no se pudo leer" in r.text.lower()
    r = _subir(client, c, h, data=b"%PDF-1.4\n" + b"0" * (10 * 1024 * 1024), nombre="enorme.pdf")
    assert r.status_code == 200 and "10 MB" in r.text
    with Session(engine) as s:
        (a,) = s.execute(select(CaAnexo)).scalars()  # solo la de 3 MB, sin el binario
        assert a.tamano > 3 * 1024 * 1024 and a.extraccion_estado == "error"


# ---------------------------------------------------------------------------
# §1.4: lista de precios de proveedor por foto o PDF
# ---------------------------------------------------------------------------


@respx.mock
def test_lista_de_precios_vista_previa_y_confirmacion(client, settings, mundo, engine):
    ruta = respx.post(_URL_OR).mock(
        return_value=_respuesta(
            [
                {"texto": "Arroz grado 1", "formato": "bolsa 1 kg", "precio": 1190},
                {"texto": "Aceite", "formato": "botella 900 ml", "precio": None},
            ]
        )
    )
    c, h = _sesion(settings, mundo["con"])
    r = client.post(
        f"/proveedores/{mundo['prov']}/lista-precios",
        files={"archivo": ("lista.png", b"\x89PNG\r\n\x1a\nfalso")}, cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 200 and "Vista previa" in r.text and "Arroz grado 1" in r.text
    assert "falta el precio" in r.text  # la fila sin precio se informa
    assert (
        json.loads(ruta.calls.last.request.content)["messages"][1]["content"][1]["type"]
        == "image_url"
    )
    with Session(engine) as s:
        assert s.execute(select(ProveedorProducto)).first() is None  # la vista previa no guarda
    r = client.post(
        f"/proveedores/{mundo['prov']}/lista-precios/confirmar",
        data={"producto": ["Arroz grado 1", "Aceite"], "formato": ["bolsa 1 kg", "botella 900 ml"],
              "precio": ["1190", "2490"]},
        cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 200 and "Precios guardados" in r.text
    with Session(engine) as s:
        pps = list(s.execute(select(ProveedorProducto).order_by(ProveedorProducto.id)).scalars())
        assert [(p.proveedor_id, p.producto_texto, p.precio_neto_actual) for p in pps] == [
            (mundo["prov"], "Arroz grado 1", 1190), (mundo["prov"], "Aceite", 2490),
        ]  # fmt: skip
        assert pps[0].fuente == "lista_llm" and (pps[0].factor_base, pps[0].unidad_base) == (
            1.0,
            "kg",
        )
        assert len(list(s.execute(select(CostoHistorial)).scalars())) == 2  # con historial


def test_lista_de_precios_de_otra_empresa_es_404(client, settings, mundo):
    c, h = _sesion(settings, mundo["con"])
    for url in (f"/proveedores/{mundo['prov_otra']}/lista-precios",):
        r = client.post(
            url, files={"archivo": ("l.png", b"\x89PNG\r\n\x1a\n")}, cookies=c, headers=h
        )
        assert r.status_code == 404
    r = client.post(
        f"/proveedores/{mundo['prov_otra']}/lista-precios/confirmar",
        data={"producto": ["x"], "formato": [""], "precio": ["1"]}, cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 404


def test_lista_de_precios_sin_modelo_avisa_flujo_manual(engine, mundo):
    s = _settings()
    client = TestClient(create_app(s, engine))
    c, h = _sesion(s, mundo["con"])
    r = client.post(
        f"/proveedores/{mundo['prov']}/lista-precios",
        files={"archivo": ("l.xlsx", _xlsx())}, cookies=c, headers=h,
    )  # fmt: skip
    assert r.status_code == 200 and "no está configurada" in r.text


def test_texto_de_linea_no_repite_el_formato():
    assert anexos.texto_de_linea(LineaExtraida(texto="Arroz 1 kg", formato="1 kg")) == "Arroz 1 kg"
    assert (
        anexos.texto_de_linea(LineaExtraida(texto="Arroz", formato="bolsa 1 kg"))
        == "Arroz bolsa 1 kg"
    )


def test_esquema_sin_columna_binaria_en_la_base(engine):
    cols = {c["name"]: c["type"] for c in inspect(engine).get_columns("ca_anexos")}
    assert "texto_extraido" in cols and not any(isinstance(t, LargeBinary) for t in cols.values())
