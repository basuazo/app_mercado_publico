"""Carga masiva de anexos y API para automatización externa (F-plat-anexos-2 §2.2–§2.3).

Web (`/catalogo/anexos/carga`): usuarios con empresa activa, CSRF en cada POST. El navegador
manda UN archivo por request (o un tramo de un ZIP) y encadena el siguiente, así cada request es
corta y el avance se ve; el servidor no guarda nada entre requests (regla 10).

API (`/api/anexos/...`): cabecera `X-Anexos-Token`, comparada en tiempo constante contra el hash
de la empresa. Sin cookies, así que sin CSRF. Un token inválido o revocado recibe 401 ANTES de
mirar la CA: el cuerpo nunca revela si una CA existe.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import check_csrf, get_db, html_require_admin, html_require_empresa
from app.api.routes.pages import _TEMPLATES, _ctx
from app.core.logging import get_logger
from app.core.tiempo import ahora_utc
from app.mercado import anexos
from app.mercado import anexos_carga as carga
from app.mercado import anexos_pendientes as pend
from app.mercado.territorio import regiones_activas
from app.models.tables import Empresa, Usuario

router = APIRouter()
_log = get_logger(__name__)

ESTADOS_PROCESABLES = (carga.NUEVA,)


def _extractor(request: Request) -> anexos.Extractor | None:
    from app.clients.openrouter import OpenRouterClient

    cliente = OpenRouterClient(request.app.state.settings, request.app.state.engine)
    return cliente if cliente.habilitado else None


def _filas(request: Request, user: Usuario, filas: list[carga.FilaCarga], modo: str) -> str:
    return _TEMPLATES.get_template("_anexos_carga_filas.html").render(
        _ctx(request, user, filas=filas, modo=modo, procesables=ESTADOS_PROCESABLES)
    )


# ===========================================================================
# Web: carga masiva
# ===========================================================================


@router.get("/catalogo/anexos/carga", response_class=HTMLResponse)
async def carga_get(
    request: Request,
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, _ = ctx
    settings = request.app.state.settings
    return _TEMPLATES.TemplateResponse(
        request,
        "catalogo_anexos_carga.html",
        _ctx(
            request,
            user,
            max_mb=anexos.MAX_BYTES // (1024 * 1024),
            max_zip_mb=anexos.MAX_ZIP_BYTES // (1024 * 1024),
            con_lectura=bool(settings.openrouter_api_key and settings.llm_modelo_extraccion),
        ),
    )


@router.post("/catalogo/anexos/carga/vista-previa", response_class=HTMLResponse)
async def carga_vista_previa(
    request: Request,
    nombres: list[str] = Form([]),
    tamanos: list[int] = Form([]),
    sha256: list[str] = Form([]),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Vista previa de varios archivos SIN subirlos: el navegador manda nombre, tamaño y
    sha256 (calculado en el navegador) de cada uno."""
    check_csrf(request, csrf_token)
    user, _ = ctx
    regiones = regiones_activas(session, request.app.state.settings)
    filas = []
    for i, nombre in enumerate(nombres[:500]):
        tamano = tamanos[i] if i < len(tamanos) else 0
        sha = sha256[i] if i < len(sha256) else ""
        f = carga.fila_vista(session, regiones, nombre, tamano, sha or None)
        f.indice = i
        filas.append(f)
    return HTMLResponse(_filas(request, user, filas, "vista"))


@router.post("/catalogo/anexos/carga/archivo", response_class=HTMLResponse)
def carga_archivo(
    request: Request,
    archivo: UploadFile = File(...),
    indice: int = Form(0),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """UN archivo de la carga masiva (síncrona: la extracción corre en el pool de hilos)."""
    check_csrf(request, csrf_token)
    user, empresa_id = ctx
    settings = request.app.state.settings
    regiones = regiones_activas(session, settings)
    data = archivo.file.read(anexos.MAX_BYTES + 1)
    fila = carga.procesar_archivo(
        session, regiones, archivo.filename or "", data, extractor=_extractor(request),
        usuario_id=user.id, empresa_id=empresa_id, fuente="masiva",
        max_paginas_imagen=settings.llm_max_paginas_imagen,
    )  # fmt: skip
    fila.indice = indice
    return HTMLResponse(_filas(request, user, [fila], "resultado"))


@router.post("/catalogo/anexos/carga/zip", response_class=HTMLResponse)
def carga_zip(
    request: Request,
    zip: UploadFile = File(...),
    accion: str = Form("vista"),
    desde: int = Form(0),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Un ZIP: `accion=vista` lee solo el directorio; `accion=procesar` procesa un tramo desde
    `desde` y avisa el siguiente en la cabecera `X-Siguiente` (vacía si terminó)."""
    check_csrf(request, csrf_token)
    user, empresa_id = ctx
    settings = request.app.state.settings
    regiones = regiones_activas(session, settings)
    try:
        if accion == "procesar":
            filas, siguiente = carga.procesar_zip(
                session, regiones, zip.file, desde, extractor=_extractor(request),
                usuario_id=user.id, empresa_id=empresa_id,
                max_paginas_imagen=settings.llm_max_paginas_imagen,
            )  # fmt: skip
        else:
            filas, siguiente = carga.vista_zip(session, regiones, zip.file), None
    except anexos.AnexoInvalido as exc:
        session.rollback()
        return HTMLResponse(
            _filas(request, user, [], "error") + _aviso_error(str(exc)),
            headers={"X-Siguiente": ""},
        )
    modo = "resultado" if accion == "procesar" else "vista"
    return HTMLResponse(
        _filas(request, user, filas, modo),
        headers={"X-Siguiente": "" if siguiente is None else str(siguiente)},
    )


def _aviso_error(texto: str) -> str:
    from markupsafe import escape

    return (
        f'<tr><td colspan="5"><div class="alert alert-danger py-2 mb-0" role="alert">'
        f"{escape(texto)}</div></td></tr>"
    )


# ===========================================================================
# Web: CA que necesitan anexo + «Abrir fichas» (F-plat-anexos-boton §2)
# ===========================================================================


@router.get("/catalogo/anexos/pendientes", response_class=HTMLResponse)
async def pendientes_get(
    request: Request,
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Lista por cierre de las CA vigentes que necesitan anexo y botón «Abrir fichas»: abre las
    próximas N fichas públicas en pestañas para pasar en cada una la extensión (`extension/`).
    El servidor no toca mercadopublico.cl: solo arma enlaces que abre la persona."""
    user, empresa_id = ctx
    return _TEMPLATES.TemplateResponse(
        request,
        "catalogo_anexos_pendientes.html",
        _ctx(
            request,
            user,
            filas=pend.filas_pendientes(session, empresa_id, ahora_utc()),
            n_defecto=pend.N_FICHAS_DEFECTO,
            n_max=pend.N_FICHAS_MAX,
        ),
    )


# ===========================================================================
# Admin: token de anexos de una empresa
# ===========================================================================


@router.post("/admin/empresas/{empresa_id}/token-anexos", response_model=None)
async def token_generar(
    request: Request,
    empresa_id: int,
    accion: str = Form("generar"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> Response:
    """«Generar token de anexos» (lo muestra UNA vez) o «Revocar»."""
    from app.api.routes.mesa import _empresa_o_404, _pagina_empresa

    check_csrf(request, csrf_token)
    empresa = _empresa_o_404(session, empresa_id)
    if accion == "revocar":
        carga.revocar_token(empresa)
        session.commit()
        return RedirectResponse(
            f"/admin/empresas/{empresa.id}?mensaje=Token%20de%20anexos%20revocado",
            status_code=303,
        )
    token = carga.generar_token(empresa)
    session.commit()
    _log.info("Token de anexos generado para la empresa %s", empresa.id)  # sin el token
    resp = _pagina_empresa(
        request,
        user,
        session,
        empresa,
        mensaje="Token de anexos generado: cópialo ahora, no se vuelve a mostrar.",
        token_anexos=token,
    )
    resp.headers["Cache-Control"] = "no-store"  # el token en claro no queda en ninguna caché
    return resp


# ===========================================================================
# API externa
# ===========================================================================


def _no_autorizado() -> JSONResponse:
    return JSONResponse({"detail": "Token inválido"}, status_code=401)


def _empresa_y_limite(request: Request, session: Session) -> Empresa | JSONResponse:
    empresa = carga.empresa_por_token(session, request.headers.get("X-Anexos-Token"))
    if empresa is None:
        return _no_autorizado()
    espera = carga.consumir_limite(session, empresa.id, ahora_utc())
    if espera is not None:
        return JSONResponse(
            {"detail": "Demasiadas solicitudes"},
            status_code=429,
            headers={"Retry-After": str(espera)},
        )
    return empresa


@router.post("/api/anexos/{codigo}", response_model=None)
def api_anexo_subir(
    request: Request,
    codigo: str,
    archivo: UploadFile = File(...),
    session: Session = Depends(get_db),
) -> JSONResponse:
    """Sube un anexo a la CA `codigo` (`fuente = api_externa`). Mismos validadores y topes
    que la subida manual. Respuesta: `{anexo_id, estado, duplicado}`."""
    empresa = _empresa_y_limite(request, session)
    if isinstance(empresa, JSONResponse):
        return empresa
    settings = request.app.state.settings
    regiones = [int(r) for r in (empresa.regiones_prioritarias or [])]
    empresa_id = empresa.id
    data = archivo.file.read(anexos.MAX_BYTES + 1)
    fila = carga.procesar_archivo(
        session, regiones, archivo.filename or f"{codigo}.bin", data,
        extractor=_extractor(request), usuario_id=None, empresa_id=empresa_id,
        fuente="api_externa", max_paginas_imagen=settings.llm_max_paginas_imagen,
        codigo=codigo.strip().upper(),
    )  # fmt: skip
    if fila.estado == carga.NO_ASOCIADO:
        return JSONResponse({"detail": "Compra Ágil no encontrada"}, status_code=404)
    if fila.estado == carga.INVALIDO:
        return JSONResponse({"detail": fila.detalle}, status_code=422)
    cuerpo: dict[str, Any] = {
        "anexo_id": fila.anexo_id,
        "estado": fila.estado,
        "duplicado": fila.duplicado,
        "lineas": fila.n_lineas,
        "fuente": "Dirección ChileCompra",
    }
    return JSONResponse(cuerpo, status_code=200 if fila.duplicado else 201)


@router.get("/api/anexos/pendientes", response_model=None)
async def api_anexos_pendientes(
    request: Request, session: Session = Depends(get_db)
) -> JSONResponse:
    """CA vigentes de las regiones de la empresa que esperan su anexo (máx. 200)."""
    empresa = _empresa_y_limite(request, session)
    if isinstance(empresa, JSONResponse):
        return empresa
    return JSONResponse(
        {
            "pendientes": carga.pendientes(session, empresa, ahora_utc()),
            "fuente": "Dirección ChileCompra",
        }
    )
