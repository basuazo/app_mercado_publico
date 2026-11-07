"""F-plat-anexos-auto: cliente del servicio de adjuntos (token anónimo + descarga), job
`mercado-anexos`, cola por documento, botón «Volver a bajar y leer», /salud y enmascarado.

Red siempre mockeada (respx): ninguna request sale a ChileCompra ni a OpenRouter. Las URLs,
claves y tokens de acá son de prueba.
"""

from __future__ import annotations

import base64
import io
import json
import logging
from datetime import timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.main import create_app
from app.clients import mp_documentos as md
from app.clients import openrouter as orc
from app.clients.base import RateLimiter
from app.core.tiempo import ahora_utc
from app.ingest import anexos_directo as ad
from app.mercado import adjuntos
from app.mercado import catalogo as cat
from app.models.base import Base
from app.models.tables import CaAdjuntoDescarga, CaAnexo, Empresa, JobRun, LlmUso, SyncState
from tests.test_plat2 import (
    _ANEXO,
    _DOS_LINEAS,
    _URL_OR,
    _ca,
    _respuesta,
    _sesion,
    _settings,
    _sin_espera,
    _usuario,
)

_URL_TOKEN = "https://token.test/realms/x/protocol/openid-connect/token"
_URL_DESC = "https://adjuntos.test/v1/descargar/{id}"
_URL_LISTAR = "https://adjuntos.test/v1/listar/{codigo}"
# Lo que `listar` devuelve por CA en los tests (lo llena `_vigente`).
_LISTADOS: dict[str, list[dict[str, str]]] = {}
_BOTON = 'action="/catalogo/ca/1-1-COT26/anexo/volver-a-bajar"'
_USER_KEY = "clave-publica-de-prueba-0123456789"
_EXTRA = {"x-api-prueba": "valor-extra-de-prueba-abcdef"}
_TOKEN = "eyJhbGciOiJub25lIn0.eyJleHAiOjk5OTk5OTk5OTl9.firma-de-prueba-token-anonimo"


@pytest.fixture()
def engine():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool

    e = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(e)
    yield e


def _cfg(**extra: Any):
    base: dict[str, Any] = {
        "openrouter_api_key": "sk-or-v1-clave-de-prueba",
        "llm_modelo_extraccion": "prueba/vision",
        "anexos_descarga_directa": True,
        "mp_adjuntos_user_key": _USER_KEY,
    }
    return _settings(**{**base, **extra})


@pytest.fixture()
def settings():
    return _cfg()


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


@pytest.fixture()
def lector(settings, engine):
    c = orc.OpenRouterClient(settings, engine, dormir=_sin_espera)
    c._limiter = RateLimiter(1000.0)
    return c


@pytest.fixture()
def dormidos() -> list[float]:
    return []


def _cliente(dormidos: list[float] | None = None, **kw: Any) -> md.DocumentosClient:
    opciones: dict[str, Any] = {
        "user_key": _USER_KEY,
        "extra": dict(_EXTRA),
        "limiter": RateLimiter(1000.0),
        "dormir": (dormidos if dormidos is not None else []).append,
        "url_token": _URL_TOKEN,
        "url_descargar": _URL_DESC,
        "url_listar": _URL_LISTAR,
    }
    return md.DocumentosClient(**{**opciones, **kw})


@pytest.fixture()
def documentos(dormidos) -> md.DocumentosClient:
    return _cliente(dormidos)


def _token_ok(expires_in: Any = 28800) -> httpx.Response:
    """Como la ficha [V, paso 0]: envelope con el token en `payload`."""
    cuerpo: dict[str, Any] = {"access_token": _TOKEN, "token_type": "Bearer"}
    if expires_in is not None:
        cuerpo["expires_in"] = expires_in
    return httpx.Response(
        200, json={"success": True, "trace": "t", "payload": cuerpo, "errores": []}
    )


def _respuesta_listar(request: httpx.Request) -> httpx.Response:
    codigo = request.url.path.rsplit("/", 1)[-1]
    files = [{"id": d["id"], "nombreArchivo": d["nombre"]} for d in _LISTADOS.get(codigo, [])]
    return httpx.Response(200, json={"success": True, "payload": {"files": files}, "errores": []})


@pytest.fixture(autouse=True)
def _listados_limpios():
    _LISTADOS.clear()
    yield
    _LISTADOS.clear()


def _archivo(contenido: bytes, tipo: str = "application/octet-stream") -> httpx.Response:
    return httpx.Response(200, content=contenido, headers={"Content-Type": tipo})


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


def _vigente(session, codigo="1-1-COT26", docs=None, productos=_ANEXO, horas=48, **kw):
    """CA vigente como en producción: `n_documentos > 0` y SIN `documentos[].id` guardados
    [V, Neon 7-nov]; los archivos los entrega `listar` (mockeado con respx)."""
    cat.sembrar_canasta(session)
    docs = docs if docs is not None else [{"id": "d1", "nombre": "Anexo 1.xlsx"}]
    _LISTADOS[codigo] = docs
    # Primera ruta registrada: gana sobre las `url__startswith` de cada test.
    respx.get(url__startswith="https://adjuntos.test/v1/listar/", name="listar").mock(
        side_effect=_respuesta_listar
    )
    _ca(
        session, codigo, productos=productos, n_documentos=kw.pop("n_documentos", len(docs) or 1),
        estado="publicada", fecha_cierre=ahora_utc() + timedelta(hours=horas), documentos=None,
        **kw,
    )  # fmt: skip
    session.commit()
    cat.procesar_catalogo(session, [16], inicial=True)


def _correr(settings, engine, documentos, lector, **kw):
    return ad.run_mercado_anexos(settings, engine, cliente=documentos, extractor=lector, **kw)


def _filas(session) -> dict[str, CaAdjuntoDescarga]:
    """Filas de archivos (sin el marcador del listado)."""
    session.expire_all()
    return {
        f.documento_id: f
        for f in session.execute(select(CaAdjuntoDescarga)).scalars()
        if f.documento_id != adjuntos.LISTADO
    }


def _marcador(session, codigo="1-1-COT26") -> CaAdjuntoDescarga | None:
    session.expire_all()
    return session.execute(
        select(CaAdjuntoDescarga).where(CaAdjuntoDescarga.ca_codigo == codigo,
                                        CaAdjuntoDescarga.documento_id == adjuntos.LISTADO)
    ).scalar_one_or_none()  # fmt: skip


# ---------------------------------------------------------------------------
# Cliente
# ---------------------------------------------------------------------------


@respx.mock
def test_cliente_pide_token_y_baja_con_sus_headers(documentos):
    tok = respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    ruta = respx.get("https://adjuntos.test/v1/descargar/uuid-1").mock(
        return_value=_archivo(b"%PDF-1.4 hola", "application/pdf")
    )
    d = documentos.descargar_adjunto("1-1-COT26", "uuid-1")
    assert d.data == b"%PDF-1.4 hola" and d.motivo is None
    pedido = ruta.calls.last.request
    assert pedido.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert pedido.headers["user_key"] == _USER_KEY
    assert pedido.headers["x-api-prueba"] == _EXTRA["x-api-prueba"]
    assert pedido.headers["User-Agent"].startswith("Rastrillo/")
    assert "cookie" not in pedido.headers
    pedido_token = tok.calls.last.request
    assert pedido_token.method == "GET" and pedido_token.content == b""  # sin payload
    assert pedido_token.headers["Accept"] == "application/json"
    assert pedido_token.headers["User-Agent"].startswith("Rastrillo/")
    # Regla 9: ni la clave ni headers de navegador (origin, referer, sec-*).
    for h in ("user_key", "x-api-prueba", "authorization", "origin", "referer", "cookie"):
        assert h not in pedido_token.headers
    assert not any(k.lower().startswith("sec-") for k in pedido_token.headers)
    documentos.descargar_adjunto("1-1-COT26", "uuid-1")
    assert tok.call_count == 1  # el token se reutiliza mientras no venza


@respx.mock
def test_cliente_renueva_el_token_al_vencer():
    reloj = [1000.0]
    c = _cliente(reloj=lambda: reloj[0])
    tok = respx.get(_URL_TOKEN).mock(return_value=_token_ok(expires_in=300))
    respx.get(url__startswith="https://adjuntos.test/").mock(return_value=_archivo(b"x"))
    c.descargar_adjunto("C", "a")
    reloj[0] += 200  # le quedan 100 s: todavía sirve
    c.descargar_adjunto("C", "a")
    assert tok.call_count == 1
    reloj[0] += 60  # le quedan 40 s (< margen de 60): se renueva
    c.descargar_adjunto("C", "a")
    assert tok.call_count == 2


def test_vencimiento_desde_el_jwt_si_no_hay_expires_in():
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1234}).encode()).decode().rstrip("=")
    assert md._vence_del_jwt(f"a.{payload}.b") == 1234.0
    assert md._vence_del_jwt("no-es-jwt") is None


@respx.mock
def test_cliente_401_renueva_una_vez_y_luego_corta(documentos):
    tok = respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(
        side_effect=[httpx.Response(401), _archivo(b"ok")]
    )
    assert documentos.descargar_adjunto("C", "a").data == b"ok"
    assert tok.call_count == 2 and ruta.call_count == 2
    ruta.side_effect = [httpx.Response(401), httpx.Response(401)]
    with pytest.raises(md.TokenNoDisponible):
        documentos.descargar_adjunto("C", "a")
    assert tok.call_count == 3  # una sola renovación por archivo: nunca en bucle


@pytest.mark.parametrize("status", [403, 429])
@respx.mock
def test_token_403_o_429_es_bloqueo(documentos, status):
    respx.get(_URL_TOKEN).mock(return_value=httpx.Response(status))
    with pytest.raises(md.DescargaBloqueada):
        documentos.descargar_adjunto("C", "a")


@pytest.mark.parametrize(
    "respuesta",
    [httpx.Response(200, json={"token_type": "Bearer"}), httpx.Response(200, text="<html>", headers={"Content-Type": "text/html"}),
     httpx.Response(200, text="no es un token"),
     httpx.Response(500)],
)  # fmt: skip
@respx.mock
def test_token_invalido_no_descarga(documentos, respuesta):
    respx.get(_URL_TOKEN).mock(return_value=respuesta)
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(return_value=_archivo(b"x"))
    with pytest.raises(md.TokenNoDisponible):
        documentos.descargar_adjunto("C", "a")
    assert ruta.call_count == 0


@respx.mock
def test_html_no_es_archivo_y_no_se_parsea(documentos):
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    respx.get(url__startswith="https://adjuntos.test/").mock(
        return_value=httpx.Response(200, text="<html>login</html>",
                                    headers={"Content-Type": "text/html; charset=utf-8"})
    )  # fmt: skip
    assert documentos.descargar_adjunto("C", "a").motivo == "no_archivo"


@respx.mock
def test_mas_de_10_mb_se_corta(documentos):
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())

    def _grande(_r: httpx.Request) -> httpx.Response:
        trozos = (b"0" * (1024 * 1024) for _ in range(11))  # sin Content-Length: streaming
        return httpx.Response(200, headers={"Content-Type": "application/pdf"}, content=trozos)

    respx.get(url__startswith="https://adjuntos.test/").mock(side_effect=_grande)
    assert documentos.descargar_adjunto("C", "a").motivo == "muy_grande"


@respx.mock
def test_504_y_timeout_enfrian_60_s(documentos, dormidos):
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(return_value=httpx.Response(504))
    with pytest.raises(md.DescargaTransitoria):
        documentos.descargar_adjunto("C", "a")
    ruta.side_effect = httpx.ReadTimeout("lento")
    with pytest.raises(md.DescargaTransitoria):
        documentos.descargar_adjunto("C", "a")
    assert dormidos == [60.0, 60.0]


@respx.mock
def test_redirect_a_otro_host_no_lleva_credenciales(documentos):
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    respx.get("https://adjuntos.test/v1/descargar/a").mock(
        return_value=httpx.Response(302, headers={"Location": "https://cdn.test/f",
                                                  "Set-Cookie": "s=1; Path=/"})
    )  # fmt: skip
    cdn = respx.get("https://cdn.test/f").mock(return_value=_archivo(b"x"))
    assert documentos.descargar_adjunto("C", "a").data == b"x"
    pedido = cdn.calls.last.request
    assert "authorization" not in pedido.headers and "user_key" not in pedido.headers
    assert "x-api-prueba" not in pedido.headers and "cookie" not in pedido.headers


@respx.mock
def test_listar_lee_payload_files_con_los_headers_justos(documentos):
    tok = respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    _LISTADOS["2467-760-COT26"] = [
        {"id": "96221142-92E7-4D2B-8A4A-2FD9818ACF50", "nombre": "BASES TECNICAS.pdf"},
        {"id": "DC9F6C66-F021-4085-9C4F-6BFFDAC98208", "nombre": "CRITERIOS.pdf"},
    ]
    ruta = respx.get("https://adjuntos.test/v1/listar/2467-760-COT26").mock(
        side_effect=_respuesta_listar
    )
    lst = documentos.listar("2467-760-COT26")
    assert lst.motivo is None and lst.archivos == [
        ("96221142-92E7-4D2B-8A4A-2FD9818ACF50", "BASES TECNICAS.pdf"),
        ("DC9F6C66-F021-4085-9C4F-6BFFDAC98208", "CRITERIOS.pdf"),
    ]
    pedido = ruta.calls.last.request
    assert pedido.headers["Authorization"] == f"Bearer {_TOKEN}"
    assert pedido.headers["user_key"] == _USER_KEY
    assert pedido.headers["User-Agent"].startswith("Rastrillo/")
    for h in ("origin", "referer", "cookie"):
        assert h not in pedido.headers
    assert not any(k.lower().startswith("sec-") for k in pedido.headers)
    assert tok.call_count == 1


@pytest.mark.parametrize(
    ("respuesta", "motivo"),
    [(httpx.Response(200, text="<html>", headers={"Content-Type": "text/html"}), "no_json"),
     (httpx.Response(200, json={"payload": {"otra": 1}}), "no_json"),
     (httpx.Response(404), "http_404")],
)  # fmt: skip
@respx.mock
def test_listar_sin_forma_conocida(documentos, respuesta, motivo):
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    respx.get(url__startswith="https://adjuntos.test/v1/listar/").mock(return_value=respuesta)
    assert documentos.listar("C") == md.Listado(None, motivo)


@respx.mock
def test_listar_403_es_bloqueo(documentos):
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    respx.get(url__startswith="https://adjuntos.test/v1/listar/").mock(
        return_value=httpx.Response(403)
    )
    with pytest.raises(md.DescargaBloqueada):
        documentos.listar("C")


@respx.mock
def test_descarga_usa_el_uuid_en_mayusculas(documentos):
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    url = "https://adjuntos.test/v1/descargar/96221142-92E7-4D2B-8A4A-2FD9818ACF50"
    ruta = respx.get(url).mock(return_value=_archivo(b"x"))
    assert documentos.descargar_adjunto("C", "96221142-92e7-4d2b-8a4a-2fd9818acf50").data == b"x"
    assert ruta.call_count == 1
    assert md.id_para_url("no-es-uuid") == "no-es-uuid"


def test_sin_clave_o_sin_url_de_token_no_esta_configurado():
    assert not _cliente(user_key="").configurado
    assert not _cliente(url_token="").configurado
    # [V] paso 0: la ficha pide el token con un GET a servicios-prd (sin payload ni user_key).
    assert md.URL_TOKEN == "https://servicios-prd.mercadopublico.cl/v1/auth/publico"
    assert md.DocumentosClient(user_key=_USER_KEY).configurado


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(200, json={"access_token": _TOKEN, "expires_in": 100}),
        httpx.Response(200, json={"token": _TOKEN}),
        httpx.Response(200, json={"success": True, "payload": {"accessToken": _TOKEN}}),
        httpx.Response(200, text=_TOKEN, headers={"Content-Type": "text/plain"}),
        httpx.Response(200, json=_TOKEN),
    ],
)
@respx.mock
def test_token_en_las_formas_posibles(documentos, respuesta):
    respx.get(_URL_TOKEN).mock(return_value=respuesta)
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(return_value=_archivo(b"x"))
    assert documentos.descargar_adjunto("C", "a").data == b"x"
    assert ruta.calls.last.request.headers["Authorization"] == f"Bearer {_TOKEN}"


def test_headers_extra_invalidos_dejan_el_cliente_sin_configurar():
    assert md.headers_extra("") == {}
    assert md.headers_extra('{"x-a": "1"}') == {"x-a": "1"}
    assert md.headers_extra("[1]") is None and md.headers_extra("{no json") is None
    assert md.headers_extra('{"x-a": 1}') is None
    c = md.DocumentosClient.desde_settings(
        _cfg(mp_adjuntos_headers_extra="{no json"), url_token=_URL_TOKEN
    )
    assert not c.configurado


@respx.mock
def test_al_pedir_cuenta_token_y_descarga(documentos):
    llamadas: list[int] = []
    documentos.al_pedir = lambda: llamadas.append(1)
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    respx.get(url__startswith="https://adjuntos.test/").mock(return_value=_archivo(b"x"))
    documentos.descargar_adjunto("C", "a")
    assert len(llamadas) == 2  # token + descarga


# ---------------------------------------------------------------------------
# Secretos: nunca en logs ni en el repr
# ---------------------------------------------------------------------------


def test_logger_enmascara_clave_headers_extra_y_token(monkeypatch):
    from app.core import logging as lg

    monkeypatch.setenv("MP_ADJUNTOS_USER_KEY", _USER_KEY)
    monkeypatch.setenv("MP_ADJUNTOS_HEADERS_EXTRA", json.dumps(_EXTRA))
    filtro = lg._SecretFilter()
    lg.enmascarar_tambien(_TOKEN)
    rec = logging.LogRecord(
        "x", logging.WARNING, __file__, 1, "k=%s e=%s t=%s",
        (_USER_KEY, _EXTRA["x-api-prueba"], _TOKEN), None,
    )  # fmt: skip
    filtro.filter(rec)
    texto = rec.getMessage()
    assert _USER_KEY not in texto and _EXTRA["x-api-prueba"] not in texto and _TOKEN not in texto
    assert texto.count("***") == 3


def test_repr_de_settings_no_expone_las_claves():
    s = _cfg(mp_adjuntos_headers_extra=json.dumps(_EXTRA))
    assert _USER_KEY not in repr(s) and _EXTRA["x-api-prueba"] not in repr(s)


@respx.mock
def test_una_corrida_no_escribe_claves_ni_token_en_el_log(
    session, engine, settings, documentos, lector, caplog
):
    _vigente(session)
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    respx.get(url__startswith="https://adjuntos.test/").mock(
        side_effect=[httpx.Response(504), httpx.Response(401), httpx.Response(401)]
    )
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    with caplog.at_level(logging.DEBUG):
        _correr(settings, engine, documentos, lector)
        _correr(settings, engine, documentos, lector)
    crudo = "\n".join(f"{r.getMessage()} {r.args!r}" for r in caplog.records)
    assert caplog.records  # hubo log (enfriamiento, corte por token)
    for secreto in (_USER_KEY, _EXTRA["x-api-prueba"], _TOKEN):
        assert secreto not in crudo


# ---------------------------------------------------------------------------
# Job `mercado-anexos`
# ---------------------------------------------------------------------------


@respx.mock
def test_job_baja_lee_y_es_idempotente(session, engine, settings, documentos, lector):
    _vigente(session, docs=[{"id": "d1", "nombre": "Anexo 1.xlsx"},
                            {"id": "d2", "nombre": "Anexo 2.xlsx"}])  # fmt: skip
    tok = respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    d1 = respx.get("https://adjuntos.test/v1/descargar/d1").mock(
        return_value=_archivo(_xlsx_con("arroz"))
    )
    respx.get("https://adjuntos.test/v1/descargar/d2").mock(
        return_value=_archivo(_xlsx_con("té"))
    )
    llm = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    res = _correr(settings, engine, documentos, lector)
    assert (res["listadas"], res["bajados"], res["extraidos"], res["requests"]) == (1, 2, 2, 4)
    assert res["ca_por_listar"] == 1 and llm.call_count == 2
    session.expire_all()
    anexos = list(session.execute(select(CaAnexo).order_by(CaAnexo.id)).scalars())
    assert [(a.fuente, a.empresa_id, a.subido_por, a.extraccion_estado) for a in anexos] == [
        ("descarga_directa", None, None, "hecha")
    ] * 2
    filas = _filas(session)
    assert {k: (f.estado, f.anexo_id) for k, f in filas.items()} == {
        "d1": ("bajado", anexos[0].id), "d2": ("bajado", anexos[1].id)
    }  # fmt: skip
    assert _marcador(session).estado == "bajado"
    assert {f.nombre for f in filas.values()} == {"Anexo 1.xlsx", "Anexo 2.xlsx"}
    assert ad.descargas_del_dia(session, ahora_utc()) == 4  # token + listar + 2 descargas
    # Re-ejecutar no repite nada.
    res2 = _correr(settings, engine, documentos, lector)
    assert (res2["ca_por_listar"], res2["archivos_en_cola"]) == (0, 0)
    assert d1.call_count == 1 and tok.call_count == 1
    assert respx.routes["listar"].call_count == 1


@respx.mock
def test_mismo_archivo_en_dos_documentos_no_se_paga_dos_veces(
    session, engine, settings, documentos, lector
):
    _vigente(session, docs=[{"id": "d1", "nombre": "a.xlsx"}, {"id": "d2", "nombre": "b.xlsx"}])
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    respx.get(url__startswith="https://adjuntos.test/").mock(
        return_value=_archivo(_xlsx_con("arroz"))
    )
    llm = respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    res = _correr(settings, engine, documentos, lector)
    assert res["extraidos"] == 1 and res["ya_existian"] == 1 and llm.call_count == 1
    assert {f.estado for f in _filas(session).values()} == {"bajado"}
    assert len(list(session.execute(select(CaAnexo)).scalars())) == 1


@pytest.mark.parametrize("status", [403, 429])
@respx.mock
def test_403_o_429_corta_y_persiste_la_pausa(
    session, engine, settings, documentos, lector, status
):
    _vigente(session, docs=[{"id": "d1", "nombre": "a"}, {"id": "d2", "nombre": "b"}])
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(
        return_value=httpx.Response(status)
    )
    res = _correr(settings, engine, documentos, lector)
    assert ruta.call_count == 1 and res["cortado_por"] == f"http_{status}"
    session.expire_all()
    hasta = ad.pausa_hasta(session)
    assert hasta is not None and hasta > ahora_utc() + timedelta(minutes=55)
    assert ad.cortes_recientes(session, ahora_utc()) == 1
    # Un bloqueo no marca el archivo: sigue `listado` y se reintenta tras la pausa.
    assert {k: f.estado for k, f in _filas(session).items()} == {"d1": "listado", "d2": "listado"}
    otro = _correr(settings, engine, documentos, lector)
    assert otro["omitido"] == "pausa" and ruta.call_count == 1


@respx.mock
def test_tope_diario_cuenta_el_token(session, engine, documentos, lector):
    _vigente(session, docs=[{"id": "d1", "nombre": "a"}, {"id": "d2", "nombre": "b"}])
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(
        return_value=httpx.Response(200, text="<html></html>", headers={"Content-Type": "text/html"})
    )
    cfg = _cfg(anexos_descarga_max=3)  # token + listar + 1 descarga
    res = _correr(cfg, engine, documentos, lector)
    assert ruta.call_count == 1 and res["tope_alcanzado"] and res["requests"] == 3
    res2 = _correr(cfg, engine, documentos, lector)
    assert res2["omitido"] == "tope_diario" and ruta.call_count == 1


@respx.mock
def test_tope_por_corrida_y_por_tiempo(session, engine, documentos, lector):
    _vigente(session, docs=[{"id": f"d{i}", "nombre": str(i)} for i in range(4)])
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(
        return_value=httpx.Response(404)
    )
    res = _correr(_cfg(anexos_descarga_max_corrida=3), engine, documentos, lector)
    assert ruta.call_count == 1 and res["tope_alcanzado"]  # token + listar + 1
    assert [f.estado for f in _filas(session).values()].count("listado") == 3  # quedan en cola
    t = iter([0.0, 0.0, 999.0, 999.0, 999.0])
    res2 = _correr(_cfg(anexos_descarga_minutos=1), engine, documentos, lector,
                   reloj=lambda: next(t))  # fmt: skip
    assert res2["cortado_por_tiempo"] and ruta.call_count == 2
    assert respx.routes["listar"].call_count == 1  # la CA no se vuelve a listar


@respx.mock
def test_fallos_definitivos_no_se_repiten_y_los_transitorios_hasta_3(
    session, engine, settings, documentos, lector
):
    _vigente(session, docs=[{"id": "html", "nombre": "a"}, {"id": "lento", "nombre": "b"}])
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    html = respx.get("https://adjuntos.test/v1/descargar/html").mock(
        return_value=httpx.Response(200, text="<html>", headers={"Content-Type": "text/html"})
    )
    lento = respx.get("https://adjuntos.test/v1/descargar/lento").mock(
        return_value=httpx.Response(504)
    )
    for _ in range(5):
        _correr(settings, engine, documentos, lector)
    assert html.call_count == 1 and lento.call_count == adjuntos.MAX_INTENTOS
    filas = _filas(session)
    assert (filas["html"].estado, filas["html"].motivo) == ("fallido", "no_archivo")
    assert (filas["lento"].motivo, filas["lento"].intentos) == ("504", 3)


@respx.mock
def test_archivo_invalido_queda_fallido_sin_reintento(session, engine, settings, documentos, lector):
    _vigente(session)
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(
        return_value=_archivo(b"MZ\x90\x00 ejecutable")
    )
    res = _correr(settings, engine, documentos, lector)
    assert res["invalidos"] == 1
    assert _filas(session)["d1"].motivo == "invalido"
    _correr(settings, engine, documentos, lector)
    assert ruta.call_count == 1


@respx.mock
def test_cola_prioriza_pedidos_alimentos_y_cierre(session):
    ahora = ahora_utc()
    _vigente(session, "1-1-COT26", horas=10)  # alimentos, cierra después
    _vigente(session, "2-1-COT26", horas=5, productos=[("43211500", "Notebook", "pc", 1, "Unidad")])
    _vigente(session, "3-1-COT26", horas=20)
    _vigente(session, "4-1-COT26", horas=30)
    _vigente(session, "5-1-COT26", docs=[], n_documentos=0)  # sin adjunto
    _vigente(session, "6-1-COT26", horas=-1)  # cerrada
    _vigente(session, "7-1-COT26", region=5)  # fuera del territorio
    _vigente(session, "8-1-COT26", anexo_resuelto_en=ahora)
    session.add_all([
        CaAdjuntoDescarga(ca_codigo="3-1-COT26", documento_id="d1", estado="pedido"),
        CaAdjuntoDescarga(ca_codigo="3-1-COT26", documento_id=adjuntos.LISTADO, estado="bajado"),
        # Listado con el archivo pendiente (una corrida anterior no alcanzó a bajarlo).
        CaAdjuntoDescarga(ca_codigo="4-1-COT26", documento_id="x", estado="listado"),
        CaAdjuntoDescarga(ca_codigo="4-1-COT26", documento_id=adjuntos.LISTADO, estado="bajado"),
    ])  # fmt: skip
    session.commit()
    cola = adjuntos.cola(session, [16], ahora)
    assert [(p.codigo, p.documento_id) for p in cola] == [
        ("3-1-COT26", "d1"), ("1-1-COT26", None), ("4-1-COT26", "x"), ("2-1-COT26", None)
    ]  # fmt: skip
    assert cola[0].pedido and cola[1].es_listado
    assert [p.codigo for p in adjuntos.cola(session, [16], ahora, limite=1)] == ["3-1-COT26"]


@respx.mock
def test_listado_vacio_o_fallido(session, engine, settings, documentos, lector):
    _vigente(session, docs=[])
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    res = _correr(settings, engine, documentos, lector)
    assert res["listadas"] == 1 and _marcador(session).motivo == "sin_archivos"
    assert adjuntos.cola(session, [16], ahora_utc()) == []  # no se vuelve a listar
    # Un listado que falla por algo transitorio se reintenta hasta 3 veces.
    _vigente(session, "2-1-COT26")
    respx.routes["listar"].side_effect = None
    respx.routes["listar"].return_value = httpx.Response(500)
    for _ in range(4):
        _correr(settings, engine, documentos, lector)
    m = _marcador(session, "2-1-COT26")
    assert (m.estado, m.motivo, m.intentos) == ("fallido", "http_500", 3)


@respx.mock
def test_ca_con_anexo_leido_de_una_persona_queda_fuera(session):
    _vigente(session)
    session.add(CaAnexo(ca_codigo="1-1-COT26", nombre_original="mio.pdf", mime="application/pdf",
                        tamano=1, sha256="a" * 64, extraccion_estado="hecha", fuente="manual"))  # fmt: skip
    session.commit()
    assert adjuntos.cola(session, [16], ahora_utc()) == []
    # Si la lectura de la persona falló, la descarga automática sí la intenta.
    a = session.execute(select(CaAnexo)).scalar_one()
    a.extraccion_estado = "error"
    session.commit()
    assert len(adjuntos.cola(session, [16], ahora_utc())) == 1


@pytest.mark.parametrize(
    ("cambios", "motivo"),
    [({"anexos_descarga_directa": False}, "apagado"), ({"mp_adjuntos_user_key": ""}, "sin_configurar"),
     ({"llm_modelo_extraccion": ""}, "sin_llm"), ({"openrouter_api_key": ""}, "sin_llm")],
)  # fmt: skip
@respx.mock
def test_apagado_sin_clave_o_sin_llm_no_pide_nada(session, engine, cambios, motivo):
    """Sin lectura automática (falta OPENROUTER_API_KEY o LLM_MODELO_EXTRACCION en Actions) no se
    pide ni token, ni listar, ni descargar: el archivo quedaría sin leer."""
    _vigente(session)
    ruta = respx.route(host__regex=r"^(token|adjuntos)\.test$").mock(return_value=_token_ok())
    cfg = _cfg(**cambios)
    lector = orc.OpenRouterClient(cfg, engine, dormir=_sin_espera)
    cliente = md.DocumentosClient.desde_settings(
        cfg, url_token=_URL_TOKEN, url_descargar=_URL_DESC, url_listar=_URL_LISTAR,
        limiter=RateLimiter(1000.0),
    )  # fmt: skip
    assert ad.run_mercado_anexos(cfg, engine, cliente=cliente, extractor=lector) == {
        "omitido": motivo
    }
    assert ruta.call_count == 0 and respx.routes["listar"].call_count == 0
    session.expire_all()
    assert ad.descargas_del_dia(session, ahora_utc()) == 0


@respx.mock
def test_sin_token_corta_sin_pausa(session, engine, settings, documentos, lector):
    _vigente(session)
    respx.get(_URL_TOKEN).mock(return_value=httpx.Response(500))
    res = _correr(settings, engine, documentos, lector)
    assert res["cortado_por"] == "token"
    session.expire_all()
    assert ad.pausa_hasta(session) is None and _filas(session) == {}


def _sin_presupuesto(session) -> None:
    session.add(LlmUso(mes=orc._mes_chile(), requests=1, tokens_in=1, tokens_out=1,
                       costo_usd=999.0, estimado=False))  # fmt: skip
    session.commit()


@respx.mock
def test_presupuesto_agotado_corta_la_corrida(session, engine, settings, documentos, lector):
    _vigente(session, docs=[{"id": "d1", "nombre": "a.xlsx"}, {"id": "d2", "nombre": "b.xlsx"}])
    _sin_presupuesto(session)
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(
        side_effect=[_archivo(_xlsx_con("arroz")), _archivo(_xlsx_con("té"))]
    )
    res = _correr(settings, engine, documentos, lector)
    assert res["cortado_por_llm"] == "sin_presupuesto" and ruta.call_count == 1
    session.expire_all()
    assert session.execute(select(CaAnexo)).scalar_one().extraccion_estado == "sin_presupuesto"


# ---------------------------------------------------------------------------
# «Volver a bajar y leer» (ficha) y /salud
# ---------------------------------------------------------------------------


@pytest.fixture()
def usuario(session) -> int:
    e = Empresa(nombre="E1", regiones_prioritarias=[16], comunas_prioritarias=[], activa=True)
    session.add(e)
    session.flush()
    uid = _usuario(session, "con@t.cl", e.id)
    session.commit()
    return uid


@respx.mock
def test_volver_a_bajar_y_leer_relee_en_la_misma_fila(
    session, engine, settings, documentos, lector, usuario
):
    from fastapi.testclient import TestClient

    _vigente(session)
    _sin_presupuesto(session)
    respx.get(_URL_TOKEN).mock(return_value=_token_ok())
    archivo = _xlsx_con("arroz")
    ruta = respx.get(url__startswith="https://adjuntos.test/").mock(return_value=_archivo(archivo))
    respx.post(_URL_OR).mock(return_value=_respuesta(_DOS_LINEAS))
    _correr(settings, engine, documentos, lector)
    (anexo,) = session.execute(select(CaAnexo)).scalars()
    assert anexo.extraccion_estado == "sin_presupuesto"
    res = _correr(settings, engine, documentos, lector)
    assert (res["ca_por_listar"], res["archivos_en_cola"]) == (0, 0)  # no relee solo

    # Llega el mes siguiente (o sube el presupuesto) y una persona pide bajarlo de nuevo.
    session.query(LlmUso).delete()
    session.commit()
    web = TestClient(create_app(settings, engine), raise_server_exceptions=True)
    c, h = _sesion(settings, usuario)
    ficha = web.get("/catalogo/ca/1-1-COT26", cookies=c)
    # Se busca el formulario, no el texto: el changelog (que se muestra en la página) lo menciona.
    assert _BOTON in ficha.text
    r = web.post("/catalogo/ca/1-1-COT26/anexo/volver-a-bajar", cookies=c, headers=h,
                 follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and "mensaje=" in r.headers["location"]
    fila = _filas(session)["d1"]
    assert (fila.estado, fila.pedido_por, fila.intentos) == ("pedido", usuario, 0)
    assert "Adjunto en cola" in web.get("/catalogo/ca/1-1-COT26", cookies=c).text

    res = _correr(settings, engine, documentos, lector)
    assert res["extraidos"] == 1 and ruta.call_count == 2
    session.expire_all()
    (releido,) = session.execute(select(CaAnexo)).scalars()  # misma fila, sin duplicar
    assert releido.id == anexo.id and releido.extraccion_estado == "hecha"
    assert _filas(session)["d1"].estado == "bajado"
    assert _BOTON not in web.get("/catalogo/ca/1-1-COT26", cookies=c).text


@respx.mock
def test_volver_a_bajar_exige_csrf_y_algo_que_bajar(session, engine, settings, usuario):
    from fastapi.testclient import TestClient

    _vigente(session)
    web = TestClient(create_app(settings, engine), raise_server_exceptions=False)
    c, h = _sesion(settings, usuario)
    assert web.post("/catalogo/ca/1-1-COT26/anexo/volver-a-bajar", cookies=c).status_code == 403
    r = web.post("/catalogo/ca/1-1-COT26/anexo/volver-a-bajar", cookies=c, headers=h,
                 follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and "error=" in r.headers["location"]  # nada que volver a bajar
    assert _filas(session) == {}
    assert web.post("/catalogo/ca/9-9-COT26/anexo/volver-a-bajar", cookies=c,
                    headers=h).status_code == 404  # fmt: skip


@respx.mock
def test_pedir_de_nuevo_salta_lo_ya_leido(session):
    _vigente(session, docs=[{"id": "ok", "nombre": "a"}, {"id": "mal", "nombre": "b"},
                            {"id": "nuevo", "nombre": "c"}])  # fmt: skip
    for i, estado in enumerate(("hecha", "error")):
        session.add(CaAnexo(ca_codigo="1-1-COT26", nombre_original="x", mime="application/pdf",
                            tamano=1, sha256=str(i) * 64, extraccion_estado=estado, paginas=1,
                            fuente="descarga_directa"))  # fmt: skip
    session.flush()
    a_ok, a_mal = session.execute(select(CaAnexo).order_by(CaAnexo.id)).scalars()
    session.add_all([
        CaAdjuntoDescarga(ca_codigo="1-1-COT26", documento_id="ok", estado="bajado", anexo_id=a_ok.id),
        CaAdjuntoDescarga(ca_codigo="1-1-COT26", documento_id="mal", estado="bajado", anexo_id=a_mal.id),
        CaAdjuntoDescarga(ca_codigo="1-1-COT26", documento_id="nuevo", estado="fallido",
                          motivo="no_archivo", intentos=1),
        CaAdjuntoDescarga(ca_codigo="1-1-COT26", documento_id="espera", estado="listado"),
        CaAdjuntoDescarga(ca_codigo="1-1-COT26", documento_id=adjuntos.LISTADO, estado="bajado"),
    ])  # fmt: skip
    session.commit()
    ca = session.get(adjuntos.CompraAgil, "1-1-COT26")
    assert adjuntos.puede_rebajar(session, ca)
    assert adjuntos.pedir_de_nuevo(session, ca, None, ahora_utc()) == 2
    session.commit()
    assert {k: f.estado for k, f in _filas(session).items()} == {
        "ok": "bajado", "mal": "pedido", "nuevo": "pedido", "espera": "listado"
    }  # fmt: skip
    assert not adjuntos.puede_rebajar(session, ca)  # ya está en cola


@respx.mock
def test_salud_muestra_pendientes_tope_y_ultima_corrida(session, engine, settings, client_admin):
    from app.api.salud_data import _anexos_salud

    _vigente(session, docs=[{"id": "d1", "nombre": "a"}, {"id": "d2", "nombre": "b"}])
    session.add(SyncState(fuente=ad.PREFIJO_DESCARGAS + ad.dia_chile(ahora_utc()),
                          requests_usadas_hoy=7))  # fmt: skip
    session.add(JobRun(job="mercado-anexos", iniciado_en=ahora_utc(), estado="ok",
                       resultado_json={"cortado_por": "http_403", "requests": 4, "extraidos": 1}))  # fmt: skip
    session.commit()
    d = _anexos_salud(session, settings)["directa"]
    assert (d["ca_por_listar"], d["archivos_en_cola"], d["ca_pendientes"]) == (1, 0, 1)
    assert (d["descargas_hoy"], d["tope_dia"]) == (7, 300)
    assert d["ultima_corrida"]["cortado_por"] == "http_403"
    assert _USER_KEY not in json.dumps(d)
    pagina = client_admin.get("/salud").text
    assert "mercado-anexos" in pagina and "cortada por http_403" in pagina
    assert _USER_KEY not in pagina


@pytest.fixture()
def client_admin(engine, settings, session):
    from fastapi.testclient import TestClient

    from app.models.enums import RolUsuario

    uid = _usuario(session, "admin@t.cl", rol=RolUsuario.ADMIN)
    session.commit()
    web = TestClient(create_app(settings, engine), raise_server_exceptions=True)
    c, _ = _sesion(settings, uid)
    web.cookies.update(c)
    return web


def test_mercado_ca_ya_no_baja_adjuntos(monkeypatch, engine):
    from app.ingest import mercado

    monkeypatch.setattr(mercado, "_sync_en_ventanas", lambda *a, **k: {"ventanas": 0})
    monkeypatch.setattr(mercado, "cola_vigentes", lambda *a, **k: [])
    monkeypatch.setattr(mercado, "cola_mercado_ca", lambda *a, **k: [])
    llamadas: list[Any] = []
    monkeypatch.setattr(ad, "run_mercado_anexos", lambda *a, **k: llamadas.append(a))
    mercado.sync_mercado_ca(_cfg(), engine, object())  # type: ignore[arg-type]
    assert not llamadas


def test_cli_conoce_el_job():
    from app.ingest.__main__ import _JOBS, _JOBS_CON_API

    assert "mercado-anexos" in _JOBS and "mercado-anexos" not in _JOBS_CON_API  # sin ticket
