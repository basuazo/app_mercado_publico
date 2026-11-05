"""Abastecimiento (F-plat-6): qué va a comprar cada comprador, cuánto y cuándo.

- `GET /abastecimiento?tab=recomendaciones|calendario|compradores|item`: solo usuarios con
  empresa activa (sin empresa, el aviso de siempre). Todo se acota a las regiones de la empresa.
- `POST /abastecimiento/recomendaciones/{id}`: marca una recomendación (hecha, descartada, vista
  o nueva). CSRF (regla 18); la empresa sale de la sesión y una recomendación de otra empresa
  responde 404 (reglas 17 y 26).
- Lee solo tablas precalculadas por `mercado-demanda`: ningún request recorre `oc_lineas_ca`.
Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.api.deps import (
    check_csrf,
    empresa_activa_id,
    get_db,
    html_require_empresa,
    html_require_user,
)
from app.api.presentacion import nombre_region
from app.api.routes.mesa import _sin_empresa
from app.api.routes.pages import _TEMPLATES, _ctx, _es_htmx
from app.mercado import abastecimiento as ab
from app.mercado.demanda import NIVELES
from app.models.tables import Usuario

router = APIRouter()

PESTANAS = ("recomendaciones", "calendario", "compradores", "item")


@router.get("/abastecimiento", response_model=None)
async def abastecimiento(
    request: Request,
    tab: str = "recomendaciones",
    nivel: str = "unspsc",
    item: str = "",
    comprador: str = "",
    q: str = "",
    cerradas: bool = False,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> Response:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    tab = tab if tab in PESTANAS else "recomendaciones"
    nivel = nivel if nivel in NIVELES else "unspsc"
    item, comprador, q = item.strip()[:100], comprador.strip()[:600], q.strip()[:100]
    ctx = ab.contexto(session, empresa_id)
    datos: dict[str, object] = {}
    if tab == "recomendaciones":
        datos["recs"] = ab.recomendaciones_ui(session, empresa_id, cerradas=cerradas)
    elif tab == "calendario":
        datos["cal"] = ab.calendario(session, ctx, nivel)
    elif tab == "compradores" and comprador:
        detalle = ab.comprador_ui(session, ctx, comprador)
        if detalle is None:
            raise HTTPException(status_code=404, detail="Comprador sin compras en tu territorio")
        datos["comprador"] = detalle
    elif tab == "compradores":
        datos["compradores"] = ab.compradores_ui(session, ctx, q)
    elif tab == "item":
        if not item:
            return RedirectResponse("/abastecimiento?tab=calendario", status_code=303)
        detalle = ab.item_ui(session, ctx, nivel, item)
        if detalle is None:
            raise HTTPException(status_code=404, detail="Ítem sin compras en tu territorio")
        datos["it"] = detalle
    return _TEMPLATES.TemplateResponse(
        request,
        "abastecimiento.html",
        _ctx(
            request,
            user,
            tab=tab,
            nivel=nivel,
            q=q,
            cerradas=cerradas,
            hoy=ctx.hoy,
            backtest=ab.resumen_backtest(ctx.ver),
            v1=ctx.v1,
            etiquetas=ab.ETIQUETAS,
            tipos=ab.TIPOS,
            nombre_mes=ab.nombre_mes,
            regiones_texto=", ".join(nombre_region(r) or str(r) for r in ctx.regiones),
            **datos,
        ),
    )


@router.post("/abastecimiento/recomendaciones/{rec_id}", response_model=None)
async def recomendacion_estado(
    request: Request,
    rec_id: int,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> Response:
    form = await request.form()
    check_csrf(request, str(form.get("csrf_token", "")))
    user, empresa_id = sesion
    rec = ab.recomendacion_de(session, empresa_id, rec_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Recomendación no encontrada")
    estado = str(form.get("estado", ""))
    if estado not in ab.ESTADOS:
        raise HTTPException(status_code=422, detail="Estado inválido")
    ab.cambiar_estado(rec, estado, user.id)
    session.commit()
    if _es_htmx(request):
        return HTMLResponse("")  # 200 vacío: la fila sale del DOM (outerHTML)
    return RedirectResponse("/abastecimiento?tab=recomendaciones", status_code=303)
