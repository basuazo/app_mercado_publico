"""Trabajo en equipo sobre las CA (F-equipo): estado compartido, hilo, campana y `/actividad`.

Ownership (reglas 17 y 26): todo pasa por `html_require_empresa` y filtra por la `empresa_id`
del usuario; una CA fuera del territorio activo, un comentario o un trabajo de otra empresa → 404.
Mutaciones con CSRF (regla 18): header `X-CSRF-Token` (HTMX) o campo `csrf_token`.
Sin correo en esta fase (regla 14): las alarmas viven dentro de la app.
"""

from __future__ import annotations

from math import ceil
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import (
    check_csrf,
    get_db,
    html_require_admin,
    html_require_empresa,
)
from app.api.routes.pages import _TEMPLATES, _ctx, _es_htmx
from app.auth.csrf import generate_csrf_token
from app.core.tiempo import ahora_utc
from app.mercado import equipo as eq
from app.mercado.territorio import regiones_activas
from app.models.enums import RolUsuario
from app.models.tables import CaEvento, CompraAgil, Empresa, Usuario

router = APIRouter()

PAGINA_ACTIVIDAD = 50
NOMBRE_MAX = 100


def _csrf(request: Request) -> str:
    return generate_csrf_token(request.app.state.settings.secret_key, request.state.csrf_nonce)


def _ca_o_404(request: Request, session: Session, codigo: str) -> tuple[str, Any]:
    """(nombre, fecha_cierre) de una CA del territorio activo; si no, 404 (regla 26)."""
    ca = session.execute(
        select(CompraAgil.nombre, CompraAgil.fecha_cierre, CompraAgil.region).where(
            CompraAgil.codigo == codigo
        )
    ).one_or_none()
    if ca is None or ca.region not in regiones_activas(session, request.app.state.settings):
        raise HTTPException(status_code=404, detail="Compra Ágil no encontrada")
    return ca.nombre, ca.fecha_cierre


def contexto_equipo(
    request: Request,
    session: Session,
    user: Usuario,
    empresa_id: int,
    codigo: str,
    *,
    origen: str,
    estado_ca: eq.EquipoCA | None = None,
) -> dict[str, Any]:
    """Contexto de `_equipo_ca.html` (lo comparten la tarjeta de la mesa y la ficha)."""
    e = estado_ca or eq.equipo_de(session, empresa_id, user, [codigo])[codigo]
    empresa = session.get(Empresa, empresa_id)
    return {
        "e": e,
        "yo": user.id,
        "origen": origen,
        "miembros": [(m.id, eq.nombre_visible(m)) for m in eq.miembros(session, empresa_id)],
        "estados": eq.ETIQUETA_ESTADO,
        "empresa_sin_rut": eq.rut_empresa(empresa) is None,
        "es_admin": user.rol == RolUsuario.ADMIN,
        "csrf_token": _csrf(request),
    }


def _render_equipo(
    request: Request,
    session: Session,
    user: Usuario,
    empresa_id: int,
    codigo: str,
    origen: str,
    *,
    status_code: int = 200,
    conflicto: eq.ConflictoVersion | None = None,
    error: str | None = None,
    requisitos_pendientes: bool = False,
) -> HTMLResponse:
    session.expire_all()
    e = eq.equipo_de(session, empresa_id, user, [codigo])[codigo]
    if conflicto is not None:
        hora = eq.hace(conflicto.cuando, ahora_utc()) if conflicto.cuando else ""
        e.conflicto = (
            f"No se aplicó tu cambio: {conflicto.quien} la dejó en «{e.etiqueta}» {hora}. "
            "Esto es lo que hay ahora."
        ).replace("  ", " ")
    e.error = error
    e.requisitos_pendientes = requisitos_pendientes
    ctx = contexto_equipo(request, session, user, empresa_id, codigo, origen=origen, estado_ca=e)
    return _TEMPLATES.TemplateResponse(
        request, "_equipo_ca.html", ctx, status_code=status_code
    )


def _volver(origen: str, codigo: str) -> str:
    return f"/oportunidad/compras_agiles/{codigo}" if origen == "ficha" else "/mesa"


def _version(valor: str) -> int:
    try:
        return max(0, int(valor))
    except (TypeError, ValueError):
        return -1  # nunca calza: se informa el conflicto con el estado actual


def _aplicar(
    request: Request,
    session: Session,
    user: Usuario,
    empresa_id: int,
    codigo: str,
    origen: str,
    accion: Any,
) -> Response:
    """Corre una mutación de estado/asignación con su manejo de conflicto y error."""
    origen = origen if origen in ("mesa", "ficha") else "mesa"
    try:
        accion()
        session.commit()
    except eq.ConflictoVersion as c:
        session.rollback()
        if not _es_htmx(request):
            raise HTTPException(status_code=409, detail=f"{c.quien} cambió esta CA antes") from c
        return _render_equipo(
            request, session, user, empresa_id, codigo, origen, status_code=409, conflicto=c
        )
    except eq.ErrorEquipo as err:
        session.rollback()
        if not _es_htmx(request):
            raise HTTPException(status_code=422, detail=str(err)) from err
        return _render_equipo(
            request, session, user, empresa_id, codigo, origen, status_code=422, error=str(err),
            requisitos_pendientes=isinstance(err, eq.RequisitosPendientes),
        )
    if not _es_htmx(request):
        return RedirectResponse(url=_volver(origen, codigo), status_code=303)
    return _render_equipo(request, session, user, empresa_id, codigo, origen)


@router.get("/equipo/ca/{codigo}/bloque", response_class=HTMLResponse)
async def equipo_bloque(
    request: Request,
    codigo: str,
    origen: str = "ficha",
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """El bloque de estado compartido (la ficha lo carga al abrirse)."""
    user, empresa_id = sesion
    _ca_o_404(request, session, codigo)
    return _render_equipo(
        request, session, user, empresa_id, codigo, origen if origen in ("mesa", "ficha") else "ficha"
    )


@router.post("/equipo/ca/{codigo}/estado", response_model=None)
async def equipo_estado(  # noqa: PLR0913
    request: Request,
    codigo: str,
    estado: str = Form(""),
    version: str = Form("0"),
    motivo: str = Form(""),
    origen: str = Form("mesa"),
    forzar_motivo: str = Form(""),
    csrf_token: str = Form(""),
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = sesion
    _ca_o_404(request, session, codigo)
    return _aplicar(
        request,
        session,
        user,
        empresa_id,
        codigo,
        origen,
        lambda: eq.cambiar_estado(
            session, empresa_id, user, codigo, estado, _version(version), motivo,
            forzar_motivo=forzar_motivo,
        ),
    )


@router.post("/equipo/ca/{codigo}/asignar", response_model=None)
async def equipo_asignar(  # noqa: PLR0913
    request: Request,
    codigo: str,
    responsable_id: str = Form(""),
    version: str = Form("0"),
    origen: str = Form("mesa"),
    csrf_token: str = Form(""),
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    """`responsable_id`: un id, `yo` («Tomar») o vacío (quitar responsable)."""
    check_csrf(request, csrf_token)
    user, empresa_id = sesion
    _ca_o_404(request, session, codigo)
    valor = responsable_id.strip()
    destino: int | None
    if valor == "yo":
        destino = user.id
    elif not valor:
        destino = None
    elif valor.isdigit():
        destino = int(valor)
    else:
        raise HTTPException(status_code=422, detail="Responsable inválido")
    return _aplicar(
        request,
        session,
        user,
        empresa_id,
        codigo,
        origen,
        lambda: eq.asignar(session, empresa_id, user, codigo, destino, _version(version)),
    )


# --- Hilo de comentarios ----------------------------------------------------------------------


def _render_hilo(
    request: Request,
    session: Session,
    user: Usuario,
    empresa_id: int,
    codigo: str,
    *,
    solo_lista: bool = False,
    error: str | None = None,
    borrador: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    nombre, _ = _ca_o_404(request, session, codigo)
    session.expire_all()
    ctx = {
        "codigo": codigo,
        "ca_nombre": nombre,
        "items": eq.hilo(session, empresa_id, codigo, ahora_utc()),
        "yo": user.id,
        "miembros_nombres": [eq.nombre_visible(m) for m in eq.miembros(session, empresa_id)],
        "texto_max": eq.TEXTO_MAX,
        "error": error,
        "borrador": borrador,
        "csrf_token": _csrf(request),
    }
    plantilla = "_equipo_hilo_lista.html" if solo_lista else "_equipo_hilo.html"
    respuesta = _TEMPLATES.TemplateResponse(request, plantilla, ctx, status_code=status_code)
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta


@router.get("/equipo/ca/{codigo}/hilo", response_class=HTMLResponse)
async def equipo_hilo(
    request: Request,
    codigo: str,
    lista: str = "",
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = sesion
    return _render_hilo(request, session, user, empresa_id, codigo, solo_lista=lista == "1")


def _tras_comentario(
    request: Request,
    session: Session,
    user: Usuario,
    empresa_id: int,
    codigo: str,
    accion: Any,
    borrador: str = "",
) -> Response:
    try:
        accion()
        session.commit()
    except eq.NoEncontrado as err:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(err)) from err
    except eq.SinPermiso as err:
        session.rollback()
        raise HTTPException(status_code=403, detail=str(err)) from err
    except eq.ErrorEquipo as err:
        session.rollback()
        if not _es_htmx(request):
            raise HTTPException(status_code=422, detail=str(err)) from err
        return _render_hilo(
            request, session, user, empresa_id, codigo, error=str(err), borrador=borrador,
            status_code=422,
        )
    if not _es_htmx(request):
        return RedirectResponse(url=f"/oportunidad/compras_agiles/{codigo}", status_code=303)
    return _render_hilo(request, session, user, empresa_id, codigo)


@router.post("/equipo/ca/{codigo}/comentarios", response_model=None)
async def equipo_comentar(
    request: Request,
    codigo: str,
    texto: str = Form(""),
    csrf_token: str = Form(""),
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = sesion
    _ca_o_404(request, session, codigo)
    return _tras_comentario(
        request,
        session,
        user,
        empresa_id,
        codigo,
        lambda: eq.comentar(session, empresa_id, user, codigo, texto),
        borrador=texto[: eq.TEXTO_MAX * 2],
    )


def _codigo_de_comentario(session: Session, empresa_id: int, comentario_id: int) -> str:
    from app.models.tables import CaComentario

    c = session.get(CaComentario, comentario_id)
    if c is None or c.empresa_id != empresa_id:
        raise HTTPException(status_code=404, detail="Comentario no encontrado")
    return c.ca_codigo


@router.post("/equipo/comentarios/{comentario_id}/editar", response_model=None)
async def equipo_comentario_editar(
    request: Request,
    comentario_id: int,
    texto: str = Form(""),
    csrf_token: str = Form(""),
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = sesion
    codigo = _codigo_de_comentario(session, empresa_id, comentario_id)
    return _tras_comentario(
        request,
        session,
        user,
        empresa_id,
        codigo,
        lambda: eq.editar_comentario(session, empresa_id, user, comentario_id, texto),
    )


@router.post("/equipo/comentarios/{comentario_id}/eliminar", response_model=None)
async def equipo_comentario_eliminar(
    request: Request,
    comentario_id: int,
    csrf_token: str = Form(""),
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = sesion
    codigo = _codigo_de_comentario(session, empresa_id, comentario_id)
    return _tras_comentario(
        request,
        session,
        user,
        empresa_id,
        codigo,
        lambda: eq.eliminar_comentario(session, empresa_id, user, comentario_id),
    )


# --- Campana ----------------------------------------------------------------------------------


@router.get("/actividad/contador", response_class=HTMLResponse)
async def actividad_contador(
    request: Request,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Solo un número (sondeo de 60 s). De paso genera las alarmas de sistema de la empresa, a lo
    más una vez por minuto (marca en `sync_state`)."""
    user, empresa_id = sesion
    ahora = ahora_utc()
    if eq.generar_si_toca(session, empresa_id, ahora):
        session.commit()
    n = eq.contar_no_leidos(session, user, empresa_id)
    respuesta = _TEMPLATES.TemplateResponse(request, "_campana_contador.html", {"n": n})
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta


def _render_campana(request: Request, session: Session, user: Usuario, empresa_id: int) -> HTMLResponse:
    ahora = ahora_utc()
    eventos = eq.listar_no_leidos(session, user, empresa_id, limite=20)
    respuesta = _TEMPLATES.TemplateResponse(
        request,
        "_campana_lista.html",
        {
            "items": eq.describir_eventos(session, eventos, user.id, ahora),
            "n": eq.contar_no_leidos(session, user, empresa_id),
            "csrf_token": _csrf(request),
        },
    )
    respuesta.headers["Cache-Control"] = "no-store"
    return respuesta


@router.get("/actividad/campana", response_class=HTMLResponse)
async def actividad_campana(
    request: Request,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    user, empresa_id = sesion
    return _render_campana(request, session, user, empresa_id)


@router.post("/actividad/leido", response_model=None)
async def actividad_leido(
    request: Request,
    csrf_token: str = Form(""),
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    check_csrf(request, csrf_token)
    user, empresa_id = sesion
    eq.marcar_leido(session, user)
    session.commit()
    if not _es_htmx(request):
        return RedirectResponse(url="/actividad", status_code=303)
    return _render_campana(request, session, user, empresa_id)


# --- /actividad -------------------------------------------------------------------------------


@router.get("/actividad", response_class=HTMLResponse)
async def actividad(  # noqa: PLR0913
    request: Request,
    persona: str = "",
    tipo: str = "",
    ca: str = "",
    pagina: int = 1,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Registro completo de la empresa, paginado, con filtros por persona, tipo y CA."""
    user, empresa_id = sesion
    cond: list[Any] = [CaEvento.empresa_id == empresa_id]
    persona_id = int(persona) if persona.isdigit() else None
    if persona_id is not None:
        cond.append(CaEvento.usuario_id == persona_id)
    tipo = tipo if tipo in eq.TIPOS_EVENTO else ""
    if tipo:
        cond.append(CaEvento.tipo == tipo)
    ca = ca.strip()[:50]
    if ca:
        cond.append(CaEvento.ca_codigo == ca)
    total = int(
        session.execute(select(func.count()).select_from(CaEvento).where(*cond)).scalar_one()
    )
    total_paginas = max(1, ceil(total / PAGINA_ACTIVIDAD))
    pagina = min(max(pagina, 1), total_paginas)
    eventos = list(
        session.execute(
            select(CaEvento)
            .where(*cond)
            .order_by(CaEvento.creado_en.desc(), CaEvento.id.desc())
            .offset((pagina - 1) * PAGINA_ACTIVIDAD)
            .limit(PAGINA_ACTIVIDAD)
        ).scalars()
    )
    # Todos los de la empresa (también los inactivos: su actividad sigue en el registro).
    personas = [
        (u.id, eq.nombre_visible(u))
        for u in session.execute(
            select(Usuario).where(Usuario.empresa_id == empresa_id).order_by(Usuario.email)
        ).scalars()
    ]
    qs = {"persona": persona if persona_id else "", "tipo": tipo, "ca": ca}
    return _TEMPLATES.TemplateResponse(
        request,
        "actividad.html",
        _ctx(
            request,
            user,
            items=eq.describir_eventos(session, eventos, user.id, ahora_utc()),
            total=total,
            pagina=pagina,
            total_paginas=total_paginas,
            personas=personas,
            tipos=eq.ETIQUETA_TIPO,
            filtro_persona=persona_id,
            filtro_tipo=tipo,
            filtro_ca=ca,
            qs_base=urlencode({k: v for k, v in qs.items() if v}),
        ),
    )


# --- Preferencias y nombre visible -------------------------------------------------------------


@router.post("/cuenta/alarmas")
async def cuenta_alarmas(
    request: Request,
    csrf_token: str = Form(""),
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    user, _ = sesion
    form = await request.form()
    activas = {str(v) for v in form.getlist("pref")} & set(eq.PREFERENCIAS)
    eq.guardar_preferencias(session, user.id, activas)
    session.commit()
    return RedirectResponse(url="/cuenta?mensaje=Alarmas+actualizadas", status_code=303)


@router.post("/admin/usuarios/{uid}/nombre")
async def admin_usuario_nombre(
    request: Request,
    uid: int,
    nombre: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Nombre visible (menciones, campana, iniciales). Vacío = la parte del correo antes de la @."""
    check_csrf(request, csrf_token)
    target = session.get(Usuario, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    target.nombre = " ".join(nombre.split())[:NOMBRE_MAX] or None
    session.commit()
    return RedirectResponse(url="/admin/usuarios?mensaje=Nombre+actualizado", status_code=303)
