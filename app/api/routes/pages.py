"""Rutas HTML: dashboard, detalle oportunidad, perfiles, admin, salud, plan anual."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.deps import (
    check_csrf,
    get_db,
    html_require_admin,
    html_require_user,
)
from app.api.query import (
    AGRUPAR_POR_VALIDOS,
    agrupar_oportunidades,
    buscar_instituciones_pac,
    check_oportunidad_access,
    detalle_competencia,
    get_item_oportunidad,
    get_oportunidades_usuario,
    listar_descartadas_detalle,
    listar_organismos_catalogo,
    listar_seguidas_detalle,
    resumen_competencia,
)
from app.api.salud_data import get_salud_data
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password, verify_password
from app.catalogos.unspsc import familias, nombre_rubro, segmentos
from app.core.logging import get_logger
from app.ingest.plan_compra import get_plan, sync_instituciones_pac, sync_sectores_organismos
from app.matching.engine import match_perfil
from app.matching.feedback import alternar_me_sirve, deshacer_descarte, listar_descartadas
from app.matching.feedback import descartar as marcar_descarte
from app.matching.perfiles import (
    PerfilInvalido,
    actualizar_perfil,
    crear_perfil,
    eliminar_perfil,
    listar_perfiles,
    obtener_perfil,
)
from app.matching.seguimiento import (
    archivar_seguimiento,
    dejar_de_seguir,
    obtener_seguimiento,
    seguir_oportunidad,
)
from app.models.enums import EstadoOportunidad, RolUsuario
from app.models.seeds import REGIONES
from app.models.tables import (
    CaProducto,
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    LicitacionItem,
    PerfilBusqueda,
    PlanCompraLinea,
    Usuario,
)

router = APIRouter()
_log = get_logger(__name__)
_TZ_CHILE = ZoneInfo("America/Santiago")
_PAC_PAGE_SIZE = 100
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


def _match_perfil_background(engine: Engine, perfil_id: int) -> None:
    """Genera matches desde la BD para un perfil, sin consumir la API externa."""
    try:
        with Session(engine) as session:
            perfil = session.get(PerfilBusqueda, perfil_id)
            if perfil is None or not perfil.activo:
                return
            match_perfil(perfil, session)
    except Exception:
        # La respuesta HTTP ya fue enviada: el fallo queda aislado y registrado.
        _log.error("automatch: error en perfil_id=%d", perfil_id, exc_info=True)


def _ctx(request: Request, user: Usuario, **extra: Any) -> dict[str, Any]:
    settings = request.app.state.settings
    return {
        "current_user": user,
        "csrf_token": generate_csrf_token(settings.secret_key, request.state.csrf_nonce),
        **extra,
    }


def _es_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


_ORDENES_VALIDOS = {"score", "cierre"}

# Preset "Alta relevancia" del control de umbral del feed (F-feed-umbral).
# "Media" usa settings.feed_min_score_default (configurable por env);
# "Todas" es 0 (sin piso) — ver `index`.
_RELEVANCIA_ALTA = 60

# Tope de matches traídos para agrupar (F-feed-agrupado): el feed agrupado ya
# no pagina globalmente (cada grupo se capa por separado, con "ver más en
# este grupo"), así que se pide "todo" de una vez. Generoso para la escala
# real de un equipo de 3-10 usuarios (regla de free tier); no es paginación.
_LIMITE_AGRUPADO = 2000
_PASSWORD_MIN_LEN = 8
_DIAS_RESUMEN_VALIDOS = {0, 3, 7}


# ---------------------------------------------------------------------------
# Dashboard principal
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    fuente: str = "",
    texto: str = "",
    perfil_id: str = "",
    orden: str = "score",
    min_score: int | None = None,
    agrupar_por: str = "motivo",
    grupo_expandido: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    perfil_id_int: int | None = int(perfil_id) if perfil_id.strip().isdigit() else None
    orden = orden if orden in _ORDENES_VALIDOS else "score"
    agrupar_por = agrupar_por if agrupar_por in AGRUPAR_POR_VALIDOS else "motivo"
    settings = request.app.state.settings
    min_score_efectivo = min_score if min_score is not None and min_score >= 0 else settings.feed_min_score_default
    items, total, total_sin_relevancia = get_oportunidades_usuario(
        session,
        user.id,
        fuente=fuente or None,
        texto=texto or None,
        perfil_id=perfil_id_int,
        orden=orden,
        min_score=min_score_efectivo,
        limit=_LIMITE_AGRUPADO,
        offset=0,
    )
    grupos, total_unico, total_apariciones = agrupar_oportunidades(
        items, agrupar_por, grupo_expandido=grupo_expandido or None
    )
    perfiles = listar_perfiles(session, user.id)
    n_descartadas = len(listar_descartadas(session, user.id))
    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            user,
            grupos=grupos,
            total_unico=total_unico,
            total_apariciones=total_apariciones,
            fuente=fuente,
            texto=texto,
            perfil_id=perfil_id,
            orden=orden,
            min_score=min_score_efectivo,
            agrupar_por=agrupar_por,
            n_ocultas_relevancia=total_sin_relevancia - total,
            relevancia_alta=_RELEVANCIA_ALTA,
            relevancia_media=settings.feed_min_score_default,
            n_descartadas=n_descartadas,
            perfiles=perfiles,
        ),
    )


# ---------------------------------------------------------------------------
# Descartadas (F10 parte 2)
# ---------------------------------------------------------------------------


@router.get("/descartadas", response_class=HTMLResponse)
async def descartadas_get(
    request: Request,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    items = listar_descartadas_detalle(session, user.id)
    return _TEMPLATES.TemplateResponse(
        request,
        "descartadas.html",
        _ctx(request, user, items=items),
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

    from app.api.presentacion import nombre_region, razones_legibles
    from app.api.query import _url_ficha, mostrar_ficha_oficial

    url_ficha = _url_ficha(fuente, codigo)
    seguimiento = obtener_seguimiento(session, user.id, fuente, codigo)

    competencia_resumen: list[Any] = []
    competencia_detalle: list[Any] = []
    if isinstance(op, Licitacion) and op.estado == EstadoOportunidad.ADJUDICADA.value:
        competencia_resumen = resumen_competencia(session, codigo)
        competencia_detalle = detalle_competencia(session, codigo)

    # Datos enriquecidos para la ficha
    items_raw: list[LicitacionItem] | list[CaProducto]
    organismo: str | None
    region_nombre: str | None
    if isinstance(op, Licitacion):
        items_raw = list(op.items)
        organismo = op.codigo_organismo
        region_nombre = None
    else:
        items_raw = list(op.productos)
        organismo = op.organismo_nombre
        region_nombre = nombre_region(op.region)

    items = [
        {
            "nombre": it.nombre,
            "cantidad": it.cantidad,
            "unidad": it.unidad,
            "rubro": nombre_rubro(it.codigo_producto) if it.codigo_producto else None,
        }
        for it in items_raw
    ]

    feedback_item = get_item_oportunidad(session, user.id, fuente, codigo)

    return _TEMPLATES.TemplateResponse(
        request,
        "oportunidad.html",
        _ctx(
            request,
            user,
            match=match,
            oportunidad=op,
            fuente=fuente,
            url_ficha=url_ficha,
            mostrar_ficha=mostrar_ficha_oficial(op.estado),
            items=items,
            organismo=organismo,
            region_nombre=region_nombre,
            razones=razones_legibles(match.razones),
            seguimiento=seguimiento,
            feedback_item=feedback_item,
            competencia_resumen=competencia_resumen,
            competencia_detalle=competencia_detalle,
        ),
    )


# ---------------------------------------------------------------------------
# Seguir / archivar oportunidades (F-seguir)
# ---------------------------------------------------------------------------


def _safe_next(next_: str, fallback: str) -> str:
    """Evita open-redirect: solo se acepta una ruta relativa propia."""
    if next_.startswith("/") and not next_.startswith("//"):
        return next_
    return fallback


def _render_card_partial(
    request: Request,
    user: Usuario,
    session: Session,
    fuente: str,
    codigo: str,
    *,
    origen: str = "dashboard",
) -> HTMLResponse:
    """Re-renderiza el estado de una oportunidad tras una acción HTMX rápida.

    `origen="dashboard"` (default) re-renderiza la tarjeta completa del feed;
    `origen="ficha"` re-renderiza solo la fila de botones de feedback de la
    ficha de detalle (`_ficha_acciones.html`) — son layouts distintos, no la
    misma tarjeta. Vacío si el usuario perdió acceso entretanto (regla 17)."""
    item = get_item_oportunidad(session, user.id, fuente, codigo)
    if item is None:
        return HTMLResponse(content="", status_code=200)
    settings = request.app.state.settings
    csrf_token = generate_csrf_token(settings.secret_key, request.state.csrf_nonce)
    template = "_ficha_acciones_partial.html" if origen == "ficha" else "_card_partial.html"
    return _TEMPLATES.TemplateResponse(
        request, template, {"item": item, "csrf_token": csrf_token}
    )


@router.post("/oportunidad/{fuente}/{codigo}/seguir", response_model=None)
async def oportunidad_seguir(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    check_csrf(request, csrf_token)
    if check_oportunidad_access(session, user.id, fuente, codigo) is None:
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    op: Licitacion | CompraAgil | None
    op = session.get(Licitacion, codigo) if fuente == "licitaciones" else session.get(CompraAgil, codigo)
    estado_actual = op.estado if op is not None else ""
    seguir_oportunidad(session, owner_id=user.id, fuente=fuente, codigo=codigo, estado_actual=estado_actual)
    session.commit()
    if _es_htmx(request):
        return _render_card_partial(request, user, session, fuente, codigo)
    return RedirectResponse(url=_safe_next(next, f"/oportunidad/{fuente}/{codigo}"), status_code=303)


@router.post("/oportunidad/{fuente}/{codigo}/me-sirve", response_model=None)
async def oportunidad_me_sirve(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    origen: str = Form("dashboard"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    """Toggle de "me sirve" (F10 parte 2): registra feedback POSITIVO, señal
    para F11. No reordena ni entrena nada aquí."""
    check_csrf(request, csrf_token)
    if check_oportunidad_access(session, user.id, fuente, codigo) is None:
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    alternar_me_sirve(session, user.id, fuente, codigo)
    session.commit()
    if _es_htmx(request):
        return _render_card_partial(request, user, session, fuente, codigo, origen=origen)
    return RedirectResponse(url=_safe_next(next, "/"), status_code=303)


@router.post("/oportunidad/{fuente}/{codigo}/descartar", response_model=None)
async def oportunidad_descartar(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    origen: str = Form("dashboard"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    """Descartar (F10 parte 2): registra feedback NEGATIVO. En el dashboard
    oculta el match del feed (reversible vía /descartadas); en la ficha
    (`origen=ficha`) no tiene sentido ocultar la página completa, así que
    re-renderiza la fila de botones reflejando el nuevo estado. Distinto de
    archivar (solo aplica a seguidas)."""
    check_csrf(request, csrf_token)
    if check_oportunidad_access(session, user.id, fuente, codigo) is None:
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    marcar_descarte(session, user.id, fuente, codigo)
    session.commit()
    if _es_htmx(request):
        if origen == "ficha":
            return _render_card_partial(request, user, session, fuente, codigo, origen="ficha")
        # 200 con cuerpo vacío, no 204: htmx no swapea en absoluto ante un 204.
        return HTMLResponse(content="", status_code=200)
    return RedirectResponse(url=_safe_next(next, "/"), status_code=303)


@router.post("/oportunidad/{fuente}/{codigo}/deshacer-descarte", response_model=None)
async def oportunidad_deshacer_descarte(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    origen: str = Form("dashboard"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    check_csrf(request, csrf_token)
    if check_oportunidad_access(session, user.id, fuente, codigo) is None:
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    if not deshacer_descarte(session, user.id, fuente, codigo):
        raise HTTPException(status_code=404, detail="Descarte no encontrado")
    session.commit()
    if _es_htmx(request):
        if origen == "ficha":
            return _render_card_partial(request, user, session, fuente, codigo, origen="ficha")
        return HTMLResponse(content="", status_code=200)
    return RedirectResponse(url=_safe_next(next, "/descartadas"), status_code=303)


@router.post("/oportunidad/{fuente}/{codigo}/archivar")
async def oportunidad_archivar(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    if not archivar_seguimiento(session, owner_id=user.id, fuente=fuente, codigo=codigo, archivada=True):
        raise HTTPException(status_code=404, detail="Seguimiento no encontrado")
    session.commit()
    return RedirectResponse(url=_safe_next(next, "/seguidas"), status_code=303)


@router.post("/oportunidad/{fuente}/{codigo}/desarchivar")
async def oportunidad_desarchivar(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    if not archivar_seguimiento(session, owner_id=user.id, fuente=fuente, codigo=codigo, archivada=False):
        raise HTTPException(status_code=404, detail="Seguimiento no encontrado")
    session.commit()
    return RedirectResponse(url=_safe_next(next, "/seguidas"), status_code=303)


@router.post("/oportunidad/{fuente}/{codigo}/dejar-de-seguir")
async def oportunidad_dejar_de_seguir(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    if not dejar_de_seguir(session, owner_id=user.id, fuente=fuente, codigo=codigo):
        raise HTTPException(status_code=404, detail="Seguimiento no encontrado")
    session.commit()
    return RedirectResponse(url=_safe_next(next, "/seguidas"), status_code=303)


@router.get("/seguidas", response_class=HTMLResponse)
async def seguidas_get(
    request: Request,
    archivadas: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    incluir_archivadas = archivadas == "1"
    items = listar_seguidas_detalle(session, user.id, incluir_archivadas=incluir_archivadas)
    return _TEMPLATES.TemplateResponse(
        request,
        "seguidas.html",
        _ctx(request, user, items=items, incluir_archivadas=incluir_archivadas),
    )


# ---------------------------------------------------------------------------
# Perfiles CRUD
# ---------------------------------------------------------------------------


def _parse_regiones(valores: list[str]) -> list[int]:
    """Convierte códigos de región del formulario a int, ignorando lo no numérico."""
    out: list[int] = []
    for v in valores:
        v = v.strip()
        if v.isdigit():
            out.append(int(v))
    return out


def _parse_monto(valor: str) -> float | None:
    """Convierte un monto opcional del formulario a float; vacío o inválido → None."""
    valor = valor.strip()
    if not valor:
        return None
    try:
        return float(valor)
    except ValueError:
        return None


def _parse_categorias(valores: list[str]) -> list[str]:
    """Convierte prefijos UNSPSC del formulario (select multiple + texto libre con
    comas, ambos bajo el mismo name) a una lista de prefijos válidos: solo
    dígitos, largo 2/4/6/8. Descarta lo inválido y deduplica preservando orden."""
    out: list[str] = []
    vistos: set[str] = set()
    for v in valores:
        for parte in v.split(","):
            p = parte.strip()
            if p.isdigit() and len(p) in (2, 4, 6, 8) and p not in vistos:
                vistos.add(p)
                out.append(p)
    return out


def _parse_organismos(valor: str) -> list[str]:
    """Convierte el campo de texto de organismos seguidos (separados por coma) a lista."""
    return [o.strip() for o in valor.split(",") if o.strip()]


def _password_valida(password: str) -> bool:
    return len(password) >= _PASSWORD_MIN_LEN


def _agrupar_familias_por_segmento(
    segmentos_list: list[tuple[str, str]], familias_list: list[tuple[str, str]]
) -> list[tuple[str, str, list[tuple[str, str]]]]:
    """Agrupa familias UNSPSC bajo su segmento (familia.codigo[:2]) para el <select>."""
    por_segmento: dict[str, list[tuple[str, str]]] = {codigo: [] for codigo, _ in segmentos_list}
    for fam_codigo, fam_nombre in familias_list:
        por_segmento.setdefault(fam_codigo[:2], []).append((fam_codigo, fam_nombre))
    return [
        (seg_codigo, seg_nombre, por_segmento.get(seg_codigo, []))
        for seg_codigo, seg_nombre in segmentos_list
    ]


@router.get("/perfiles", response_class=HTMLResponse)
async def perfiles_get(
    request: Request,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
    mensaje: str = "",
    error: str = "",
) -> HTMLResponse:
    settings = request.app.state.settings
    try:
        sync_instituciones_pac(session, settings)
        sync_sectores_organismos(session, settings)
    except httpx.HTTPError:
        # Catálogo de organismos (F-plan/F-datos) sin red disponible: degrada al
        # input manual en vez de romper la página (regla 6). Si ya había caché
        # de una corrida anterior, listar_organismos_catalogo igual la sirve.
        _log.warning("perfiles_get: no se pudo sincronizar el catálogo de organismos", exc_info=True)

    organismos_catalogo = listar_organismos_catalogo(session)
    organismos_json = [
        {"id": o.codigo_entidad, "nombre": o.razon_social, "sector": o.sector or "Sin clasificación"}
        for o in organismos_catalogo
    ]

    perfiles = listar_perfiles(session, user.id)
    rubros_por_perfil = {
        p.id: [nombre_rubro(c) or c for c in (p.categorias_unspsc or [])] for p in perfiles
    }
    return _TEMPLATES.TemplateResponse(
        request,
        "perfiles.html",
        _ctx(
            request,
            user,
            perfiles=perfiles,
            regiones_disponibles=REGIONES,
            rubros_agrupados=_agrupar_familias_por_segmento(segmentos(), familias()),
            rubros_por_perfil=rubros_por_perfil,
            organismos_catalogo_disponible=bool(organismos_json),
            organismos_json=organismos_json,
            mensaje=mensaje,
            error=error,
        ),
    )


@router.post("/perfiles/rut-proveedor")
async def perfil_rut_proveedor(
    request: Request,
    rut_proveedor: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Guarda el RUT de proveedor del usuario (opcional) para resaltar sus
    propias ofertas en el análisis de competencia (F-competencia)."""
    check_csrf(request, csrf_token)
    user.rut_proveedor = rut_proveedor.strip() or None
    session.commit()
    return RedirectResponse(url="/perfiles?mensaje=RUT+de+proveedor+actualizado", status_code=303)


@router.post("/cuenta/resumen")
async def cuenta_resumen_configurar(
    request: Request,
    dias_resumen: str = Form("3"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    try:
        dias = int(dias_resumen)
    except ValueError:
        dias = -1
    if dias not in _DIAS_RESUMEN_VALIDOS:
        return RedirectResponse(
            url=f"/perfiles?error={quote('Cadencia de resumen inválida')}",
            status_code=303,
        )
    user.dias_resumen = dias
    session.commit()
    return RedirectResponse(url="/perfiles?mensaje=Preferencia+de+resumen+actualizada", status_code=303)


@router.post("/cuenta/password")
async def cuenta_password_cambiar(
    request: Request,
    password_actual: str = Form(""),
    password_nueva: str = Form(""),
    password_confirmacion: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    if not verify_password(password_actual, user.password_hash):
        return RedirectResponse(
            url=f"/perfiles?error={quote('Contraseña actual incorrecta')}",
            status_code=303,
        )
    if password_nueva != password_confirmacion:
        return RedirectResponse(
            url=f"/perfiles?error={quote('La nueva contraseña y su confirmación no coinciden')}",
            status_code=303,
        )
    if not _password_valida(password_nueva):
        return RedirectResponse(
            url=f"/perfiles?error={quote('La nueva contraseña debe tener al menos 8 caracteres')}",
            status_code=303,
        )
    user.password_hash = hash_password(password_nueva)
    session.commit()
    return RedirectResponse(url="/perfiles?mensaje=Contraseña+actualizada", status_code=303)


@router.post("/perfiles/nuevo")
async def perfil_crear(
    request: Request,
    background_tasks: BackgroundTasks,
    nombre: str = Form(...),
    keywords: str = Form(""),
    excluir: str = Form(""),
    fuentes: list[str] = Form(default=[]),
    regiones: list[str] = Form(default=[]),
    monto_min_clp: str = Form(""),
    monto_max_clp: str = Form(""),
    categorias_unspsc: list[str] = Form(default=[]),
    organismos_seguidos: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    ex_list = [k.strip() for k in excluir.split(",") if k.strip()]
    fuentes_list = fuentes or ["licitaciones", "compras_agiles"]
    regiones_list = _parse_regiones(regiones)
    monto_min = _parse_monto(monto_min_clp)
    monto_max = _parse_monto(monto_max_clp)
    categorias_list = _parse_categorias(categorias_unspsc)
    organismos_list = _parse_organismos(organismos_seguidos)
    if monto_min is not None and monto_max is not None and monto_min > monto_max:
        msg = quote("El monto mínimo no puede ser mayor al monto máximo")
        return RedirectResponse(url=f"/perfiles?error={msg}", status_code=303)
    try:
        perfil = crear_perfil(
            session,
            owner_id=user.id,
            nombre=nombre,
            keywords=kw_list,
            keywords_excluir=ex_list,
            regiones=regiones_list,
            monto_min_clp=monto_min,
            monto_max_clp=monto_max,
            categorias_unspsc=categorias_list,
            organismos_seguidos=organismos_list,
            fuentes=fuentes_list,
        )
    except PerfilInvalido as exc:
        return RedirectResponse(url=f"/perfiles?error={quote(str(exc))}", status_code=303)
    session.commit()
    background_tasks.add_task(_match_perfil_background, request.app.state.engine, perfil.id)
    return RedirectResponse(url="/perfiles?mensaje=Perfil+creado", status_code=303)


@router.post("/perfiles/{perfil_id}/eliminar")
async def perfil_eliminar(
    request: Request,
    perfil_id: int,
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
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
    background_tasks: BackgroundTasks,
    nombre: str = Form(...),
    keywords: str = Form(""),
    excluir: str = Form(""),
    fuentes: list[str] = Form(default=[]),
    regiones: list[str] = Form(default=[]),
    monto_min_clp: str = Form(""),
    monto_max_clp: str = Form(""),
    categorias_unspsc: list[str] = Form(default=[]),
    organismos_seguidos: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    perfil = obtener_perfil(session, perfil_id, user.id)
    if perfil is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    ex_list = [k.strip() for k in excluir.split(",") if k.strip()]
    fuentes_list = fuentes or ["licitaciones", "compras_agiles"]
    regiones_list = _parse_regiones(regiones)
    monto_min = _parse_monto(monto_min_clp)
    monto_max = _parse_monto(monto_max_clp)
    categorias_list = _parse_categorias(categorias_unspsc)
    organismos_list = _parse_organismos(organismos_seguidos)
    if monto_min is not None and monto_max is not None and monto_min > monto_max:
        msg = quote("El monto mínimo no puede ser mayor al monto máximo")
        return RedirectResponse(url=f"/perfiles?error={msg}", status_code=303)
    try:
        actualizar_perfil(
            session,
            perfil_id=perfil_id,
            owner_id=user.id,
            nombre=nombre,
            keywords=kw_list,
            keywords_excluir=ex_list,
            regiones=regiones_list,
            monto_min_clp=monto_min,
            monto_max_clp=monto_max,
            categorias_unspsc=categorias_list,
            organismos_seguidos=organismos_list,
            fuentes=fuentes_list,
        )
    except PerfilInvalido as exc:
        return RedirectResponse(url=f"/perfiles?error={quote(str(exc))}", status_code=303)
    session.commit()
    background_tasks.add_task(_match_perfil_background, request.app.state.engine, perfil_id)
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
    check_csrf(request, csrf_token)
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


@router.post("/admin/usuarios/{uid}/password", response_model=None)
async def admin_usuario_password_reset(
    request: Request,
    uid: int,
    password_nueva: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    check_csrf(request, csrf_token)
    target = session.get(Usuario, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    if not _password_valida(password_nueva):
        return RedirectResponse(
            url=f"/admin/usuarios?error={quote('La nueva contraseña debe tener al menos 8 caracteres')}",
            status_code=303,
        )

    target.password_hash = hash_password(password_nueva)
    session.commit()
    usuarios = list(session.execute(select(Usuario).order_by(Usuario.id)).scalars())
    return _TEMPLATES.TemplateResponse(
        request,
        "admin_usuarios.html",
        _ctx(
            request,
            user,
            usuarios=usuarios,
            mensaje="Contraseña reseteada",
            error="",
            password_reseteada={"email": target.email, "password": password_nueva},
        ),
    )


@router.post("/admin/usuarios/{uid}/desactivar")
async def admin_usuario_desactivar(
    request: Request,
    uid: int,
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_admin),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
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


# ---------------------------------------------------------------------------
# Plan Anual de Compra (F-plan) — consulta pública, sin scoping de ownership
# ---------------------------------------------------------------------------


@router.get("/plan-anual", response_class=HTMLResponse)
async def plan_anual_get(
    request: Request,
    institucion: str = "",
    codigo_entidad: str = "",
    agno: str = "",
    pagina: int = 1,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    settings = request.app.state.settings
    sync_instituciones_pac(session, settings)
    sync_sectores_organismos(session, settings)

    anio_actual = datetime.now(_TZ_CHILE).year
    anios_disponibles = list(range(settings.plan_compra_anio_inicio, anio_actual + 1))

    agno_int = int(agno) if agno.strip().isdigit() else anio_actual
    codigo_entidad_int = int(codigo_entidad) if codigo_entidad.strip().isdigit() else None

    sugerencias = buscar_instituciones_pac(session, institucion) if institucion.strip() else []

    institucion_seleccionada: InstitucionPAC | None = None
    lineas_totales: list[PlanCompraLinea] = []
    sin_plan = False
    if codigo_entidad_int is not None:
        institucion_seleccionada = session.get(InstitucionPAC, codigo_entidad_int)
        resultado = get_plan(session, settings, codigo_entidad_int, agno_int)
        sin_plan = resultado.estado == "sin_plan"
        lineas_totales = resultado.lineas

    total_estimado = sum(linea.monto_estimado_clp or 0.0 for linea in lineas_totales)
    total_filas = len(lineas_totales)
    total_paginas = max(1, (total_filas + _PAC_PAGE_SIZE - 1) // _PAC_PAGE_SIZE)
    pagina = max(1, min(pagina, total_paginas))
    offset = (pagina - 1) * _PAC_PAGE_SIZE
    lineas_pagina = lineas_totales[offset : offset + _PAC_PAGE_SIZE]

    return _TEMPLATES.TemplateResponse(
        request,
        "plan_anual.html",
        _ctx(
            request,
            user,
            institucion_texto=institucion,
            sugerencias=sugerencias,
            codigo_entidad=codigo_entidad_int,
            institucion_seleccionada=institucion_seleccionada,
            agno=agno_int,
            anios_disponibles=anios_disponibles,
            sin_plan=sin_plan,
            lineas=lineas_pagina,
            total_filas=total_filas,
            total_estimado=total_estimado,
            pagina=pagina,
            total_paginas=total_paginas,
        ),
    )
