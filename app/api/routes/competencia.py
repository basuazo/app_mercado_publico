"""Competencia y estrategia (F-plat-comp): `/competencia`, `/competencia/{rut}`, `/estrategia`.

- Solo usuarios con empresa activa; sin empresa, el aviso de siempre (`_sin_empresa`).
- Todo se acota a las regiones de la empresa (`/estrategia`: «solo regiones activas de la
  empresa»; un competidor sin actividad en ellas responde 404).
- Leen solo `competencia_mes` y `competencia_comprador_mes` (precalculadas por el job): ningún
  request recorre `ca_cotizaciones`.
- Los competidores son datos públicos de ChileCompra: no se exportan en bloque ni se envían a
  terceros. Lo que pertenece a la empresa (regla 26) es su RUT, que solo marca «nosotros».
Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.api.deps import empresa_activa_id, get_db, html_require_user
from app.api.presentacion import nombre_region
from app.api.routes.mesa import _sin_empresa
from app.api.routes.pages import _TEMPLATES, _ctx
from app.mercado.competencia import (
    MESES_UI,
    ORDENES_MAPA,
    buscar_competidores,
    cara_a_cara,
    competidores_de,
    mapa_mercado,
    perfil_competidor,
    ranking_territorio,
    regiones_empresa,
    rut_empresa,
)
from app.models.tables import Usuario

router = APIRouter()


@router.get("/competencia", response_class=HTMLResponse)
async def competencia(
    request: Request,
    q: str = "",
    comprador: str = "",
    familia: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    regiones = regiones_empresa(session, empresa_id)
    nosotros = rut_empresa(session, empresa_id)
    q, comprador, familia = q.strip()[:100], comprador.strip()[:600], familia.strip()[:100]
    vista_comprador = (
        competidores_de(session, comprador, familia or None, regiones, nosotros=nosotros)
        if comprador
        else None
    )
    resultados = buscar_competidores(session, q, regiones, nosotros=nosotros) if q else None
    ranking = (
        ranking_territorio(session, regiones, nosotros=nosotros)
        if vista_comprador is None and resultados is None
        else []
    )
    return _TEMPLATES.TemplateResponse(
        request,
        "competencia.html",
        _ctx(
            request,
            user,
            q=q,
            vista_comprador=vista_comprador,
            resultados=resultados,
            ranking=ranking,
            tiene_rut=nosotros is not None,
            regiones_texto=", ".join(nombre_region(r) or str(r) for r in regiones),
            meses=MESES_UI,
            qs_estrategia=urlencode({"familia": familia}) if familia else "",
        ),
    )


@router.get("/competencia/{rut}", response_class=HTMLResponse)
async def competencia_perfil(
    request: Request,
    rut: str,
    vs: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    regiones = regiones_empresa(session, empresa_id)
    perfil = perfil_competidor(session, rut[:30], regiones)
    if perfil is None:
        raise HTTPException(status_code=404, detail="Competidor sin actividad en tu territorio")
    nosotros = rut_empresa(session, empresa_id)
    duelo = cara_a_cara(session, perfil.rut, vs[:30], regiones) if vs.strip() else None
    return _TEMPLATES.TemplateResponse(
        request,
        "competencia_perfil.html",
        _ctx(
            request,
            user,
            p=perfil,
            es_nosotros=nosotros is not None and perfil.rut == nosotros,
            nosotros=nosotros,
            vs=vs.strip()[:30],
            duelo=duelo,
            region_proveedor=nombre_region(perfil.region_proveedor)
            if perfil.region_proveedor is not None
            else None,
        ),
    )


@router.get("/estrategia", response_class=HTMLResponse)
async def estrategia(
    request: Request,
    familia: str = "",
    orden: str = "monto",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    orden = orden if orden in ORDENES_MAPA else "monto"
    mapa = mapa_mercado(session, empresa_id, familia=familia.strip()[:100], orden=orden)
    return _TEMPLATES.TemplateResponse(
        request,
        "estrategia.html",
        _ctx(
            request,
            user,
            mapa=mapa,
            familia=familia.strip()[:100],
            orden=orden,
            regiones_texto=", ".join(nombre_region(r) or str(r) for r in mapa.regiones),
            meses=MESES_UI,
            qs_familia=urlencode({"familia": familia.strip()}) if familia.strip() else "",
        ),
    )
