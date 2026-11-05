"""Curación del catálogo canónico (F-plat-1): `/catalogo`.

Usuarios con empresa activa (el admin sin empresa no entra: regla 26). El catálogo es COMÚN:
las correcciones son compartidas y `revisado_por` deja la huella. Crear y editar productos:
cualquier usuario con empresa; fusionar: solo admin. Las líneas se ven solo para CA de las
regiones activas. Los mensajes de error no filtran datos de otras empresas: no hay ninguno.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import check_csrf, get_db, html_require_empresa
from app.api.routes.pages import _TEMPLATES, _ctx
from app.catalogos.canonico import normalizar_texto
from app.catalogos.mercado import FAMILIAS_ALIMENTOS, norm
from app.mercado import anexos
from app.mercado import catalogo as cat
from app.mercado.territorio import regiones_activas
from app.models.enums import RolUsuario
from app.models.tables import (
    CaAnexo,
    CaLinea,
    CompraAgil,
    ProductoAlias,
    ProductoCanonico,
    ProductoFormato,
    Usuario,
)

router = APIRouter()

POR_PAGINA = 25
_TABS = ("pendientes", "productos", "alias")
ETIQUETA_BANDERA = {
    "completo": ("Completo", "success"),
    "incompleto": ("Líneas por revisar", "warning"),
    "sin_detalle": ("Sin detalle (anexo)", "secondary"),
}


def _csv_float(texto: str) -> float | None:
    t = (texto or "").strip().replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _linea_o_404(
    session: Session, linea_id: int, regiones: list[int]
) -> tuple[CaLinea, CompraAgil]:
    fila = session.execute(
        select(CaLinea, CompraAgil)
        .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
        .where(CaLinea.id == linea_id, CompraAgil.region.in_(regiones))
    ).first()
    if fila is None:
        raise HTTPException(status_code=404, detail="Línea no encontrada")
    return fila[0], fila[1]


def _ca_o_404(session: Session, codigo: str, regiones: list[int]) -> CompraAgil:
    ca = session.get(CompraAgil, codigo)
    if ca is None or ca.region not in regiones:
        raise HTTPException(status_code=404, detail="Compra Ágil no encontrada")
    return ca


def _productos_activos(session: Session, q: str = "", limite: int = 30) -> list[ProductoCanonico]:
    filas = session.execute(
        select(ProductoCanonico)
        .where(ProductoCanonico.activo.is_(True))
        .order_by(ProductoCanonico.nombre)
    ).scalars()
    nq = norm(q)
    out = [p for p in filas if not nq or nq in norm(p.nombre)]
    return out[:limite]


def _formatos_de(session: Session, producto_id: int | None) -> list[ProductoFormato]:
    if producto_id is None:
        return []
    return list(
        session.execute(
            select(ProductoFormato)
            .where(ProductoFormato.producto_id == producto_id)
            .order_by(ProductoFormato.factor_base, ProductoFormato.nombre)
        ).scalars()
    )


def _vista_linea(
    session: Session, ln: CaLinea, ca: CompraAgil, nombres: dict[int, str]
) -> dict[str, Any]:
    nombre = nombres.get(ln.producto_id) if ln.producto_id else None
    return {
        "ln": ln,
        "ca": ca,
        "producto_nombre": nombre,
        # El selector parte con el producto sugerido o confirmado; el resto se busca por HTMX.
        "productos": [{"id": ln.producto_id, "nombre": nombre}] if nombre else [],
        "formatos": _formatos_de(session, ln.producto_id),
    }


def _nombres_productos(session: Session) -> dict[int, str]:
    filas = session.execute(select(ProductoCanonico.id, ProductoCanonico.nombre)).tuples()
    return {i: n for i, n in filas}


def _html_fila(
    request: Request, user: Usuario, session: Session, ln: CaLinea, ca: CompraAgil, vista: str
) -> str:
    ctx = _ctx(
        request, user, v=_vista_linea(session, ln, ca, _nombres_productos(session)), vista=vista
    )
    return _TEMPLATES.get_template("_catalogo_linea.html").render(ctx)


# ===========================================================================
# Página principal
# ===========================================================================


@router.get("/catalogo", response_class=HTMLResponse)
async def catalogo_get(
    request: Request,
    tab: str = "pendientes",
    q: str = "",
    pagina: int = 1,
    mensaje: str = "",
    error: str = "",
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, _empresa_id = ctx
    tab = tab if tab in _TABS else "pendientes"
    regiones = regiones_activas(session, request.app.state.settings)
    pagina = max(1, pagina)
    datos: dict[str, Any] = {}

    resumen_estados: dict[str, int] = {
        e: n
        for e, n in session.execute(
            select(CaLinea.estado, func.count())
            .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
            .where(CompraAgil.region.in_(regiones))
            .group_by(CaLinea.estado)
        ).tuples()
    }
    if tab == "pendientes":
        base = (
            select(CaLinea, CompraAgil)
            .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
            .where(CompraAgil.region.in_(regiones), CaLinea.estado.in_(("pendiente", "dudosa")))
        )
        total = session.execute(
            select(func.count()).select_from(base.order_by(None).subquery())
        ).scalar_one()
        filas = session.execute(
            base.order_by(
                CompraAgil.monto_disponible_clp.desc().nulls_last(), CaLinea.ca_codigo, CaLinea.n
            )
            .limit(POR_PAGINA)
            .offset((pagina - 1) * POR_PAGINA)
        ).all()
        nombres = _nombres_productos(session)
        datos = {
            "lineas": [_vista_linea(session, ln, ca, nombres) for ln, ca in filas],
            "total": total,
        }
    elif tab == "productos":
        nq = norm(q)
        todos_p = [
            p
            for p in session.execute(
                select(ProductoCanonico).order_by(
                    ProductoCanonico.activo.desc(), ProductoCanonico.nombre
                )
            ).scalars()
            if not nq or nq in norm(p.nombre) or nq in norm(p.familia or "")
        ]
        total = len(todos_p)
        pagina_p = todos_p[(pagina - 1) * POR_PAGINA : pagina * POR_PAGINA]
        conteo_lineas: dict[int, int] = {
            pid: n
            for pid, n in session.execute(
                select(CaLinea.producto_id, func.count())
                .where(CaLinea.producto_id.isnot(None), CaLinea.estado == "ok")
                .group_by(CaLinea.producto_id)
            ).tuples()
            if pid is not None
        }
        datos = {
            "productos": pagina_p,
            "total": total,
            "n_lineas": conteo_lineas,
            "familias": sorted(set(FAMILIAS_ALIMENTOS.values())),
        }
    else:
        nq = normalizar_texto(q)
        consulta = (
            select(ProductoAlias, ProductoCanonico.nombre, Usuario.email)
            .join(ProductoCanonico, ProductoCanonico.id == ProductoAlias.producto_id)
            .outerjoin(Usuario, Usuario.id == ProductoAlias.revisado_por)
        )
        if nq:
            consulta = consulta.where(ProductoAlias.texto_normalizado.contains(nq))
        total = session.execute(
            select(func.count()).select_from(consulta.order_by(None).subquery())
        ).scalar_one()
        filas_a = session.execute(
            consulta.order_by(ProductoAlias.veces_visto.desc(), ProductoAlias.id.desc())
            .limit(POR_PAGINA)
            .offset((pagina - 1) * POR_PAGINA)
        ).all()
        datos = {"alias": filas_a, "total": total}

    return _TEMPLATES.TemplateResponse(
        request,
        "catalogo.html",
        _ctx(
            request,
            user,
            tab=tab,
            q=q,
            pagina=pagina,
            por_pagina=POR_PAGINA,
            resumen=resumen_estados,
            mensaje=mensaje,
            error=error,
            es_admin=user.rol == RolUsuario.ADMIN,
            **datos,
        ),
    )


# ===========================================================================
# Selectores HTMX (producto y formato)
# ===========================================================================


@router.get("/catalogo/buscar-producto", response_class=HTMLResponse)
async def catalogo_buscar_producto(
    request: Request,
    linea_id: int,
    q: str = "",
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, _ = ctx
    productos = _productos_activos(session, q, 30)
    return _TEMPLATES.TemplateResponse(
        request,
        "_catalogo_selector.html",
        _ctx(
            request,
            user,
            linea_id=linea_id,
            productos=productos,
            producto_sel=productos[0].id if productos else None,
            formatos=_formatos_de(session, productos[0].id if productos else None),
            formato_sel=None,
        ),
    )


@router.get("/catalogo/formatos", response_class=HTMLResponse)
async def catalogo_formatos(
    request: Request,
    linea_id: int,
    producto_id: int | None = None,
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, _ = ctx
    return _TEMPLATES.TemplateResponse(
        request,
        "_catalogo_formatos.html",
        _ctx(
            request,
            user,
            linea_id=linea_id,
            formatos=_formatos_de(session, producto_id),
            formato_sel=None,
        ),
    )


# ===========================================================================
# Líneas: confirmar, anexo, manuales
# ===========================================================================


def _aviso(texto: str, clase: str = "success") -> str:
    from markupsafe import escape

    return (
        f'<div id="catalogo-aviso" hx-swap-oob="true" class="alert alert-{clase} py-2" '
        f'role="status">{escape(texto)}</div>'
    )


def _respuesta_linea(
    request: Request,
    user: Usuario,
    session: Session,
    ln: CaLinea,
    ca: CompraAgil,
    vista: str,
    texto_aviso: str,
) -> Response:
    """Pendientes: la fila desaparece (200 vacío + aviso fuera de banda; nunca 204, que HTMX
    ignora). Ficha de CA y tabla del anexo: la fila se repinta con su estado nuevo."""
    if vista == "pendientes":
        return HTMLResponse(_aviso(texto_aviso))
    return HTMLResponse(_html_fila(request, user, session, ln, ca, vista) + _aviso(texto_aviso))


def _volver(request: Request, vista: str, ca_codigo: str, mensaje: str) -> RedirectResponse:
    destino = f"/catalogo/ca/{quote(ca_codigo)}" if vista in ("ca", "anexo") else "/catalogo"
    return RedirectResponse(f"{destino}?mensaje={quote(mensaje)}", status_code=303)


@router.post("/catalogo/lineas/{linea_id}/confirmar", response_model=None)
async def catalogo_confirmar(
    request: Request,
    linea_id: int,
    producto_id: int = Form(...),
    formato_id: str = Form(""),
    cantidad: str = Form(""),
    vista: str = Form("pendientes"),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, _ = ctx
    regiones = regiones_activas(session, request.app.state.settings)
    ln, ca = _linea_o_404(session, linea_id, regiones)
    try:
        nueva_cantidad = None
        if cantidad.strip() and ln.anexo_id is not None:
            nueva_cantidad = _csv_float(cantidad)
            if nueva_cantidad is None:
                raise cat.CatalogoError("La cantidad debe ser un número")
        otras = cat.confirmar_linea(
            session,
            ln,
            producto_id=producto_id,
            formato_id=int(formato_id) if formato_id.strip().isdigit() else None,
            usuario_id=user.id,
            cantidad=nueva_cantidad,
        )
    except cat.CatalogoError as exc:
        session.rollback()
        # Sin swap (el aviso va fuera de banda): la fila se queda para corregir y reintentar.
        return HTMLResponse(_aviso(str(exc), "danger"), headers={"HX-Reswap": "none"})
    session.commit()
    texto = "Línea confirmada." + (
        f" Se aplicó a {otras} línea(s) más con el mismo texto." if otras else ""
    )
    if request.headers.get("HX-Request") != "true":
        return _volver(request, vista, ln.ca_codigo, texto)
    session.refresh(ln)
    return _respuesta_linea(request, user, session, ln, ca, vista, texto)


@router.post("/catalogo/lineas/{linea_id}/anexo", response_model=None)
async def catalogo_anexo(
    request: Request,
    linea_id: int,
    vista: str = Form("pendientes"),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, _ = ctx
    regiones = regiones_activas(session, request.app.state.settings)
    ln, ca = _linea_o_404(session, linea_id, regiones)
    cat.marcar_sin_detalle(session, ln, user.id)
    session.commit()
    texto = "Marcada como «sin detalle (anexo)»."
    if request.headers.get("HX-Request") != "true":
        return _volver(request, vista, ln.ca_codigo, texto)
    session.refresh(ln)
    return _respuesta_linea(
        request, user, session, ln, ca, "ca" if vista == "ca" else "pendientes", texto
    )


@router.get("/catalogo/ca/{codigo}", response_class=HTMLResponse)
async def catalogo_ca(
    request: Request,
    codigo: str,
    mensaje: str = "",
    error: str = "",
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, _ = ctx
    regiones = regiones_activas(session, request.app.state.settings)
    ca = _ca_o_404(session, codigo, regiones)
    lineas = list(
        session.execute(
            select(CaLinea).where(CaLinea.ca_codigo == ca.codigo).order_by(CaLinea.n)
        ).scalars()
    )
    nombres = _nombres_productos(session)
    vistas = [_vista_linea(session, ln, ca, nombres) for ln in lineas]
    return _TEMPLATES.TemplateResponse(
        request,
        "catalogo_ca.html",
        _ctx(
            request,
            user,
            ca=ca,
            lineas=vistas,
            bandera=ETIQUETA_BANDERA.get(ca.detalle_lineas, ETIQUETA_BANDERA["sin_detalle"]),
            productos_todos=_productos_activos(session, "", 400),
            mensaje=mensaje,
            error=error,
            **_estado_anexos(session, ca, lineas),
        ),
    )


# ===========================================================================
# Anexos (F-plat-2): subir, confirmar todas, «Anexo resuelto»
# ===========================================================================

ETIQUETA_EXTRACCION = {
    "hecha": ("Leído", "success"),
    "pendiente": ("Pendiente", "secondary"),
    "sin_presupuesto": ("Manual (sin lectura automática)", "warning"),
    "error": ("Manual (no se pudo leer)", "danger"),
}


def _estado_anexos(session: Session, ca: CompraAgil, lineas: list[CaLinea]) -> dict[str, Any]:
    return {
        "anexos": list(
            session.execute(
                select(CaAnexo).where(CaAnexo.ca_codigo == ca.codigo).order_by(CaAnexo.id.desc())
            ).scalars()
        ),
        "etiqueta_extraccion": ETIQUETA_EXTRACCION,
        "n_placeholders": sum(
            1 for ln in lineas if ln.origen == "anexo" and ln.estado == "pendiente"
        ),
        "n_por_confirmar": sum(
            1 for ln in lineas if ln.anexo_id is not None and ln.estado == "dudosa"
        ),
    }


def _extractor(request: Request) -> anexos.Extractor | None:
    """OpenRouter si está configurado; si no, None (flujo manual)."""
    from app.clients.openrouter import OpenRouterClient

    cliente = OpenRouterClient(request.app.state.settings, request.app.state.engine)
    return cliente if cliente.habilitado else None


@router.post("/oportunidad/{fuente}/{codigo}/anexo", response_model=None)
def anexo_subir(
    request: Request,
    fuente: str,
    codigo: str,
    archivo: UploadFile = File(...),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """Sube un anexo a una CA y lo convierte en líneas `dudosa` (F-plat-2 §1.2–§1.3).

    Síncrona a propósito: la extracción puede tardar ~1 min y FastAPI la corre en el pool de
    hilos. El archivo se procesa en memoria y NO se guarda (reglas 10 y 11)."""
    check_csrf(request, csrf_token)
    if fuente != "compras_agiles":
        raise HTTPException(status_code=404, detail="Solo las Compras Ágiles tienen anexos")
    user, _ = ctx
    settings = request.app.state.settings
    ca = _ca_o_404(session, codigo, regiones_activas(session, settings))
    data = archivo.file.read(anexos.MAX_BYTES + 1)
    es_htmx = request.headers.get("HX-Request") == "true"
    base = f"/catalogo/ca/{quote(ca.codigo)}"
    try:
        res = anexos.procesar_anexo_ca(
            session,
            ca,
            data,
            archivo.filename or "",
            usuario_id=user.id,
            extractor=_extractor(request),
            max_paginas_imagen=settings.llm_max_paginas_imagen,
        )
    except anexos.AnexoInvalido as exc:
        session.rollback()
        if es_htmx:
            return _TEMPLATES.TemplateResponse(
                request, "_anexo_resultado.html", _ctx(request, user, ca=ca, error=str(exc))
            )
        return RedirectResponse(f"{base}?error={quote(str(exc))}", status_code=303)
    session.commit()
    if not es_htmx:
        return RedirectResponse(f"{base}?mensaje={quote(res.aviso)}", status_code=303)
    nombres = _nombres_productos(session)
    session.refresh(ca)
    return _TEMPLATES.TemplateResponse(
        request,
        "_anexo_resultado.html",
        _ctx(
            request,
            user,
            ca=ca,
            anexo=res.anexo,
            aviso=res.aviso,
            etiqueta_extraccion=ETIQUETA_EXTRACCION,
            lineas=[_vista_linea(session, ln, ca, nombres) for ln in res.lineas],
            n_placeholders=sum(
                1
                for ln in session.execute(
                    select(CaLinea).where(CaLinea.ca_codigo == ca.codigo)
                ).scalars()
                if ln.origen == "anexo" and ln.estado == "pendiente"
            ),
        ),
    )


@router.post("/catalogo/ca/{codigo}/anexo/confirmar-todas", response_model=None)
async def anexo_confirmar_todas(
    request: Request,
    codigo: str,
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """Confirma con su sugerencia las líneas de anexo `dudosa` que tienen producto. Las que no
    tienen producto quedan para elegirlo fila por fila."""
    check_csrf(request, csrf_token)
    user, _ = ctx
    ca = _ca_o_404(session, codigo, regiones_activas(session, request.app.state.settings))
    lineas = list(
        session.execute(
            select(CaLinea).where(
                CaLinea.ca_codigo == ca.codigo,
                CaLinea.anexo_id.isnot(None),
                CaLinea.estado == "dudosa",
            )
        ).scalars()
    )
    hechas = sin_producto = 0
    for ln in lineas:
        if ln.producto_id is None:
            sin_producto += 1
            continue
        try:
            cat.confirmar_linea(
                session, ln, producto_id=ln.producto_id, formato_id=ln.formato_id,
                usuario_id=user.id,
            )  # fmt: skip
        except cat.CatalogoError:
            sin_producto += 1
            continue
        hechas += 1
    session.commit()
    texto = f"{hechas} línea(s) confirmada(s)." + (
        f" {sin_producto} sin producto: elígelo en cada fila." if sin_producto else ""
    )
    return RedirectResponse(
        f"/catalogo/ca/{quote(ca.codigo)}?mensaje={quote(texto)}", status_code=303
    )


@router.post("/catalogo/ca/{codigo}/anexo-resuelto", response_model=None)
async def anexo_resuelto(
    request: Request,
    codigo: str,
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """La persona ya tipeó las líneas: borra los placeholders «sin detalle (anexo)». Agregar una
    línea manual NO lo hace sola, porque pueden faltar filas: lo decide la persona."""
    check_csrf(request, csrf_token)
    ca = _ca_o_404(session, codigo, regiones_activas(session, request.app.state.settings))
    borradas = anexos.resolver_anexo(session, ca)
    session.commit()
    texto = "Anexo marcado como resuelto." + (
        f" Se quitó {borradas} línea(s) «sin detalle»." if borradas else ""
    )
    return RedirectResponse(
        f"/catalogo/ca/{quote(ca.codigo)}?mensaje={quote(texto)}", status_code=303
    )


@router.post("/catalogo/ca/{codigo}/lineas", response_model=None)
async def catalogo_linea_manual(
    request: Request,
    codigo: str,
    producto_id: int = Form(...),
    formato_id: str = Form(""),
    cantidad: str = Form(""),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, _ = ctx
    regiones = regiones_activas(session, request.app.state.settings)
    ca = _ca_o_404(session, codigo, regiones)
    base = f"/catalogo/ca/{quote(ca.codigo)}"
    try:
        cat.agregar_linea_manual(
            session,
            ca,
            producto_id=producto_id,
            formato_id=int(formato_id) if formato_id.strip().isdigit() else None,
            cantidad=_csv_float(cantidad),
            usuario_id=user.id,
        )
    except cat.CatalogoError as exc:
        session.rollback()
        return RedirectResponse(f"{base}?error={quote(str(exc))}", status_code=303)
    session.commit()
    return RedirectResponse(f"{base}?mensaje={quote('Línea agregada')}", status_code=303)


@router.post("/catalogo/lineas/{linea_id}/quitar", response_model=None)
async def catalogo_quitar_manual(
    request: Request,
    linea_id: int,
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    regiones = regiones_activas(session, request.app.state.settings)
    ln, _ca = _linea_o_404(session, linea_id, regiones)
    base = f"/catalogo/ca/{quote(ln.ca_codigo)}"
    es_htmx = request.headers.get("HX-Request") == "true"
    try:
        cat.quitar_linea_manual(session, ln)
    except cat.CatalogoError as exc:
        session.rollback()
        if es_htmx:
            return HTMLResponse(_aviso(str(exc), "danger"), headers={"HX-Reswap": "none"})
        return RedirectResponse(f"{base}?error={quote(str(exc))}", status_code=303)
    session.commit()
    if es_htmx:  # la fila desaparece: 200 con solo el aviso fuera de banda (nunca 204)
        return HTMLResponse(_aviso("Línea quitada."))
    return RedirectResponse(f"{base}?mensaje={quote('Línea quitada')}", status_code=303)


# ===========================================================================
# Productos
# ===========================================================================


def _redir_catalogo(tab: str, clave: str, texto: str) -> RedirectResponse:
    return RedirectResponse(f"/catalogo?tab={tab}&{clave}={quote(texto)}", status_code=303)


@router.post("/catalogo/productos", response_model=None)
async def catalogo_producto_crear(
    request: Request,
    nombre: str = Form(""),
    unidad_base: str = Form(""),
    familia: str = Form(""),
    unspsc: str = Form(""),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, _ = ctx
    try:
        p = cat.crear_producto(
            session,
            nombre=nombre,
            unidad_base=unidad_base,
            familia=familia,
            unspsc=unspsc,
            usuario_id=user.id,
        )
    except cat.CatalogoError as exc:
        session.rollback()
        return _redir_catalogo("productos", "error", str(exc))
    session.commit()
    return RedirectResponse(
        f"/catalogo/productos/{p.id}?mensaje={quote('Producto creado')}", status_code=303
    )


def _producto_o_404(session: Session, producto_id: int) -> ProductoCanonico:
    p = session.get(ProductoCanonico, producto_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return p


@router.get("/catalogo/productos/{producto_id}", response_class=HTMLResponse)
async def catalogo_producto_get(
    request: Request,
    producto_id: int,
    mensaje: str = "",
    error: str = "",
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, _ = ctx
    p = _producto_o_404(session, producto_id)
    otros = [
        o
        for o in _productos_activos(session, "", 400)
        if o.id != p.id and o.unidad_base == p.unidad_base
    ]
    return _TEMPLATES.TemplateResponse(
        request,
        "catalogo_producto.html",
        _ctx(
            request,
            user,
            p=p,
            formatos=_formatos_de(session, p.id),
            n_lineas=session.execute(
                select(func.count()).select_from(CaLinea).where(CaLinea.producto_id == p.id)
            ).scalar_one(),
            n_alias=session.execute(
                select(func.count())
                .select_from(ProductoAlias)
                .where(ProductoAlias.producto_id == p.id)
            ).scalar_one(),
            fusionables=otros,
            es_admin=user.rol == RolUsuario.ADMIN,
            familias=sorted(set(FAMILIAS_ALIMENTOS.values())),
            mensaje=mensaje,
            error=error,
        ),
    )


@router.post("/catalogo/productos/{producto_id}", response_model=None)
async def catalogo_producto_editar(
    request: Request,
    producto_id: int,
    nombre: str = Form(""),
    familia: str = Form(""),
    unspsc: str = Form(""),
    activo: str = Form(""),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    p = _producto_o_404(session, producto_id)
    base = f"/catalogo/productos/{p.id}"
    nombre = " ".join(nombre.split())
    if not nombre:
        return RedirectResponse(
            f"{base}?error={quote('El nombre es obligatorio')}", status_code=303
        )
    choque = session.execute(
        select(ProductoCanonico.id).where(
            func.lower(ProductoCanonico.nombre) == nombre.lower(), ProductoCanonico.id != p.id
        )
    ).first()
    if choque:
        return RedirectResponse(
            f"{base}?error={quote('Ya existe un producto con ese nombre')}", status_code=303
        )
    p.nombre = nombre[:200]
    p.familia = familia.strip()[:100] or None
    p.unspsc_sugerido = unspsc.strip()[:20] or None
    p.activo = bool(activo)
    session.commit()
    return RedirectResponse(f"{base}?mensaje={quote('Producto guardado')}", status_code=303)


@router.post("/catalogo/productos/{producto_id}/formatos", response_model=None)
async def catalogo_formato_agregar(
    request: Request,
    producto_id: int,
    nombre: str = Form(""),
    factor_base: str = Form(""),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    p = _producto_o_404(session, producto_id)
    base = f"/catalogo/productos/{p.id}"
    factor = _csv_float(factor_base)
    try:
        cat.agregar_formato(session, p, nombre, factor or 0)
    except cat.CatalogoError as exc:
        session.rollback()
        return RedirectResponse(f"{base}?error={quote(str(exc))}", status_code=303)
    session.commit()
    return RedirectResponse(f"{base}?mensaje={quote('Formato agregado')}", status_code=303)


@router.post("/catalogo/productos/{producto_id}/fusionar", response_model=None)
async def catalogo_producto_fusionar(
    request: Request,
    producto_id: int,
    destino_id: int = Form(...),
    csrf_token: str = Form(""),
    ctx: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, _ = ctx
    if user.rol != RolUsuario.ADMIN:
        raise HTTPException(
            status_code=403, detail="Solo un administrador puede fusionar productos"
        )
    p = _producto_o_404(session, producto_id)
    try:
        r = cat.fusionar_productos(session, p.id, destino_id)
    except cat.CatalogoError as exc:
        session.rollback()
        return RedirectResponse(
            f"/catalogo/productos/{p.id}?error={quote(str(exc))}", status_code=303
        )
    session.commit()
    msg = quote(f"Fusionado: {r['lineas']} línea(s) movidas")
    return RedirectResponse(f"/catalogo/productos/{destino_id}?mensaje={msg}", status_code=303)


def lineas_para_ficha(
    session: Session, ca: CompraAgil, user: Usuario, regiones: list[int]
) -> dict[str, Any] | None:
    """Sección «Líneas» de la ficha de una CA: solo para CA de regiones activas, usuarios con
    empresa activa y CA que tengan líneas o anexos (F-plat-2: ahí se sube el anexo). Si no,
    None (la ficha no cambia)."""
    from app.api.deps import empresa_activa_id

    if ca.region not in regiones or empresa_activa_id(session, user) is None:
        return None
    filas = session.execute(
        select(CaLinea, ProductoCanonico.nombre)
        .outerjoin(ProductoCanonico, ProductoCanonico.id == CaLinea.producto_id)
        .where(CaLinea.ca_codigo == ca.codigo)
        .order_by(CaLinea.n)
    ).all()
    if not filas and not (ca.n_documentos or 0):
        return None
    return {
        "filas": [{"ln": ln, "producto": nombre} for ln, nombre in filas],
        "bandera": ETIQUETA_BANDERA.get(ca.detalle_lineas, ETIQUETA_BANDERA["sin_detalle"]),
        "sin_detalle": ca.detalle_lineas == "sin_detalle",
        "n_documentos": ca.n_documentos,
    }
