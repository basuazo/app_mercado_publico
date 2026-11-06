"""Capa «Inventario» (F-plat-capas): `/inventario`.

Lo que la empresa tiene o vende, con la misma llave `producto × formato` que la demanda y que el
catálogo de proveedores. Solo usuarios con empresa activa; TODA query filtra por el `empresa_id`
de la sesión y un id de otra empresa responde 404, nunca 403 (reglas 17 y 26). Mutaciones con CSRF
(regla 18). La planilla (hoja `inventario`, solo XLSX) es la forma principal de cargarlo.
"""

from __future__ import annotations

import base64
import binascii
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
from app.api.selector import contexto_selector, entero_o_none
from app.mercado import importar
from app.mercado import oferta_propia as op
from app.models.tables import Proveedor, Usuario

router = APIRouter()

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DESTINO_NUEVO = "inv-nuevo"


def _no_encontrado() -> HTTPException:
    return HTTPException(status_code=404, detail="Producto no encontrado")


def _proveedores(session: Session, empresa_id: int) -> list[Proveedor]:
    return list(
        session.execute(
            select(Proveedor)
            .where(Proveedor.empresa_id == empresa_id, Proveedor.activo.is_(True))
            .order_by(Proveedor.nombre)
        ).scalars()
    )


def _numero(texto: str, nombre: str, errores: dict[str, str], *, entero: bool = False) -> Any:
    """Número opcional de un campo de texto; un error se anota en `errores[nombre]`."""
    try:
        v = importar.convertir_numero(texto)
    except ValueError:
        errores[nombre] = "No es un número"
        return None
    if v is None:
        return None
    if v < 0:
        errores[nombre] = "No puede ser negativo"
        return None
    if entero:
        if v != int(v):
            errores[nombre] = "Debe ser un número entero"
            return None
        return int(v)
    return v


# ---------------------------------------------------------------------------
# Lista
# ---------------------------------------------------------------------------


@router.get("/inventario", response_class=HTMLResponse)
async def inventario_lista(
    request: Request,
    q: str = "",
    inactivos: str = "",
    mensaje: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    filas = op.listar(session, empresa_id, solo_activos=inactivos != "1", texto=q)
    return _TEMPLATES.TemplateResponse(
        request,
        "inventario.html",
        _ctx(
            request,
            user,
            filas=filas,
            q=q,
            inactivos=inactivos == "1",
            mensaje=mensaje,
            bajo_minimo=sum(1 for v in filas if v.ep.activo and v.bajo_minimo),
        ),
    )


# --- Plantilla e importación (antes de /inventario/{id}) ---------------------------


@router.get("/inventario/plantilla.xlsx")
async def inventario_plantilla(
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
) -> Response:
    return Response(
        content=importar.generar_plantilla_inventario_xlsx(),
        media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="inventario_plantilla.xlsx"'},
    )


def _pagina_importar(
    request: Request, user: Usuario, *, status_code: int = 200, **extra: Any
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "inventario_importar.html",
        _ctx(request, user, **{"resumen": None, "error": "", **extra}),
        status_code=status_code,
    )


@router.get("/inventario/importar", response_class=HTMLResponse)
async def inventario_importar_get(
    request: Request, ue: tuple[Usuario, int] = Depends(html_require_empresa)
) -> HTMLResponse:
    return _pagina_importar(request, ue[0])


@router.post("/inventario/importar", response_class=HTMLResponse)
async def inventario_importar_previa(
    request: Request,
    archivo: UploadFile = File(...),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    nombre = archivo.filename or ""
    data = await archivo.read(importar.MAX_BYTES + 1)
    try:
        planilla = importar.parsear_inventario(data, nombre)
    except importar.PlanillaInvalida as exc:
        return _pagina_importar(request, user, status_code=422, error=str(exc))
    # Vista previa = la misma escritura de la confirmación, revertida.
    resumen = importar.aplicar_inventario(session, empresa_id, planilla, usuario_id=user.id)
    session.rollback()
    return _pagina_importar(
        request,
        user,
        resumen=resumen,
        nombre_archivo=nombre,
        # El archivo viaja en el formulario: no se guarda en disco (regla 10).
        archivo_b64=base64.b64encode(data).decode("ascii"),
    )


@router.post("/inventario/importar/confirmar", response_model=None)
async def inventario_importar_confirmar(
    request: Request,
    nombre_archivo: str = Form(""),
    archivo_b64: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        data = base64.b64decode(archivo_b64.encode("ascii"), validate=True)
        planilla = importar.parsear_inventario(data, nombre_archivo)
    except (binascii.Error, UnicodeEncodeError, importar.PlanillaInvalida) as exc:
        return _pagina_importar(
            request, user, status_code=422, error=f"No pude procesar el archivo: {exc}"
        )
    resumen = importar.aplicar_inventario(session, empresa_id, planilla, usuario_id=user.id)
    session.commit()
    return _pagina_importar(request, user, resumen=resumen, confirmado=True)


# ---------------------------------------------------------------------------
# Agregar producto
# ---------------------------------------------------------------------------


def _form_nuevo(
    request: Request,
    user: Usuario,
    session: Session,
    empresa_id: int,
    valores: dict[str, str] | None = None,
    errores: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    v = valores or {}
    return _TEMPLATES.TemplateResponse(
        request,
        "_inventario_nuevo.html",
        _ctx(
            request,
            user,
            v=v,
            errores=errores or {},
            cat=contexto_selector(
                session, entero_o_none(v.get("producto_id")), entero_o_none(v.get("formato_id")),
                _DESTINO_NUEVO,
            ),
            proveedores=_proveedores(session, empresa_id),
        ),
        status_code=status_code,
    )


@router.get("/inventario/nuevo/form", response_class=HTMLResponse)
async def inventario_form_nuevo(
    request: Request,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    return _form_nuevo(request, ue[0], session, ue[1])


@router.post("/inventario", response_model=None)
async def inventario_crear(
    request: Request,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user, empresa_id = ue
    v = {
        k: str(form.get(k, ""))
        for k in ("producto_id", "formato_id", "stock", "stock_minimo", "plazo", "proveedor_id",
                  "marca", "notas")
    }  # fmt: skip
    errores: dict[str, str] = {}
    producto_id = entero_o_none(v["producto_id"])
    if producto_id is None:
        errores["producto_id"] = "Elige un producto del catálogo"
    stock = _numero(v["stock"], "stock", errores)
    minimo = _numero(v["stock_minimo"], "stock_minimo", errores)
    plazo = _numero(v["plazo"], "plazo", errores, entero=True)
    if not errores:
        assert producto_id is not None
        try:
            ep, estado = op.guardar(
                session,
                empresa_id,
                user.id,
                producto_id=producto_id,
                formato_id=entero_o_none(v["formato_id"]),
                stock=stock,
                stock_minimo=minimo,
                plazo=plazo,
                proveedor_habitual_id=entero_o_none(v["proveedor_id"]),
                marca=v["marca"],
                notas=v["notas"],
                referencia="alta",
            )
            if estado != "nuevo":
                session.rollback()
                errores["producto_id"] = (
                    "Ya tienes ese producto en ese formato: edítalo en la tabla"
                    if ep.activo
                    else "Ese producto está desactivado: muéstralo con «Ver inactivos» y reactívalo"
                )
        except op.ErrorInventario as exc:
            session.rollback()
            errores["producto_id"] = str(exc)
        except op.InventarioAjeno:
            session.rollback()
            raise _no_encontrado() from None
    if errores:
        return _form_nuevo(request, user, session, empresa_id, v, errores, 422)
    session.commit()
    url = "/inventario?mensaje=" + quote("Producto agregado al inventario.")
    if _es_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


# ---------------------------------------------------------------------------
# Fila: ver, editar, ajustar stock
# ---------------------------------------------------------------------------


def _fila(
    request: Request,
    user: Usuario,
    session: Session,
    empresa_id: int,
    ep_id: int,
    *,
    plantilla: str = "_inventario_fila.html",
    **extra: Any,
) -> HTMLResponse:
    try:
        vista = op.vista_de(session, empresa_id, ep_id)
    except op.InventarioAjeno:
        raise _no_encontrado() from None
    return _TEMPLATES.TemplateResponse(
        request,
        plantilla,
        _ctx(request, user, v=vista, proveedores=_proveedores(session, empresa_id), **extra),
    )


@router.get("/inventario/{ep_id}/fila", response_class=HTMLResponse)
async def inventario_fila(
    request: Request,
    ep_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    return _fila(request, ue[0], session, ue[1], ep_id)


@router.get("/inventario/{ep_id}/form", response_class=HTMLResponse)
async def inventario_form_editar(
    request: Request,
    ep_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    return _fila(request, ue[0], session, ue[1], ep_id, plantilla="_inventario_edicion.html",
                 errores={})  # fmt: skip


@router.post("/inventario/{ep_id}", response_model=None)
async def inventario_editar(
    request: Request,
    ep_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """Edición en línea: mínimo, plazo, proveedor habitual, marca, notas y activo (el stock
    se mueve con «Ajustar stock», que deja su movimiento)."""
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user, empresa_id = ue
    try:
        op.obtener(session, empresa_id, ep_id)
    except op.InventarioAjeno:
        raise _no_encontrado() from None
    errores: dict[str, str] = {}
    minimo = _numero(str(form.get("stock_minimo", "")), "stock_minimo", errores)
    plazo = _numero(str(form.get("plazo", "")), "plazo", errores, entero=True)
    if not errores:
        try:
            op.actualizar(
                session,
                empresa_id,
                ep_id,
                user.id,
                stock_minimo=minimo,
                plazo=plazo,
                proveedor_habitual_id=entero_o_none(form.get("proveedor_id")),
                marca=str(form.get("marca", "")),
                notas=str(form.get("notas", "")),
                activo=bool(form.get("activo")),
            )
        except op.ErrorInventario as exc:
            session.rollback()
            errores["plazo"] = str(exc)
        except op.InventarioAjeno:
            session.rollback()
            raise _no_encontrado() from None
    if errores:
        r = _fila(request, user, session, empresa_id, ep_id, plantilla="_inventario_edicion.html",
                  errores=errores)  # fmt: skip
        r.status_code = 422
        return r
    session.commit()
    return _fila(request, user, session, empresa_id, ep_id)


@router.get("/inventario/{ep_id}/ajuste", response_class=HTMLResponse)
async def inventario_form_ajuste(
    request: Request,
    ep_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    return _fila(request, ue[0], session, ue[1], ep_id, plantilla="_inventario_ajuste.html",
                 errores={}, valores={}, motivos=op.MOTIVOS_AJUSTE, etiquetas=op.ETIQUETA_MOTIVO)  # fmt: skip


@router.post("/inventario/{ep_id}/stock", response_model=None)
async def inventario_ajustar(
    request: Request,
    ep_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """«Ajustar stock»: delta con signo + motivo (+ referencia). Nunca deja el stock negativo."""
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user, empresa_id = ue
    valores = {k: str(form.get(k, "")) for k in ("delta", "motivo", "referencia")}
    errores: dict[str, str] = {}
    try:
        delta = importar.convertir_numero(valores["delta"])
    except ValueError:
        delta = None
    if delta is None:
        errores["delta"] = "Escribe la cantidad (con signo: 5 suma, -2 resta)"
    else:
        try:
            op.ajustar_stock(
                session, empresa_id, ep_id, delta, valores["motivo"], valores["referencia"], user.id
            )
        except op.ErrorInventario as exc:
            session.rollback()
            errores["delta"] = str(exc)
        except op.InventarioAjeno:
            session.rollback()
            raise _no_encontrado() from None
    if errores:
        r = _fila(request, user, session, empresa_id, ep_id, plantilla="_inventario_ajuste.html",
                  errores=errores, valores=valores, motivos=op.MOTIVOS_AJUSTE,
                  etiquetas=op.ETIQUETA_MOTIVO)  # fmt: skip
        r.status_code = 422
        return r
    session.commit()
    return _fila(request, user, session, empresa_id, ep_id)


@router.get("/inventario/{ep_id}/movimientos", response_class=HTMLResponse)
async def inventario_movimientos(
    request: Request,
    ep_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        filas = op.movimientos(session, ue[1], ep_id)
    except op.InventarioAjeno:
        raise _no_encontrado() from None
    return _TEMPLATES.TemplateResponse(
        request,
        "_inventario_movimientos.html",
        _ctx(request, ue[0], filas=filas, etiquetas=op.ETIQUETA_MOTIVO, ep_id=ep_id),
    )
