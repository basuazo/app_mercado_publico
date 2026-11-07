"""Mesa del día (F-mesa-v0; F-plat-5): `/mesa`, con las Compras Ágiles vigentes del territorio.

Ownership (regla 26): solo usuarios con empresa activa; sin empresa, `/mesa` muestra el aviso
(`_sin_empresa`) y los parciales HTMX responden 403. El territorio es la unión de regiones
activas (regla 25); una CA de otra región es indistinguible de una inexistente (404).
F-plat-5: la tarjeta suma precio sugerido, costo, margen y P(ganar) leídos de
`evaluaciones_cache` (la escribe el job `mercado-mesa`); la lista NUNCA evalúa en el request.
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
from app.api.routes.lotes import contexto_mesa
from app.api.routes.mesa import _sin_empresa
from app.api.routes.pages import _TEMPLATES, _ctx
from app.core.tiempo import ahora_utc
from app.mercado import equipo as eq
from app.mercado import oferta_propia as op
from app.mercado.comprador import historial_comprador
from app.mercado.mesa_dia import (
    CIERRES,
    DETALLES,
    ORDENES,
    PAGINA,
    FiltrosMesa,
    buscar_codigos,
    evaluaciones_de,
    familias_de_ca,
    familias_por_ca,
    filtrar_precio,
    tarjetas,
    valor_orden,
)
from app.mercado.mesa_dia import con_prediccion as predichas_de
from app.mercado.territorio import regiones_activas
from app.models.enums import EstadoOportunidad
from app.models.tables import CompraAgil, Empresa, Usuario

router = APIRouter()

MARGEN_MIN_MAX = 100.0


def _filtros(  # noqa: PLR0913 - una dimensión filtrable = un parámetro
    familia: str,
    cierre: str,
    detalle: str,
    anexos: str,
    monto_min: str,
    q: str,
    con_costo: str = "",
    margen_min: str = "",
    con_prediccion: str = "",
    orden: str = "",
    con_stock: str = "",
) -> FiltrosMesa:
    """Parámetros GET → filtros; un valor inválido se ignora (nunca rompe la pantalla)."""
    try:
        monto = int(monto_min.replace(".", "").strip()) if monto_min.strip() else None
    except ValueError:
        monto = None
    try:
        margen = float(margen_min.replace(",", ".").strip()) if margen_min.strip() else None
    except ValueError:
        margen = None
    if margen is not None and not -MARGEN_MIN_MAX <= margen <= MARGEN_MIN_MAX:
        margen = None
    return FiltrosMesa(
        familia=familia.strip(),
        cierre=cierre if cierre in CIERRES else "",
        detalle=detalle if detalle in DETALLES else "",
        con_anexos=anexos == "1",
        monto_min=monto if monto and monto > 0 else None,
        q=q.strip()[:200],
        con_costo=con_costo == "1",
        margen_min=margen / 100 if margen is not None else None,
        con_prediccion=con_prediccion == "1",
        orden=orden if orden in ORDENES else "",
        con_stock=con_stock == "1",
    )


def _comunas(session: Session, empresa_id: int) -> list[str]:
    empresa = session.get(Empresa, empresa_id)
    return [c for c in (empresa.comunas_prioritarias if empresa else []) or [] if c]


def _margen_texto(f: FiltrosMesa) -> str:
    if f.margen_min is None:
        return ""
    v = f.margen_min * 100
    return str(int(v)) if v == int(v) else f"{v:g}"


@router.get("/mesa", response_class=HTMLResponse)
async def mesa(  # noqa: PLR0913 - una dimensión filtrable = un parámetro
    request: Request,
    familia: str = "",
    cierre: str = "",
    detalle: str = "",
    anexos: str = "",
    monto_min: str = "",
    q: str = "",
    equipo: str = "",
    descartadas: str = "",
    con_costo: str = "",
    margen_min: str = "",
    con_prediccion: str = "",
    orden: str = "",
    con_stock: str = "",
    pagina: int = 1,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    empresa_id = empresa_activa_id(session, user)
    if empresa_id is None:
        return _sin_empresa(request, user)
    equipo = equipo if equipo in eq.FILTROS_EQUIPO else ""
    ver_descartadas = descartadas == "1"
    ahora = ahora_utc()
    regiones = regiones_activas(session, request.app.state.settings)
    comunas = _comunas(session, empresa_id)
    filtros = _filtros(
        familia, cierre, detalle, anexos, monto_min, q, con_costo, margen_min, con_prediccion,
        orden, con_stock,
    )
    familias = familias_por_ca(session, regiones, ahora)
    cache = evaluaciones_de(session, empresa_id)
    predichas = predichas_de(session, empresa_id)
    codigos = buscar_codigos(
        session, regiones, comunas, filtros, ahora, familias,
        valor=None if filtros.orden == "cierre" else valor_orden(cache),
    )  # fmt: skip
    codigos = filtrar_precio(codigos, filtros, cache, predichas)
    # F-plat-capas: cobertura del inventario (una consulta por lote de CA, nunca por tarjeta).
    # El filtro «Con stock» la calcula para todas; sin filtro, solo para la página.
    hay_inventario = op.tiene_inventario(session, empresa_id)
    cobertura: dict[str, op.ResumenCA] = {}
    if filtros.con_stock:
        cobertura = op.resumen_por_ca(session, empresa_id, codigos) if hay_inventario else {}
        codigos = op.filtrar_con_stock(codigos, cobertura)
    # F-equipo: filtro «Equipo»; las descartadas se ocultan salvo que se pidan.
    codigos = eq.filtrar_equipo(session, empresa_id, user.id, codigos, equipo, ver_descartadas)
    total = len(codigos)
    total_paginas = max(1, ceil(total / PAGINA))
    pagina = min(max(pagina, 1), total_paginas)
    items = tarjetas(
        session, codigos[(pagina - 1) * PAGINA : pagina * PAGINA], comunas, familias, ahora
    )
    if hay_inventario and not filtros.con_stock:
        cobertura = op.resumen_por_ca(session, empresa_id, [it["codigo"] for it in items])
    for it in items:
        it["stock"] = cobertura.get(it["codigo"]) if hay_inventario else None
        it["url_publica"] = _url_ficha("compras_agiles", it["codigo"])
        ev = cache.get(it["codigo"])
        it["precio"] = ev.datos if ev else None
        it["evaluada_hace"] = eq.hace(ev.evaluada_en, ahora) if ev else ""
        it["con_prediccion"] = it["codigo"] in predichas
    estado_equipo = eq.equipo_de(session, empresa_id, user, [it["codigo"] for it in items])
    # F-plat-lote: casilla «Cotizar» por tarjeta y lotes donde aún se puede agregar.
    lotes_mesa = contexto_mesa(session, empresa_id, [it["codigo"] for it in items])
    opciones_familia = sorted({f for fs in familias.values() for f in fs})
    qs = {
        "familia": filtros.familia,
        "cierre": filtros.cierre,
        "detalle": filtros.detalle,
        "anexos": "1" if filtros.con_anexos else "",
        "monto_min": str(filtros.monto_min or ""),
        "q": filtros.q,
        "equipo": equipo,
        "descartadas": "1" if ver_descartadas else "",
        "con_costo": "1" if filtros.con_costo else "",
        "margen_min": _margen_texto(filtros),
        "con_prediccion": "1" if filtros.con_prediccion else "",
        "orden": filtros.orden,
        "con_stock": "1" if filtros.con_stock else "",
    }
    empresa = session.get(Empresa, empresa_id)
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
            margen_min_texto=_margen_texto(filtros),
            opciones_familia=opciones_familia,
            comunas_prioritarias=comunas,
            qs_base=urlencode({k: v for k, v in qs.items() if v}),
            mostrar_publica=mostrar_ficha_oficial(EstadoOportunidad.PUBLICADA.value),
            estado_equipo=estado_equipo,
            filtro_equipo=equipo,
            ver_descartadas=ver_descartadas,
            filtros_equipo=eq.ETIQUETA_FILTRO,
            yo=user.id,
            miembros=[(m.id, eq.nombre_visible(m)) for m in eq.miembros(session, empresa_id)],
            estados=eq.ETIQUETA_ESTADO,
            empresa_sin_rut=eq.rut_empresa(empresa) is None,
            hay_cache=bool(cache),
            lote_de=lotes_mesa["lote_de"],
            lotes_abiertos=lotes_mesa["lotes_abiertos"],
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
    equipo: str = "",
    descartadas: str = "",
    con_costo: str = "",
    margen_min: str = "",
    con_prediccion: str = "",
    orden: str = "",
    con_stock: str = "",
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Conteo en vivo (HTMX), con los mismos filtros: solo un número, nunca filas."""
    user, empresa_id = sesion
    ahora = ahora_utc()
    regiones = regiones_activas(session, request.app.state.settings)
    filtros = _filtros(
        familia, cierre, detalle, anexos, monto_min, q, con_costo, margen_min, con_prediccion,
        orden, con_stock,
    )
    codigos = buscar_codigos(session, regiones, [], filtros, ahora)
    if filtros.con_costo or filtros.margen_min is not None or filtros.con_prediccion:
        codigos = filtrar_precio(
            codigos,
            filtros,
            evaluaciones_de(session, empresa_id),
            predichas_de(session, empresa_id),
        )
    if filtros.con_stock:
        codigos = op.filtrar_con_stock(codigos, op.resumen_por_ca(session, empresa_id, codigos))
    codigos = eq.filtrar_equipo(
        session,
        empresa_id,
        user.id,
        codigos,
        equipo if equipo in eq.FILTROS_EQUIPO else "",
        descartadas == "1",
    )
    return _TEMPLATES.TemplateResponse(request, "_explorador_conteo.html", {"total": len(codigos)})


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
    # F-plat-comp: enlace a los competidores del comprador × la familia de la tarjeta (la misma
    # etiqueta de la mesa); «Varias» o «Sin productos» enlazan sin familia.
    familias = familias_de_ca(session, codigo)
    qs = {"comprador": hist.clave}
    if len(familias) == 1:
        qs["familia"] = familias[0]
    return _TEMPLATES.TemplateResponse(
        request,
        "_mesa_comprador.html",
        {
            "h": hist,
            "region_nombre": nombre_region(ca.region),
            "url_competencia": "/competencia?" + urlencode(qs),
        },
    )


@router.get("/mesa/ca/{codigo}/panel", response_class=HTMLResponse)
async def mesa_panel(
    request: Request,
    codigo: str,
    sesion: tuple[Usuario, int] = Depends(html_require_empresa),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Panel lateral de la CA (F-dis-datos): datos que ya existen, sin evaluar en el GET. Una CA
    inexistente o fuera del territorio es 404 (el mismo `_ca_o_404` de la ficha de precio)."""
    from app.api.routes.mesa_precio import _ca_o_404
    from app.mercado.panel_ca import panel

    _, empresa_id = sesion
    _ca_o_404(request, session, codigo)
    datos = panel(session, empresa_id, codigo, ahora_utc())
    if datos is None:
        raise HTTPException(status_code=404, detail="Compra Ágil no encontrada")
    return _TEMPLATES.TemplateResponse(request, "_mesa_panel.html", {"p": datos})
