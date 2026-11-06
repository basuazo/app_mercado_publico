"""Checklist, cotización formal, ventas y caja (F-plat-seguimiento).

Solo usuarios con empresa activa (`html_require_empresa`); TODA query filtra por el `empresa_id` de la
sesión y un id de otra empresa responde 404 (reglas 17 y 26). Mutaciones con CSRF (regla 18).
El DOCX se genera en memoria y se devuelve: no se guarda (regla 11); el número correlativo sí.
Patrón de las ventas: POST → 303 a `/ventas/{id}` con `mensaje`; un error se pinta en la misma
pantalla (422 entrada, 409 conflicto de versión). Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import check_csrf, get_db, html_require_empresa
from app.api.routes.mesa_precio import _ca_o_404
from app.api.routes.pages import _TEMPLATES, _ctx, _es_htmx
from app.auth.csrf import generate_csrf_token
from app.mercado import cotizacion as cot
from app.mercado import equipo as eq
from app.mercado import oferta as of
from app.mercado import oferta_propia as op
from app.mercado import requisitos as rq
from app.mercado import ventas as vt
from app.models.enums import RolUsuario
from app.models.tables import Empresa, Usuario

router = APIRouter()

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_ERRORES_VENTA = (vt.ErrorVenta, op.ErrorInventario)


def _csrf(request: Request) -> str:
    return generate_csrf_token(request.app.state.settings.secret_key, request.state.csrf_nonce)


def _ficha(codigo: str) -> str:
    return f"/oportunidad/compras_agiles/{quote(codigo, safe='')}"


# ---------------------------------------------------------------------------
# Requisitos de una CA (bloque HTMX de la ficha)
# ---------------------------------------------------------------------------


def _bloque_requisitos(
    request: Request,
    session: Session,
    user: Usuario,
    empresa_id: int,
    codigo: str,
    *,
    mensaje: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    session.expire_all()
    oferta = of.oferta_de(session, empresa_id, codigo)
    empresa = session.get(Empresa, empresa_id)
    t = eq.trabajo_de(session, empresa_id, codigo)
    venta = vt.venta_de_ca(session, empresa_id, codigo)
    reqs = rq.listar(session, empresa_id, codigo)
    return _TEMPLATES.TemplateResponse(
        request,
        "_requisitos.html",
        {
            "codigo": codigo,
            "reqs": reqs,
            "pendientes": [r for r in reqs if r.obligatorio and not r.cumplido],
            "oferta": oferta,
            "condiciones": cot.condiciones_de(oferta, empresa) if oferta and empresa else "",
            "venta": venta,
            "puede_registrar_venta": venta is None and t is not None and t.estado == eq.ENVIADA,
            "etiqueta_venta": vt.ETIQUETA_ESTADO,
            "mensaje": mensaje,
            "error": error,
            "csrf_token": _csrf(request),
        },
        status_code=status_code,
    )


@router.get("/requisitos/ca/{codigo}", response_class=HTMLResponse)
async def requisitos_bloque(
    request: Request,
    codigo: str,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """El bloque «Requisitos» de la ficha. Al abrirlo siembra el checklist si la CA ya está en
    trabajo (idempotente): así una CA priorizada antes de esta fase también lo tiene."""
    user, empresa_id = ue
    _ca_o_404(request, session, codigo)
    t = eq.trabajo_de(session, empresa_id, codigo)
    if (
        t is not None
        and t.estado in eq.ACTIVOS + (eq.ENVIADA,)
        and rq.sembrar_ca(session, empresa_id, codigo)
    ):
        session.commit()
    return _bloque_requisitos(request, session, user, empresa_id, codigo)


def _mutar_requisitos(
    request: Request,
    session: Session,
    user: Usuario,
    empresa_id: int,
    codigo: str,
    accion: Callable[[], Any],
) -> Response:
    try:
        accion()
        session.commit()
    except rq.RequisitoAjeno:
        session.rollback()
        raise HTTPException(status_code=404, detail="Requisito no encontrado") from None
    except rq.ErrorRequisito as err:
        session.rollback()
        if not _es_htmx(request):
            raise HTTPException(status_code=422, detail=str(err)) from err
        return _bloque_requisitos(
            request, session, user, empresa_id, codigo, error=str(err), status_code=422
        )
    if not _es_htmx(request):
        return RedirectResponse(url=_ficha(codigo), status_code=303)
    return _bloque_requisitos(request, session, user, empresa_id, codigo)


@router.post("/requisitos/ca/{codigo}/marcar/{req_id}", response_model=None)
async def requisito_marcar(
    request: Request,
    codigo: str,
    req_id: int,
    cumplido: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    _ca_o_404(request, session, codigo)
    return _mutar_requisitos(
        request, session, user, empresa_id, codigo,
        lambda: _marcar_de_la_ca(session, empresa_id, user, codigo, req_id, bool(cumplido)),
    )  # fmt: skip


def _marcar_de_la_ca(
    session: Session, empresa_id: int, user: Usuario, codigo: str, req_id: int, cumplido: bool
) -> None:
    r = rq.marcar(session, empresa_id, user.id, req_id, cumplido)
    if r.ca_codigo != codigo:  # el id es de la empresa pero de otra CA: no es de esta ficha
        raise rq.RequisitoAjeno(req_id)


@router.post("/requisitos/ca/{codigo}/agregar", response_model=None)
async def requisito_agregar(
    request: Request,
    codigo: str,
    texto: str = Form(""),
    obligatorio: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    _ca_o_404(request, session, codigo)
    return _mutar_requisitos(
        request, session, user, empresa_id, codigo,
        lambda: rq.agregar_manual(session, empresa_id, codigo, texto, bool(obligatorio)),
    )  # fmt: skip


@router.post("/requisitos/ca/{codigo}/quitar/{req_id}", response_model=None)
async def requisito_quitar(
    request: Request,
    codigo: str,
    req_id: int,
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    _ca_o_404(request, session, codigo)

    def _quitar() -> None:
        quitar_de = rq.listar(session, empresa_id, codigo)
        if not any(r.id == req_id for r in quitar_de):
            raise rq.RequisitoAjeno(req_id)
        rq.quitar(session, empresa_id, req_id)

    return _mutar_requisitos(request, session, user, empresa_id, codigo, _quitar)


# ---------------------------------------------------------------------------
# Plantilla de la empresa
# ---------------------------------------------------------------------------


def _pagina_plantilla(
    request: Request,
    user: Usuario,
    session: Session,
    empresa_id: int,
    *,
    mensaje: str = "",
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "requisitos_plantilla.html",
        _ctx(
            request,
            user,
            items=rq.plantilla(session, empresa_id),
            mensaje=mensaje,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/requisitos", response_class=HTMLResponse)
async def requisitos_plantilla(
    request: Request,
    mensaje: str = "",
    error: str = "",
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = ue
    return _pagina_plantilla(request, user, session, empresa_id, mensaje=mensaje, error=error)


@router.post("/requisitos/plantilla/agregar", response_model=None)
async def plantilla_agregar(
    request: Request,
    texto: str = Form(""),
    obligatorio: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        rq.agregar_a_plantilla(session, empresa_id, texto, bool(obligatorio))
        session.commit()
    except rq.ErrorRequisito as err:
        session.rollback()
        return _pagina_plantilla(request, user, session, empresa_id, error=str(err), status_code=422)
    return RedirectResponse(url=f"/requisitos?mensaje={quote('Requisito agregado')}", status_code=303)


@router.post("/requisitos/plantilla/{req_id}", response_model=None)
async def plantilla_editar(
    request: Request,
    req_id: int,
    texto: str = Form(""),
    obligatorio: str = Form(""),
    activo: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    try:
        rq.editar_plantilla(
            session, empresa_id, req_id,
            texto=texto, obligatorio=bool(obligatorio), activo=bool(activo),
        )  # fmt: skip
        session.commit()
    except rq.RequisitoAjeno:
        session.rollback()
        raise HTTPException(status_code=404, detail="Requisito no encontrado") from None
    except rq.ErrorRequisito as err:
        session.rollback()
        return _pagina_plantilla(request, user, session, empresa_id, error=str(err), status_code=422)
    return RedirectResponse(url=f"/requisitos?mensaje={quote('Plantilla guardada')}", status_code=303)


# ---------------------------------------------------------------------------
# Cotización formal (DOCX)
# ---------------------------------------------------------------------------


def _nombre_archivo(codigo: str) -> str:
    return "".join(ch for ch in codigo if ch.isalnum() or ch in "-_") or "oferta"


def _entregar_docx(
    request: Request,
    session: Session,
    empresa_id: int,
    codigo: str,
    *,
    validez: str | None = None,
    condiciones: str | None = None,
) -> Response:
    ca = _ca_o_404(request, session, codigo)
    oferta = of.oferta_de(session, empresa_id, codigo)
    empresa = session.get(Empresa, empresa_id)
    if oferta is None or empresa is None:
        raise HTTPException(status_code=404, detail="Guarda la oferta antes de generar la cotización")
    try:
        cot.configurar(oferta, validez, condiciones)
        numero = cot.asignar_numero(session, empresa_id, oferta)
        cot.marcar_generada(oferta)
        lineas = of.armar(session, empresa_id, ca, None, oferta)
        contenido = cot.docx(empresa, ca, oferta, lineas)
        session.commit()
    except cot.ErrorCotizacion as err:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(err)) from err
    return Response(
        content=contenido,
        media_type=_DOCX,
        headers={
            "Content-Disposition": f'attachment; filename="cotizacion-{_nombre_archivo(codigo)}.docx"',
            "X-Numero-Cotizacion": numero,
        },
    )


@router.get("/mesa/ca/{codigo}/cotizacion.docx")
async def cotizacion_docx(
    request: Request,
    codigo: str,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """Descarga la cotización formal de la oferta GUARDADA. La primera vez asigna el número."""
    return _entregar_docx(request, session, ue[1], codigo)


@router.post("/mesa/ca/{codigo}/cotizacion.docx")
async def cotizacion_docx_configurada(
    request: Request,
    codigo: str,
    validez_dias: str = Form(""),
    condiciones: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """Guarda la validez y las condiciones de ESTA oferta y descarga el documento."""
    check_csrf(request, csrf_token)
    return _entregar_docx(
        request, session, ue[1], codigo, validez=validez_dias, condiciones=condiciones
    )


# ---------------------------------------------------------------------------
# Ventas
# ---------------------------------------------------------------------------


def _venta_404() -> HTTPException:
    return HTTPException(status_code=404, detail="Venta no encontrada")


@router.get("/ventas", response_class=HTMLResponse)
async def ventas_lista(
    request: Request,
    estado: str = "",
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = ue
    estado = estado if estado in vt.ESTADOS else ""
    return _TEMPLATES.TemplateResponse(
        request,
        "ventas.html",
        _ctx(
            request,
            user,
            filas=vt.listar(session, empresa_id, estado or None),
            conteo=vt.conteo_por_estado(session, empresa_id),
            estado=estado,
            estados=vt.ETIQUETA_ESTADO,
        ),
    )


def _pagina_venta(
    request: Request,
    user: Usuario,
    session: Session,
    empresa_id: int,
    venta_id: int,
    *,
    status_code: int = 200,
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    session.expire_all()
    try:
        v = vt.vista(session, empresa_id, venta_id)
    except vt.VentaAjena:
        raise _venta_404() from None
    return _TEMPLATES.TemplateResponse(
        request,
        "venta.html",
        _ctx(
            request,
            user,
            v=v,
            opciones_compra=vt.productos_de_proveedores(session, empresa_id, v.venta),
            estados=vt.ETIQUETA_ESTADO,
            mensaje=mensaje,
            error=error,
            pendientes_de_entrega=vt.PENDIENTES_DE_ENTREGA,
        ),
        status_code=status_code,
    )


@router.get("/ventas/{venta_id}", response_class=HTMLResponse)
async def venta_detalle(
    request: Request,
    venta_id: int,
    mensaje: str = "",
    error: str = "",
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = ue
    return _pagina_venta(request, user, session, empresa_id, venta_id, mensaje=mensaje, error=error)


def _version(texto: str) -> int | None:
    try:
        return int(texto) if texto.strip() else None
    except ValueError:
        return -1  # nunca calza: se informa el conflicto con lo que hay


def _mutar_venta(
    request: Request,
    user: Usuario,
    session: Session,
    empresa_id: int,
    venta_id: int,
    accion: Callable[[], Any],
    mensaje: str,
) -> Response:
    """Corre una mutación de la venta: éxito → 303 al detalle; error → la pantalla con el motivo."""
    try:
        resultado = accion()
        session.commit()
    except (vt.VentaAjena, of.ProveedorAjeno):
        session.rollback()
        raise _venta_404() from None
    except vt.ConflictoVenta as c:
        session.rollback()
        return _pagina_venta(
            request, user, session, empresa_id, venta_id, status_code=409,
            error=f"{c.quien} cambió esta venta mientras la veías: revisa y vuelve a intentarlo",
        )  # fmt: skip
    except _ERRORES_VENTA as err:
        session.rollback()
        return _pagina_venta(
            request, user, session, empresa_id, venta_id, status_code=422, error=str(err)
        )
    avisos = resultado[1] if isinstance(resultado, tuple) and len(resultado) == 2 else []
    texto = mensaje + (" Avisos: " + "; ".join(avisos) + "." if avisos else "")
    return RedirectResponse(url=f"/ventas/{venta_id}?mensaje={quote(texto)}", status_code=303)


@router.post("/ventas/nueva", response_model=None)
async def venta_nueva(
    request: Request,
    codigo: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """«Registrar venta» a mano desde una CA enviada (la automática nace al detectar `ganada`)."""
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    _ca_o_404(request, session, codigo)
    try:
        v = vt.crear_manual(session, empresa_id, user, codigo)
        session.commit()
    except vt.ErrorVenta as err:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(err)) from err
    return RedirectResponse(url=f"/ventas/{v.id}", status_code=303)


@router.post("/ventas/{venta_id}/oc", response_model=None)
async def venta_oc(
    request: Request,
    venta_id: int,
    version: str = Form(""),
    oc_numero: str = Form(""),
    oc_fecha: str = Form(""),
    oc_monto: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    return _mutar_venta(
        request, user, session, empresa_id, venta_id,
        lambda: vt.registrar_oc(session, empresa_id, user, venta_id, _version(version),
                                numero=oc_numero, fecha=oc_fecha, monto=oc_monto),
        "Orden de compra registrada.",
    )  # fmt: skip


@router.post("/ventas/{venta_id}/logistica", response_model=None)
async def venta_logistica(
    request: Request,
    venta_id: int,
    version: str = Form(""),
    direccion: str = Form(""),
    horario: str = Form(""),
    recibe: str = Form(""),
    comprometida: str = Form(""),
    notas: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    return _mutar_venta(
        request, user, session, empresa_id, venta_id,
        lambda: vt.guardar_entrega_datos(session, empresa_id, user, venta_id, _version(version),
                                         direccion=direccion, horario=horario, recibe=recibe,
                                         comprometida=comprometida, notas=notas),
        "Datos de entrega guardados.",
    )  # fmt: skip


@router.post("/ventas/{venta_id}/compras", response_model=None)
async def venta_compra(
    request: Request,
    venta_id: int,
    version: str = Form(""),
    proveedor_producto_id: str = Form(""),
    cantidad: str = Form(""),
    precio_neto: str = Form(""),
    fecha: str = Form(""),
    pagar_en: str = Form(""),
    pagada: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    if not proveedor_producto_id.strip().isdigit():
        return _pagina_venta(request, user, session, empresa_id, venta_id, status_code=422,
                             error="Elige el producto del proveedor")  # fmt: skip
    return _mutar_venta(
        request, user, session, empresa_id, venta_id,
        lambda: vt.registrar_compra(session, empresa_id, user, venta_id, _version(version),
                                    proveedor_producto_id=int(proveedor_producto_id),
                                    cantidad=cantidad, precio_neto=precio_neto, fecha=fecha,
                                    pagada=bool(pagada), pagar_en=pagar_en),
        "Compra registrada.",
    )  # fmt: skip


@router.post("/ventas/{venta_id}/compras/{compra_id}/pagada", response_model=None)
async def venta_compra_pagada(
    request: Request,
    venta_id: int,
    compra_id: int,
    pagada: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue

    def _marcar() -> None:
        c = vt.marcar_compra_pagada(session, empresa_id, compra_id, bool(pagada))
        if c.venta_id != venta_id:
            raise vt.VentaAjena(compra_id)

    return _mutar_venta(request, user, session, empresa_id, venta_id, _marcar, "Compra actualizada.")


@router.post("/ventas/{venta_id}/entrega", response_model=None)
async def venta_entrega(
    request: Request,
    venta_id: int,
    version: str = Form(""),
    fecha_real: str = Form(""),
    guia: str = Form(""),
    recibe: str = Form(""),
    horario: str = Form(""),
    direccion: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    return _mutar_venta(
        request, user, session, empresa_id, venta_id,
        lambda: vt.registrar_entrega(session, empresa_id, user, venta_id, _version(version),
                                     fecha_real=fecha_real, guia=guia, recibe=recibe,
                                     horario=horario, direccion=direccion),
        "Entrega registrada: el stock se descontó.",
    )  # fmt: skip


@router.post("/ventas/{venta_id}/factura", response_model=None)
async def venta_factura(
    request: Request,
    venta_id: int,
    version: str = Form(""),
    factura_numero: str = Form(""),
    factura_fecha: str = Form(""),
    factura_monto: str = Form(""),
    plazo_pago_dias: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    return _mutar_venta(
        request, user, session, empresa_id, venta_id,
        lambda: vt.registrar_factura(session, empresa_id, user, venta_id, _version(version),
                                     numero=factura_numero, fecha=factura_fecha,
                                     monto=factura_monto, plazo_pago_dias=plazo_pago_dias),
        "Factura registrada.",
    )  # fmt: skip


@router.post("/ventas/{venta_id}/pago", response_model=None)
async def venta_pago(
    request: Request,
    venta_id: int,
    version: str = Form(""),
    fecha: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    return _mutar_venta(
        request, user, session, empresa_id, venta_id,
        lambda: vt.registrar_pago(session, empresa_id, user, venta_id, _version(version), fecha=fecha),
        "Pago registrado.",
    )  # fmt: skip


@router.post("/ventas/{venta_id}/anular", response_model=None)
async def venta_anular(
    request: Request,
    venta_id: int,
    version: str = Form(""),
    motivo: str = Form(""),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = ue
    return _mutar_venta(
        request, user, session, empresa_id, venta_id,
        lambda: vt.anular(session, empresa_id, user, venta_id, _version(version), motivo),
        "Venta anulada.",
    )  # fmt: skip


# ---------------------------------------------------------------------------
# Caja
# ---------------------------------------------------------------------------


@router.get("/caja", response_class=HTMLResponse)
async def caja_pagina(
    request: Request,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = ue
    return _TEMPLATES.TemplateResponse(
        request,
        "caja.html",
        _ctx(request, user, c=vt.caja(session, empresa_id), horizontes=vt.HORIZONTES_CAJA),
    )


def es_admin(user: Usuario) -> bool:
    return user.rol == RolUsuario.ADMIN
