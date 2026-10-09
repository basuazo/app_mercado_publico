"""Queries compartidas entre rutas HTML y API REST."""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast
from urllib.parse import quote

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session, defer

from app.api.presentacion import nombre_region, razones_legibles, texto_cierre
from app.catalogos.unspsc import nombre_rubro
from app.core.tiempo import TZ_CHILE, a_utc_naive, ahora_utc, borde_del_dia_utc_naive
from app.core.vigencia import (
    CA_SIN_CIERRE_VIGENCIA_DIAS,
    condicion_ca_vigente,
    condicion_lic_vigente,
    condicion_vencida_en_ventana,
    es_vigente,
    fecha_vencimiento,
)
from app.matching.feedback import listar_descartadas, listar_feedback_usuario, obtener_feedback
from app.matching.perfiles import listar_perfiles
from app.matching.seguimiento import esta_guardada, listar_seguidas, obtener_seguimiento
from app.models.enums import SECTOR_SIN_CLASIFICACION, EstadoOportunidad, ValorFeedback
from app.models.tables import (
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    MatchFeedback,
    OfertaCompetencia,
    OportunidadMatch,
    OportunidadSeguida,
)

FUENTES_VALIDAS = ("licitaciones", "compras_agiles")


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
    m: OportunidadMatch | None,
    op: Licitacion | CompraAgil,
    *,
    feedback_valor: str | None,
    guardada: bool,
    ahora: datetime,
    score_max: float | None = None,
    perfiles: list[str] | None = None,
) -> dict[str, Any]:
    """Arma el dict de presentación de una oportunidad para el feed o una
    tarjeta individual (re-render HTMX tras guardar).

    `m` es None en Mi registro para una guardada sin match (F-registro): sin score
    ni razones. `score_max` es el mejor score del usuario cuando hay varios matches
    (varios perfiles) de la misma oportunidad; `perfiles` son los nombres de los
    perfiles que la traen (F-match-1)."""
    dias: float | None = None
    if op.fecha_cierre is not None:
        delta = op.fecha_cierre - ahora
        dias = max(0.0, delta.total_seconds() / 86400)

    monto: float | None = None
    organismo: str | None = None
    fuente = "licitaciones" if isinstance(op, Licitacion) else "compras_agiles"
    if isinstance(op, Licitacion):
        monto = op.monto_clp
        # Nombre y región llegan con el detalle (F-datos-1); el código, de respaldo.
        organismo = op.organismo_nombre or op.codigo_organismo
    else:
        monto = op.monto_disponible_clp
        organismo = op.organismo_nombre
    reg: int | None = op.region

    score = score_max if score_max is not None else (m.score if m is not None else None)
    return {
        "match": m,
        "oportunidad": op,
        "fuente": fuente,
        "codigo": op.codigo,
        "score": score,
        "nombre": op.nombre,
        "estado": op.estado,
        "fecha_cierre": op.fecha_cierre,
        "fecha_publicacion": getattr(op, "fecha_publicacion", None),
        "dias_al_cierre": dias,
        "monto": monto,
        "organismo": organismo,
        "region": reg,
        "region_nombre": nombre_region(reg),
        "razones": razones_legibles(m.razones if m is not None else None),
        "url_ficha": _url_ficha(fuente, op.codigo),
        "mostrar_ficha": mostrar_ficha_oficial(op.estado),
        "guardada": guardada,
        "feedback": feedback_valor,
        "perfiles": list(perfiles or []),
    }


def nombres_de_perfiles(matches: Iterable[OportunidadMatch], nombres: dict[int, str]) -> list[str]:
    """Nombres de los perfiles de esos matches, sin repetir y en el orden recibido."""
    vistos: list[str] = []
    for m in matches:
        nombre = nombres.get(m.perfil_id)
        if nombre is not None and nombre not in vistos:
            vistos.append(nombre)
    return vistos


def get_item_oportunidad(
    session: Session,
    user_id: int,
    fuente: str,
    codigo: str,
) -> dict[str, Any] | None:
    """Arma el mismo dict que `get_oportunidades_usuario` para UNA oportunidad,
    usado para re-renderizar su tarjeta tras una acción HTMX (guardar).

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
    ahora = ahora_utc()
    nombres = {p.id: p.nombre for p in listar_perfiles(session, user_id)}
    return _construir_item(
        m,
        op,
        feedback_valor=feedback.valor if feedback is not None else None,
        guardada=esta_guardada(session, user_id, fuente, codigo),
        ahora=ahora,
        perfiles=nombres_de_perfiles(
            matches_de_oportunidad(session, user_id, fuente, codigo), nombres
        ),
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

    La vigencia (F-vigencia) NO vive acá: es un prefiltro de SQL + `es_vigente`
    aplicado ANTES de armar los items (ver `get_oportunidades_usuario`), no una
    faceta con leave-one-out — por eso tampoco tiene predicado en `_PREDICADOS`.
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
    perfiles: frozenset[int] | None = None
    keywords: frozenset[str] | None = None
    min_score: int = 0


def _pasa_fuente(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    return filtros.fuente is None or item["match"].fuente == filtros.fuente


def _pasa_region(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    """Región de la CA o de la licitación (F-datos-1 la trae con el detalle).

    Una oportunidad con región NO informada pasa (la licitación sin detalle
    todavía), igual que un monto no informado.
    """
    if filtros.region is None:
        return True
    if item["region"] is None:
        return bool(item["match"].fuente == "licitaciones")
    return bool(item["region"] == filtros.region)


def _sin_tildes(texto: str | None) -> str:
    """Minúsculas y sin tildes: "reparacion" calza con "Reparación"."""
    descompuesto = unicodedata.normalize("NFD", (texto or "").casefold())
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def _pasa_texto(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    """El texto, sin tildes ni mayúsculas, dentro del nombre O del organismo."""
    if not filtros.texto:
        return True
    buscado = _sin_tildes(filtros.texto)
    return buscado in _sin_tildes(item["nombre"]) or buscado in _sin_tildes(item.get("organismo"))


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
    de docs/archivo/00-estado-actual.md) y el matching ya las trata como abiertas —
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


def _matches_de(item: dict[str, Any]) -> list[OportunidadMatch]:
    """Todos los matches del usuario con la oportunidad (uno por perfil). El item
    trae el mejor en `match`; los tests y las guardadas sin dedupe traen solo ese."""
    return cast(list[OportunidadMatch], item.get("matches") or [item["match"]])


def _pasa_perfiles(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    return filtros.perfiles is None or any(
        m.perfil_id in filtros.perfiles for m in _matches_de(item)
    )


def _keywords_de(item: dict[str, Any]) -> set[str]:
    """Unión de `keywords_hit` de todos los matches de la oportunidad."""
    return {
        str(kw) for m in _matches_de(item) for kw in ((m.razones or {}).get("keywords_hit") or [])
    }


def _pasa_keywords(item: dict[str, Any], filtros: FiltrosFeed) -> bool:
    """Filtra por `razones["keywords_hit"]` del match (F-vigencia 3-bis):
    cualquier intersección con las palabras clave elegidas hace pasar el item."""
    if filtros.keywords is None:
        return True
    return bool(_keywords_de(item) & filtros.keywords)


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
    "perfiles": _pasa_perfiles,
    "keywords": _pasa_keywords,
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


def _clave_faceta_fuente(item: dict[str, Any]) -> list[str]:
    return [str(item["match"].fuente)]


def _clave_faceta_region(item: dict[str, Any]) -> list[str]:
    """Código de región como string; los nombres los resuelve
    `presentacion.nombre_region` en la capa de plantilla, no acá. Una región no
    informada cae en "sin_region"."""
    reg = item["region"]
    return ["sin_region" if reg is None else str(reg)]


def _clave_faceta_perfil(item: dict[str, Any]) -> list[str]:
    return [str(m.perfil_id) for m in _matches_de(item)]


def _clave_faceta_keyword(item: dict[str, Any]) -> list[str]:
    """Un item puede pertenecer a VARIAS claves (repetición intencional: si
    matcheó por 2 keywords, cuenta para las 2), a diferencia de fuente/región/
    perfil (siempre una)."""
    return sorted(_keywords_de(item))


# faceta -> (clave del filtro PROPIO que no se le aplica, extractor de claves).
# El extractor devuelve una LISTA porque "keyword" puede sumar un item a más
# de un conteo a la vez (ver `_clave_faceta_keyword`).
_FACETAS: dict[str, tuple[str, Callable[[dict[str, Any]], list[str]]]] = {
    "fuente": ("fuente", _clave_faceta_fuente),
    "region": ("region", _clave_faceta_region),
    "perfil": ("perfiles", _clave_faceta_perfil),
    "keyword": ("keywords", _clave_faceta_keyword),
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
    for faceta, (filtro_propio, claves_de) in _FACETAS.items():
        conteo: dict[str, int] = {}
        for item in _aplicar_filtros(items, filtros, excepto=filtro_propio):
            for clave in claves_de(item):
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
        fechas = [
            f for f in (getattr(m, "fecha_match", None) for m in _matches_de(item)) if f is not None
        ]
        if not fechas:
            continue
        fecha_match = min(fechas)
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
        # Mejor match: relevancia descendente y, a igual relevancia, cierre más
        # próximo primero (sin cierre al final).
        items.sort(
            key=lambda r: (
                -r["match"].score,
                r["fecha_cierre"] is None,
                r["fecha_cierre"] or datetime.min,
            )
        )


def get_oportunidades_usuario(
    session: Session,
    user_id: int,
    fuente: str | None = None,
    region: int | None = None,
    texto: str | None = None,
    perfil_ids: list[int] | frozenset[int] | None = None,
    orden: str = "score",
    min_score: int = 0,
    monto_min: float | None = None,
    monto_max: float | None = None,
    incluir_monto_no_informado: bool = True,
    cierre_desde: datetime | None = None,
    cierre_hasta: datetime | None = None,
    incluir_sin_fecha_cierre: bool = True,
    keywords: list[str] | frozenset[str] | None = None,
    solo_vigentes: bool = True,
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

    - `orden`: "score" (default, mejor match primero; a igual relevancia, cierre
      más próximo), "cierre" (cierran antes
      primero, sin fecha al final) o "monto" (mayor primero, no informados al
      final). Paginación después de aplicar todos los filtros y el orden.
    - `min_score`: piso de `OportunidadMatch.score` (umbral de relevancia del
      feed); 0 = sin piso. `total_sin_filtro_relevancia` es el total que habría
      sin aplicarlo, para mostrar "N ocultas por baja relevancia".
    - Una oportunidad que calza con varios perfiles sale UNA vez (con el match de
      mayor relevancia y `perfiles` con los nombres); los conteos y facetas se
      calculan después de deduplicar.
    - `region`: la de la CA o la de la licitación; sin región informada pasa
      (licitación) — ver `_pasa_region`.
    - `monto_min`/`monto_max` van contra el monto ya normalizado;
      `cierre_desde`/`cierre_hasta` contra `fecha_cierre` (naive en UTC: un
      borde naive se interpreta como hora de Chile, igual que el resto del
      proyecto). Los dos `incluir_*` en `True` evitan que el filtro se coma las
      oportunidades sin el dato.
    - `perfil_ids`: subconjunto de perfiles del usuario a mostrar (F-vigencia
      3-bis, selección múltiple); None = todos sus perfiles activos.
    - `keywords`: palabras clave de sus perfiles a exigir en
      `razones["keywords_hit"]` del match; None = todas.
    - `solo_vigentes`: True (default) deja solo lo que todavía se puede
      postular (`app/core/vigencia.es_vigente`); F-registro lo pasa en False
      para revisar también lo guardado/vencido.

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
        perfiles=frozenset(perfil_ids) if perfil_ids is not None else None,
        keywords=frozenset(keywords) if keywords is not None else None,
        min_score=min_score,
    )
    vacio = ResultadoFeed(
        items=[], total=0, total_sin_filtro_relevancia=0, facetas=calcular_facetas([], filtros)
    )

    perfiles = listar_perfiles(session, user_id)
    if not perfiles:
        return vacio

    perfil_ids_usuario = [p.id for p in perfiles]

    stmt = select(OportunidadMatch).where(OportunidadMatch.perfil_id.in_(perfil_ids_usuario))

    ahora = ahora_utc()

    # Prefiltro de vigencia EN SQL (F-vigencia): candidatos por fecha, antes de
    # cargar un solo match en memoria — el beneficio real es que el feed deja
    # de crecer con el histórico (techo de los 512 MB de Render). No es la
    # decisión final: la familia del estado (ABIERTA/DESCONOCIDO) no es una
    # comparación SQL simple, así que `es_vigente` la confirma en Python más
    # abajo sobre este subconjunto ya angosto.
    if solo_vigentes:
        lic_candidatos = select(Licitacion.codigo).where(Licitacion.fecha_cierre > ahora)
        ca_candidatos = select(CompraAgil.codigo).where(
            or_(
                CompraAgil.fecha_cierre > ahora,
                and_(
                    CompraAgil.fecha_cierre.is_(None),
                    CompraAgil.fecha_publicacion
                    >= ahora - timedelta(days=CA_SIN_CIERRE_VIGENCIA_DIAS),
                ),
            )
        )
        stmt = stmt.where(
            or_(
                and_(
                    OportunidadMatch.fuente == "licitaciones",
                    OportunidadMatch.codigo_oportunidad.in_(lic_candidatos),
                ),
                and_(
                    OportunidadMatch.fuente == "compras_agiles",
                    OportunidadMatch.codigo_oportunidad.in_(ca_candidatos),
                ),
            )
        )

    # `fuente`, `perfiles` y `keywords` NO se filtran en SQL: sus facetas
    # tienen que poder contar lo que el propio filtro esconde (leave-one-out
    # de `calcular_facetas`), y para eso los items de todas las fuentes/
    # perfiles/keywords tienen que estar cargados primero.
    #
    # F-coherencia revisó si la faceta podía salir de UNA consulta agregada
    # (`GROUP BY fuente`) para dejar de pagar esa memoria, y NO se puede sin
    # mentir: de los filtros del feed, varios (region, texto, monto, cierre)
    # dependen de las filas de Licitacion/CompraAgil, no de oportunidades_match.
    # Una agregada que solo respete perfil, min_score y descartadas devuelve un
    # número MAYOR que el real apenas el usuario usa cualquiera de esos otros
    # filtros — y F-feed-ui-2 los expone todos. Respetarlos en SQL sería
    # reimplementar el pipeline completo sobre dos tablas: dos fuentes de
    # verdad para el mismo número. Se mantiene el cálculo en Python (ver el
    # resumen de F-coherencia). La vigencia es la excepción: es un prefiltro
    # duro (arriba), no una faceta — no necesita leave-one-out.
    stmt = stmt.order_by(OportunidadMatch.score.desc(), OportunidadMatch.id)
    matches = list(session.execute(stmt).scalars())
    nombres_perfil = {p.id: p.nombre for p in perfiles}

    feedback_map = listar_feedback_usuario(session, user_id)
    guardadas_set = {(s.fuente, s.codigo_oportunidad) for s in listar_seguidas(session, user_id)}

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

    # Un ítem por oportunidad (F-match-1): los matches vienen del de mayor
    # relevancia al menor, así que el primero de cada (fuente, código) es el que
    # se muestra y los demás solo suman su perfil.
    result: list[dict[str, Any]] = []
    por_oportunidad: dict[tuple[str, str], dict[str, Any]] = {}

    for m in matches:
        op: Licitacion | CompraAgil | None
        if m.fuente == "licitaciones":
            op = lics.get(m.codigo_oportunidad)
        else:
            op = cas.get(m.codigo_oportunidad)

        if op is None:
            continue

        existente = por_oportunidad.get((m.fuente, m.codigo_oportunidad))
        if existente is not None:
            existente["matches"].append(m)
            existente["perfiles"] = nombres_de_perfiles(existente["matches"], nombres_perfil)
            continue

        if solo_vigentes and not es_vigente(
            op.estado, op.fecha_cierre, m.fuente, ahora, op.fecha_publicacion
        ):
            continue

        feedback = feedback_map.get((m.fuente, m.codigo_oportunidad))
        if feedback is not None and feedback.valor == ValorFeedback.DESCARTE.value:
            continue

        item = _construir_item(
            m,
            op,
            feedback_valor=feedback.valor if feedback is not None else None,
            guardada=(m.fuente, m.codigo_oportunidad) in guardadas_set,
            ahora=ahora,
            perfiles=nombres_de_perfiles([m], nombres_perfil),
        )
        item["matches"] = [m]
        por_oportunidad[(m.fuente, m.codigo_oportunidad)] = item
        result.append(item)

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

    ahora = ahora_utc()
    result: list[dict[str, Any]] = []
    for s in seguidas:
        op: Licitacion | CompraAgil | None
        if s.fuente == "licitaciones":
            op = lics.get(s.codigo_oportunidad)
        else:
            op = cas.get(s.codigo_oportunidad)

        fecha_cierre = op.fecha_cierre if op is not None else None
        dias = None if fecha_cierre is None else max(
            0.0, (fecha_cierre - ahora).total_seconds() / 86400
        )
        result.append({
            "seguimiento": s,
            "nombre": op.nombre if op is not None else s.codigo_oportunidad,
            "estado": op.estado if op is not None else s.estado_visto,
            "fecha_cierre": fecha_cierre,
            # Mismo texto que la tarjeta y la ficha (F-coherencia).
            "cierre_texto": texto_cierre(fecha_cierre, dias, s.fuente),
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


# ---------------------------------------------------------------------------
# Mi registro (F-registro)
#
# Todo acotado por `user_id` (regla 17) y sin cargar el histórico de matches
# (regla 12): "vencidas recientes" se prefiltra en SQL por ventana de vencimiento.
# ---------------------------------------------------------------------------


def _cargar_ops(
    session: Session, fuente: str, codigos: list[str]
) -> dict[str, Licitacion | CompraAgil]:
    """Las oportunidades de esos códigos en una query, sin `raw_json` (pesado y
    que el registro no pinta)."""
    if not codigos:
        return {}
    modelo: type[Licitacion] | type[CompraAgil] = (
        Licitacion if fuente == "licitaciones" else CompraAgil
    )
    filas = session.execute(
        select(modelo).options(defer(modelo.raw_json)).where(modelo.codigo.in_(codigos))
    ).scalars()
    return {op.codigo: op for op in cast(Iterable[Licitacion | CompraAgil], filas)}


def _mejores_matches(
    session: Session, perfil_ids: list[int], fuente: str, codigos: list[str]
) -> dict[str, OportunidadMatch]:
    """El match de mayor score del usuario por código, solo para esos códigos."""
    if not codigos or not perfil_ids:
        return {}
    mejores: dict[str, OportunidadMatch] = {}
    filas = session.execute(
        select(OportunidadMatch)
        .where(
            OportunidadMatch.perfil_id.in_(perfil_ids),
            OportunidadMatch.fuente == fuente,
            OportunidadMatch.codigo_oportunidad.in_(codigos),
        )
        .order_by(OportunidadMatch.score.desc(), OportunidadMatch.id)
    ).scalars()
    for m in filas:
        mejores.setdefault(m.codigo_oportunidad, m)
    return mejores


def _item_sin_oportunidad(s: OportunidadSeguida) -> dict[str, Any]:
    """Guardada cuya oportunidad ya no existe: datos mínimos del seguimiento, sin romper."""
    return {
        "match": None,
        "oportunidad": None,
        "fuente": s.fuente,
        "codigo": s.codigo_oportunidad,
        "score": None,
        "nombre": s.codigo_oportunidad,
        "estado": s.estado_visto,
        "fecha_cierre": None,
        "fecha_publicacion": None,
        "dias_al_cierre": None,
        "monto": None,
        "organismo": None,
        "region": None,
        "region_nombre": None,
        "razones": [],
        "url_ficha": _url_ficha(s.fuente, s.codigo_oportunidad),
        "mostrar_ficha": False,
        "guardada": not s.archivada,
        "feedback": None,
        "perfiles": [],
    }


def listar_registro_guardadas(
    session: Session, user_id: int, *, archivadas: bool
) -> list[dict[str, Any]]:
    """Seguimientos del usuario como items de tarjeta, con o sin match.

    Parte de `oportunidades_seguidas` (una guardada puede no tener match: CA del
    explorador, o licitación cuyo match borró la limpieza) y carga en lote las
    oportunidades y los matches del usuario SOLO de esos códigos. Cada item trae
    `seguimiento`, `vigente` (`es_vigente`) y `fecha_vencimiento`. Orden: los más
    recientes primero (el llamador reordena por pestaña)."""
    seguidas = [
        s
        for s in listar_seguidas(session, user_id, incluir_archivadas=True)
        if s.archivada == archivadas
    ]
    perfil_ids = [p.id for p in listar_perfiles(session, user_id)]
    ahora = ahora_utc()
    ops: dict[str, dict[str, Licitacion | CompraAgil]] = {}
    matches: dict[str, dict[str, OportunidadMatch]] = {}
    for fuente in FUENTES_VALIDAS:
        codigos = [s.codigo_oportunidad for s in seguidas if s.fuente == fuente]
        ops[fuente] = _cargar_ops(session, fuente, codigos)
        matches[fuente] = _mejores_matches(session, perfil_ids, fuente, codigos)

    items: list[dict[str, Any]] = []
    for s in seguidas:
        op = ops.get(s.fuente, {}).get(s.codigo_oportunidad)
        if op is None:
            item = _item_sin_oportunidad(s)
            item["vigente"] = False
            item["fecha_vencimiento"] = None
        else:
            m = matches[s.fuente].get(s.codigo_oportunidad)
            item = _construir_item(
                m, op, feedback_valor=None, guardada=not s.archivada, ahora=ahora
            )
            item["vigente"] = es_vigente(
                op.estado, op.fecha_cierre, s.fuente, ahora, getattr(op, "fecha_publicacion", None)
            )
            item["fecha_vencimiento"] = fecha_vencimiento(op, s.fuente)
        item["seguimiento"] = s
        item["sin_match"] = item["match"] is None
        items.append(item)
    return items


def _vencidas_stmt(
    fuente: str,
    user_id: int,
    perfil_ids: list[int],
    ahora: datetime,
    dias: int,
    min_score: float,
) -> Any:
    """(codigo, score máximo) de las oportunidades con match del usuario que
    vencieron en los últimos `dias` días, no están guardadas (archivadas incluidas)
    ni descartadas, y cuyo mejor score alcanza `min_score`."""
    modelo: type[Licitacion] | type[CompraAgil] = (
        Licitacion if fuente == "licitaciones" else CompraAgil
    )
    guardada = exists().where(
        OportunidadSeguida.owner_id == user_id,
        OportunidadSeguida.fuente == fuente,
        OportunidadSeguida.codigo_oportunidad == modelo.codigo,
    )
    descartada = exists().where(
        MatchFeedback.usuario_id == user_id,
        MatchFeedback.fuente == fuente,
        MatchFeedback.codigo_oportunidad == modelo.codigo,
        MatchFeedback.valor == ValorFeedback.DESCARTE.value,
    )
    score = func.max(OportunidadMatch.score)
    return (
        select(modelo.codigo.label("codigo"), score.label("score"))
        .join(
            OportunidadMatch,
            and_(
                OportunidadMatch.fuente == fuente,
                OportunidadMatch.codigo_oportunidad == modelo.codigo,
            ),
        )
        .where(
            OportunidadMatch.perfil_id.in_(perfil_ids),
            condicion_vencida_en_ventana(fuente, ahora - timedelta(days=dias), ahora),
            ~guardada,
            ~descartada,
        )
        .group_by(modelo.codigo)
        .having(score >= min_score)
    )


def _dias_para_ocultar(vencimiento: datetime | None, ahora: datetime, dias: int) -> int:
    """Días enteros (>= 1) que le quedan a una vencida antes de ocultarse."""
    if vencimiento is None:
        return 1
    restante = (vencimiento + timedelta(days=dias) - ahora).total_seconds() / 86400
    return max(1, math.ceil(restante))


def listar_vencidas_recientes(
    session: Session, user_id: int, ahora: datetime, *, dias: int, min_score: float
) -> list[dict[str, Any]]:
    """Pestaña "Vencidas recientes": vencimiento más reciente primero. Cada item
    trae `score` (el máximo del usuario), `fecha_vencimiento` y `oculta_en_dias`
    (N >= 1; 1 = "se oculta hoy"). Nunca carga el histórico: el prefiltro es SQL."""
    perfil_ids = [p.id for p in listar_perfiles(session, user_id)]
    if not perfil_ids:
        return []
    items: list[dict[str, Any]] = []
    for fuente in FUENTES_VALIDAS:
        filas = session.execute(
            _vencidas_stmt(fuente, user_id, perfil_ids, ahora, dias, min_score)
        ).all()
        if not filas:
            continue
        codigos = [f.codigo for f in filas]
        puntajes = {f.codigo: float(f.score) for f in filas}
        ops = _cargar_ops(session, fuente, codigos)
        matches = _mejores_matches(session, perfil_ids, fuente, codigos)
        for codigo in codigos:
            op = ops.get(codigo)
            if op is None:
                continue
            if es_vigente(
                op.estado, op.fecha_cierre, fuente, ahora, getattr(op, "fecha_publicacion", None)
            ):
                continue
            item = _construir_item(
                matches.get(codigo),
                op,
                feedback_valor=None,
                guardada=False,
                ahora=ahora,
                score_max=puntajes[codigo],
            )
            venc = fecha_vencimiento(op, fuente)
            item["fecha_vencimiento"] = venc
            item["oculta_en_dias"] = _dias_para_ocultar(venc, ahora, dias)
            items.append(item)
    items.sort(key=lambda i: (i["fecha_vencimiento"] or datetime.min, i["codigo"]), reverse=True)
    return items


def contar_vencidas_recientes(
    session: Session, user_id: int, ahora: datetime, *, dias: int, min_score: float
) -> int:
    """La misma condición de `listar_vencidas_recientes` en un `count` (dashboard y
    etiqueta de la pestaña), sin traer filas."""
    perfil_ids = [p.id for p in listar_perfiles(session, user_id)]
    if not perfil_ids:
        return 0
    total = 0
    for fuente in FUENTES_VALIDAS:
        sub = _vencidas_stmt(fuente, user_id, perfil_ids, ahora, dias, min_score).subquery()
        total += int(session.execute(select(func.count()).select_from(sub)).scalar_one())
    return total


def resumen_competencia(session: Session, licitacion_codigo: str) -> list[dict[str, Any]]:
    """Resumen de competencia por proveedor (F-competencia), incluyendo a quienes
    ofertaron pero NO ganaron — panorama competitivo completo, no solo ganadores
    (ver deuda señalada en docs/archivo/00-estado-actual.md, resuelta en F10 parte 3).
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


@dataclass(frozen=True)
class ConteoPerfil:
    """Oportunidades de un perfil que siguen vigentes, por fuente, y cuándo entró
    el último match (cualquiera, vigente o no)."""

    licitaciones: int = 0
    compras_agiles: int = 0
    ultimo_match: datetime | None = None

    @property
    def total(self) -> int:
        return self.licitaciones + self.compras_agiles


def conteo_vigentes_por_perfil(
    session: Session, perfil_ids: list[int], ahora: datetime | None = None
) -> dict[int, ConteoPerfil]:
    """Vigentes por perfil en UNA consulta agrupada (no una por perfil): matches
    con join a licitaciones/compras ágiles y la definición de `vigencia.py`.
    Perfiles sin matches no aparecen en el resultado."""
    if not perfil_ids:
        return {}
    ahora = ahora or ahora_utc()
    es_lic = OportunidadMatch.fuente == "licitaciones"
    es_ca = OportunidadMatch.fuente == "compras_agiles"
    filas = session.execute(
        select(
            OportunidadMatch.perfil_id,
            func.count(case((and_(es_lic, condicion_lic_vigente(ahora)), 1))),
            func.count(case((and_(es_ca, condicion_ca_vigente(ahora)), 1))),
            func.max(OportunidadMatch.fecha_match),
        )
        .select_from(OportunidadMatch)
        .outerjoin(
            Licitacion, and_(es_lic, Licitacion.codigo == OportunidadMatch.codigo_oportunidad)
        )
        .outerjoin(
            CompraAgil, and_(es_ca, CompraAgil.codigo == OportunidadMatch.codigo_oportunidad)
        )
        .where(OportunidadMatch.perfil_id.in_(perfil_ids))
        .group_by(OportunidadMatch.perfil_id)
    ).all()
    return {pid: ConteoPerfil(int(n_lic), int(n_ca), ultimo) for pid, n_lic, n_ca, ultimo in filas}


def razones_sociales(session: Session, codigos: Iterable[str]) -> dict[str, str]:
    """Razón social por código de organismo (los numéricos, desde `instituciones_pac`).
    El código que no esté en el catálogo no aparece: el llamador muestra el código."""
    numericos = {int(c): c for c in codigos if str(c).strip().isdigit()}
    if not numericos:
        return {}
    filas = session.execute(
        select(InstitucionPAC.codigo_entidad, InstitucionPAC.razon_social).where(
            InstitucionPAC.codigo_entidad.in_(list(numericos))
        )
    ).all()
    return {numericos[cod]: " ".join(nombre.split()) for cod, nombre in filas if nombre.strip()}


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
        .order_by(OportunidadMatch.score.desc(), OportunidadMatch.id)
        .limit(1)
    ).scalar_one_or_none()


def puede_actuar(session: Session, user_id: int, fuente: str, codigo: str) -> bool:
    """Si el usuario puede abrir la ficha y guardar/descartar esta oportunidad (F-guardar).

    La oportunidad tiene que existir y, además: el usuario tiene un match con ella,
    o es una Compra Ágil (el explorador lista todas las vigentes), o ya la tiene
    guardada/descartada (p. ej. una licitación guardada cuyo match se limpió). Una
    licitación sin nada de eso sigue sin abrirse: no hay explorador de licitaciones.
    Los datos son públicos; lo que es del dueño (regla 17) son sus acciones, que
    siempre van filtradas por su `user_id`.
    """
    if fuente not in FUENTES_VALIDAS:
        return False
    modelo: type[Licitacion] | type[CompraAgil] = Licitacion if fuente == "licitaciones" else CompraAgil
    if session.get(modelo, codigo) is None:
        return False
    if fuente == "compras_agiles":
        return True
    if check_oportunidad_access(session, user_id, fuente, codigo) is not None:
        return True
    seguida = session.execute(
        select(OportunidadSeguida.id).where(
            OportunidadSeguida.owner_id == user_id,
            OportunidadSeguida.fuente == fuente,
            OportunidadSeguida.codigo_oportunidad == codigo,
        )
    ).first()
    if seguida is not None:
        return True
    feedback = session.execute(
        select(MatchFeedback.id).where(
            MatchFeedback.usuario_id == user_id,
            MatchFeedback.fuente == fuente,
            MatchFeedback.codigo_oportunidad == codigo,
        )
    ).first()
    return feedback is not None


def estado_acciones(session: Session, user_id: int, fuente: str, codigo: str) -> dict[str, Any] | None:
    """Lo que pintan los botones Guardar/Descartar de la ficha, con o sin match.
    None si la oportunidad no existe."""
    op: Licitacion | CompraAgil | None
    op = session.get(Licitacion, codigo) if fuente == "licitaciones" else session.get(CompraAgil, codigo)
    if op is None:
        return None
    seguimiento = obtener_seguimiento(session, user_id, fuente, codigo)
    feedback = obtener_feedback(session, user_id, fuente, codigo)
    return {
        "fuente": fuente,
        "codigo": codigo,
        "nombre": op.nombre,
        "guardada": seguimiento is not None and not seguimiento.archivada,
        "archivada": seguimiento is not None and seguimiento.archivada,
        "descartada": feedback is not None and feedback.valor == ValorFeedback.DESCARTE.value,
        "con_match": check_oportunidad_access(session, user_id, fuente, codigo) is not None,
    }


def matches_de_oportunidad(
    session: Session, user_id: int, fuente: str, codigo: str
) -> list[OportunidadMatch]:
    """Los matches de ESTE usuario con la oportunidad (uno por perfil activo):
    qué perfil y qué palabras la trajeron, para el modal "Descartar"."""
    perfil_ids = [p.id for p in listar_perfiles(session, user_id)]
    if not perfil_ids:
        return []
    return list(
        session.execute(
            select(OportunidadMatch)
            .where(
                OportunidadMatch.perfil_id.in_(perfil_ids),
                OportunidadMatch.fuente == fuente,
                OportunidadMatch.codigo_oportunidad == codigo,
            )
            .order_by(OportunidadMatch.score.desc())
        ).scalars()
    )


def contar_archivadas(session: Session, user_id: int) -> int:
    """Cuántas oportunidades archivadas tiene el usuario (etiqueta de la pestaña)."""
    return int(
        session.execute(
            select(func.count())
            .select_from(OportunidadSeguida)
            .where(OportunidadSeguida.owner_id == user_id, OportunidadSeguida.archivada.is_(True))
        ).scalar_one()
    )


def contar_descartadas(session: Session, user_id: int) -> int:
    """Cuántas oportunidades descartó el usuario: la misma condición que
    `listar_descartadas`, como `count` (etiqueta de la pestaña, sin traer filas)."""
    return int(
        session.execute(
            select(func.count())
            .select_from(MatchFeedback)
            .where(
                MatchFeedback.usuario_id == user_id,
                MatchFeedback.valor == ValorFeedback.DESCARTE.value,
            )
        ).scalar_one()
    )
