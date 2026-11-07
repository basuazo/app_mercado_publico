"""Datos de «Hoy» (F-dis-datos): vista previa para el admin y «Desde tu última visita».

- `GET /hoy/previa` (solo admin, no va en el menú): las cifras, «Te necesita», el embudo, la
  actividad y las novedades como tablas planas, para comparar los números con la realidad antes
  de vestirlos (F-dis-hoy-mesa). La portada `/` no cambia.
- `POST /hoy/visto`: guarda `visto_en = ahora` en la vista «hoy» del usuario (CSRF, regla 18).

Ownership (reglas 17 y 26): la empresa sale de la sesión; nada se lee del cliente salvo el
período y el filtro, validados contra listas cerradas.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    check_csrf,
    empresa_activa_id,
    get_db,
    html_require_admin,
    html_require_user,
)
from app.api.routes.pages import _TEMPLATES, _ctx, _es_htmx
from app.core.tiempo import ahora_utc
from app.mercado import equipo as eq
from app.mercado import hoy
from app.mercado.territorio import regiones_activas
from app.models.tables import Usuario

router = APIRouter()


@router.get("/hoy/previa", response_class=HTMLResponse)
async def hoy_previa(
    request: Request,
    periodo: str = "hoy",
    tipo: str = "",
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> Response:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        # El mismo aviso que la portada para una cuenta sin empresa activa.
        return _TEMPLATES.TemplateResponse(
            request, "proveedores_sin_empresa.html", _ctx(request, user)
        )
    periodo = periodo if periodo in hoy.PERIODOS else "hoy"
    tipo = tipo if tipo in hoy.TIPOS else ""
    ahora = ahora_utc()
    regiones = regiones_activas(session, request.app.state.settings)
    pendientes = hoy.te_necesita(session, empresa_id, user.id, regiones, ahora)
    desde = hoy.desde_ultima_visita(session, user.id, ahora)
    return _TEMPLATES.TemplateResponse(
        request,
        "hoy_previa.html",
        _ctx(
            request,
            user,
            periodo=periodo,
            periodos=hoy.ETIQUETA_PERIODO,
            cifras=hoy.cifras(session, empresa_id, regiones, periodo, ahora),
            tipo=tipo,
            tipos=hoy.ETIQUETA_TIPO,
            conteo=hoy.conteo_por_tipo(pendientes),
            total=len(pendientes),
            pendientes=[p for p in pendientes if not tipo or p.tipo == tipo],
            embudo=hoy.embudo(session, empresa_id, ahora),
            actividad=eq.ultimas_actividades(session, empresa_id, user.id, ahora),
            novedades=hoy.novedades(session, empresa_id, user.id, desde, ahora),
            desde=eq.hora_chile(desde, "%d/%m %H:%M"),
            fuente=hoy.FUENTE,
        ),
    )


@router.post("/hoy/visto", response_model=None)
async def hoy_visto(
    request: Request,
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> Response:
    """«Desde tu última visita» → visto. Con HTMX responde 200 vacío (el bloque desaparece con
    `hx-swap="outerHTML"`); sin HTMX vuelve a «Hoy»."""
    check_csrf(request, csrf_token)
    hoy.marcar_visto(session, user.id, ahora_utc())
    session.commit()
    if _es_htmx(request):
        return HTMLResponse("", status_code=200)
    return RedirectResponse(url="/?hoy=1", status_code=303)
