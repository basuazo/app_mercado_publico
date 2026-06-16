"""Rutas HTML: dashboard, detalle oportunidad, perfiles, admin, salud."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import (
    check_csrf,
    get_db,
    html_require_admin,
    html_require_user,
)
from app.api.query import check_oportunidad_access, get_oportunidades_usuario
from app.api.salud_data import get_salud_data
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password
from app.matching.perfiles import (
    actualizar_perfil,
    crear_perfil,
    eliminar_perfil,
    listar_perfiles,
    obtener_perfil,
)
from app.models.enums import FrecuenciaAlerta, RolUsuario
from app.models.tables import CompraAgil, Licitacion, Usuario

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _ctx(request: Request, user: Usuario, **extra: Any) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "current_user": user,
        "csrf_token": generate_csrf_token(settings.secret_key, user.id),
        **extra,
    }


# ---------------------------------------------------------------------------
# Dashboard principal
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    fuente: str = "",
    texto: str = "",
    perfil_id: str = "",
    pagina: int = 1,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    limit = 20
    offset = (pagina - 1) * limit
    perfil_id_int: int | None = int(perfil_id) if perfil_id.strip().isdigit() else None
    items, total = get_oportunidades_usuario(
        session,
        user.id,
        fuente=fuente or None,
        texto=texto or None,
        perfil_id=perfil_id_int,
        limit=limit,
        offset=offset,
    )
    perfiles = listar_perfiles(session, user.id)
    total_paginas = max(1, (total + limit - 1) // limit)
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            user,
            items=items,
            total=total,
            pagina=pagina,
            total_paginas=total_paginas,
            fuente=fuente,
            texto=texto,
            perfil_id=perfil_id,
            perfiles=perfiles,
        ),
    )


# ---------------------------------------------------------------------------
# Detalle oportunidad
# ---------------------------------------------------------------------------


@router.get("/oportunidad/{fuente}/{codigo}", response_class=HTMLResponse)
async def oportunidad_detalle(
    request: Request,
    fuente: str,
    codigo: str,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    match = check_oportunidad_access(session, user.id, fuente, codigo)
    if match is None:
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")

    op: Licitacion | CompraAgil | None = None
    if fuente == "licitaciones":
        op = session.get(Licitacion, codigo)
    elif fuente == "compras_agiles":
        op = session.get(CompraAgil, codigo)
    if op is None:
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")

    from app.api.query import _url_ficha
    url_ficha = _url_ficha(fuente, codigo)

    return _TEMPLATES.TemplateResponse(
        request,
        "oportunidad.html",
        _ctx(request, user, match=match, oportunidad=op, fuente=fuente, url_ficha=url_ficha),
    )


# ---------------------------------------------------------------------------
# Perfiles CRUD
# ---------------------------------------------------------------------------


@router.get("/perfiles", response_class=HTMLResponse)
async def perfiles_get(
    request: Request,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    perfiles = listar_perfiles(session, user.id)
    return _TEMPLATES.TemplateResponse(
        request,
        "perfiles.html",
        _ctx(
            request,
            user,
            perfiles=perfiles,
            frecuencias=list(FrecuenciaAlerta),
            mensaje=mensaje,
            error=error,
        ),
    )


@router.post("/perfiles/nuevo")
async def perfil_crear(
    request: Request,
    nombre: str = Form(...),
    keywords: str = Form(""),
    excluir: str = Form(""),
    fuentes: list[str] = Form(default=[]),
    frecuencia_alerta: str = Form("inmediata"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, user.id, csrf_token)
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    ex_list = [k.strip() for k in excluir.split(",") if k.strip()]
    fuentes_list = fuentes or ["licitaciones", "compras_agiles"]
    crear_perfil(
        session,
        owner_id=user.id,
        nombre=nombre,
        keywords=kw_list,
        keywords_excluir=ex_list,
        fuentes=fuentes_list,
        frecuencia_alerta=FrecuenciaAlerta(frecuencia_alerta),
    )
    session.commit()
    return RedirectResponse(url="/perfiles?mensaje=Perfil+creado", status_code=303)


@router.post("/perfiles/{perfil_id}/eliminar")
async def perfil_eliminar(
    request: Request,
    perfil_id: int,
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, user.id, csrf_token)
    perfil = obtener_perfil(session, perfil_id, user.id)
    if perfil is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    eliminar_perfil(session, perfil_id, user.id)
    session.commit()
    return RedirectResponse(url="/perfiles?mensaje=Perfil+eliminado", status_code=303)


@router.post("/perfiles/{perfil_id}/editar")
async def perfil_editar(
    request: Request,
    perfil_id: int,
    nombre: str = Form(...),
    keywords: str = Form(""),
    excluir: str = Form(""),
    fuentes: list[str] = Form(default=[]),
    frecuencia_alerta: str = Form("inmediata"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, user.id, csrf_token)
    perfil = obtener_perfil(session, perfil_id, user.id)
    if perfil is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    ex_list = [k.strip() for k in excluir.split(",") if k.strip()]
    fuentes_list = fuentes or ["licitaciones", "compras_agiles"]
    actualizar_perfil(
        session,
        perfil_id=perfil_id,
        owner_id=user.id,
        nombre=nombre,
        keywords=kw_list,
        keywords_excluir=ex_list,
        fuentes=fuentes_list,
        frecuencia_alerta=FrecuenciaAlerta(frecuencia_alerta),
    )
    session.commit()
    return RedirectResponse(url="/perfiles?mensaje=Perfil+actualizado", status_code=303)


# ---------------------------------------------------------------------------
# Admin: usuarios
# ---------------------------------------------------------------------------


@router.get("/admin/usuarios", response_class=HTMLResponse)
async def admin_usuarios_get(
    request: Request,
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    usuarios = list(session.execute(select(Usuario).order_by(Usuario.id)).scalars())
    return _TEMPLATES.TemplateResponse(
        request,
        "admin_usuarios.html",
        _ctx(request, user, usuarios=usuarios, mensaje=mensaje, error=error),
    )


@router.post("/admin/usuarios/nuevo")
async def admin_usuario_crear(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    rol: str = Form("usuario"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, user.id, csrf_token)
    existente = session.execute(
        select(Usuario).where(Usuario.email == email)
    ).scalar_one_or_none()
    if existente:
        return RedirectResponse(
            url="/admin/usuarios?error=Email+ya+registrado", status_code=303
        )
    nuevo = Usuario(
        email=email,
        password_hash=hash_password(password),
        rol=RolUsuario(rol),
        activo=True,
    )
    session.add(nuevo)
    session.commit()
    return RedirectResponse(url="/admin/usuarios?mensaje=Usuario+creado", status_code=303)


@router.post("/admin/usuarios/{uid}/desactivar")
async def admin_usuario_desactivar(
    request: Request,
    uid: int,
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, user.id, csrf_token)
    target = session.get(Usuario, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if target.id == user.id:
        return RedirectResponse(
            url="/admin/usuarios?error=No+puedes+desactivarte+a+ti+mismo", status_code=303
        )
    target.activo = False
    session.commit()
    return RedirectResponse(url="/admin/usuarios?mensaje=Usuario+desactivado", status_code=303)


# ---------------------------------------------------------------------------
# Salud del sistema (admin)
# ---------------------------------------------------------------------------


@router.get("/salud", response_class=HTMLResponse)
async def salud_get(
    request: Request,
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    settings = request.app.state.settings
    data = get_salud_data(session, settings)
    return _TEMPLATES.TemplateResponse(
        request,
        "salud.html",
        _ctx(request, user, salud=data),
    )
