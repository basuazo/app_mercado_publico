"""Rutas de la plataforma de Compra Ágil (F-plat-3): empresas, proveedores, costos, logística.

- `/admin/empresas`: solo admin (crea empresas, asigna usuarios).
- `/proveedores…`: usuarios con empresa activa; TODA query filtra por `empresa_id` de la
  sesión y un id de otra empresa responde 404, nunca 403 (regla 26, R-p3).
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import date
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
    html_require_admin,
    html_require_empresa,
    html_require_user,
)
from app.api.routes.pages import _TEMPLATES, _ctx, _es_htmx
from app.api.selector import contexto_selector, entero_o_none
from app.catalogos.mercado import FAMILIAS_ALIMENTOS, norm
from app.mercado import importar, vinculo
from app.mercado.catalogo import cargar_catalogo
from app.mercado.proveedores import (
    UNIDADES,
    asegurar_tarifas_iniciales,
    buscar_producto,
    guardar_producto,
    hoy_chile,
    normalizar_rut,
    normalizar_unidad,
    variaciones_de,
)
from app.models.seeds import REGIONES
from app.models.tables import (
    CostoHistorial,
    Empresa,
    ProductoCanonico,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
    Usuario,
)

router = APIRouter()

_REGIONES_VALIDAS = {codigo for codigo, _ in REGIONES}
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _csv_a_lista(texto: str) -> list[str]:
    vistos: dict[str, str] = {}
    for parte in (texto or "").split(","):
        t = parte.strip()
        if t and norm(t) not in vistos:
            vistos[norm(t)] = t
    return list(vistos.values())


def _num_o_none(texto: str, nombre: str, errores: dict[str, str], *, entero: bool = False) -> Any:
    t = (texto or "").strip()
    if not t:
        return None
    try:
        v = importar.convertir_numero(t)
    except ValueError:
        errores[nombre] = "Debe ser un número"
        return None
    return None if v is None else (int(round(v)) if entero else v)


# ===========================================================================
# Admin: empresas
# ===========================================================================


def _vista_empresas(session: Session) -> list[dict[str, Any]]:
    empresas = list(session.execute(select(Empresa).order_by(Empresa.id)).scalars())
    conteo: dict[int, int] = {}
    for eid in session.execute(select(Usuario.empresa_id)).scalars():
        if eid is not None:
            conteo[eid] = conteo.get(eid, 0) + 1
    nombres = dict(REGIONES)
    return [
        {
            "e": e,
            "usuarios": conteo.get(e.id, 0),
            "regiones": ", ".join(nombres.get(r, str(r)) for r in e.regiones_prioritarias or []),
        }
        for e in empresas
    ]


def _pagina_empresas(
    request: Request,
    user: Usuario,
    session: Session,
    *,
    form: dict[str, Any] | None = None,
    errores: dict[str, str] | None = None,
    status_code: int = 200,
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "admin_empresas.html",
        _ctx(
            request,
            user,
            empresas=_vista_empresas(session),
            f=form,
            errores=errores or {},
            regiones_disponibles=REGIONES,
            mensaje=mensaje,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/admin/empresas", response_class=HTMLResponse)
async def admin_empresas_get(
    request: Request,
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    return _pagina_empresas(request, user, session, mensaje=mensaje, error=error)


def _leer_empresa_form(
    session: Session,
    empresa_id: int | None,
    nombre: str,
    rut: str,
    comuna_base: str,
    regiones: list[int],
    comunas: str,
) -> tuple[dict[str, Any], dict[str, str]]:
    errores: dict[str, str] = {}
    nombre = nombre.strip()
    if not nombre:
        errores["nombre"] = "El nombre es obligatorio"
    rut_norm: str | None = None
    try:
        rut_norm = normalizar_rut(rut)
    except ValueError:
        errores["rut"] = "RUT inválido (ej. 76123456-7)"
    if rut_norm:
        choque = session.execute(
            select(Empresa.id).where(Empresa.rut == rut_norm, Empresa.id != (empresa_id or 0))
        ).first()
        if choque:
            errores["rut"] = "Ya existe una empresa con ese RUT"
    regiones_ok = sorted({r for r in regiones if r in _REGIONES_VALIDAS})
    if not regiones_ok:
        errores["regiones"] = "Elige al menos una región"
    datos = {
        "nombre": nombre[:200],
        "rut": rut_norm,
        "comuna_base": comuna_base.strip()[:100] or None,
        "regiones_prioritarias": regiones_ok,
        "comunas_prioritarias": _csv_a_lista(comunas),
    }
    return datos, errores


@router.post("/admin/empresas/nueva", response_model=None)
async def admin_empresa_crear(
    request: Request,
    nombre: str = Form(""),
    rut: str = Form(""),
    comuna_base: str = Form(""),
    regiones: list[int] = Form([]),
    comunas: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    datos, errores = _leer_empresa_form(session, None, nombre, rut, comuna_base, regiones, comunas)
    if errores:
        form = {**datos, "rut": rut, "comunas": comunas}
        return _pagina_empresas(request, user, session, form=form, errores=errores, status_code=422)
    empresa = Empresa(**datos, activa=True)
    session.add(empresa)
    session.flush()
    asegurar_tarifas_iniciales(session, empresa.id)
    session.commit()
    return RedirectResponse(
        url=f"/admin/empresas/{empresa.id}?mensaje={quote('Empresa creada')}", status_code=303
    )


def _empresa_o_404(session: Session, empresa_id: int) -> Empresa:
    empresa = session.get(Empresa, empresa_id)
    if empresa is None:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    return empresa


def _pagina_empresa(
    request: Request,
    user: Usuario,
    session: Session,
    empresa: Empresa,
    *,
    errores: dict[str, str] | None = None,
    form: dict[str, Any] | None = None,
    status_code: int = 200,
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    miembros = list(
        session.execute(
            select(Usuario).where(Usuario.empresa_id == empresa.id).order_by(Usuario.email)
        ).scalars()
    )
    sin_empresa = list(
        session.execute(
            select(Usuario)
            .where(Usuario.empresa_id.is_(None), Usuario.activo.is_(True))
            .order_by(Usuario.email)
        ).scalars()
    )
    valores = form or {
        "nombre": empresa.nombre,
        "rut": empresa.rut or "",
        "comuna_base": empresa.comuna_base or "",
        "regiones_prioritarias": list(empresa.regiones_prioritarias or []),
        "comunas": ", ".join(empresa.comunas_prioritarias or []),
        "activa": empresa.activa,
    }
    return _TEMPLATES.TemplateResponse(
        request,
        "admin_empresa.html",
        _ctx(
            request,
            user,
            empresa=empresa,
            f=valores,
            errores=errores or {},
            miembros=miembros,
            sin_empresa=sin_empresa,
            regiones_disponibles=REGIONES,
            mensaje=mensaje,
            error=error,
        ),
        status_code=status_code,
    )


@router.get("/admin/empresas/{empresa_id}", response_class=HTMLResponse)
async def admin_empresa_get(
    request: Request,
    empresa_id: int,
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    empresa = _empresa_o_404(session, empresa_id)
    return _pagina_empresa(request, user, session, empresa, mensaje=mensaje, error=error)


@router.post("/admin/empresas/{empresa_id}", response_model=None)
async def admin_empresa_editar(
    request: Request,
    empresa_id: int,
    nombre: str = Form(""),
    rut: str = Form(""),
    comuna_base: str = Form(""),
    regiones: list[int] = Form([]),
    comunas: str = Form(""),
    activa: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    empresa = _empresa_o_404(session, empresa_id)
    datos, errores = _leer_empresa_form(
        session, empresa.id, nombre, rut, comuna_base, regiones, comunas
    )
    if errores:
        form = {
            **datos,
            "rut": rut,
            "comunas": comunas,
            "regiones_prioritarias": regiones,
            "activa": bool(activa),
        }
        return _pagina_empresa(
            request, user, session, empresa, errores=errores, form=form, status_code=422
        )
    for campo, valor in datos.items():
        setattr(empresa, campo, valor)
    empresa.activa = bool(activa)
    session.commit()
    return RedirectResponse(
        url=f"/admin/empresas/{empresa.id}?mensaje={quote('Empresa guardada')}", status_code=303
    )


@router.post("/admin/empresas/{empresa_id}/usuarios/asignar", response_model=None)
async def admin_empresa_asignar(
    request: Request,
    empresa_id: int,
    usuario_id: int = Form(...),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    empresa = _empresa_o_404(session, empresa_id)
    destino = session.get(Usuario, usuario_id)
    if destino is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    base = f"/admin/empresas/{empresa.id}"
    if destino.empresa_id is not None and destino.empresa_id != empresa.id:
        return RedirectResponse(
            url=f"{base}?error={quote('Ese usuario ya pertenece a otra empresa: quítalo primero')}",
            status_code=303,
        )
    destino.empresa_id = empresa.id
    session.commit()
    return RedirectResponse(url=f"{base}?mensaje={quote('Usuario asignado')}", status_code=303)


@router.post("/admin/empresas/{empresa_id}/usuarios/{uid}/quitar", response_model=None)
async def admin_empresa_quitar(
    request: Request,
    empresa_id: int,
    uid: int,
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    empresa = _empresa_o_404(session, empresa_id)
    destino = session.get(Usuario, uid)
    if destino is None or destino.empresa_id != empresa.id:
        raise HTTPException(status_code=404, detail="Usuario no encontrado en la empresa")
    destino.empresa_id = None
    session.commit()
    return RedirectResponse(
        url=f"/admin/empresas/{empresa.id}?mensaje={quote('Usuario quitado')}", status_code=303
    )


# ===========================================================================
# Proveedores
# ===========================================================================


def _sin_empresa(request: Request, user: Usuario, status_code: int = 200) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "proveedores_sin_empresa.html", _ctx(request, user), status_code=status_code
    )


def _proveedor_o_404(session: Session, empresa_id: int, proveedor_id: int) -> Proveedor:
    """Un proveedor de OTRA empresa es indistinguible de uno inexistente: 404 (regla 26)."""
    prov = session.execute(
        select(Proveedor).where(Proveedor.id == proveedor_id, Proveedor.empresa_id == empresa_id)
    ).scalar_one_or_none()
    if prov is None:
        raise HTTPException(status_code=404, detail="Proveedor no encontrado")
    return prov


def _producto_o_404(session: Session, prov: Proveedor, producto_id: int) -> ProveedorProducto:
    pp = session.execute(
        select(ProveedorProducto).where(
            ProveedorProducto.id == producto_id, ProveedorProducto.proveedor_id == prov.id
        )
    ).scalar_one_or_none()
    if pp is None:
        raise HTTPException(status_code=404, detail="Producto no encontrado")
    return pp


@router.get("/proveedores", response_class=HTMLResponse)
async def proveedores_lista(
    request: Request,
    q: str = "",
    mensaje: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    consulta = select(Proveedor).where(Proveedor.empresa_id == empresa_id)
    termino = q.strip()
    todos = list(session.execute(consulta.order_by(Proveedor.nombre)).scalars())
    if termino:
        t = norm(termino)
        todos = [
            p
            for p in todos
            if t in norm(p.nombre)
            or t in norm(p.comuna)
            or any(t in norm(r) for r in (p.rubros or []))
        ]
    n_productos: dict[int, int] = {}
    if todos:
        for pid in session.execute(
            select(ProveedorProducto.proveedor_id).where(
                ProveedorProducto.proveedor_id.in_([p.id for p in todos])
            )
        ).scalars():
            n_productos[pid] = n_productos.get(pid, 0) + 1
    sin_vincular, _total = vinculo.contar_sin_vincular(session, empresa_id)
    return _TEMPLATES.TemplateResponse(
        request,
        "proveedores.html",
        _ctx(
            request,
            user,
            proveedores=todos,
            n_productos=n_productos,
            q=q,
            mensaje=mensaje,
            sin_vincular=sin_vincular,
        ),
    )


_VOLVER = re.compile(r"^/proveedores(/\d+)?$")


@router.post("/proveedores/vincular-pendientes", response_model=None)
async def proveedores_vincular_pendientes(
    request: Request,
    volver: str = Form("/proveedores"),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """«Vincular pendientes» (F-plat-capas §1.3): aplica la sugerencia única y de confianza alta
    a los productos de proveedores de LA EMPRESA que no tienen producto del catálogo.
    Idempotente: no pisa vínculos. No es una migración: la pulsa una persona."""
    check_csrf(request, csrf_token)
    _, empresa_id = ue
    r = vinculo.vincular_pendientes(session, empresa_id)
    session.commit()
    texto = (
        f"Vinculados al catálogo: {r['vinculados']}. "
        f"Siguen sin vincular: {r['sin_vincular']} (elige el producto a mano al editarlos)."
    )
    destino = volver if _VOLVER.fullmatch(volver) else "/proveedores"
    return RedirectResponse(url=f"{destino}?mensaje={quote(texto)}", status_code=303)


# --- Plantilla, importación y logística (antes de /proveedores/{id}) ----------


@router.get("/proveedores/plantilla.xlsx")
async def proveedores_plantilla(
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
) -> Response:
    return Response(
        content=importar.generar_plantilla_xlsx(),
        media_type=_XLSX,
        headers={"Content-Disposition": 'attachment; filename="proveedores_plantilla.xlsx"'},
    )


@router.get("/proveedores/importar", response_class=HTMLResponse)
async def proveedores_importar_get(
    request: Request,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request, "proveedores_importar.html", _ctx(request, ue[0], resumen=None, error="")
    )


def _pagina_importar(
    request: Request, user: Usuario, *, status_code: int = 200, **extra: Any
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "proveedores_importar.html",
        _ctx(request, user, **{"resumen": None, "error": "", **extra}),
        status_code=status_code,
    )


@router.post("/proveedores/importar", response_class=HTMLResponse)
async def proveedores_importar_previa(
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
        planilla = importar.parsear(data, nombre)
    except importar.PlanillaInvalida as exc:
        return _pagina_importar(request, user, status_code=422, error=str(exc))
    # Vista previa = la misma escritura de la confirmación, revertida.
    resumen = importar.aplicar(session, empresa_id, planilla, usuario_id=user.id)
    session.rollback()
    return _pagina_importar(
        request,
        user,
        resumen=resumen,
        nombre_archivo=nombre,
        # El archivo viaja en el formulario: no se guarda en disco (regla 10).
        archivo_b64=base64.b64encode(data).decode("ascii"),
    )


@router.post("/proveedores/importar/confirmar", response_model=None)
async def proveedores_importar_confirmar(
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
        planilla = importar.parsear(data, nombre_archivo)
    except (binascii.Error, UnicodeEncodeError, importar.PlanillaInvalida) as exc:
        return _pagina_importar(
            request, user, status_code=422, error=f"No pude procesar el archivo: {exc}"
        )
    resumen = importar.aplicar(session, empresa_id, planilla, usuario_id=user.id)
    session.commit()
    return _pagina_importar(request, user, resumen=resumen, confirmado=True)


@router.get("/proveedores/logistica", response_class=HTMLResponse)
async def logistica_get(
    request: Request,
    mensaje: str = "",
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = ue
    tarifas = list(
        session.execute(
            select(TarifaLogistica)
            .where(TarifaLogistica.empresa_id == empresa_id)
            .order_by(TarifaLogistica.ambito.desc(), TarifaLogistica.nombre)
        ).scalars()
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "proveedores_logistica.html",
        _ctx(request, user, tarifas=tarifas, mensaje=mensaje, error=""),
    )


@router.post("/proveedores/logistica", response_model=None)
async def logistica_guardar(
    request: Request,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """Guarda todas las tarifas de la empresa (campos `fijo_<id>`, `kg_<id>`, `dias_<id>`,
    `notas_<id>`) y, si viene `nueva_nombre`, agrega una tarifa."""
    user, empresa_id = ue
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    errores: list[str] = []
    tarifas = list(
        session.execute(
            select(TarifaLogistica).where(TarifaLogistica.empresa_id == empresa_id)
        ).scalars()
    )
    for t in tarifas:  # solo ids de ESTA empresa: un id ajeno en el form no existe aquí
        err: dict[str, str] = {}
        fijo = _num_o_none(str(form.get(f"fijo_{t.id}", "")), "fijo", err, entero=True)
        kg = _num_o_none(str(form.get(f"kg_{t.id}", "")), "kg", err)
        dias = _num_o_none(str(form.get(f"dias_{t.id}", "")), "dias", err, entero=True)
        if err or (fijo is not None and fijo < 0) or (kg is not None and kg < 0):
            errores.append(f"{t.nombre}: valores inválidos")
            continue
        t.costo_fijo_clp = fijo or 0
        t.costo_por_kg_clp = kg or 0
        t.dias = dias
        t.notas = str(form.get(f"notas_{t.id}", "")).strip() or None
    nueva = str(form.get("nueva_nombre", "")).strip()
    if nueva:
        ambito = str(form.get("nueva_ambito", "provincia"))
        existe = any(t.ambito == ambito and norm(t.nombre) == norm(nueva) for t in tarifas)
        err = {}
        fijo = _num_o_none(str(form.get("nueva_fijo", "")), "fijo", err, entero=True)
        kg = _num_o_none(str(form.get("nueva_kg", "")), "kg", err)
        dias = _num_o_none(str(form.get("nueva_dias", "")), "dias", err, entero=True)
        if ambito not in {"comuna", "provincia"} or existe or err:
            errores.append(f"Tarifa nueva {nueva!r}: ámbito inválido, repetida o con valores inválidos")
        else:
            session.add(
                TarifaLogistica(
                    empresa_id=empresa_id,
                    ambito=ambito,
                    nombre=nueva[:100],
                    costo_fijo_clp=fijo or 0,
                    costo_por_kg_clp=kg or 0,
                    dias=dias,
                )
            )
    if errores:
        session.rollback()
        return _TEMPLATES.TemplateResponse(
            request,
            "proveedores_logistica.html",
            _ctx(
                request,
                user,
                tarifas=tarifas,
                mensaje="",
                error="No se guardó: " + "; ".join(errores),
            ),
            status_code=422,
        )
    session.commit()
    return RedirectResponse(
        url=f"/proveedores/logistica?mensaje={quote('Tarifas guardadas')}", status_code=303
    )


# --- Proveedor: formulario HTMX -----------------------------------------------


def _valores_proveedor(p: Proveedor | None) -> dict[str, Any]:
    if p is None:
        return {
            "nombre": "", "rut": "", "comuna": "", "direccion": "", "contacto_nombre": "",
            "telefono": "", "email": "", "rubros": "", "plazo_pago_dias": "",
            "minimo_compra_clp": "", "despacha": False, "notas": "",
        }  # fmt: skip
    return {
        "nombre": p.nombre,
        "rut": p.rut or "",
        "comuna": p.comuna or "",
        "direccion": p.direccion or "",
        "contacto_nombre": p.contacto_nombre or "",
        "telefono": p.telefono or "",
        "email": p.email or "",
        "rubros": ", ".join(p.rubros or []),
        "plazo_pago_dias": "" if p.plazo_pago_dias is None else p.plazo_pago_dias,
        "minimo_compra_clp": "" if p.minimo_compra_clp is None else p.minimo_compra_clp,
        "despacha": p.despacha,
        "notas": p.notas or "",
    }


def _form_proveedor(
    request: Request,
    user: Usuario,
    proveedor: Proveedor | None,
    valores: dict[str, Any] | None = None,
    errores: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    action = "/proveedores/nuevo" if proveedor is None else f"/proveedores/{proveedor.id}"
    return _TEMPLATES.TemplateResponse(
        request,
        "_proveedor_form.html",
        _ctx(
            request,
            user,
            f={
                "form_id": "prov-nuevo" if proveedor is None else f"prov-{proveedor.id}",
                "action": action,
                "valores": valores if valores is not None else _valores_proveedor(proveedor),
                "errores": errores or {},
                "familias": sorted(set(FAMILIAS_ALIMENTOS.values())),
            },
        ),
        status_code=status_code,
    )


@router.get("/proveedores/nuevo/form", response_class=HTMLResponse)
async def proveedor_form_nuevo(
    request: Request, ue: tuple[Usuario, int] = Depends(html_require_empresa)
) -> HTMLResponse:
    return _form_proveedor(request, ue[0], None)


@router.get("/proveedores/{proveedor_id}/form", response_class=HTMLResponse)
async def proveedor_form_editar(
    request: Request,
    proveedor_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    prov = _proveedor_o_404(session, ue[1], proveedor_id)
    return _form_proveedor(request, ue[0], prov)


async def _guardar_proveedor(
    request: Request,
    user: Usuario,
    session: Session,
    empresa_id: int,
    proveedor: Proveedor | None,
    campos: dict[str, str],
    despacha: bool,
) -> Response:
    errores: dict[str, str] = {}
    nombre = campos["nombre"].strip()
    if not nombre:
        errores["nombre"] = "El nombre es obligatorio"
    rut: str | None = None
    try:
        rut = normalizar_rut(campos["rut"])
    except ValueError:
        errores["rut"] = "RUT inválido (ej. 76123456-7)"
    plazo = _num_o_none(campos["plazo_pago_dias"], "plazo_pago_dias", errores, entero=True)
    minimo = _num_o_none(campos["minimo_compra_clp"], "minimo_compra_clp", errores, entero=True)
    if not errores:
        # Mismo RUT o mismo nombre normalizado en la empresa = duplicado.
        otros = session.execute(
            select(Proveedor).where(
                Proveedor.empresa_id == empresa_id,
                Proveedor.id != (proveedor.id if proveedor else 0),
            )
        ).scalars()
        for otro in otros:
            if (rut and otro.rut == rut) or norm(otro.nombre) == norm(nombre):
                errores["nombre"] = "Ya tienes un proveedor con ese nombre o RUT"
                break
    if errores:
        return _form_proveedor(request, user, proveedor, {**campos, "despacha": despacha}, errores, 422)
    destino = proveedor or Proveedor(empresa_id=empresa_id, activo=True)
    destino.nombre = nombre[:300]
    destino.rut = rut
    destino.comuna = campos["comuna"].strip()[:100] or None
    destino.direccion = campos["direccion"].strip()[:300] or None
    destino.contacto_nombre = campos["contacto_nombre"].strip()[:200] or None
    destino.telefono = campos["telefono"].strip()[:50] or None
    destino.email = campos["email"].strip()[:255] or None
    destino.rubros = _csv_a_lista(campos["rubros"])
    destino.plazo_pago_dias = plazo
    destino.minimo_compra_clp = minimo
    destino.despacha = despacha
    destino.notas = campos["notas"].strip() or None
    if proveedor is None:
        session.add(destino)
    session.commit()
    url = f"/proveedores/{destino.id}"
    if _es_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


async def _campos_proveedor(request: Request) -> tuple[dict[str, str], bool]:
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    claves = ["nombre", "rut", "comuna", "direccion", "contacto_nombre", "telefono", "email", "rubros", "plazo_pago_dias", "minimo_compra_clp", "notas"]
    return {k: str(form.get(k, "")) for k in claves}, bool(form.get("despacha"))


@router.post("/proveedores/nuevo", response_model=None)
async def proveedor_crear(
    request: Request,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    campos, despacha = await _campos_proveedor(request)
    return await _guardar_proveedor(request, ue[0], session, ue[1], None, campos, despacha)


@router.post("/proveedores/{proveedor_id}", response_model=None)
async def proveedor_editar(
    request: Request,
    proveedor_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    campos, despacha = await _campos_proveedor(request)
    prov = _proveedor_o_404(session, ue[1], proveedor_id)
    return await _guardar_proveedor(request, ue[0], session, ue[1], prov, campos, despacha)


@router.post("/proveedores/{proveedor_id}/activo", response_model=None)
async def proveedor_activo(
    request: Request,
    proveedor_id: int,
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    prov = _proveedor_o_404(session, ue[1], proveedor_id)
    prov.activo = not prov.activo
    session.commit()
    return RedirectResponse(url=f"/proveedores/{prov.id}", status_code=303)


# --- Ficha ----------------------------------------------------------------------


# --- Lista de precios por foto o PDF (F-plat-2 §1.4) ----------------------------
# Mismo extractor que los anexos de CA, con el esquema `precios`; la confirmación es la
# importación de F-plat-3 (`importar.aplicar`, con historial) sobre ESTE proveedor.


def _pagina_lista(
    request: Request, user: Usuario, prov: Proveedor, *, status_code: int = 200, **extra: Any
) -> HTMLResponse:
    return _TEMPLATES.TemplateResponse(
        request,
        "proveedor_lista_precios.html",
        _ctx(request, user, **{"p": prov, "resumen": None, "error": "", "filas": [], **extra}),
        status_code=status_code,
    )


@router.post("/proveedores/{proveedor_id}/lista-precios", response_class=HTMLResponse)
def proveedor_lista_precios(
    request: Request,
    proveedor_id: int,
    archivo: UploadFile = File(...),
    csrf_token: str = Form(""),
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Lee la lista con el modelo y muestra la vista previa editable (no guarda nada).

    Tras verificar el proveedor se suelta la conexión (`rollback`) mientras se lee el archivo y
    responde el modelo (regla 15); después el proveedor se recarga con su empresa."""
    from app.clients.openrouter import LLMError, OpenRouterClient
    from app.mercado import anexos

    check_csrf(request, csrf_token)
    user, empresa_id = ue
    prov = _proveedor_o_404(session, empresa_id, proveedor_id)
    settings = request.app.state.settings
    data = archivo.file.read(anexos.MAX_BYTES + 1)
    try:
        mime = anexos.validar_archivo(data)
    except anexos.AnexoInvalido as exc:
        return _pagina_lista(request, user, prov, status_code=422, error=str(exc))
    session.rollback()  # solo hubo lecturas; las páginas de error recargan `prov` al pintarse
    res = anexos.extraer_texto(data, mime, max_paginas_imagen=settings.llm_max_paginas_imagen)
    if res.error or not res.hay_contenido:
        return _pagina_lista(
            request, user, prov, status_code=422,
            error=(res.error or "El archivo no trae contenido legible") + ". Carga los precios a mano o con la planilla.",
        )  # fmt: skip
    cliente = OpenRouterClient(settings, request.app.state.engine)
    if not cliente.habilitado:
        return _pagina_lista(
            request, user, prov,
            error="La lectura automática no está configurada: carga los precios a mano o con la planilla.",
        )  # fmt: skip
    try:
        ext = cliente.extraer_lineas(res.texto or None, res.imagenes, esquema="precios")
    except LLMError as exc:
        return _pagina_lista(
            request, user, prov,
            error=f"No se pudo leer la lista ({exc}). Carga los precios a mano o con la planilla.",
        )  # fmt: skip
    prov = _proveedor_o_404(session, empresa_id, proveedor_id)  # recarga tras el rollback
    filas = [
        (ln.texto, ln.formato or "", "" if ln.precio is None else f"{ln.precio:g}")
        for ln in ext.lineas
    ]
    planilla = importar.planilla_de_lista(prov, filas)
    resumen = importar.aplicar(
        session, empresa_id, planilla, usuario_id=user.id, proveedor_fijo=prov, fuente="lista_llm"
    )
    session.rollback()  # vista previa: la misma escritura de la confirmación, revertida
    return _pagina_lista(
        request,
        user,
        prov,
        resumen=resumen,
        filas=filas,
        aviso_paginas=res.paginas_omitidas,
        sin_filas=not filas,
    )


@router.post("/proveedores/{proveedor_id}/lista-precios/confirmar", response_model=None)
async def proveedor_lista_precios_confirmar(
    request: Request,
    proveedor_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token") or ""))
    user, empresa_id = ue
    prov = _proveedor_o_404(session, empresa_id, proveedor_id)
    productos = [str(x) for x in form.getlist("producto")]
    formatos = [str(x) for x in form.getlist("formato")]
    precios = [str(x) for x in form.getlist("precio")]
    if not (len(productos) == len(formatos) == len(precios)):
        return _pagina_lista(request, user, prov, status_code=422, error="Formulario incompleto")
    filas = list(zip(productos, formatos, precios, strict=True))
    planilla = importar.planilla_de_lista(prov, filas)
    resumen = importar.aplicar(
        session, empresa_id, planilla, usuario_id=user.id, proveedor_fijo=prov, fuente="lista_llm"
    )
    session.commit()
    return _pagina_lista(request, user, prov, resumen=resumen, confirmado=True)


@router.get("/proveedores/{proveedor_id}", response_class=HTMLResponse)
async def proveedor_ficha(
    request: Request,
    proveedor_id: int,
    mensaje: str = "",
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = ue
    prov = _proveedor_o_404(session, empresa_id, proveedor_id)
    productos = list(
        session.execute(
            select(ProveedorProducto)
            .where(ProveedorProducto.proveedor_id == prov.id)
            .order_by(ProveedorProducto.producto_texto, ProveedorProducto.formato_texto)
        ).scalars()
    )
    variaciones = variaciones_de(session, productos)
    ids_canonicos = {pp.producto_id for pp in productos if pp.producto_id is not None}
    nombres_canonicos: dict[int, str] = (
        {
            i: n
            for i, n in session.execute(
                select(ProductoCanonico.id, ProductoCanonico.nombre).where(
                    ProductoCanonico.id.in_(ids_canonicos)
                )
            )
        }
        if ids_canonicos
        else {}
    )
    sin_vincular, _total = vinculo.contar_sin_vincular(session, empresa_id, prov.id)
    filas = [
        {
            "pp": pp,
            "catalogo": nombres_canonicos.get(pp.producto_id) if pp.producto_id else None,
            "v": variaciones[pp.id],
            "por_unidad": (
                pp.precio_neto_actual / pp.factor_base
                if pp.factor_base and pp.unidad_base
                else None
            ),
        }
        for pp in productos
    ]
    return _TEMPLATES.TemplateResponse(
        request,
        "proveedor_ficha.html",
        _ctx(
            request,
            user,
            p=prov,
            filas=filas,
            rubros_texto=", ".join(prov.rubros or []),
            sin_vincular=sin_vincular,
            mensaje=mensaje,
        ),
    )


# --- Productos ------------------------------------------------------------------


def _valores_producto(pp: ProveedorProducto | None) -> dict[str, Any]:
    if pp is None:
        return {
            "producto_texto": "", "formato_texto": "", "factor_base": "", "unidad_base": "",
            "precio_neto": "", "vigente_desde": hoy_chile().isoformat(), "disponible": True,
            "codigo_unspsc": "", "producto_id": "", "formato_id": "",
        }  # fmt: skip
    return {
        "producto_texto": pp.producto_texto,
        "formato_texto": pp.formato_texto,
        "factor_base": "" if pp.factor_base is None else f"{pp.factor_base:g}",
        "unidad_base": pp.unidad_base or "",
        "precio_neto": f"{pp.precio_neto_actual:g}",
        "vigente_desde": hoy_chile().isoformat(),
        "disponible": pp.disponible,
        "codigo_unspsc": pp.codigo_unspsc or "",
        "producto_id": pp.producto_id or "",
        "formato_id": pp.formato_id or "",
    }


def _form_producto(
    request: Request,
    user: Usuario,
    session: Session,
    prov: Proveedor,
    pp: ProveedorProducto | None,
    valores: dict[str, Any] | None = None,
    errores: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    base = f"/proveedores/{prov.id}/productos"
    form_id = f"prod-{prov.id}-{pp.id if pp else 'nuevo'}"
    vals = valores if valores is not None else _valores_producto(pp)
    return _TEMPLATES.TemplateResponse(
        request,
        "_producto_form.html",
        _ctx(
            request,
            user,
            f={
                "form_id": form_id,
                "action": base if pp is None else f"{base}/{pp.id}",
                "editando": pp is not None,
                "valores": vals,
                "errores": errores or {},
                "unidades": UNIDADES,
                # F-plat-capas §1.3: «producto del catálogo» con el selector de las líneas de CA.
                "cat": contexto_selector(
                    session,
                    entero_o_none(vals.get("producto_id")),
                    entero_o_none(vals.get("formato_id")),
                    form_id,
                ),
            },
        ),
        status_code=status_code,
    )


@router.get("/proveedores/{proveedor_id}/productos/form", response_class=HTMLResponse)
async def producto_form_nuevo(
    request: Request,
    proveedor_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    prov = _proveedor_o_404(session, ue[1], proveedor_id)
    return _form_producto(request, ue[0], session, prov, None)


@router.get("/proveedores/{proveedor_id}/productos/{producto_id}/form", response_class=HTMLResponse)
async def producto_form_editar(
    request: Request,
    proveedor_id: int,
    producto_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    prov = _proveedor_o_404(session, ue[1], proveedor_id)
    pp = _producto_o_404(session, prov, producto_id)
    return _form_producto(request, ue[0], session, prov, pp)


async def _guardar_producto_form(
    request: Request,
    user: Usuario,
    session: Session,
    prov: Proveedor,
    pp: ProveedorProducto | None,
) -> Response:
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    campos = {
        k: str(form.get(k, ""))
        for k in ["producto_texto", "formato_texto", "factor_base", "unidad_base", "precio_neto", "vigente_desde", "codigo_unspsc", "producto_id", "formato_id"]
    }
    producto_id = entero_o_none(campos["producto_id"])
    formato_id = entero_o_none(campos["formato_id"])
    disponible = bool(form.get("disponible"))
    errores: dict[str, str] = {}
    producto = pp.producto_texto if pp else campos["producto_texto"].strip()
    formato = pp.formato_texto if pp else campos["formato_texto"].strip()
    if not producto:
        errores["producto_texto"] = "El producto es obligatorio"
    precio = _num_o_none(campos["precio_neto"], "precio_neto", errores)
    if "precio_neto" not in errores and (precio is None or precio <= 0):
        errores["precio_neto"] = "El precio neto debe ser mayor que 0"
    factor = _num_o_none(campos["factor_base"], "factor_base", errores)
    unidad_txt = campos["unidad_base"].strip()
    unidad = normalizar_unidad(unidad_txt)
    if unidad_txt and unidad is None:
        errores["unidad_base"] = "Debe ser kg, L o u"
    if "factor_base" not in errores and "unidad_base" not in errores:
        if factor is not None and factor <= 0:
            errores["factor_base"] = "Debe ser mayor que 0"
        elif (factor is None) != (unidad is None):
            errores["unidad_base"] = "Factor y unidad van juntos (o ninguno: no compite por kg)"
    vigente: date | None = None
    try:
        vigente = importar.convertir_fecha(campos["vigente_desde"])
    except ValueError:
        errores["vigente_desde"] = "Fecha inválida (AAAA-MM-DD)"
    if pp is None and not errores and buscar_producto(session, prov.id, producto, formato):
        errores["producto_texto"] = "Ese producto con ese formato ya existe: edítalo en la tabla"
    if errores:
        return _form_producto(
            request, user, session, prov, pp, {**campos, "disponible": disponible}, errores, 422
        )
    assert precio is not None
    try:
        guardar_producto(
            session,
            prov,
            producto_texto=producto,
            formato_texto=formato,
            precio_neto=precio,
            vigente_desde=vigente,
            factor_base=factor,
            unidad_base=unidad,
            codigo_unspsc=campos["codigo_unspsc"],
            disponible=disponible,
            fuente="manual",
            usuario_id=user.id,
            producto_id=producto_id,
            formato_id=formato_id if producto_id is not None else None,
            # Sin producto elegido: se vincula solo si la sugerencia es única y de confianza alta.
            catalogo=cargar_catalogo(session) if producto_id is None else None,
        )
    except ValueError as exc:  # el vínculo elegido no calza (formato ajeno, unidad distinta)
        session.rollback()
        return _form_producto(
            request, user, session, prov, pp,
            {**campos, "disponible": disponible}, {"producto_id": str(exc)}, 422,
        )  # fmt: skip
    session.commit()
    url = f"/proveedores/{prov.id}"
    if _es_htmx(request):
        return Response(status_code=200, headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


@router.post("/proveedores/{proveedor_id}/productos", response_model=None)
async def producto_crear(
    request: Request,
    proveedor_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    prov = _proveedor_o_404(session, ue[1], proveedor_id)
    return await _guardar_producto_form(request, ue[0], session, prov, None)


@router.post("/proveedores/{proveedor_id}/productos/{producto_id}", response_model=None)
async def producto_editar(
    request: Request,
    proveedor_id: int,
    producto_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    prov = _proveedor_o_404(session, ue[1], proveedor_id)
    pp = _producto_o_404(session, prov, producto_id)
    return await _guardar_producto_form(request, ue[0], session, prov, pp)


@router.get("/proveedores/{proveedor_id}/productos/{producto_id}/historial", response_class=HTMLResponse)
async def producto_historial(
    request: Request,
    proveedor_id: int,
    producto_id: int,
    ue: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    prov = _proveedor_o_404(session, ue[1], proveedor_id)
    pp = _producto_o_404(session, prov, producto_id)
    filas = list(
        session.execute(
            select(CostoHistorial)
            .where(CostoHistorial.proveedor_producto_id == pp.id)
            .order_by(CostoHistorial.vigente_desde.desc(), CostoHistorial.id.desc())
        ).scalars()
    )
    return _TEMPLATES.TemplateResponse(
        request, "_producto_historial.html", _ctx(request, ue[0], pp=pp, filas=filas)
    )

