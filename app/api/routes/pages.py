"""Rutas HTML: dashboard, detalle oportunidad, perfiles, admin, salud, plan anual."""

from __future__ import annotations

import calendar
import csv
import io
import re
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import escape
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.api.deps import (
    check_csrf,
    get_db,
    html_require_admin,
    html_require_user,
)
from app.api.presentacion import (
    banda_relevancia,
    banda_urgencia,
    formato_clp,
    nombre_region,
    presentacion_estado,
    razones_tipificadas,
    registrar_filtros,
    texto_cierre,
)
from app.api.query import (
    AGRUPAR_POR_VALIDOS,
    LIMITE_PAGINA_DEFAULT,
    agrupar_oportunidades,
    buscar_instituciones_pac,
    check_oportunidad_access,
    detalle_competencia,
    estado_acciones,
    get_item_oportunidad,
    get_oportunidades_usuario,
    listar_descartadas_detalle,
    listar_organismos_catalogo,
    listar_seguidas_detalle,
    matches_de_oportunidad,
    puede_actuar,
    resumen_competencia,
)
from app.api.salud_data import get_salud_data
from app.auth.csrf import generate_csrf_token
from app.auth.password import hash_password, verify_password
from app.catalogos.unspsc import familias, nombre_rubro, segmentos
from app.changelog import entradas_changelog, fecha_ultima_novedad
from app.core.logging import get_logger
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.explorador_ca import (
    CIERRES as CIERRES_EXPLORADOR,
)
from app.explorador_ca import (
    ORDENES as ORDENES_EXPLORADOR,
)
from app.explorador_ca import (
    PAGE_SIZE as PAGE_SIZE_EXPLORADOR,
)
from app.explorador_ca import (
    FiltrosExplorador,
    agregar_favorito,
    filas_vocabulario,
    listar_favoritos,
    palabras_validas,
    prefijos_validos,
    quitar_favorito,
    sugerencias_de_vocabulario,
    vocabulario_por_familia,
    vocabulario_vacio,
)
from app.explorador_ca import buscar as buscar_explorador
from app.explorador_ca import contar as contar_explorador
from app.ingest.plan_compra import (
    anio_completo_cargado,
    get_plan,
    sync_instituciones_pac,
    sync_sectores_organismos,
)
from app.matching.engine import (
    contar_limpieza,
    criterio_perfil,
    match_perfil,
)
from app.matching.feedback import descartar as marcar_descarte
from app.matching.feedback import deshacer_descarte, listar_descartadas
from app.matching.perfiles import (
    PerfilInvalido,
    actualizar_perfil,
    crear_perfil,
    eliminar_perfil,
    excluir_palabras,
    listar_perfiles,
    normalizar_palabras_excluir,
    obtener_perfil,
    palabras_sugeridas,
    quitar_exclusiones,
    verificar_exclusiones,
)
from app.matching.seguimiento import (
    alternar_guardada,
    archivar_seguimiento,
    dejar_de_seguir,
    guardar,
)
from app.matching.text import build_exclude_tsquery, build_tsquery, keywords_validas
from app.models.enums import SECTOR_SIN_CLASIFICACION, EstadoOportunidad, RolUsuario
from app.models.seeds import REGIONES
from app.models.tables import (
    CaProducto,
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    LicitacionItem,
    PerfilBusqueda,
    PlanCompraLinea,
    SyncState,
    Usuario,
)
from app.plan_busqueda import (
    FiltrosPlanBusqueda,
    buscar_lineas,
    buscar_por_organismo,
    contar_plan,
    lineas_para_exportar,
)

router = APIRouter()
_log = get_logger(__name__)
_PAC_PAGE_SIZE = 100
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
registrar_filtros(_TEMPLATES.env)


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
    ultima_novedad = fecha_ultima_novedad()
    return {
        "current_user": user,
        "csrf_token": generate_csrf_token(settings.secret_key, request.state.csrf_nonce),
        "changelog_entries": entradas_changelog(),
        "ultima_novedad_fecha": ultima_novedad,
        **extra,
    }


def _es_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _decorar_item(item: dict[str, Any], settings: Any) -> dict[str, Any]:
    """Agrega a un item del feed lo que la tarjeta necesita para pintarse.

    Todo sale de funciones puras de `presentacion`: la plantilla no decide
    umbrales ni mapea estados. La banda de match usa los MISMOS cortes que los
    presets del feed y que el badge de la ficha — por eso la tarjeta ya no
    tiene sus propios `>= 80` / `>= 50`.
    """
    item["banda"] = banda_relevancia(
        item["match"].score, _RELEVANCIA_ALTA, settings.feed_min_score_default
    )
    item["urgencia"] = banda_urgencia(item["dias_al_cierre"])
    item["estado_badge"] = presentacion_estado(item["estado"])
    item["cierre_texto"] = texto_cierre(
        item["fecha_cierre"], item["dias_al_cierre"], item["match"].fuente
    )
    item["razones_chips"] = razones_tipificadas(item["match"].razones)
    return item


_ORDENES_VALIDOS = {"score", "cierre", "monto"}

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


def _hay_novedades_pendientes(user: Usuario) -> bool:
    ultima = fecha_ultima_novedad()
    if ultima is None:
        return False
    return user.novedades_visto_hasta is None or user.novedades_visto_hasta < ultima


# ---------------------------------------------------------------------------
# Filtros del feed (F-feed-ui-2): parseo defensivo y armado del querystring
# ---------------------------------------------------------------------------

_FUENTES_VALIDAS = ("licitaciones", "compras_agiles")
_ETIQUETA_FUENTE = {"licitaciones": "Licitaciones", "compras_agiles": "Compra Ágil"}

# Orden fijo de los parámetros en la URL: dos estados iguales producen la MISMA
# URL, que es lo que la hace compartible y comparable.
#
# F-vigencia: "estado" ya NO filtra (la sección "Estado" del panel perdió
# sentido — con solo_vigentes=True lo único que queda siempre es "Abierta"),
# así que sale de acá; un enlace viejo con `?estado=...` lo sigue aceptando
# FastAPI (parámetro declarado en `index`) pero ya no se re-serializa.
_ORDEN_PARAMS = (
    "perfil_id",
    "texto",
    "fuente",
    "region",
    "monto_min",
    "monto_max",
    "excluir_sin_monto",
    "cierre_desde",
    "cierre_hasta",
    "excluir_sin_cierre",
    "kw",
    "min_score",
    "orden",
    "agrupar_por",
    "grupo_expandido",
    "offset",
)


def _url_feed(actual: dict[str, Any], **cambios: Any) -> str:
    """ÚNICO lugar donde se arma el querystring del feed.

    Con siete dimensiones filtrables, concatenar a mano en cada control
    garantiza que tarde o temprano uno quede sin codificar — es el bug que ya
    rompió F-ui-fixes con un "&" escrito en el buscador. Acá el escapado lo
    hace `urlencode` sobre la estructura completa, no un `| urlencode` por
    valor repartido por la plantilla.

    Un valor `None`, `""` o `[]` en `cambios` BORRA ese parámetro. Los que no
    están activos no se serializan, para que la URL no crezca con ruido.
    """
    nuevo = dict(actual)
    # Cualquier cambio vuelve a la primera página: quedarse en el offset
    # anterior con menos resultados deja la lista vacía sin explicación. Las
    # propias flechas de paginación pasan `offset` explícito en `cambios`.
    nuevo.pop("offset", None)
    for clave, valor in cambios.items():
        if valor is None or valor == "" or valor == []:
            nuevo.pop(clave, None)
        else:
            nuevo[clave] = valor
    # `quote_via=quote` para que el espacio salga %20 y no "+": los dos son
    # válidos en un querystring, pero %20 es el que sobrevive si la URL se
    # copia a otro contexto.
    qs = urlencode(
        [(k, nuevo[k]) for k in _ORDEN_PARAMS if k in nuevo], doseq=True, quote_via=quote
    )
    return f"/?{qs}" if qs else "/"


def _url_alternar(actual: dict[str, Any], clave: str, valor: str) -> str:
    """Agrega o saca un valor de un parámetro multivaluado (fuente, estado)."""
    actuales = list(actual.get(clave) or [])
    if valor in actuales:
        actuales.remove(valor)
    else:
        actuales.append(valor)
    return _url_feed(actual, **{clave: actuales})


def _entero(valor: str) -> int | None:
    v = (valor or "").strip()
    return int(v) if v.isdigit() else None


def _enteros(valores: list[str]) -> list[int]:
    """Como `_entero`, para un `Query(default=[])` multivaluado."""
    out: list[int] = []
    for v in valores:
        n = _entero(v)
        if n is not None:
            out.append(n)
    return out


def _monto(valor: str) -> float | None:
    """"$5.000.000", "5.000.000" o "5000000" -> 5000000.0; basura -> None.

    El campo se muestra con punto de miles, así que lo que vuelve trae puntos:
    quedarse solo con los dígitos es más robusto que exigir un formato.
    """
    solo_digitos = re.sub(r"\D", "", valor or "")
    return float(solo_digitos) if solo_digitos else None


def _fecha_borde(valor: str, *, fin_de_dia: bool) -> datetime | None:
    """"AAAA-MM-DD" -> datetime naive; basura -> None (regla 6).

    Naive a propósito: `_pasa_cierre` lo interpreta como hora de Chile, igual
    que el resto del proyecto (ver `app/core/tiempo.py`). El borde de "hasta"
    es el FIN del día: quien filtra "hasta el 30" espera ver lo que cierra el
    30, no perderlo por la hora.
    """
    try:
        dia = date.fromisoformat((valor or "").strip())
    except ValueError:
        return None
    return datetime.combine(dia, time(23, 59, 59) if fin_de_dia else time(0, 0, 0))


def _chips_filtro(  # noqa: PLR0913 - un filtro activo = un chip
    estado_qs: dict[str, Any],
    *,
    texto: str,
    perfil_ids: list[int],
    nombres_perfil: dict[int, str],
    fuentes: list[str],
    region: int | None,
    monto_min: float | None,
    monto_max: float | None,
    incluir_sin_monto: bool,
    cierre_desde: datetime | None,
    cierre_hasta: datetime | None,
    incluir_sin_cierre: bool,
    preset_activo: dict[str, Any] | None,
    keywords_sel: list[str],
    min_score: int,
    relevancia_media: int,
) -> list[dict[str, str]]:
    """Un chip por valor aplicado, con la dimensión como prefijo y el enlace
    que lo quita. Se arma acá y no en la plantilla: es la lista que dice qué
    está filtrando de verdad, y tiene que salir del MISMO estado con el que se
    consultó, no de una lectura paralela del querystring."""
    chips: list[dict[str, str]] = []

    def agregar(etiqueta: str, **quitar: Any) -> None:
        chips.append({"etiqueta": etiqueta, "href": _url_feed(estado_qs, **quitar)})

    for pid in perfil_ids:
        restantes = [p for p in perfil_ids if p != pid]
        chips.append(
            {
                "etiqueta": f"Perfil: {nombres_perfil.get(pid, str(pid))}",
                "href": _url_feed(estado_qs, perfil_id=restantes),
            }
        )
    if texto:
        agregar(f"Texto: {texto}", texto=None)
    if len(fuentes) == 1:
        agregar(f"Fuente: {_ETIQUETA_FUENTE[fuentes[0]]}", fuente=None)
    if region is not None:
        agregar(f"Región: {nombre_region(region) or region}", region=None)
    if monto_min is not None or monto_max is not None:
        if monto_min is not None and monto_max is not None:
            glosa = f"{formato_clp(monto_min)} – {formato_clp(monto_max)}"
        elif monto_min is not None:
            glosa = f"desde {formato_clp(monto_min)}"
        else:
            glosa = f"hasta {formato_clp(monto_max)}"
        agregar(f"Monto: {glosa}", monto_min=None, monto_max=None)
    if not incluir_sin_monto:
        agregar("Monto: solo con monto informado", excluir_sin_monto=None)
    if cierre_desde is not None or cierre_hasta is not None:
        if preset_activo is not None:
            glosa = str(preset_activo["etiqueta"]).lower()
        elif cierre_desde is not None and cierre_hasta is not None:
            glosa = f"{cierre_desde.strftime('%d/%m/%Y')} – {cierre_hasta.strftime('%d/%m/%Y')}"
        elif cierre_desde is not None:
            glosa = f"desde {cierre_desde.strftime('%d/%m/%Y')}"
        else:
            glosa = f"hasta {cierre_hasta.strftime('%d/%m/%Y')}"  # type: ignore[union-attr]
        agregar(f"Cierra: {glosa}", cierre_desde=None, cierre_hasta=None)
    if not incluir_sin_cierre:
        agregar("Cierre: solo con fecha informada", excluir_sin_cierre=None)
    for kw in keywords_sel:
        restantes_kw = [k for k in keywords_sel if k != kw]
        chips.append(
            {
                "etiqueta": f"Palabra clave: {kw}",
                "href": _url_feed(estado_qs, kw=restantes_kw),
            }
        )
    if min_score != relevancia_media:
        glosa = "todas" if min_score == 0 else f"sobre {min_score}"
        agregar(f"Relevancia: {glosa}", min_score=None)

    return chips


def _presets_cierre(hoy: date) -> list[dict[str, Any]]:
    """Atajos del filtro de cierre. Son rangos de fechas como cualquier otro:
    no hay un "modo preset" aparte que mantener sincronizado con el rango."""
    fin_de_mes = hoy.replace(day=calendar.monthrange(hoy.year, hoy.month)[1])
    return [
        {"clave": "hoy", "etiqueta": "Cierra hoy", "desde": hoy, "hasta": hoy},
        {"clave": "3d", "etiqueta": "3 días", "desde": hoy, "hasta": hoy + timedelta(days=3)},
        {"clave": "7d", "etiqueta": "7 días", "desde": hoy, "hasta": hoy + timedelta(days=7)},
        {"clave": "mes", "etiqueta": "Este mes", "desde": hoy, "hasta": fin_de_mes},
    ]


# ---------------------------------------------------------------------------
# Dashboard principal
# ---------------------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
async def index(  # noqa: PLR0913 - una dimensión filtrable = un parámetro
    request: Request,
    fuente: list[str] = Query(default=[]),
    texto: str = "",
    perfil_id: list[str] = Query(default=[]),
    region: str = "",
    monto_min: str = "",
    monto_max: str = "",
    excluir_sin_monto: str = "",
    cierre_desde: str = "",
    cierre_hasta: str = "",
    excluir_sin_cierre: str = "",
    # F-vigencia: "estado" ya no filtra (ver `_ORDEN_PARAMS`); se sigue
    # declarando para que un enlace compartido antiguo con `?estado=...` no
    # rompa con un 422, aunque el valor se ignore.
    estado: list[str] = Query(default=[]),  # noqa: ARG001
    kw: list[str] = Query(default=[]),
    orden: str = "score",
    min_score: int | None = None,
    agrupar_por: str = "ninguno",
    grupo_expandido: str = "",
    offset: int = 0,
    panel: str = "",
    incluir_sin_monto: str = "",
    incluir_sin_cierre: str = "",
    # Aviso tras "Descartar y excluir" (F-guardar): no se re-serializan en los enlaces.
    excluido_perfil: str = "",
    excluido: list[str] = Query(default=[]),
    excluidos_n: str = "",
    restaurado: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    settings = request.app.state.settings
    orden = orden if orden in _ORDENES_VALIDOS else "score"
    agrupar_por = agrupar_por if agrupar_por in AGRUPAR_POR_VALIDOS else "ninguno"
    min_score_efectivo = (
        min_score if min_score is not None and min_score >= 0 else settings.feed_min_score_default
    )

    # Perfiles y palabras clave del usuario, antes de parsear sus filtros: la
    # faceta de palabra clave (F-vigencia 3-bis) solo ofrece las de los
    # perfiles SELECCIONADOS (si hay filtro de perfil), pero una `kw` inválida
    # se descarta contra TODOS sus perfiles (regla 17 — nunca contra un
    # subconjunto que dependa de otro filtro ya aplicado).
    perfiles = listar_perfiles(session, user.id)
    perfil_ids_propios = {p.id for p in perfiles}
    nombres_perfil = {p.id: p.nombre for p in perfiles}
    keywords_propias = {palabra for p in perfiles for palabra in (p.keywords or [])}
    aviso_exclusion = _aviso_exclusion(perfiles, excluido_perfil, excluido, excluidos_n)

    perfil_ids_sel: list[int] = []
    for pid in _enteros(perfil_id):
        if pid in perfil_ids_propios and pid not in perfil_ids_sel:
            perfil_ids_sel.append(pid)

    kw_sel: list[str] = []
    for k in kw:
        if k in keywords_propias and k not in kw_sel:
            kw_sel.append(k)

    # Las dos casillas "incluir…" van MARCADAS por defecto. Una URL que no
    # dice nada tiene que incluir: al revés, cualquier enlace pelado escondería
    # las oportunidades a las que la fuente no informa el dato, que son muchas.
    #
    # De ahí las dos representaciones. Un envío del panel se reconoce por
    # `panel=1` y ahí sí vale la regla del HTML (casilla ausente = desmarcada).
    # En los enlaces que arma `_url_feed` viaja solo lo EXCEPCIONAL
    # (`excluir_sin_*=1`), así que la URL compartible queda corta y su default
    # es el seguro. Esa es la forma canónica: `estado_qs` normaliza a ella.
    desde_el_panel = panel == "1"
    incluir_monto_no_informado = (
        incluir_sin_monto == "1" if desde_el_panel else excluir_sin_monto != "1"
    )
    incluir_sin_fecha_cierre = (
        incluir_sin_cierre == "1" if desde_el_panel else excluir_sin_cierre != "1"
    )

    fuentes_sel = [f for f in fuente if f in _FUENTES_VALIDAS]
    # Las dos marcadas (o ninguna) es "sin filtro": el modelo de datos tiene una
    # sola fuente por match y no existe el conjunto vacío.
    fuente_filtro = fuentes_sel[0] if len(fuentes_sel) == 1 else None
    region_int = _entero(region)
    monto_min_val = _monto(monto_min)
    monto_max_val = _monto(monto_max)
    hoy_chile = datetime.now(TZ_CHILE).date()
    cierre_desde_dt = _fecha_borde(cierre_desde, fin_de_dia=False)
    if cierre_desde_dt is not None and cierre_desde_dt.date() < hoy_chile:
        # El atajo "Fecha de cierre" no ofrece rangos en el pasado (F-vigencia):
        # con el feed ya filtrado a lo vigente, un "desde" atrasado no escondía
        # nada que no filtrara ya la vigencia, pero sí confundía al mostrarse
        # tal cual se escribió. Se acota a hoy y la URL/el campo reflejan eso.
        cierre_desde_dt = datetime.combine(hoy_chile, time(0, 0, 0))
        cierre_desde = hoy_chile.isoformat()
    cierre_hasta_dt = _fecha_borde(cierre_hasta, fin_de_dia=True)

    # Estado de la URL: solo lo activo. De acá salen TODOS los enlaces.
    estado_qs: dict[str, Any] = {}
    if perfil_ids_sel:
        estado_qs["perfil_id"] = perfil_ids_sel
    if texto:
        estado_qs["texto"] = texto
    if fuentes_sel:
        estado_qs["fuente"] = fuentes_sel
    if region_int is not None:
        estado_qs["region"] = region_int
    if monto_min_val is not None:
        estado_qs["monto_min"] = int(monto_min_val)
    if monto_max_val is not None:
        estado_qs["monto_max"] = int(monto_max_val)
    if not incluir_monto_no_informado:
        estado_qs["excluir_sin_monto"] = "1"
    if cierre_desde_dt is not None:
        estado_qs["cierre_desde"] = cierre_desde.strip()
    if cierre_hasta_dt is not None:
        estado_qs["cierre_hasta"] = cierre_hasta.strip()
    if not incluir_sin_fecha_cierre:
        estado_qs["excluir_sin_cierre"] = "1"
    if kw_sel:
        estado_qs["kw"] = kw_sel
    if min_score_efectivo != settings.feed_min_score_default:
        estado_qs["min_score"] = min_score_efectivo
    if orden != "score":
        estado_qs["orden"] = orden
    if agrupar_por != "ninguno":
        estado_qs["agrupar_por"] = agrupar_por

    # Sin agrupación manda la paginación; agrupando manda el cap por grupo
    # (F-feed-filtros, Bloque 7).
    sin_agrupar = agrupar_por == "ninguno"
    limite = LIMITE_PAGINA_DEFAULT if sin_agrupar else _LIMITE_AGRUPADO
    offset_efectivo = max(0, offset) if sin_agrupar else 0

    resultado = get_oportunidades_usuario(
        session,
        user.id,
        fuente=fuente_filtro,
        region=region_int,
        texto=texto or None,
        perfil_ids=perfil_ids_sel or None,
        orden=orden,
        min_score=min_score_efectivo,
        monto_min=monto_min_val,
        monto_max=monto_max_val,
        incluir_monto_no_informado=incluir_monto_no_informado,
        cierre_desde=cierre_desde_dt,
        cierre_hasta=cierre_hasta_dt,
        incluir_sin_fecha_cierre=incluir_sin_fecha_cierre,
        keywords=kw_sel or None,
        limit=limite,
        offset=offset_efectivo,
    )
    items = resultado.items
    for item in items:
        _decorar_item(item, settings)
    grupos, total_unico, total_apariciones = agrupar_oportunidades(
        items, agrupar_por, grupo_expandido=grupo_expandido or None
    )
    n_descartadas = len(listar_descartadas(session, user.id))

    # Opciones de la faceta "Palabra clave" (F-vigencia 3-bis): las de los
    # perfiles SELECCIONADOS si hay filtro de perfil, si no las de todos los
    # perfiles del usuario.
    perfiles_para_kw = [p for p in perfiles if not perfil_ids_sel or p.id in perfil_ids_sel]
    keywords_opciones = sorted({k for p in perfiles_para_kw for k in (p.keywords or [])})

    presets_cierre = _presets_cierre(hoy_chile)
    for preset in presets_cierre:
        preset["activo"] = (
            cierre_desde.strip() == preset["desde"].isoformat()
            and cierre_hasta.strip() == preset["hasta"].isoformat()
        )
        preset["href"] = _url_feed(
            estado_qs,
            cierre_desde=preset["desde"].isoformat(),
            cierre_hasta=preset["hasta"].isoformat(),
        )
    preset_activo = next((p for p in presets_cierre if p["activo"]), None)

    facetas = resultado.facetas
    chips = _chips_filtro(
        estado_qs,
        texto=texto,
        perfil_ids=perfil_ids_sel,
        nombres_perfil=nombres_perfil,
        fuentes=fuentes_sel,
        region=region_int,
        monto_min=monto_min_val,
        monto_max=monto_max_val,
        incluir_sin_monto=incluir_monto_no_informado,
        cierre_desde=cierre_desde_dt,
        cierre_hasta=cierre_hasta_dt,
        incluir_sin_cierre=incluir_sin_fecha_cierre,
        preset_activo=preset_activo,
        keywords_sel=kw_sel,
        min_score=min_score_efectivo,
        relevancia_media=settings.feed_min_score_default,
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "index.html",
        _ctx(
            request,
            user,
            grupos=grupos,
            total=resultado.total,
            total_unico=total_unico,
            total_apariciones=total_apariciones,
            nuevas_hoy=resultado.nuevas_hoy,
            items_en_pagina=len(items),
            offset=offset_efectivo,
            limite_pagina=LIMITE_PAGINA_DEFAULT,
            sin_agrupar=sin_agrupar,
            # --- estado de los filtros, ya parseado ---
            fuentes_sel=fuentes_sel,
            texto=texto,
            perfil_ids_sel=perfil_ids_sel,
            region=region_int,
            monto_min=int(monto_min_val) if monto_min_val is not None else None,
            monto_max=int(monto_max_val) if monto_max_val is not None else None,
            incluir_sin_monto=incluir_monto_no_informado,
            cierre_desde=cierre_desde.strip(),
            cierre_hasta=cierre_hasta.strip(),
            incluir_sin_cierre=incluir_sin_fecha_cierre,
            kw_sel=kw_sel,
            orden=orden,
            min_score=min_score_efectivo,
            agrupar_por=agrupar_por,
            # --- opciones y conteos del panel ---
            opciones_fuente=[
                {
                    "valor": f,
                    "etiqueta": _ETIQUETA_FUENTE[f],
                    "n": facetas.get("fuente", {}).get(f),
                    "marcada": (not fuentes_sel) or (f in fuentes_sel),
                }
                for f in _FUENTES_VALIDAS
            ],
            opciones_perfil=[
                {
                    "valor": p.id,
                    "etiqueta": p.nombre,
                    "n": facetas.get("perfil", {}).get(str(p.id)),
                    "marcada": p.id in perfil_ids_sel,
                }
                for p in perfiles
            ],
            opciones_keyword=[
                {
                    "valor": k,
                    "etiqueta": k,
                    "n": facetas.get("keyword", {}).get(k),
                    "marcada": k in kw_sel,
                }
                for k in keywords_opciones
            ],
            opciones_region=[
                {
                    "codigo": codigo,
                    "nombre": nombre,
                    "n": facetas.get("region", {}).get(str(codigo)),
                }
                for codigo, nombre in REGIONES
            ],
            presets_cierre=presets_cierre,
            chips=chips,
            url_limpiar=_url_feed({}, orden=orden if orden != "score" else None,
                                  agrupar_por=agrupar_por if agrupar_por != "ninguno" else None),
            # --- enlaces, todos desde el mismo helper ---
            url_feed=lambda **cambios: _url_feed(estado_qs, **cambios),
            url_alternar=lambda clave, valor: _url_alternar(estado_qs, clave, valor),
            n_ocultas_relevancia=resultado.total_sin_filtro_relevancia - resultado.total,
            relevancia_alta=_RELEVANCIA_ALTA,
            relevancia_media=settings.feed_min_score_default,
            n_descartadas=n_descartadas,
            perfiles=perfiles,
            mostrar_tutorial=not user.tutorial_visto,
            mostrar_novedades=_hay_novedades_pendientes(user),
            aviso_exclusion=aviso_exclusion,
            exclusion_restaurada=restaurado == "1",
        ),
    )


def _aviso_exclusion(
    perfiles: list[PerfilBusqueda], perfil_id: str, palabras: list[str], n: str
) -> dict[str, Any] | None:
    """Datos del aviso "excluiste X del perfil Y" tras Descartar y excluir. Solo
    con un perfil propio (regla 17) y palabras que sigan en sus exclusiones: un
    enlace manipulado no muestra nada."""
    pid = _entero(perfil_id)
    perfil = next((p for p in perfiles if p.id == pid), None)
    if perfil is None:
        return None
    actuales = {str(w).lower() for w in (perfil.keywords_excluir or [])}
    vigentes = [w for w in palabras if w.lower() in actuales]
    if not vigentes:
        return None
    return {
        "perfil_id": perfil.id,
        "perfil": perfil.nombre,
        "palabras": vigentes,
        "n": _entero(n) or 0,
    }


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


def _url_volver_feed(request: Request) -> str:
    """URL del feed desde la que se abrió la ficha, con sus filtros intactos.

    La ficha no conoce el estado del feed: lo toma del Referer, pero solo si
    apunta a la raíz de ESTE mismo host (evita open-redirect y no inventa
    filtros). Cualquier otro caso vuelve al feed limpio, como antes.
    """
    ref = request.headers.get("referer", "")
    if not ref:
        return "/"
    partes = urlsplit(ref)
    if partes.netloc and partes.netloc != request.url.netloc:
        return "/"
    # El explorador (F-ca-explorar) también manda a la ficha: volver conserva sus filtros.
    if partes.path not in ("/", "/compras-agiles"):
        return "/"
    return partes.path + ("?" + partes.query if partes.query else "")


@router.get("/oportunidad/{fuente}/{codigo}", response_class=HTMLResponse)
async def oportunidad_detalle(
    request: Request,
    fuente: str,
    codigo: str,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    # Sin match se abren las Compras Ágiles (el explorador lista todas las
    # vigentes) y lo que el usuario ya guardó o descartó (F-guardar): ver
    # `puede_actuar`. Las acciones van siempre con su propio user_id (regla 17).
    if not puede_actuar(session, user.id, fuente, codigo):
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    match = check_oportunidad_access(session, user.id, fuente, codigo)

    op: Licitacion | CompraAgil | None = None
    if fuente == "licitaciones":
        op = session.get(Licitacion, codigo)
    elif fuente == "compras_agiles":
        op = session.get(CompraAgil, codigo)
    if op is None:
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")

    from app.api.presentacion import nombre_region, razones_legibles
    from app.api.query import _url_ficha, mostrar_ficha_oficial

    settings = request.app.state.settings

    url_ficha = _url_ficha(fuente, codigo)

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

    # Mismo cálculo que `_construir_item`, con o sin match.
    dias_al_cierre = (
        max(0.0, (op.fecha_cierre - ahora_utc()).total_seconds() / 86400)
        if op.fecha_cierre is not None
        else None
    )

    return _TEMPLATES.TemplateResponse(
        request,
        "oportunidad.html",
        _ctx(
            request,
            user,
            match=match,
            # Misma definición de "alta"/"media" que los presets del feed:
            # el badge de la ficha ya no puede contradecir al filtro (2.2).
            banda=(
                banda_relevancia(match.score, _RELEVANCIA_ALTA, settings.feed_min_score_default)
                if match is not None
                else None
            ),
            # El cierre lo deciden las MISMAS funciones puras que en la tarjeta
            # (F-coherencia): la ficha tenía una copia divergente que mostraba
            # la medianoche derivada de un ddmmaaaa como si fuera hora real.
            urgencia=banda_urgencia(dias_al_cierre),
            cierre_texto=texto_cierre(op.fecha_cierre, dias_al_cierre, fuente),
            oportunidad=op,
            fuente=fuente,
            url_ficha=url_ficha,
            mostrar_ficha=mostrar_ficha_oficial(op.estado),
            items=items,
            organismo=organismo,
            region_nombre=region_nombre,
            razones=razones_legibles(match.razones) if match is not None else [],
            acciones=estado_acciones(session, user.id, fuente, codigo),
            competencia_resumen=competencia_resumen,
            competencia_detalle=competencia_detalle,
            url_volver=_url_volver_feed(request),
            volver_etiqueta=(
                "Explorar Compras Ágiles"
                if _url_volver_feed(request).startswith("/compras-agiles")
                else "Dashboard"
            ),
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


_ORIGENES = ("dashboard", "ficha", "explorador")


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

    - `origen="dashboard"` (default): la tarjeta completa del feed (necesita match;
      vacío si el usuario lo perdió entretanto, regla 17);
    - `origen="ficha"`: solo la fila de botones de la ficha (`_ficha_acciones.html`);
    - `origen="explorador"`: los botones de la fila del explorador de CA, que no
      tiene match (F-guardar).
    """
    settings = request.app.state.settings
    csrf_token = generate_csrf_token(settings.secret_key, request.state.csrf_nonce)
    if origen in ("ficha", "explorador"):
        acciones = estado_acciones(session, user.id, fuente, codigo)
        if acciones is None:
            return HTMLResponse(content="", status_code=200)
        template = (
            "_ficha_acciones_partial.html" if origen == "ficha" else "_explorador_acciones_partial.html"
        )
        return _TEMPLATES.TemplateResponse(
            request, template, {"acciones": acciones, "csrf_token": csrf_token}
        )
    item = get_item_oportunidad(session, user.id, fuente, codigo)
    if item is None:
        return HTMLResponse(content="", status_code=200)
    # La tarjeta re-renderizada tiene que traer lo mismo que la del feed: si no,
    # tras un swap quedaría sin banda, sin badge de estado y sin urgencia.
    _decorar_item(item, settings)
    return _TEMPLATES.TemplateResponse(
        request, "_card_partial.html", {"item": item, "csrf_token": csrf_token}
    )


def _guardar(
    request: Request,
    user: Usuario,
    session: Session,
    fuente: str,
    codigo: str,
    *,
    accion: str,
    next_: str,
    origen: str,
    fallback: str,
) -> HTMLResponse | RedirectResponse:
    """Guardar (F-guardar): queda en Mi registro y avisa de sus cambios.

    `accion`: "alternar" (toggle del botón), "guardar" (idempotente) o "quitar"
    (`dejar_de_seguir`; 404 si no estaba, como antes)."""
    if accion == "quitar":
        if not dejar_de_seguir(session, owner_id=user.id, fuente=fuente, codigo=codigo):
            raise HTTPException(status_code=404, detail="Seguimiento no encontrado")
    else:
        if not puede_actuar(session, user.id, fuente, codigo):
            raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
        if accion == "guardar":
            guardar(session, user.id, fuente, codigo)
        else:
            alternar_guardada(session, user.id, fuente, codigo)
    session.commit()
    if _es_htmx(request):
        return _render_card_partial(
            request, user, session, fuente, codigo, origen=origen if origen in _ORIGENES else "dashboard"
        )
    return RedirectResponse(url=_safe_next(next_, fallback), status_code=303)


@router.post("/oportunidad/{fuente}/{codigo}/guardar", response_model=None)
async def oportunidad_guardar(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    origen: str = Form("dashboard"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    """Toggle Guardar / Guardada (F-guardar)."""
    check_csrf(request, csrf_token)
    return _guardar(
        request, user, session, fuente, codigo,
        accion="alternar", next_=next, origen=origen, fallback=f"/oportunidad/{fuente}/{codigo}",
    )


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
    """Deprecated (F-guardar): alias del toggle de `/guardar`."""
    check_csrf(request, csrf_token)
    return _guardar(
        request, user, session, fuente, codigo,
        accion="alternar", next_=next, origen=origen, fallback="/",
    )


@router.post("/oportunidad/{fuente}/{codigo}/seguir", response_model=None)
async def oportunidad_seguir(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    origen: str = Form("dashboard"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    """Deprecated (F-guardar): alias de `/guardar` que solo guarda (idempotente)."""
    check_csrf(request, csrf_token)
    return _guardar(
        request, user, session, fuente, codigo,
        accion="guardar", next_=next, origen=origen, fallback=f"/oportunidad/{fuente}/{codigo}",
    )


@router.post("/oportunidad/{fuente}/{codigo}/dejar-de-seguir", response_model=None)
async def oportunidad_dejar_de_seguir(
    request: Request,
    fuente: str,
    codigo: str,
    next: str = Form(""),
    origen: str = Form("dashboard"),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse | RedirectResponse:
    """Deprecated (F-guardar): alias de `/guardar` que solo quita de guardadas."""
    check_csrf(request, csrf_token)
    return _guardar(
        request, user, session, fuente, codigo,
        accion="quitar", next_=next, origen=origen, fallback="/seguidas",
    )


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
    """Descartar: registra feedback NEGATIVO y, si estaba guardada, la quita de
    guardadas (exclusión mutua, F-guardar). En el dashboard oculta el match del
    feed (reversible vía /descartadas); en el explorador la fila desaparece; en
    la ficha re-renderiza la fila de botones."""
    check_csrf(request, csrf_token)
    if not puede_actuar(session, user.id, fuente, codigo):
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    marcar_descarte(session, user.id, fuente, codigo)
    session.commit()
    if _es_htmx(request):
        if origen == "ficha":
            return _render_card_partial(request, user, session, fuente, codigo, origen="ficha")
        if origen == "explorador":
            # La fila se reemplaza por un marcador oculto que solo lleva el anuncio.
            return _TEMPLATES.TemplateResponse(
                request,
                "_explorador_descartada.html",
                {"acciones": estado_acciones(session, user.id, fuente, codigo)},
            )
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
    if not puede_actuar(session, user.id, fuente, codigo):
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    if not deshacer_descarte(session, user.id, fuente, codigo):
        raise HTTPException(status_code=404, detail="Descarte no encontrado")
    session.commit()
    if _es_htmx(request):
        if origen == "ficha":
            return _render_card_partial(request, user, session, fuente, codigo, origen="ficha")
        return HTMLResponse(content="", status_code=200)
    return RedirectResponse(url=_safe_next(next, "/descartadas"), status_code=303)


# ---------------------------------------------------------------------------
# Descartar y excluir palabra (F-guardar, sección E)
# ---------------------------------------------------------------------------


@router.get("/oportunidad/{fuente}/{codigo}/descartar-opciones", response_class=HTMLResponse)
async def oportunidad_descartar_opciones(
    request: Request,
    fuente: str,
    codigo: str,
    origen: str = "dashboard",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Contenido del modal "Descartar": qué perfil y qué palabras la trajeron,
    palabras sugeridas para excluir y en qué perfil. Solo con match propio."""
    matches = matches_de_oportunidad(session, user.id, fuente, codigo)
    acciones = estado_acciones(session, user.id, fuente, codigo)
    if not matches or acciones is None:
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    perfiles = {p.id: p for p in listar_perfiles(session, user.id)}
    motivos = [
        {
            "perfil_id": m.perfil_id,
            "perfil": perfiles[m.perfil_id].nombre,
            "keywords": list((m.razones or {}).get("keywords_hit") or []),
        }
        for m in matches
        if m.perfil_id in perfiles
    ]
    keywords_perfiles = [
        str(k) for m in matches if m.perfil_id in perfiles for k in (perfiles[m.perfil_id].keywords or [])
    ]
    settings = request.app.state.settings
    return _TEMPLATES.TemplateResponse(
        request,
        "_modal_descartar.html",
        {
            "acciones": acciones,
            "motivos": motivos,
            "sugeridas": palabras_sugeridas(acciones["nombre"] or "", keywords_perfiles, session),
            "origen": origen if origen in ("dashboard", "ficha") else "dashboard",
            "csrf_token": generate_csrf_token(settings.secret_key, request.state.csrf_nonce),
        },
    )


@router.get("/oportunidad/{fuente}/{codigo}/excluir-vista-previa", response_class=HTMLResponse)
async def oportunidad_excluir_vista_previa(
    fuente: str,  # noqa: ARG001 - la ruta cuelga de la oportunidad del modal
    codigo: str,  # noqa: ARG001
    perfil_id: str = "",
    palabras: list[str] = Query(default=[]),
    palabra_nueva: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Vista previa (HTMX), solo números: cuántos matches del perfil saldrían con
    la(s) palabra(s) agregada(s), con la MISMA condición que
    `limpiar_matches_perfil` (`contar_limpieza`). No escribe nada."""
    pid = _entero(perfil_id)
    perfil = obtener_perfil(session, pid, user.id) if pid is not None else None
    if perfil is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    try:
        lista = normalizar_palabras_excluir([*palabras, palabra_nueva])
    except PerfilInvalido as exc:
        return HTMLResponse(content=str(escape(str(exc))))
    if not lista:
        return HTMLResponse(content="Elige o escribe al menos una palabra.")
    try:
        verificar_exclusiones(session, [str(k) for k in (perfil.keywords or [])], lista)
    except PerfilInvalido as exc:
        return HTMLResponse(content=str(escape(str(exc))))
    n = contar_limpieza(session, criterio_perfil(perfil, lista))
    texto = (
        f"Con esto salen {n} oportunidad{'es' if n != 1 else ''} vigentes del perfil "
        f"«{perfil.nombre}» (contando esta, si contiene la palabra)."
    )
    return HTMLResponse(content=str(escape(texto)))


@router.post("/oportunidad/{fuente}/{codigo}/descartar-y-excluir", response_model=None)
async def oportunidad_descartar_y_excluir(
    request: Request,
    fuente: str,
    codigo: str,
    perfil_id: str = Form(""),
    palabras: list[str] = Form(default=[]),
    palabra_nueva: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Descarta y agrega la(s) palabra(s) a las exclusiones del perfil; lo que ya
    no calza sale del feed. Vuelve al feed con un aviso y "Deshacer"."""
    check_csrf(request, csrf_token)
    if not puede_actuar(session, user.id, fuente, codigo):
        raise HTTPException(status_code=404, detail="Oportunidad no encontrada")
    pid = _entero(perfil_id)
    perfil = obtener_perfil(session, pid, user.id) if pid is not None else None
    if perfil is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    try:
        lista = normalizar_palabras_excluir([*palabras, palabra_nueva])
    except PerfilInvalido as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if not lista:
        raise HTTPException(status_code=400, detail="Elige o escribe al menos una palabra")
    try:
        # Antes de descartar: si la exclusión choca, no se hace nada.
        verificar_exclusiones(session, [str(k) for k in (perfil.keywords or [])], lista)
    except PerfilInvalido as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    marcar_descarte(session, user.id, fuente, codigo)
    resultado = excluir_palabras(session, user.id, perfil.id, lista)
    session.commit()
    agregadas, borrados = resultado if resultado is not None else ([], 0)
    qs = urlencode(
        [
            ("excluido_perfil", perfil.id),
            *[("excluido", w) for w in agregadas],
            ("excluidos_n", borrados),
        ],
        quote_via=quote,
    )
    return RedirectResponse(url=f"/?{qs}", status_code=303)


@router.post("/perfiles/{perfil_id}/deshacer-exclusion")
async def perfil_deshacer_exclusion(
    request: Request,
    perfil_id: int,
    background_tasks: BackgroundTasks,
    palabras: list[str] = Form(default=[]),
    next: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    """Deshacer de "Descartar y excluir": quita las palabras y re-ejecuta el
    matching del perfil en segundo plano (como al editarlo), que recrea los
    matches (con `fecha_match` nueva)."""
    check_csrf(request, csrf_token)
    perfil = quitar_exclusiones(session, user.id, perfil_id, palabras)
    if perfil is None:
        raise HTTPException(status_code=404, detail="Perfil no encontrado")
    session.commit()
    background_tasks.add_task(_match_perfil_background, request.app.state.engine, perfil_id)
    destino = _safe_next(next, "/")
    if destino == "/":
        destino = "/?restaurado=1"
    return RedirectResponse(url=destino, status_code=303)


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
            rubros_favoritos=[
                {"prefijo": p, "nombre": nombre_rubro(p) or p}
                for p in listar_favoritos(session, user.id)
            ],
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


@router.post("/cuenta/tutorial-visto")
async def cuenta_tutorial_visto(
    request: Request,
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    user.tutorial_visto = True
    session.commit()
    return RedirectResponse(url="/", status_code=303)


@router.post("/cuenta/novedades-visto")
async def cuenta_novedades_visto(
    request: Request,
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    user.novedades_visto_hasta = fecha_ultima_novedad()
    session.commit()
    return RedirectResponse(url="/", status_code=303)


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
    # El match en segundo plano agrega lo nuevo y, al final, borra lo vigente que
    # ya no calza con el perfil editado (`limpiar_matches_perfil`, F-guardar).
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
# Plan Anual de Compra (F-plan / F-plan-busqueda) — consulta pública, sin
# scoping de ownership. Dos pestañas en la misma ruta: "palabra" (búsqueda
# inversa por descripción, nueva) y "organismo" (la de F-plan, sin cambios).
# ---------------------------------------------------------------------------

_PALABRA_PAGE_SIZE = 50


def _mes_actual_chile() -> int:
    return datetime.now(TZ_CHILE).month


def _parse_organismos_ids(valores: list[str]) -> list[int]:
    return [int(v.strip()) for v in valores if v.strip().isdigit()]


def _keywords_union_perfiles(perfiles: list[PerfilBusqueda]) -> tuple[list[str], list[str]]:
    """Une keywords/keywords_excluir de varios perfiles, sin duplicados y en
    orden estable — usado por la vista "Para mis perfiles" (§3-bis)."""
    incluir: list[str] = []
    excluir: list[str] = []
    vistos_i: set[str] = set()
    vistos_e: set[str] = set()
    for p in perfiles:
        for k in p.keywords or []:
            if k and k not in vistos_i:
                vistos_i.add(k)
                incluir.append(k)
        for k in p.keywords_excluir or []:
            if k and k not in vistos_e:
                vistos_e.add(k)
                excluir.append(k)
    return incluir, excluir


def _filtros_desde_query(
    session: Session,
    user: Usuario,
    *,
    agno: int,
    q: str,
    organismos: list[str],
    sector: str,
    todo_el_anio: bool,
    monto_min: str,
    monto_max: str,
) -> FiltrosPlanBusqueda:
    """Arma los filtros de búsqueda por palabra. Sin texto escrito, cae a la
    vista "Para mis perfiles" (unión de keywords/exclusiones de los perfiles
    ACTIVOS de este usuario — nunca los de otro, regla 17)."""
    q_strip = q.strip()
    q_include: str | None = q_strip or None
    q_exclude: str | None = None
    if not q_strip:
        perfiles_activos = [p for p in listar_perfiles(session, user.id) if p.activo]
        incluir, excluir = _keywords_union_perfiles(perfiles_activos)
        q_include = build_tsquery(incluir) if keywords_validas(incluir) else None
        q_exclude = build_exclude_tsquery(excluir) if keywords_validas(excluir) else None

    return FiltrosPlanBusqueda(
        agno=agno,
        organismos=_parse_organismos_ids(organismos) or None,
        sector=sector.strip() or None,
        desde_mes=None if todo_el_anio else _mes_actual_chile(),
        monto_min=_parse_monto(monto_min),
        monto_max=_parse_monto(monto_max),
        q_include=q_include,
        q_exclude=q_exclude,
    )


@router.get("/plan-anual", response_class=HTMLResponse)
async def plan_anual_get(
    request: Request,
    tab: str = "palabra",
    q: str = "",
    organismos: list[str] = Query(default=[]),
    sector: str = "",
    todo_el_anio: bool = False,
    monto_min: str = "",
    monto_max: str = "",
    vista: str = "organismo",
    orden: str = "relevancia",
    pagina_lineas: int = 1,
    institucion: str = "",
    codigo_entidad: str = "",
    agno: str = "",
    pagina: int = 1,
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    settings = request.app.state.settings
    try:
        sync_instituciones_pac(session, settings)
        sync_sectores_organismos(session, settings)
    except httpx.HTTPError:
        _log.warning("plan_anual_get: no se pudo sincronizar el catálogo de organismos", exc_info=True)

    anio_actual = datetime.now(TZ_CHILE).year
    anios_disponibles = list(range(settings.plan_compra_anio_inicio, anio_actual + 1))
    agno_int = int(agno) if agno.strip().isdigit() else anio_actual

    organismos_catalogo = listar_organismos_catalogo(session)
    organismos_json = [
        {"id": o.codigo_entidad, "nombre": o.razon_social, "sector": o.sector or SECTOR_SIN_CLASIFICACION}
        for o in organismos_catalogo
    ]
    sectores_disponibles = sorted({o.sector or SECTOR_SIN_CLASIFICACION for o in organismos_catalogo})

    if tab == "organismo":
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
                tab="organismo",
                agno=agno_int,
                anios_disponibles=anios_disponibles,
                organismos_json=organismos_json,
                sectores_disponibles=sectores_disponibles,
                institucion_texto=institucion,
                sugerencias=sugerencias,
                codigo_entidad=codigo_entidad_int,
                institucion_seleccionada=institucion_seleccionada,
                sin_plan=sin_plan,
                lineas=lineas_pagina,
                total_filas=total_filas,
                total_estimado=total_estimado,
                pagina=pagina,
                total_paginas=total_paginas,
            ),
        )

    # --- tab == "palabra" (F-plan-busqueda) ---------------------------------
    cargando_completo = not anio_completo_cargado(session, agno_int)
    sync_anual = session.get(SyncState, f"plan_compra_anual_{agno_int}")
    actualizado_al = sync_anual.cursor if sync_anual else None

    perfiles_activos = [p for p in listar_perfiles(session, user.id) if p.activo]
    q_strip = q.strip()
    modo_mis_perfiles = not q_strip
    incluir_mis_perfiles, excluir_mis_perfiles = (
        _keywords_union_perfiles(perfiles_activos) if modo_mis_perfiles else ([], [])
    )
    sin_busqueda = (
        modo_mis_perfiles
        and not incluir_mis_perfiles
        and not excluir_mis_perfiles
        and not organismos
        and not sector.strip()
    )

    resultados_organismo: list[Any] = []
    lineas_lista: list[PlanCompraLinea] = []
    total_lineas_vista = 0
    conteo = {"n_lineas": 0, "monto_total": 0.0, "n_organismos": 0}
    if not cargando_completo and not sin_busqueda:
        filtros = _filtros_desde_query(
            session,
            user,
            agno=agno_int,
            q=q,
            organismos=organismos,
            sector=sector,
            todo_el_anio=todo_el_anio,
            monto_min=monto_min,
            monto_max=monto_max,
        )
        conteo = contar_plan(session, filtros)
        if vista == "lineas":
            lineas_lista, total_lineas_vista = buscar_lineas(
                session, filtros, orden=orden, pagina=pagina_lineas, page_size=_PALABRA_PAGE_SIZE
            )
        else:
            resultados_organismo = buscar_por_organismo(session, filtros)

    chips_perfiles = [
        {"id": p.id, "nombre": p.nombre, "keywords": list(p.keywords or [])} for p in perfiles_activos
    ]
    total_paginas_lineas = max(1, (total_lineas_vista + _PALABRA_PAGE_SIZE - 1) // _PALABRA_PAGE_SIZE)

    return _TEMPLATES.TemplateResponse(
        request,
        "plan_anual.html",
        _ctx(
            request,
            user,
            tab="palabra",
            agno=agno_int,
            anios_disponibles=anios_disponibles,
            organismos_json=organismos_json,
            sectores_disponibles=sectores_disponibles,
            organismos_sel=_parse_organismos_ids(organismos),
            sector_sel=sector,
            q=q,
            todo_el_anio=todo_el_anio,
            monto_min=monto_min,
            monto_max=monto_max,
            vista=vista,
            orden=orden,
            pagina_lineas=pagina_lineas,
            total_paginas_lineas=total_paginas_lineas,
            modo_mis_perfiles=modo_mis_perfiles,
            sin_busqueda=sin_busqueda,
            sin_perfiles=not perfiles_activos,
            cargando_completo=cargando_completo,
            actualizado_al=actualizado_al,
            chips_perfiles=chips_perfiles,
            resultados_organismo=resultados_organismo,
            lineas=lineas_lista,
            total_lineas=total_lineas_vista,
            conteo=conteo,
        ),
    )


@router.get("/plan-anual/conteo", response_class=HTMLResponse)
async def plan_anual_conteo(
    request: Request,
    agno: str = "",
    q: str = "",
    organismos: list[str] = Query(default=[]),
    sector: str = "",
    todo_el_anio: bool = False,
    monto_min: str = "",
    monto_max: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Conteo en vivo (HTMX): solo números, respeta los mismos filtros que la
    búsqueda principal — nunca expone filas."""
    anio_actual = datetime.now(TZ_CHILE).year
    agno_int = int(agno) if agno.strip().isdigit() else anio_actual
    filtros = _filtros_desde_query(
        session,
        user,
        agno=agno_int,
        q=q,
        organismos=organismos,
        sector=sector,
        todo_el_anio=todo_el_anio,
        monto_min=monto_min,
        monto_max=monto_max,
    )
    conteo = contar_plan(session, filtros)
    return _TEMPLATES.TemplateResponse(request, "_plan_anual_conteo.html", {"conteo": conteo})


@router.get("/plan-anual/export.csv")
async def plan_anual_export_csv(
    agno: str = "",
    q: str = "",
    organismos: list[str] = Query(default=[]),
    sector: str = "",
    todo_el_anio: bool = False,
    monto_min: str = "",
    monto_max: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> Response:
    """CSV de lo filtrado (tope `_EXPORT_MAX_FILAS` en app.plan_busqueda), con
    la leyenda de fuente como primera línea."""
    anio_actual = datetime.now(TZ_CHILE).year
    agno_int = int(agno) if agno.strip().isdigit() else anio_actual
    filtros = _filtros_desde_query(
        session,
        user,
        agno=agno_int,
        q=q,
        organismos=organismos,
        sector=sector,
        todo_el_anio=todo_el_anio,
        monto_min=monto_min,
        monto_max=monto_max,
    )
    lineas = lineas_para_exportar(session, filtros)

    buf = io.StringIO()
    buf.write("Fuente: Dirección ChileCompra\n")
    writer = csv.writer(buf)
    writer.writerow(
        [
            "institucion",
            "codigo_entidad",
            "descripcion_producto",
            "cantidad_estimada",
            "monto_unitario_clp",
            "monto_estimado_clp",
            "mes_estimado",
            "trimestre_estimado",
            "estado_planificacion",
        ]
    )
    for linea in lineas:
        writer.writerow(
            [
                linea.institucion_nombre,
                linea.codigo_entidad,
                linea.descripcion_producto,
                linea.cantidad_estimada,
                linea.monto_unitario_clp,
                linea.monto_estimado_clp,
                linea.mes_estimado,
                linea.trimestre_estimado,
                linea.estado_planificacion,
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="plan-anual-{agno_int}.csv"'},
    )


# ---------------------------------------------------------------------------
# Explorador de Compras Ágiles (F-ca-explorar) — todo desde la base, sin API.
# Universo, filtros y paginación viven en app.explorador_ca; acá solo se lee la
# query, se arma el estado normalizado (para URLs y chips) y se pinta.
# ---------------------------------------------------------------------------

_EXPLORADOR_ORDEN_PARAMS = (
    "categorias_unspsc",
    "incluir_posibles",
    "palabras_rubro",
    "region",
    "monto_min",
    "monto_max",
    "excluir_sin_monto",
    "cierre",
    "texto",
    "organismo",
    "orden",
    "pagina",
)
_ETIQUETA_CIERRE_EXPLORADOR = {"hoy": "hoy", "3d": "en 3 días", "7d": "en una semana"}
_REGIONES_VALIDAS = {codigo for codigo, _ in REGIONES}


def _url_explorador(actual: dict[str, Any], **cambios: Any) -> str:
    """Único lugar donde se arma el querystring del explorador (mismo criterio
    que `_url_feed`: `urlencode` sobre la estructura completa, un valor vacío
    borra el parámetro y cualquier cambio vuelve a la página 1).

    `sin_favoritos=1` viaja SIEMPRE: sin él, un enlace que deja la lista de
    rubros vacía (quitar el último chip) haría reaparecer los favoritos."""
    nuevo = dict(actual)
    nuevo.pop("pagina", None)
    for clave, valor in cambios.items():
        if valor is None or valor == "" or valor == []:
            nuevo.pop(clave, None)
        else:
            nuevo[clave] = valor
    pares = [(k, nuevo[k]) for k in _EXPLORADOR_ORDEN_PARAMS if k in nuevo]
    pares.append(("sin_favoritos", "1"))
    return "/compras-agiles?" + urlencode(pares, doseq=True, quote_via=quote)


def _armar_filtros_explorador(  # noqa: PLR0913 - una dimensión filtrable = un parámetro
    session: Session,
    user: Usuario,
    *,
    categorias_unspsc: list[str],
    incluir_posibles: str,
    palabras_rubro: list[str],
    region: list[str],
    monto_min: str,
    monto_max: str,
    excluir_sin_monto: str,
    incluir_sin_monto: str,
    cierre: str,
    texto: str,
    organismo: str,
    orden: str,
    panel: str,
    sin_favoritos: str,
) -> tuple[FiltrosExplorador, dict[str, Any], list[str]]:
    """Filtros normalizados + su forma canónica en querystring + favoritos del usuario.

    Rubros: si la URL trae `categorias_unspsc` mandan; si no, y la URL no dice
    `sin_favoritos=1`, entran los favoritos del usuario (al llegar a la pantalla
    vienen preseleccionados)."""
    favoritos = listar_favoritos(session, user.id)
    prefijos = prefijos_validos(categorias_unspsc)
    if not prefijos and sin_favoritos != "1":
        prefijos = list(favoritos)

    # Igual que el feed: un envío del panel (`panel=1`) trae la casilla ausente =
    # desmarcada; en los enlaces canónicos viaja solo lo excepcional.
    incluir_monto = incluir_sin_monto == "1" if panel == "1" else excluir_sin_monto != "1"
    regiones = [r for r in _enteros(region) if r in _REGIONES_VALIDAS]
    m_min, m_max = _monto(monto_min), _monto(monto_max)
    orden = orden if orden in ORDENES_EXPLORADOR else "cierre"
    cierre = cierre if cierre in CIERRES_EXPLORADOR else ""

    filtros = FiltrosExplorador(
        usuario_id=user.id,
        prefijos=prefijos,
        incluir_posibles=incluir_posibles == "1" and bool(prefijos),
        palabras_rubro=palabras_validas(palabras_rubro),
        regiones=regiones,
        monto_min=m_min,
        monto_max=m_max,
        incluir_sin_monto=incluir_monto,
        cierre=cierre or None,
        texto=texto.strip(),
        organismo=organismo.strip(),
        orden=orden,
    )
    estado_qs: dict[str, Any] = {
        "categorias_unspsc": prefijos,
        "incluir_posibles": "1" if filtros.incluir_posibles else "",
        "palabras_rubro": filtros.palabras_rubro,
        "region": [str(r) for r in regiones],
        "monto_min": str(int(m_min)) if m_min is not None else "",
        "monto_max": str(int(m_max)) if m_max is not None else "",
        "excluir_sin_monto": "" if incluir_monto else "1",
        "cierre": cierre,
        "texto": filtros.texto,
        "organismo": filtros.organismo,
        "orden": "" if orden == "cierre" else orden,
    }
    estado_qs = {k: v for k, v in estado_qs.items() if v not in ("", [])}
    return filtros, estado_qs, favoritos


def _chips_explorador(estado_qs: dict[str, Any], filtros: FiltrosExplorador) -> list[dict[str, str]]:
    """Un chip por valor aplicado, con el enlace que lo quita (mismo criterio que
    `_chips_filtro` del feed: salen del estado con el que se consultó)."""
    chips: list[dict[str, str]] = []

    def agregar(etiqueta: str, **quitar: Any) -> None:
        chips.append({"etiqueta": etiqueta, "href": _url_explorador(estado_qs, **quitar)})

    for p in filtros.prefijos:
        restantes = [x for x in filtros.prefijos if x != p]
        agregar(f"Rubro: {nombre_rubro(p) or p}", categorias_unspsc=restantes)
    if filtros.incluir_posibles:
        agregar("Incluye posibles", incluir_posibles=None)
    for palabra in filtros.palabras_rubro:
        restantes_p = [x for x in filtros.palabras_rubro if x != palabra]
        agregar(f"Palabra: {palabra}", palabras_rubro=restantes_p)
    for r in filtros.regiones:
        restantes_r = [str(x) for x in filtros.regiones if x != r]
        agregar(f"Región: {nombre_region(r) or r}", region=restantes_r)
    if filtros.monto_min is not None or filtros.monto_max is not None:
        if filtros.monto_min is not None and filtros.monto_max is not None:
            glosa = f"{formato_clp(filtros.monto_min)} – {formato_clp(filtros.monto_max)}"
        elif filtros.monto_min is not None:
            glosa = f"desde {formato_clp(filtros.monto_min)}"
        else:
            glosa = f"hasta {formato_clp(filtros.monto_max)}"
        agregar(f"Monto: {glosa}", monto_min=None, monto_max=None)
    if not filtros.incluir_sin_monto:
        agregar("Monto: solo con monto informado", excluir_sin_monto=None)
    if filtros.cierre:
        agregar(f"Cierra {_ETIQUETA_CIERRE_EXPLORADOR[filtros.cierre]}", cierre=None)
    if filtros.texto:
        agregar(f"Texto: {filtros.texto}", texto=None)
    if filtros.organismo:
        agregar(f"Organismo: {filtros.organismo}", organismo=None)
    return chips


@router.get("/compras-agiles", response_class=HTMLResponse)
async def explorador_ca_get(  # noqa: PLR0913 - una dimensión filtrable = un parámetro
    request: Request,
    categorias_unspsc: list[str] = Query(default=[]),
    incluir_posibles: str = "",
    palabras_rubro: list[str] = Query(default=[]),
    region: list[str] = Query(default=[]),
    monto_min: str = "",
    monto_max: str = "",
    excluir_sin_monto: str = "",
    incluir_sin_monto: str = "",
    cierre: str = "",
    texto: str = "",
    organismo: str = "",
    orden: str = "cierre",
    pagina: int = 1,
    panel: str = "",
    sin_favoritos: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    filtros, estado_qs, favoritos = _armar_filtros_explorador(
        session,
        user,
        categorias_unspsc=categorias_unspsc,
        incluir_posibles=incluir_posibles,
        palabras_rubro=palabras_rubro,
        region=region,
        monto_min=monto_min,
        monto_max=monto_max,
        excluir_sin_monto=excluir_sin_monto,
        incluir_sin_monto=incluir_sin_monto,
        cierre=cierre,
        texto=texto,
        organismo=organismo,
        orden=orden,
        panel=panel,
        sin_favoritos=sin_favoritos,
    )
    # Una sola lectura del vocabulario: sirve a las sugerencias, al aviso y a la búsqueda.
    filas_vocab = filas_vocabulario(session, filtros.prefijos)
    resultado = buscar_explorador(
        session, filtros, pagina=pagina, vocab=vocabulario_por_familia(filas_vocab)
    )
    chips = _chips_explorador(estado_qs, filtros)
    familias_catalogo = {codigo for codigo, _ in familias()}
    aviso_vocabulario = None
    if filtros.prefijos and not filas_vocab:
        aviso_vocabulario = (
            "Todavía no hay vocabulario calculado (se actualiza cada lunes), así que "
            "aún no hay palabras sugeridas ni posibles."
            if vocabulario_vacio(session)
            else "Estos rubros no tienen palabras típicas aprendidas (códigos nuevos o "
            "sin nombre en español)."
        )

    return _TEMPLATES.TemplateResponse(
        request,
        "compras_agiles.html",
        _ctx(
            request,
            user,
            filtros=filtros,
            resultado=resultado,
            chips=chips,
            regiones_disponibles=REGIONES,
            rubros_agrupados=_agrupar_familias_por_segmento(segmentos(), familias()),
            rubros_elegidos=[
                {"prefijo": p, "nombre": nombre_rubro(p) or p, "favorito": p in favoritos}
                for p in filtros.prefijos
            ],
            aviso_vocabulario=aviso_vocabulario,
            sugerencias=sugerencias_de_vocabulario(filas_vocab),
            # Las familias (4 dígitos) del catálogo van marcadas en el acordeón; lo demás
            # (segmentos, prefijos finos) viaja en el campo de texto, para que aplicar
            # el formulario no lo pierda.
            rubros_preseleccion=[p for p in filtros.prefijos if p in familias_catalogo],
            rubros_extra=",".join(p for p in filtros.prefijos if p not in familias_catalogo),
            monto_min=filtros.monto_min,
            monto_max=filtros.monto_max,
            url_ex=lambda **cambios: _url_explorador(estado_qs, **cambios),
            url_limpiar="/compras-agiles?sin_favoritos=1",
            pagina_actual=resultado.pagina,
            page_size=PAGE_SIZE_EXPLORADOR,
            next_actual=_url_explorador(estado_qs, pagina=resultado.pagina if resultado.pagina > 1 else None),
        ),
    )


@router.get("/compras-agiles/conteo", response_class=HTMLResponse)
async def explorador_ca_conteo(  # noqa: PLR0913 - una dimensión filtrable = un parámetro
    request: Request,
    categorias_unspsc: list[str] = Query(default=[]),
    incluir_posibles: str = "",
    palabras_rubro: list[str] = Query(default=[]),
    region: list[str] = Query(default=[]),
    monto_min: str = "",
    monto_max: str = "",
    excluir_sin_monto: str = "",
    incluir_sin_monto: str = "",
    cierre: str = "",
    texto: str = "",
    organismo: str = "",
    orden: str = "cierre",
    panel: str = "",
    sin_favoritos: str = "",
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> HTMLResponse:
    """Conteo en vivo (HTMX): solo un número, con los mismos filtros de la
    pantalla — nunca expone filas."""
    filtros, _, _ = _armar_filtros_explorador(
        session,
        user,
        categorias_unspsc=categorias_unspsc,
        incluir_posibles=incluir_posibles,
        palabras_rubro=palabras_rubro,
        region=region,
        monto_min=monto_min,
        monto_max=monto_max,
        excluir_sin_monto=excluir_sin_monto,
        incluir_sin_monto=incluir_sin_monto,
        cierre=cierre,
        texto=texto,
        organismo=organismo,
        orden=orden,
        panel=panel,
        sin_favoritos=sin_favoritos,
    )
    return _TEMPLATES.TemplateResponse(
        request, "_explorador_conteo.html", {"total": contar_explorador(session, filtros)}
    )


@router.post("/rubros-favoritos/agregar")
async def rubro_favorito_agregar(
    request: Request,
    prefijo: str = Form(""),
    next: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    try:
        agregar_favorito(session, user.id, prefijo)
    except ValueError:
        raise HTTPException(status_code=400, detail="Rubro inválido") from None
    session.commit()
    return RedirectResponse(url=_safe_next(next, "/perfiles"), status_code=303)


@router.post("/rubros-favoritos/quitar")
async def rubro_favorito_quitar(
    request: Request,
    prefijo: str = Form(""),
    next: str = Form(""),
    csrf_token: str = Form(""),
    user: Usuario = Depends(html_require_user),
    session: Session = Depends(get_db),
) -> RedirectResponse:
    check_csrf(request, csrf_token)
    # Solo los del propio usuario (regla 17): el filtro por owner_id va en la query.
    quitar_favorito(session, user.id, prefijo)
    session.commit()
    return RedirectResponse(url=_safe_next(next, "/perfiles"), status_code=303)
