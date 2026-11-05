"""Mesa del día v0 (F-mesa-v0): `/mesa`, con las Compras Ágiles vigentes del territorio.

Ownership (regla 26): solo usuarios con empresa activa; sin empresa, `/mesa` muestra el aviso
(`_sin_empresa`) y los parciales HTMX responden 403. El territorio es la unión de regiones
activas (regla 25); una CA de otra región es indistinguible de una inexistente (404).
F-plat-5 crece sobre esta misma pantalla.
"""

from __future__ import annotations

from math import ceil
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import empresa_activa_id, get_db, html_require_empresa, html_require_user
from app.api.presentacion import nombre_region
from app.api.query import _url_ficha, mostrar_ficha_oficial
from app.api.routes.mesa import _sin_empresa
from app.api.routes.pages import _TEMPLATES, _ctx
from app.core.tiempo import ahora_utc
from app.mercado.comprador import historial_comprador
from app.mercado.mesa_dia import (
    CIERRES,
    DETALLES,
    PAGINA,
    FiltrosMesa,
    buscar_codigos,
    familias_por_ca,
    tarjetas,
)
from app.mercado.territorio import regiones_activas
from app.models.enums import EstadoOportunidad
from app.models.tables import CompraAgil, Empresa, Usuario

router = APIRouter()


def _filtros(
    familia: str, cierre: str, detalle: str, anexos: str, monto_min: str, q: str
) -> FiltrosMesa:
    """Parámetros GET → filtros; un valor inválido se ignora (nunca rompe la pantalla)."""
    try:
        monto = int(monto_min.replace(".", "").strip()) if monto_min.strip() else None
    except ValueError:
        monto = None
    return FiltrosMesa(
        familia=familia.strip(),
        cierre=cierre if cierre in CIERRES else "",
        detalle=detalle if detalle in DETALLES else "",
        con_anexos=anexos == "1",
        monto_min=monto if monto and monto > 0 else None,
        q=q.strip()[:200],
    )


def _comunas(session: Session, empresa_id: int) -> list[str]:
    empresa = session.get(Empresa, empresa_id)
    return [c for c in (empresa.comunas_prioritarias if empresa else []) or [] if c]


@router.get("/mesa", response_class=HTMLResponse)
async def mesa(  # noqa: PLR0913 - una dimensión filtrable = un parámetro
    request: Request,
    familia: str = "",
    cierre: str = "",
    detalle: str = "",
    anexos: str = "",
    monto_min: str = "",
    q: str = "",
    pagina: int = 1,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    ahora = ahora_utc()
    regiones = regiones_activas(session, request.app.state.settings)
    comunas = _comunas(session, empresa_id)
    filtros = _filtros(familia, cierre, detalle, anexos, monto_min, q)
    familias = familias_por_ca(session, regiones, ahora)
    codigos = buscar_codigos(session, regiones, comunas, filtros, ahora, familias)
    total = len(codigos)
    total_paginas = max(1, ceil(total / PAGINA))
    pagina = min(max(pagina, 1), total_paginas)
    items = tarjetas(
        session, codigos[(pagina - 1) * PAGINA : pagina * PAGINA], comunas, familias, ahora
    )
    for it in items:
        it["url_publica"] = _url_ficha("compras_agiles", it["codigo"])
    opciones_familia = sorted({f for fs in familias.values() for f in fs})
    qs = {
        "familia": filtros.familia,
        "cierre": filtros.cierre,
        "detalle": filtros.detalle,
        "anexos": "1" if filtros.con_anexos else "",
        "monto_min": str(filtros.monto_min or ""),
        "q": filtros.q,
    }
    return _TEMPLATES.TemplateResponse(
        request,
        "mesa_dia.html",
        _ctx(
            request,
            user,
            items=items,
            total=total,
            pagina=pagina,
            total_paginas=total_paginas,
            filtros=filtros,
            opciones_familia=opciones_familia,
            comunas_prioritarias=comunas,
            qs_base=urlencode({k: v for k, v in qs.items() if v}),
            mostrar_publica=mostrar_ficha_oficial(EstadoOportunidad.PUBLICADA.value),
        ),
    )


@router.get("/mesa/conteo", response_class=HTMLResponse)
async def mesa_conteo(  # noqa: PLR0913
    request: Request,
    familia: str = "",
    cierre: str = "",
    detalle: str = "",
    anexos: str = "",
    monto_min: str = "",
    q: str = "",
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Conteo en vivo (HTMX), con los mismos filtros: solo un número, nunca filas."""
    ahora = ahora_utc()
    regiones = regiones_activas(session, request.app.state.settings)
    codigos = buscar_codigos(
        session, regiones, [], _filtros(familia, cierre, detalle, anexos, monto_min, q), ahora
    )
    return _TEMPLATES.TemplateResponse(
        request, "_explorador_conteo.html", {"total": len(codigos)}
    )


@router.get("/mesa/ca/{codigo}/comprador", response_class=HTMLResponse)
async def mesa_comprador(
    request: Request,
    codigo: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Historial del comprador (últimos 12 meses), calculado al abrir el desplegable."""
    regiones = regiones_activas(session, request.app.state.settings)
    ca = session.execute(
        select(CompraAgil.organismo_rut, CompraAgil.unidad_compra, CompraAgil.region).where(
            CompraAgil.codigo == codigo
        )
    ).one_or_none()
    if ca is None or ca.region not in regiones:
        raise HTTPException(status_code=404, detail="Compra Ágil no encontrada")
    hist = historial_comprador(session, ca.organismo_rut, ca.unidad_compra, ahora=ahora_utc())
    return _TEMPLATES.TemplateResponse(
        request,
        "_mesa_comprador.html",
        {"h": hist, "region_nombre": nombre_region(ca.region)},
    )
