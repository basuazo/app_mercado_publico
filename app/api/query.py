"""Queries compartidas entre rutas HTML y API REST."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.presentacion import nombre_region, razones_legibles
from app.catalogos.unspsc import nombre_rubro
from app.core.tiempo import TZ_CHILE, a_utc_naive, ahora_utc, borde_del_dia_utc_naive
from app.matching.feedback import listar_descartadas, listar_feedback_usuario, obtener_feedback
from app.matching.perfiles import listar_perfiles
from app.matching.seguimiento import listar_seguidas, obtener_seguimiento
from app.models.enums import (
    SECTOR_SIN_CLASIFICACION,
    EstadoOportunidad,
    FamiliaEstado,
    ValorFeedback,
    familia_de_estado,
)
from app.models.tables import (
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    OfertaCompetencia,
    OportunidadMatch,
)


def _url_ficha(fuente: str, codigo: str) -> str:
    """URL de la ficha oficial en Mercado Público.

    Para licitaciones es la ficha estándar (DetailsAcquisition): accesible para
    proveedores con sesión iniciada en procesos abiertos. El parámetro
    `qs` de esa página espera un token interno ENCRIPTADO (no el
    `CodigoExterno`); en cambio `idlicitacion=<CodigoExterno>` hace que el
    propio Mercado Público resuelva y redirija al `qs` correcto — verificado
    en `docs/10-enlace-ficha.md` (reproduce byte a byte el token real). Para
    Compra Ágil se apunta al buscador público vigente. Ver
    `mostrar_ficha_oficial` para cuándo conviene exponer este enlace en la UI.
    """
    if fuente == "licitaciones":
        return (
            "https://www.mercadopublico.cl/Procurement/Modules/RFB/"
            f"DetailsAcquisition.aspx?idlicitacion={quote(codigo, safe='')}"
        )
    return "https://buscador.mercadopublico.cl/compra-agil"


def mostrar_ficha_oficial(estado: str | None) -> bool:
    """La ficha oficial solo es navegable de forma fiable en procesos abiertos.

    En procesos cerrados/terminales Mercado Público responde
    "No Pertenece a la unidad de la ficha" a quien no es la unidad dueña, así
    que no exponemos el enlace en esos casos.
    """
    return estado == EstadoOportunidad.PUBLICADA.value


def _construir_item(
    m: OportunidadMatch,
    op: Licitacion | CompraAgil,
    *,
    feedback_valor: str | None,
    siguiendo: bool,
    ahora: datetime,
) -> dict[str, Any]:
    """Arma el dict de presentación de una oportunidad para el feed o una
    tarjeta individual (re-render HTMX tras seguir/me-sirve)."""
    dias: float | None = None
    if op.fecha_cierre is not None:
        delta = op.fecha_cierre - ahora
        dias = max(0.0, delta.total_seconds() / 86400)

    monto: float | None = None
    organismo: str | None = None
    reg: int | None = None
    if isinstance(op, Licitacion):
        monto = op.monto_clp
        organismo = op.codigo_organismo
    else:
        monto = op.monto_disponible_clp
        organismo = op.organismo_nombre
        reg = op.region

    return {
        "match": m,
        "oportunidad": op,
        "nombre": op.nombre,
        "estado": op.estado,
        "fecha_cierre": op.fecha_cierre,
        "dias_al_cierre": dias,
        "monto": monto,
        "organismo": organismo,
        "region": reg,
        "region_nombre": nombre_region(reg),
        "razones": razones_legibles(m.razones),
        "url_ficha": _url_ficha(m.fuente, m.codigo_oportunidad),
        "mostrar_ficha": mostrar_ficha_oficial(op.estado),
        "siguiendo": siguiendo,
        "feedback": feedback_valor,
    }


def get_item_oportunidad(
    session: Session,
    user_id: int,
    fuente: str,
    codigo: str,
) -> dict[str, Any] | None:
    """Arma el mismo dict que `get_oportunidades_usuario` para UNA oportunidad,
    usado para re-renderizar su tarjeta tras una acción HTMX (seguir/me-sirve).

    None si el usuario no tiene acceso (ownership, regla 17) o la oportunidad
    subyacente ya no existe.
    """
    m = check_oportunidad_access(session, user_id, fuente, codigo)
    if m is None:
        return None

    op: Licitacion | CompraAgil | None
    op = session.get(Licitacion, codigo) if fuente == "licitaciones" else session.get(CompraAgil, codigo)
    if op is None:
        return None

    feedback = obtener_feedback(session, user_id, fuente, codigo)
    siguiendo = obtener_seguimiento(session, user_id, fuente, codigo) is not None
    ahora = ahora_utc()
    return _construir_item(
        m,
        op,
        feedback_valor=feedback.valor if feedback is not None else None,
        siguiendo=siguiendo,
        ahora=ahora,
    )


# ---------------------------------------------------------------------------
# Filtros, facetas y conteos del feed (F-feed-filtros)
#
# Todo lo de este bloque son funciones PURAS sobre la lista de items que
# `get_oportunidades_usuario` ya cargó: ni una query agregada, ni un COUNT por
# faceta. Seis agregados por carga de página contra Neon free es el patrón que
# ya agotó las CU-horas (docs/11 y reglas 10-15 de CLAUDE.md).
# ---------------------------------------------------------------------------


# Página del feed cuando NO se agrupa (`agrupar_por="ninguno"`). Con agrupación
# manda el cap por grupo de `agrupar_oportunidades` y el offset se ignora.
LIMITE_PAGINA_DEFAULT = 20


@dataclass(frozen=True)
class ResultadoFeed:
    """Lo que el feed necesita para pintarse, en un solo objeto.

    `total` es después de TODOS los filtros y antes de paginar;
    `total_sin_filtro_relevancia` es el mismo conteo sin aplicar `min_score`
    (para "N ocultas por baja relevancia"). `facetas` y `nuevas_hoy` se
    calculan sobre el conjunto ya cargado, sin tocar la base.
    """

    items: list[dict[str, Any]]
    total: int
    total_sin_filtro_relevancia: int
    facetas: dict[str, dict[str, int]] = field(default_factory=dict)
    nuevas_hoy: int = 0


@dataclass(frozen=True)
class FiltrosFeed:
    """Los filtros del feed, todos opcionales y todos aplicados en Python.

    `None` (o el default) siempre significa "sin filtro". Los dos `incluir_*`
    van en `True` a propósito: ver `_pasa_monto` y `_pasa_cierre`.
    """

    fuente: str | None = None
    region: int | None = None
    texto: str | None = None
    monto_min: float | None = None
    monto_max: float | None = None
    incluir_monto_no_informado: bool = True
    cierre_desde: datetime | None = None
    cierre_hasta: datetime | None = None
    incluir_sin_fecha_cierre: bool = True
    familias: frozenset[FamiliaEstado] | None = None
    min_score: int = 0


def _pasa_fuente(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    return filtros.fuente is None or item["match"].fuente == filtros.fuente


def _pasa_region(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    """OJO: el filtro de región solo discrimina Compra Ágil.

    `Licitacion` no guarda región (es un cambio de modelo y de ingesta, con su
    propia fase), así que las licitaciones pasan TODAS. Quien exponga este
    filtro en la UI tiene que decírselo al usuario.
    """
    if filtros.region is None:
        return True
    if item["match"].fuente != "compras_agiles":
        return True
    return bool(item["region"] == filtros.region)


def _pasa_texto(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    if not filtros.texto:
        return True
    return filtros.texto.lower() in (item["nombre"] or "").lower()


def _pasa_monto(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    """Filtro sobre el `monto` YA normalizado por `_construir_item`
    (`monto_clp` en licitaciones, `monto_disponible_clp` en Compra Ágil).

    Un monto no informado pasa salvo que se pida lo contrario: falta seguido en
    la fuente oficial y excluirlo por omisión escondería oportunidades reales.
    """
    monto = item["monto"]
    if monto is None:
        return filtros.incluir_monto_no_informado
    if filtros.monto_min is not None and monto < filtros.monto_min:
        return False
    return not (filtros.monto_max is not None and monto > filtros.monto_max)


def _pasa_cierre(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    """Rango sobre `fecha_cierre`, que en la base es naive en UTC
    (`app/core/tiempo`, criterio de almacenamiento de F-fecha-cierre), así que
    los bordes se normalizan con `a_utc_naive` antes de comparar.

    Un item sin fecha de cierre pasa salvo que se pida lo contrario: Compra
    Ágil está dejando `fecha_cierre` en NULL incluso en las publicadas (deuda
    de docs/00-estado-actual.md) y el matching ya las trata como abiertas —
    con el default en False este filtro las borraría todas del feed.
    """
    cierre = item["fecha_cierre"]
    if cierre is None:
        return filtros.incluir_sin_fecha_cierre
    if filtros.cierre_desde is not None and cierre < a_utc_naive(filtros.cierre_desde):
        return False
    return not (
        filtros.cierre_hasta is not None and cierre > a_utc_naive(filtros.cierre_hasta)
    )


def _pasa_familia(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    if filtros.familias is None:
        return True
    return familia_de_estado(item["estado"]) in filtros.familias


def _pasa_min_score(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    return bool(item["match"].score >= filtros.min_score)


# clave de filtro -> predicado. La clave es la que `_aplicar_filtros` sabe
# saltarse (facetas y `total_sin_filtro_relevancia` piden justo eso).
_PREDICADOS: dict[str, Callable[[dict[str, Any], FiltrosFeed], bool]] = {
    "fuente": _pasa_fuente,
    "region": _pasa_region,
    "texto": _pasa_texto,
    "monto": _pasa_monto,
    "cierre": _pasa_cierre,
    "familias": _pasa_familia,
    "min_score": _pasa_min_score,
}


def _aplicar_filtros(
    items: Iterable[dict[str, Any]],
    filtros: FiltrosFeed,
    *,
    excepto: str | None = None,
) -> list[dict[str, Any]]:
    """Los items que pasan todos los filtros, salvo el de clave `excepto`."""
    predicados = [p for clave, p in _PREDICADOS.items() if clave != excepto]
    return [item for item in items if all(p(item, filtros) for p in predicados)]


def _clave_faceta_fuente(item: dict[str, Any]) -> str:
    return str(item["match"].fuente)


def _clave_faceta_estado(item: dict[str, Any]) -> str:
    return familia_de_estado(item["estado"]).value


def _clave_faceta_region(item: dict[str, Any]) -> str:
    """Código de región como string; los nombres los resuelve
    `presentacion.nombre_region` en la capa de plantilla, no acá.

    Las licitaciones caen todas en "sin_region" porque el modelo no guarda su
    región (ver `_pasa_region`)."""
    reg = item["region"]
    return "sin_region" if reg is None else str(reg)


# faceta -> (clave del filtro PROPIO que no se le aplica, extractor de la clave)
_FACETAS: dict[str, tuple[str, Callable[[dict[str, Any]], str]]] = {
    "fuente": ("fuente", _clave_faceta_fuente),
    "estado": ("familias", _clave_faceta_estado),
    "region": ("region", _clave_faceta_region),
}


def calcular_facetas(
    items: Iterable[dict[str, Any]], filtros: FiltrosFeed
) -> dict[str, dict[str, int]]:
    """Conteos por faceta sobre el conjunto ya cargado.

    Regla de búsqueda facetada: el conteo de cada faceta se calcula con todos
    los demás filtros aplicados MENOS el suyo propio. Si el usuario ya filtró
    por "Licitaciones", el conteo de Compra Ágil tiene que seguir diciendo
    cuántas habría si soltara ese filtro; si no, el número baja a cero y el
    filtro se vuelve una trampa de la que no se puede salir.
    """
    items = list(items)
    facetas: dict[str, dict[str, int]] = {}
    for faceta, (filtro_propio, clave_de) in _FACETAS.items():
        conteo: dict[str, int] = {}
        for item in _aplicar_filtros(items, filtros, excepto=filtro_propio):
            clave = clave_de(item)
            conteo[clave] = conteo.get(clave, 0) + 1
        # Orden estable y útil para la UI: más frecuentes primero.
        facetas[faceta] = dict(sorted(conteo.items(), key=lambda kv: (-kv[1], kv[0])))
    return facetas


def contar_nuevas_hoy(items: Iterable[dict[str, Any]], *, hoy: date) -> int:
    """Cuántos matches aparecieron HOY, con el día calendario de Chile.

    `hoy` es la fecha en `America/Santiago` (regla 5: el proceso corre en UTC,
    su fecha no sirve). `fecha_match` es naive en UTC y es inmutable por diseño
    —la primera vez que esa oportunidad matcheó ese perfil, no se re-toca al
    re-scorear (F-notificaciones)—, así que el conteo es confiable.
    """
    inicio = borde_del_dia_utc_naive(hoy, fin_de_dia=False)
    fin = borde_del_dia_utc_naive(hoy + timedelta(days=1), fin_de_dia=False)
    total = 0
    for item in items:
        fecha_match = getattr(item["match"], "fecha_match", None)
        if fecha_match is None:
            continue
        if inicio <= fecha_match < fin:
            total += 1
    return total


def _ordenar(items: list[dict[str, Any]], orden: str) -> None:
    """Ordena in-place. Un `orden` desconocido cae al default (score), sin romper."""
    if orden == "cierre":
        items.sort(key=lambda r: (r["dias_al_cierre"] is None, r["dias_al_cierre"]))
    elif orden == "monto":
        # Descendente, con los no informados AL FINAL: un monto que la fuente
        # no entrega no es un monto cero.
        items.sort(key=lambda r: (r["monto"] is None, -(r["monto"] or 0.0)))
    else:
        items.sort(key=lambda r: r["match"].score, reverse=True)


def get_oportunidades_usuario(
    session: Session,
    user_id: int,
    fuente: str | None = None,
    region: int | None = None,
    texto: str | None = None,
    perfil_id: int | None = None,
    orden: str = "score",
    min_score: int = 0,
    monto_min: float | None = None,
    monto_max: float | None = None,
    incluir_monto_no_informado: bool = True,
    cierre_desde: datetime | None = None,
    cierre_hasta: datetime | None = None,
    incluir_sin_fecha_cierre: bool = True,
    familias: set[FamiliaEstado] | frozenset[FamiliaEstado] | None = None,
    limit: int = LIMITE_PAGINA_DEFAULT,
    offset: int = 0,
) -> ResultadoFeed:
    """Retorna un `ResultadoFeed` con las oportunidades_match de los perfiles
    activos del usuario.

    Todos los filtros se aplican en Python después de cargar los matches, sobre
    el MISMO conjunto ya cargado: es lo que permite calcular las facetas sin
    una sola query agregada extra. Excluye las oportunidades que el usuario
    descartó (feedback F10 parte 2) — esas solo se ven en la vista "ver
    descartadas" (`listar_descartadas_detalle`).

    - `orden`: "score" (default, mejor match primero), "cierre" (cierran antes
      primero, sin fecha al final) o "monto" (mayor primero, no informados al
      final). Paginación después de aplicar todos los filtros y el orden.
    - `min_score`: piso de `OportunidadMatch.score` (umbral de relevancia del
      feed); 0 = sin piso. `total_sin_filtro_relevancia` es el total que habría
      sin aplicarlo, para mostrar "N ocultas por baja relevancia".
    - `region`: **solo afecta a Compra Ágil**. `Licitacion` no guarda región,
      así que las licitaciones pasan todas — ver `_pasa_region`.
    - `monto_min`/`monto_max` van contra el monto ya normalizado;
      `cierre_desde`/`cierre_hasta` contra `fecha_cierre` (naive en UTC: un
      borde naive se interpreta como hora de Chile, igual que el resto del
      proyecto). Los dos `incluir_*` en `True` evitan que el filtro se coma las
      oportunidades sin el dato.
    - `familias`: familias de `EstadoOportunidad` (F-feed-ui-1); None = todas.

    El feed carga todos los matches del usuario en memoria y recién después
    filtra y corta. A la escala de un equipo de 3-10 usuarios aguanta de sobra
    en los 512 MB de Render, pero es el techo conocido si el volumen crece.
    """
    filtros = FiltrosFeed(
        fuente=fuente,
        region=region,
        texto=texto,
        monto_min=monto_min,
        monto_max=monto_max,
        incluir_monto_no_informado=incluir_monto_no_informado,
        cierre_desde=cierre_desde,
        cierre_hasta=cierre_hasta,
        incluir_sin_fecha_cierre=incluir_sin_fecha_cierre,
        familias=frozenset(familias) if familias is not None else None,
        min_score=min_score,
    )
    vacio = ResultadoFeed(
        items=[], total=0, total_sin_filtro_relevancia=0, facetas=calcular_facetas([], filtros)
    )

    perfiles = listar_perfiles(session, user_id)
    if not perfiles:
        return vacio

    perfil_ids = [p.id for p in perfiles]

    stmt = select(OportunidadMatch).where(OportunidadMatch.perfil_id.in_(perfil_ids))

    # `fuente` NO se filtra en SQL: la faceta de fuente tiene que poder contar
    # la fuente descartada (regla de leave-one-out de `calcular_facetas`), y
    # para eso los items de ambas fuentes tienen que estar cargados.
    if perfil_id is not None:
        if perfil_id not in perfil_ids:
            return vacio
        stmt = stmt.where(OportunidadMatch.perfil_id == perfil_id)

    stmt = stmt.order_by(OportunidadMatch.score.desc())
    matches = list(session.execute(stmt).scalars())

    feedback_map = listar_feedback_usuario(session, user_id)
    siguiendo_set = {(s.fuente, s.codigo_oportunidad) for s in listar_seguidas(session, user_id)}

    # Batch-load oportunidades
    lic_codigos = [m.codigo_oportunidad for m in matches if m.fuente == "licitaciones"]
    ca_codigos = [m.codigo_oportunidad for m in matches if m.fuente == "compras_agiles"]

    lics: dict[str, Licitacion] = {}
    if lic_codigos:
        for lic in session.execute(
            select(Licitacion).where(Licitacion.codigo.in_(lic_codigos))
        ).scalars():
            lics[lic.codigo] = lic

    cas: dict[str, CompraAgil] = {}
    if ca_codigos:
        for c in session.execute(
            select(CompraAgil).where(CompraAgil.codigo.in_(ca_codigos))
        ).scalars():
            cas[c.codigo] = c

    ahora = ahora_utc()
    result: list[dict[str, Any]] = []

    for m in matches:
        op: Licitacion | CompraAgil | None
        if m.fuente == "licitaciones":
            op = lics.get(m.codigo_oportunidad)
        else:
            op = cas.get(m.codigo_oportunidad)

        if op is None:
            continue

        feedback = feedback_map.get((m.fuente, m.codigo_oportunidad))
        if feedback is not None and feedback.valor == ValorFeedback.DESCARTE.value:
            continue

        result.append(
            _construir_item(
                m,
                op,
                feedback_valor=feedback.valor if feedback is not None else None,
                siguiendo=(m.fuente, m.codigo_oportunidad) in siguiendo_set,
                ahora=ahora,
            )
        )

    # `result` es el conjunto cargado (sin filtrar): la base de las facetas.
    total_sin_filtro = len(_aplicar_filtros(result, filtros, excepto="min_score"))
    facetas = calcular_facetas(result, filtros)

    filtrados = _aplicar_filtros(result, filtros)
    _ordenar(filtrados, orden)

    total = len(filtrados)
    nuevas_hoy = contar_nuevas_hoy(filtrados, hoy=datetime.now(TZ_CHILE).date())
    return ResultadoFeed(
        items=filtrados[offset : offset + limit],
        total=total,
        total_sin_filtro_relevancia=total_sin_filtro,
        facetas=facetas,
        nuevas_hoy=nuevas_hoy,
    )


# Tope de items visibles por grupo antes de "ver más en este grupo" (evita
# explotar el DOM cuando el umbral de relevancia está en "Todas").
CAP_GRUPO_DEFAULT = 10

AGRUPAR_POR_VALIDOS = {"motivo", "region", "fuente", "ninguno"}

# Clave del grupo implícito de `agrupar_por="ninguno"` (lista plana paginada).
GRUPO_UNICO_KEY = "ninguno:todas"


def _claves_grupo(item: dict[str, Any], agrupar_por: str) -> list[tuple[str, str]]:
    """Las claves (tipo, label) de grupo a las que pertenece un item.

    "motivo" puede devolver VARIAS claves (repetición intencional: una
    oportunidad que matchea por 2 rubros + 1 keyword aparece en 3 grupos).
    "region"/"fuente" son categorizaciones únicas — siempre devuelven 1 clave.
    No se ofrece agrupar por organismo/sector: `codigo_organismo` viene vacío
    en licitaciones (ver docs/08-datos-organismos.md §3-bis d) — la mayoría de
    los grupos quedarían "sin organismo", sin valor para el usuario.
    """
    if agrupar_por == "region":
        return [("region", item["region_nombre"] or "Sin región")]
    if agrupar_por == "fuente":
        nombre = "Licitaciones" if item["match"].fuente == "licitaciones" else "Compra Ágil"
        return [("fuente", nombre)]

    # "motivo" (default)
    razones = item["match"].razones or {}
    motivos: list[tuple[str, str]] = []
    for codigo in razones.get("categorias_hit") or []:
        motivos.append(("rubro", nombre_rubro(str(codigo)) or str(codigo)))
    for kw in razones.get("keywords_hit") or []:
        motivos.append(("keyword", str(kw)))
    if razones.get("organismo_seguido"):
        motivos.append(("organismo_seguido", "Organismo seguido"))
    if not motivos:
        motivos.append(("otros", "Otros"))
    return motivos


def agrupar_oportunidades(
    items: list[dict[str, Any]],
    agrupar_por: str = "motivo",
    *,
    cap_por_grupo: int = CAP_GRUPO_DEFAULT,
    grupo_expandido: str | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Agrupa items YA filtrados por relevancia y ordenados (score/cierre) por
    `get_oportunidades_usuario`. Retorna (grupos, total_unico, total_apariciones).

    `agrupar_por`: "motivo" (default, con repetición intencional entre
    grupos), "region" o "fuente" (categorización única), o "ninguno" (un solo
    grupo implícito con todos los items y SIN cap). El orden de los
    items dentro de cada grupo respeta el orden de `items` de entrada (no se
    reordena). Los grupos se ordenan por su mejor score (desc). Cada grupo se
    capa a `cap_por_grupo` items salvo que `grupo_expandido` sea su `key`
    (control "ver más en este grupo", sin reordenar todo el feed).

    Quién manda sobre cuántos items se ven, que es donde esto choca con
    F-feed-agrupado: con "ninguno" manda la paginación de
    `get_oportunidades_usuario` (`limit`/`offset`) y acá no se capa nada; al
    agrupar manda el cap por grupo y el `offset` no participa.
    """
    if agrupar_por == "ninguno":
        items = list(items)
        grupo = {
            "key": GRUPO_UNICO_KEY,
            "tipo": "ninguno",
            "label": "Todas",
            "items": items,
            "count": len(items),
            "mejor_score": max((i["match"].score for i in items), default=0.0),
        }
        total_unico = len({(i["match"].fuente, i["match"].codigo_oportunidad) for i in items})
        return [grupo], total_unico, len(items)

    grupos_map: dict[str, dict[str, Any]] = {}

    for item in items:
        for tipo, label in _claves_grupo(item, agrupar_por):
            key = f"{tipo}:{label}"
            grupo = grupos_map.setdefault(
                key,
                {"key": key, "tipo": tipo, "label": label, "items": [], "count": 0, "mejor_score": 0.0},
            )
            grupo["items"].append(item)
            grupo["count"] += 1
            grupo["mejor_score"] = max(grupo["mejor_score"], item["match"].score)

    total_apariciones = sum(g["count"] for g in grupos_map.values())
    total_unico = len({(i["match"].fuente, i["match"].codigo_oportunidad) for i in items})

    grupos = sorted(grupos_map.values(), key=lambda g: g["mejor_score"], reverse=True)
    for grupo in grupos:
        if grupo["key"] != grupo_expandido and len(grupo["items"]) > cap_por_grupo:
            grupo["items"] = grupo["items"][:cap_por_grupo]

    return grupos, total_unico, total_apariciones


def listar_seguidas_detalle(
    session: Session,
    user_id: int,
    *,
    incluir_archivadas: bool = False,
) -> list[dict[str, Any]]:
    """Seguimientos del usuario enriquecidos con el estado/datos actuales de la oportunidad.

    Si la oportunidad subyacente ya no existe (caso raro), degrada a los datos
    mínimos guardados en el seguimiento en vez de romper el render.
    """
    seguidas = listar_seguidas(session, user_id, incluir_archivadas=incluir_archivadas)

    lic_codigos = [s.codigo_oportunidad for s in seguidas if s.fuente == "licitaciones"]
    ca_codigos = [s.codigo_oportunidad for s in seguidas if s.fuente == "compras_agiles"]

    lics: dict[str, Licitacion] = {}
    if lic_codigos:
        for lic in session.execute(
            select(Licitacion).where(Licitacion.codigo.in_(lic_codigos))
        ).scalars():
            lics[lic.codigo] = lic

    cas: dict[str, CompraAgil] = {}
    if ca_codigos:
        for c in session.execute(
            select(CompraAgil).where(CompraAgil.codigo.in_(ca_codigos))
        ).scalars():
            cas[c.codigo] = c

    result: list[dict[str, Any]] = []
    for s in seguidas:
        op: Licitacion | CompraAgil | None
        if s.fuente == "licitaciones":
            op = lics.get(s.codigo_oportunidad)
        else:
            op = cas.get(s.codigo_oportunidad)

        result.append({
            "seguimiento": s,
            "nombre": op.nombre if op is not None else s.codigo_oportunidad,
            "estado": op.estado if op is not None else s.estado_visto,
            "fecha_cierre": op.fecha_cierre if op is not None else None,
            "url_ficha_app": f"/oportunidad/{s.fuente}/{s.codigo_oportunidad}",
        })
    return result


def listar_descartadas_detalle(session: Session, user_id: int) -> list[dict[str, Any]]:
    """Oportunidades descartadas del usuario (F10 parte 2), enriquecidas con
    el nombre/estado actuales — mismo patrón que `listar_seguidas_detalle`.

    Si la oportunidad subyacente ya no existe, degrada al código crudo en vez
    de romper el render (regla 6)."""
    descartadas = listar_descartadas(session, user_id)

    lic_codigos = [d.codigo_oportunidad for d in descartadas if d.fuente == "licitaciones"]
    ca_codigos = [d.codigo_oportunidad for d in descartadas if d.fuente == "compras_agiles"]

    lics: dict[str, Licitacion] = {}
    if lic_codigos:
        for lic in session.execute(
            select(Licitacion).where(Licitacion.codigo.in_(lic_codigos))
        ).scalars():
            lics[lic.codigo] = lic

    cas: dict[str, CompraAgil] = {}
    if ca_codigos:
        for c in session.execute(
            select(CompraAgil).where(CompraAgil.codigo.in_(ca_codigos))
        ).scalars():
            cas[c.codigo] = c

    result: list[dict[str, Any]] = []
    for d in descartadas:
        op: Licitacion | CompraAgil | None
        if d.fuente == "licitaciones":
            op = lics.get(d.codigo_oportunidad)
        else:
            op = cas.get(d.codigo_oportunidad)

        result.append({
            "feedback": d,
            "nombre": op.nombre if op is not None else d.codigo_oportunidad,
            "estado": op.estado if op is not None else None,
        })
    return result


def resumen_competencia(session: Session, licitacion_codigo: str) -> list[dict[str, Any]]:
    """Resumen de competencia por proveedor (F-competencia), incluyendo a quienes
    ofertaron pero NO ganaron — panorama competitivo completo, no solo ganadores
    (ver deuda señalada en docs/00-estado-actual.md, resuelta en F10 parte 3).
    Por proveedor: items_ofertados (cuántos ítems ofertó en total), items_ganados
    (cuántos le fueron adjudicados) y total_adjudicado (suma de
    monto_linea_adjudicada de sus ofertas seleccionadas). Agrupa por
    rut_proveedor (más estable que el nombre, ver docs/05-competencia.md §3).
    Orden: ganadores primero por total_adjudicado desc; no-ganadores después
    por items_ofertados desc. Vacío si no hay ofertas capturadas aún."""
    ofertas = list(
        session.execute(
            select(OfertaCompetencia).where(OfertaCompetencia.licitacion_codigo == licitacion_codigo)
        ).scalars()
    )
    por_proveedor: dict[str, dict[str, Any]] = {}
    for o in ofertas:
        entry = por_proveedor.setdefault(
            o.rut_proveedor,
            {
                "rut_proveedor": o.rut_proveedor,
                "nombre_proveedor": o.nombre_proveedor,
                "items_ofertados": 0,
                "items_ganados": 0,
                "total_adjudicado": 0.0,
            },
        )
        entry["items_ofertados"] += 1
        if o.seleccionada:
            entry["items_ganados"] += 1
            entry["total_adjudicado"] += o.monto_linea_adjudicada or 0.0
    resumen = list(por_proveedor.values())
    resumen.sort(
        key=lambda d: (
            d["items_ganados"] == 0,
            -d["total_adjudicado"],
            -d["items_ofertados"],
        )
    )
    return resumen


def detalle_competencia(session: Session, licitacion_codigo: str) -> list[dict[str, Any]]:
    """Detalle por ítem de todas las ofertas (seleccionadas y no) de una licitación."""
    ofertas = session.execute(
        select(OfertaCompetencia)
        .where(OfertaCompetencia.licitacion_codigo == licitacion_codigo)
        .order_by(OfertaCompetencia.codigo_item, OfertaCompetencia.seleccionada.desc())
    ).scalars()
    return [
        {
            "codigo_item": o.codigo_item,
            "rut_proveedor": o.rut_proveedor,
            "nombre_proveedor": o.nombre_proveedor,
            "monto_unitario": o.monto_unitario,
            "monto_linea_adjudicada": o.monto_linea_adjudicada,
            "seleccionada": o.seleccionada,
        }
        for o in ofertas
    ]


def buscar_instituciones_pac(
    session: Session,
    texto: str,
    *,
    limit: int = 20,
) -> list[InstitucionPAC]:
    """Autocomplete del Plan Anual de Compra: instituciones por razón social
    (caché de app.ingest.plan_compra.sync_instituciones_pac). Vacío si `texto`
    está vacío — no lista las ~1.333 instituciones de una sola vez."""
    texto = texto.strip()
    if not texto:
        return []
    return list(
        session.execute(
            select(InstitucionPAC)
            .where(InstitucionPAC.razon_social.ilike(f"%{texto}%"))
            .order_by(InstitucionPAC.razon_social)
            .limit(limit)
        ).scalars()
    )


def listar_organismos_catalogo(session: Session) -> list[InstitucionPAC]:
    """Catálogo completo de organismos (F-plan + sector de F-datos) para el
    multi-select agrupado por sector del formulario de perfiles (F10).

    Orden alfabético por `sector` deja "Sin clasificación" al final de forma
    natural (empieza con "Si", posterior a los 7 sectores nombrados en orden
    alfabético — ver docs/08-datos-organismos.md §3-bis b), sin necesidad de
    un caso especial. Vacío si el catálogo aún no se sincronizó (regla 6: la
    ruta debe degradar a un campo de texto libre en ese caso, no romper)."""
    orden_sector = func.coalesce(InstitucionPAC.sector, SECTOR_SIN_CLASIFICACION)
    return list(
        session.execute(select(InstitucionPAC).order_by(orden_sector, InstitucionPAC.razon_social)).scalars()
    )


def check_oportunidad_access(
    session: Session,
    user_id: int,
    fuente: str,
    codigo: str,
) -> OportunidadMatch | None:
    """Retorna el match si el usuario tiene acceso, None si no."""
    perfiles = listar_perfiles(session, user_id)
    if not perfiles:
        return None
    perfil_ids = [p.id for p in perfiles]
    return session.execute(
        select(OportunidadMatch)
        .where(
            OportunidadMatch.perfil_id.in_(perfil_ids),
            OportunidadMatch.fuente == fuente,
            OportunidadMatch.codigo_oportunidad == codigo,
        )
        .limit(1)
    ).scalar_one_or_none()
