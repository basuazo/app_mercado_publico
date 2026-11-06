"""Cotización por lote (F-plat-lote): `/lotes`, `/lotes/{id}` y «En trabajo» (`/trabajo`).

Solo usuarios con empresa activa; TODA query filtra por el `empresa_id` de la sesión y un id de
otra empresa responde 404 (reglas 17 y 26). Mutaciones con CSRF (regla 18) y patrón
POST → 303 a la pantalla del lote (que muestra `mensaje`/`error`). El cambio de proveedor de una
línea es HTMX: responde la pantalla y el cliente reemplaza `#lote-contenido` (`hx-select`), así
todos los formularios quedan con la `version` nueva. Esta fase no envía WhatsApp ni correo: genera
el texto y la planilla; la persona los manda.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    check_csrf,
    empresa_activa_id,
    get_db,
    html_require_empresa,
    html_require_user,
)
from app.api.routes.mesa import _sin_empresa
from app.api.routes.pages import _TEMPLATES, _ctx, _es_htmx
from app.core.tiempo import ahora_utc
from app.mercado import equipo as eq
from app.mercado import importar
from app.mercado import lote as lt
from app.models.tables import Lote, Usuario

router = APIRouter()

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _404() -> HTTPException:
    return HTTPException(status_code=404, detail="Lote no encontrado")


def _a_lote(lote_id: int, *, mensaje: str = "", error: str = "") -> RedirectResponse:
    qs = f"?mensaje={quote(mensaje)}" if mensaje else (f"?error={quote(error)}" if error else "")
    return RedirectResponse(url=f"/lotes/{lote_id}{qs}", status_code=303)


def _pagina(
    request: Request,
    user: Usuario,
    session: Session,
    empresa_id: int,
    lote_id: int,
    *,
    status_code: int = 200,
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    try:
        v = lt.vista(session, empresa_id, lote_id)
    except lt.LoteAjeno:
        raise _404() from None
    return _TEMPLATES.TemplateResponse(
        request,
        "lote.html",
        _ctx(
            request,
            user,
            v=v,
            mensaje=mensaje,
            error=error,
            estados=lt.ETIQUETA_ESTADO,
            estados_ca=eq.ETIQUETA_ESTADO,
            etiqueta_solicitud=lt.ETIQUETA_SOLICITUD,
            canales=lt.CANALES,
            resultados=eq.ETIQUETA_RESULTADO,
            editable=v.lote.estado in lt.EDITABLES,
            ahora=ahora_utc(),
        ),
        status_code=status_code,
    )


def _fallo(
    request: Request,
    user: Usuario,
    session: Session,
    empresa_id: int,
    lote_id: int,
    exc: Exception,
) -> Response:
    """Error de una mutación: se revierte y se pinta el lote con el motivo (422 o 409)."""
    session.rollback()
    if isinstance(exc, lt.LoteAjeno):
        raise _404()
    if isinstance(exc, lt.ConflictoLote | eq.ConflictoVersion):
        return _pagina(request, user, session, empresa_id, lote_id, status_code=409,
                       error=f"{exc.quien} cambió este lote mientras lo veías: revisa y vuelve a intentarlo")  # fmt: skip
    if isinstance(exc, lt.CaEnOtroLote):
        return _pagina(request, user, session, empresa_id, lote_id, status_code=409, error=str(exc))
    return _pagina(request, user, session, empresa_id, lote_id, status_code=422, error=str(exc))


_ERRORES = (lt.ErrorLote, lt.LoteAjeno, lt.ConflictoLote, lt.CaEnOtroLote, eq.ErrorEquipo)


def _version(texto: str) -> int | None:
    try:
        return int(texto) if texto.strip() else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Crear desde la mesa y listar
# ---------------------------------------------------------------------------


@router.post("/lotes", response_model=None)
async def lote_crear(
    request: Request,
    codigos: list[str] = Form([]),
    lote_id: str = Form(""),
    nombre: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """«Cotizar seleccionadas»: crea un lote o, si se elige, agrega al lote abierto."""
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    destino = _version(lote_id)
    try:
        if destino:
            lote = lt.agregar_cas(session, empresa_id, user, destino, codigos)
        else:
            lote = lt.crear(session, empresa_id, user, codigos, nombre=nombre)
    except lt.CaEnOtroLote as exc:
        session.rollback()
        return _TEMPLATES.TemplateResponse(
            request, "lote_aviso.html",
            _ctx(request, user, titulo="Esa Compra Ágil ya está en otro lote", texto=str(exc),
                 lote_id=exc.lote_id),
            status_code=409,
        )  # fmt: skip
    except lt.LoteAjeno:
        session.rollback()
        raise _404() from None
    except (lt.ErrorLote, lt.ConflictoLote, eq.ErrorEquipo, eq.ConflictoVersion) as exc:
        session.rollback()
        return _TEMPLATES.TemplateResponse(
            request, "lote_aviso.html",
            _ctx(request, user, titulo="No se pudo armar el lote", texto=str(exc), lote_id=None),
            status_code=409 if isinstance(exc, lt.ConflictoLote | eq.ConflictoVersion) else 422,
        )  # fmt: skip
    session.commit()
    return _a_lote(lote.id, mensaje="Lote listo: revisa el agregado y pide precios")


@router.get("/lotes", response_class=HTMLResponse)
async def lotes_lista(
    request: Request,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    lotes = list(
        session.execute(
            select(Lote).where(Lote.empresa_id == empresa_id).order_by(Lote.creado_en.desc())
        ).scalars()
    )
    return _TEMPLATES.TemplateResponse(
        request, "lotes.html", _ctx(request, user, lotes=lotes, estados=lt.ETIQUETA_ESTADO)
    )


@router.get("/trabajo", response_class=HTMLResponse)
async def en_trabajo(
    request: Request,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """«En trabajo»: lotes abiertos, CA en curso sin lote y las que cierran en 24 h."""
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    ahora = ahora_utc()
    return _TEMPLATES.TemplateResponse(
        request,
        "trabajo.html",
        _ctx(
            request,
            user,
            t=lt.en_trabajo(session, empresa_id, ahora),
            ahora=ahora,
            estados=lt.ETIQUETA_ESTADO,
            estados_ca=eq.ETIQUETA_ESTADO,
        ),  # fmt: skip
    )


# ---------------------------------------------------------------------------
# Pantalla del lote
# ---------------------------------------------------------------------------


@router.get("/lotes/{lote_id}", response_class=HTMLResponse)
async def lote_ver(
    request: Request,
    lote_id: int,
    mensaje: str = "",
    error: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    return _pagina(request, user, session, empresa_id, lote_id, mensaje=mensaje[:300],
                   error=error[:300])  # fmt: skip


@router.post("/lotes/{lote_id}/cas/{codigo}/quitar", response_model=None)
async def lote_quitar_ca(
    request: Request,
    lote_id: int,
    codigo: str,
    version: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        lt.quitar_ca(session, empresa_id, user, lote_id, codigo, _version(version))
    except (*_ERRORES, eq.ConflictoVersion) as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    return _a_lote(lote_id, mensaje=f"Se quitó {codigo} del lote")


@router.post("/lotes/{lote_id}/recalcular", response_model=None)
async def lote_recalcular(
    request: Request,
    lote_id: int,
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        lt.recalcular_lote(session, empresa_id, lote_id)
    except _ERRORES as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    return _a_lote(lote_id, mensaje="Agregado recalculado")


@router.post("/lotes/{lote_id}/lineas/{linea_id}/proveedor", response_model=None)
async def lote_proveedor_linea(
    request: Request,
    lote_id: int,
    linea_id: int,
    proveedor: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """HTMX: responde la pantalla entera; el cliente toma `#lote-contenido`."""
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        lt.cambiar_proveedor(session, empresa_id, lote_id, linea_id, proveedor)
    except _ERRORES as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    if _es_htmx(request):
        return _pagina(request, user, session, empresa_id, lote_id, mensaje="Proveedor actualizado")
    return _a_lote(lote_id, mensaje="Proveedor actualizado")


@router.post("/lotes/{lote_id}/estado", response_model=None)
async def lote_estado(
    request: Request,
    lote_id: int,
    estado: str = Form(""),
    version: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        avisos = lt.cambiar_estado(session, empresa_id, user, lote_id, estado, _version(version))
    except _ERRORES as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    texto = f"Lote {lt.ETIQUETA_ESTADO.get(estado, estado).lower()}"
    if avisos:
        texto += ". Ojo: " + "; ".join(avisos)
    return _a_lote(lote_id, mensaje=texto)


@router.post("/lotes/{lote_id}/ofertas", response_model=None)
async def lote_armar_ofertas(
    request: Request,
    lote_id: int,
    version: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        r = lt.armar_ofertas(session, empresa_id, user, lote_id, _version(version))
    except (*_ERRORES, eq.ConflictoVersion) as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    texto = f"Ofertas armadas: {r.armadas}"
    if r.sin_oferta:
        texto += f" · con líneas sin precio: {len(r.sin_oferta)} (complétalas en la ficha)"
    return _a_lote(lote_id, mensaje=texto)


# ---------------------------------------------------------------------------
# Solicitudes de precio
# ---------------------------------------------------------------------------


def _solicitud_del_lote(session: Session, empresa_id: int, lote_id: int, sid: int) -> None:
    s, _ = lt.solicitud_de(session, empresa_id, sid)
    if s.lote_id != lote_id:
        raise lt.LoteAjeno(sid)


@router.post("/lotes/{lote_id}/solicitudes/{sid}/texto", response_model=None)
async def solicitud_texto(
    request: Request,
    lote_id: int,
    sid: int,
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        _solicitud_del_lote(session, empresa_id, lote_id, sid)
        lt.regenerar_texto(session, empresa_id, sid)
    except _ERRORES as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    return _a_lote(lote_id, mensaje="Mensaje generado: cópialo y envíalo")


@router.get("/lotes/{lote_id}/solicitudes/{sid}/planilla.xlsx")
async def solicitud_planilla(
    lote_id: int,
    sid: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    _, empresa_id = ue
    try:
        _solicitud_del_lote(session, empresa_id, lote_id, sid)
        data = lt.xlsx_solicitud(session, empresa_id, sid)
    except lt.LoteAjeno:
        raise _404() from None
    return Response(
        content=data,
        media_type=_XLSX,
        headers={"Content-Disposition": f'attachment; filename="solicitud_precios_{sid}.xlsx"'},
    )


@router.post("/lotes/{lote_id}/solicitudes/{sid}/enviada", response_model=None)
async def solicitud_enviada(
    request: Request,
    lote_id: int,
    sid: int,
    canal: str = Form(""),
    version: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        _solicitud_del_lote(session, empresa_id, lote_id, sid)
        lt.marcar_enviada(session, empresa_id, user, sid, canal, _version(version))
    except _ERRORES as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    return _a_lote(lote_id, mensaje="Solicitud marcada como enviada")


@router.post("/lotes/{lote_id}/solicitudes/{sid}/sin-respuesta", response_model=None)
async def solicitud_sin_respuesta(
    request: Request,
    lote_id: int,
    sid: int,
    version: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        _solicitud_del_lote(session, empresa_id, lote_id, sid)
        lt.marcar_sin_respuesta(session, empresa_id, sid, _version(version))
    except _ERRORES as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    return _a_lote(lote_id, mensaje="Solicitud marcada sin respuesta")


def _si_no(valor: str) -> bool | None:
    return {"si": True, "no": False}.get((valor or "").strip().lower())


@router.post("/lotes/{lote_id}/solicitudes/{sid}/respuesta", response_model=None)
async def solicitud_respuesta(
    request: Request,
    lote_id: int,
    sid: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """Respuesta tipeada: `precio_<id>`, `tramos_<id>`, `disp_<id>` (si|no|''), `plazo_<id>`
    por `solicitud_lineas.id`."""
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user, empresa_id = ue
    try:
        _solicitud_del_lote(session, empresa_id, lote_id, sid)
        s, _ = lt.solicitud_de(session, empresa_id, sid)
        respuestas: dict[int, lt.Respuesta] = {}
        for f in lt.filas_de(session, s):
            i = f.solicitud_linea.id
            r = lt.validar_respuesta(
                str(form.get(f"precio_{i}", "")), str(form.get(f"tramos_{i}", "")),
                _si_no(str(form.get(f"disp_{i}", ""))), str(form.get(f"plazo_{i}", "")),
                bruto=True,  # F-plat-impuestos: la pantalla pide el precio con impuestos
            )  # fmt: skip
            respuestas[i] = r
        n = lt.registrar_respuesta(session, empresa_id, user, sid, respuestas,
                                   _version(str(form.get("version", ""))))  # fmt: skip
    except _ERRORES as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    return _a_lote(
        lote_id,
        mensaje=f"Respuesta registrada: {n} precio{'s' if n != 1 else ''} guardado{'s' if n != 1 else ''}",
    )


@router.post("/lotes/{lote_id}/solicitudes/{sid}/planilla", response_model=None)
async def solicitud_respuesta_planilla(
    request: Request,
    lote_id: int,
    sid: int,
    archivo: UploadFile = File(...),
    version: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """La planilla que devolvió el proveedor (la misma que se descargó, llena)."""
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    data = await archivo.read(importar.MAX_BYTES + 1)
    try:
        _solicitud_del_lote(session, empresa_id, lote_id, sid)
        if not (archivo.filename or "").lower().endswith((".xlsx", ".xlsm")):
            raise lt.ErrorLote("Sube la planilla .xlsx que descargaste para este proveedor")
        respuestas, errores = lt.leer_xlsx_respuesta(session, empresa_id, sid, data)
        if not respuestas:
            raise lt.ErrorLote(
                "La planilla no trae respuestas"
                + (": " + "; ".join(errores[:3]) if errores else "")
            )
        n = lt.registrar_respuesta(session, empresa_id, user, sid, respuestas, _version(version))
    except _ERRORES as exc:
        return _fallo(request, user, session, empresa_id, lote_id, exc)
    session.commit()
    texto = f"Planilla registrada: {n} precio{'s' if n != 1 else ''}"
    if errores:
        texto += (
            f" · {len(errores)} fila{'s' if len(errores) != 1 else ''} con error: "
            + "; ".join(errores[:3])
        )
    return _a_lote(lote_id, mensaje=texto)


def contexto_mesa(session: Session, empresa_id: int, codigos: list[str]) -> dict[str, Any]:
    """Lo que la mesa necesita para «Cotizar seleccionadas»: en qué lote está cada CA de la
    página y los lotes donde aún se puede agregar."""
    return {
        "lote_de": lt.lotes_de_codigos(session, empresa_id, codigos),
        "lotes_abiertos": lt.lotes_abiertos(session, empresa_id),
    }
