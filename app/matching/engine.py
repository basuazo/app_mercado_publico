"""Motor de matching: score, candidatos FTS y match_perfil/match_todos.

Arquitectura:
- relevancia (y _rubros_hit) son funciones puras sin DB.
- _candidatos_licitaciones/_candidatos_ca usan Postgres FTS (text() con bindparams).
- _hits_licitaciones/_hits_ca detectan, también con Postgres FTS y de forma
  set-based (una query por fuente, no por candidato), qué keywords matchean
  cada candidato. match_perfil las invoca una vez por fuente y reparte los
  resultados a _score_licitacion/_score_ca.
- match_perfil no llama a ningún cliente HTTP; devuelve sin_detalle para que
  el orchestrator decida qué detalles buscar respetando el presupuesto de cuota.

Invariante recall/score (F9c): la detección de keywords_hit usa la MISMA
tsquery por keyword (criterio en app.matching.text.keywords_validas) que el
recall de _candidatos_*. No existe cálculo por substring en Python — recall y
score quedan unificados en un solo motor (Postgres FTS 'spanish').
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import (
    String,
    and_,
    bindparam,
    delete,
    exists,
    func,
    literal_column,
    or_,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from app.core.logging import get_logger
from app.core.tiempo import ahora_utc
from app.core.vigencia import condicion_ca_vigente, condicion_lic_vigente
from app.matching.text import build_exclude_tsquery, build_tsquery, keywords_validas
from app.models.tables import (
    CaProducto,
    CompraAgil,
    InstitucionPAC,
    Licitacion,
    LicitacionItem,
    OportunidadMatch,
    PerfilBusqueda,
    Usuario,
)

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Funciones de scoring puras (sin BD — testables directamente)
# ---------------------------------------------------------------------------


# Relevancia (F-match-1): mide solo qué tan bien calza la oportunidad con las
# palabras, rubros y organismos del perfil. Urgencia y competencia NO suman: se
# ven en el orden y en la fecha. Constantes ajustables tras la simulación.
BASE_HIT_NOMBRE = 50.0  # >= 1 keyword con acierto en el nombre
BASE_HIT_OTRO_CAMPO = 35.0  # >= 1 keyword, pero solo en ítem/producto o en la descripción
BONUS_KEYWORD_ADICIONAL = 10.0  # por cada keyword distinta extra con acierto
TOPE_BONUS_KEYWORDS = 20.0
BONUS_RUBRO = 20.0
BONUS_ORGANISMO = 15.0
BASE_SIN_KEYWORD = 40.0  # sin keyword pero con rubro u organismo
BONUS_RUBRO_Y_ORGANISMO_SIN_KEYWORD = 15.0
TOPE_RELEVANCIA = 100.0


def relevancia(
    keywords_hit: list[str],
    hit_en_nombre: bool,
    rubro_hit: bool,
    organismo_seguido: bool,
) -> float:
    """Relevancia 0–100 de una oportunidad para un perfil (función pura).

    Con keywords: base 50 (acierto en el nombre) o 35 (solo en ítem/producto o en la
    descripción), +10 por cada keyword distinta adicional (máx. +20), +20 rubro,
    +15 organismo.
    Sin keywords: 40 si hay rubro u organismo (55 si están ambos); 0 si ninguno.
    """
    distintas = len(set(keywords_hit))
    if distintas:
        total = BASE_HIT_NOMBRE if hit_en_nombre else BASE_HIT_OTRO_CAMPO
        total += min(TOPE_BONUS_KEYWORDS, BONUS_KEYWORD_ADICIONAL * (distintas - 1))
        if rubro_hit:
            total += BONUS_RUBRO
        if organismo_seguido:
            total += BONUS_ORGANISMO
    elif rubro_hit or organismo_seguido:
        total = BASE_SIN_KEYWORD
        if rubro_hit and organismo_seguido:
            total += BONUS_RUBRO_Y_ORGANISMO_SIN_KEYWORD
    else:
        total = 0.0
    return min(TOPE_RELEVANCIA, total)


def normalizar_rut(rut: str | None) -> str:
    """RUT sin puntos, guion ni espacios y con la `k` en minúscula (para comparar)."""
    return re.sub(r"[.\-\s]", "", rut or "").lower()


def _rubros_hit(categorias_unspsc: list[str], codigos_producto: list[str]) -> list[str]:
    """Prefijos de categorias_unspsc que matchean algún codigo_producto (LIKE 'prefijo%')."""
    if not categorias_unspsc:
        return []
    return [
        prefijo
        for prefijo in categorias_unspsc
        if any(cp.startswith(prefijo) for cp in codigos_producto if cp)
    ]


# ---------------------------------------------------------------------------
# Fragmentos SQL para FTS (Postgres únicamente)
# Siempre usados como text().bindparams(q=...) — nunca interpolados.
# ---------------------------------------------------------------------------

def _tsq(param: str) -> str:
    """tsquery 'spanish' con unaccent, igual que el tsv (si no, 'reparación'→repar vs reparacion).
    `param` es un nombre de bindparam o columna fijos del código (':q', ':qx', 'kw.keyword'),
    nunca un dato de la persona."""
    return f"websearch_to_tsquery('spanish', inmutable_unaccent({param}))"


_FTS_LIC_INCLUDE = (
    f"(licitaciones.tsv @@ {_tsq(':q')} "
    "OR EXISTS ("
    "SELECT 1 FROM licitacion_items li "
    "WHERE li.licitacion_codigo = licitaciones.codigo "
    "AND to_tsvector('spanish', inmutable_unaccent(li.nombre)) "
    f"@@ {_tsq(':q')}))"
)
# Exclusión (F-match-1 §1.7): SOLO sobre el nombre. Una palabra excluida dice "no
# es de lo mío" y eso lo dice el título; un ítem suelto o la descripción
# ("no incluye arriendo") generaban falsos negativos. La inclusión no cambia.
_FTS_LIC_EXCLUDE = (
    "NOT (to_tsvector('spanish', inmutable_unaccent(coalesce(licitaciones.nombre, ''))) "
    f"@@ {_tsq(':qx')})"
)
# Texto de un producto de CA para FTS: nombre + descripción del comprador
# (F-detalles-match). La MISMA expresión en recall (INCLUDE/EXCLUDE) y en
# _HITS_CA_SQL, para no romper el invariante recall/score de F9c.
_FTS_CA_PRODUCTO = "to_tsvector('spanish', inmutable_unaccent(p.nombre || ' ' || p.descripcion))"

_FTS_CA_INCLUDE = (
    f"(compras_agiles.tsv @@ {_tsq(':q')} "
    "OR EXISTS ("
    "SELECT 1 FROM ca_productos p "
    "WHERE p.ca_codigo = compras_agiles.codigo "
    f"AND {_FTS_CA_PRODUCTO} "
    f"@@ {_tsq(':q')}))"
)
_FTS_CA_EXCLUDE = (
    "NOT (to_tsvector('spanish', inmutable_unaccent(coalesce(compras_agiles.nombre, ''))) "
    f"@@ {_tsq(':qx')})"
)


# ---------------------------------------------------------------------------
# Queries de candidatos (requieren Postgres con tsv GENERATED)
# ---------------------------------------------------------------------------

# Protege RAM en Render (512 MB): suficiente para detectar matches relevantes.
_MAX_CANDIDATOS = 500


def condicion_texto_ca(texto: str) -> Any:
    """`_FTS_CA_INCLUDE` con `texto` como parámetro: nombre/descripción O productos.
    La usa el Texto del explorador (F-guardar) para buscar igual que los perfiles."""
    return text(_FTS_CA_INCLUDE).bindparams(q=texto)


_CHOQUES_SQL = text(
    "SELECT DISTINCT p.palabra "
    "FROM unnest(:kws) AS k(keyword), unnest(:pals) AS p(palabra) "
    "WHERE to_tsvector('spanish', inmutable_unaccent(k.keyword)) "
    f"@@ {_tsq('p.palabra')}"
).bindparams(bindparam("kws", type_=ARRAY(String)), bindparam("pals", type_=ARRAY(String)))


def exclusiones_que_chocan(session: Session, keywords: list[str], palabras: list[str]) -> list[str]:
    """Las `palabras` a excluir cuya tsquery calza con alguna keyword del perfil
    (excluirlas sacaría justo lo que el perfil busca): "saludable" vs "salud" choca
    (misma raíz); "salud mental" vs "salud" no (exige ambas palabras). Mantiene el
    orden de `palabras`. Una sola query parametrizada, con la misma normalización
    (`inmutable_unaccent` + 'spanish') que el recall. En SQLite (tests sin
    Postgres) no hay FTS y devuelve `[]`."""
    if not keywords or not palabras or session.get_bind().dialect.name != "postgresql":
        return []
    chocan = set(session.execute(_CHOQUES_SQL, {"kws": list(keywords), "pals": list(palabras)}).scalars())
    return [p for p in palabras if p in chocan]


def _vigencia_lic(ahora: datetime) -> list[Any]:
    """La misma "vigente" que el feed y Explorar CA (`app/core/vigencia.py`, F-ajustes)."""
    return [condicion_lic_vigente(ahora)]


def _vigencia_ca(ahora: datetime) -> list[Any]:
    """Incluye el tope de 7 días para las CA sin cierre (`condicion_ca_vigente`)."""
    return [condicion_ca_vigente(ahora)]


def _rut_normalizado(col: Any) -> Any:
    """`normalizar_rut` en SQL: sin puntos, guion ni espacios, en minúscula."""
    return func.lower(func.replace(func.replace(func.replace(col, ".", ""), "-", ""), " ", ""))


def _criterio_lic(
    q: str | None,
    qx: str | None,
    categorias_unspsc: list[str] | None,
    organismos_seguidos: list[str] | None,
) -> list[Any]:
    """Inclusión (FTS OR rubro OR organismo; sin ninguno, todo pasa) y exclusión.
    Lo comparten el recall (`_candidatos_licitaciones`) y la limpieza."""
    conds: list[Any] = []
    inclusion: list[Any] = []
    if q:
        inclusion.append(text(_FTS_LIC_INCLUDE).bindparams(q=q))
    if categorias_unspsc:
        inclusion.append(
            exists().where(
                LicitacionItem.licitacion_codigo == Licitacion.codigo,
                or_(*[LicitacionItem.codigo_producto.like(f"{p}%") for p in categorias_unspsc]),
            )
        )
    if organismos_seguidos:
        inclusion.append(Licitacion.codigo_organismo.in_(organismos_seguidos))
    if inclusion:
        conds.append(or_(*inclusion))
    if qx:
        conds.append(text(_FTS_LIC_EXCLUDE).bindparams(qx=qx))
    return conds


def _criterio_ca(
    q: str | None,
    qx: str | None,
    categorias_unspsc: list[str] | None,
    organismos_seguidos: list[str] | None,
) -> list[Any]:
    """Análogo a `_criterio_lic` (rubro vía ca_productos, organismo vía organismo_rut).

    `organismos_seguidos` son RUT YA normalizados (`normalizar_rut`): la CA no trae
    `codigo_entidad`, así que el perfil se traduce antes con `instituciones_pac.rut`."""
    conds: list[Any] = []
    inclusion: list[Any] = []
    if q:
        inclusion.append(text(_FTS_CA_INCLUDE).bindparams(q=q))
    if categorias_unspsc:
        inclusion.append(
            exists().where(
                CaProducto.ca_codigo == CompraAgil.codigo,
                or_(*[CaProducto.codigo_producto.like(f"{p}%") for p in categorias_unspsc]),
            )
        )
    if organismos_seguidos:
        inclusion.append(_rut_normalizado(CompraAgil.organismo_rut).in_(organismos_seguidos))
    if inclusion:
        conds.append(or_(*inclusion))
    if qx:
        conds.append(text(_FTS_CA_EXCLUDE).bindparams(qx=qx))
    return conds


def _filtro_licitaciones(
    regiones: Sequence[int] | None, monto_min: float | None, monto_max: float | None
) -> list[Any]:
    """Región y monto de una licitación. Región o monto no informados PASAN (el
    match lleva `region_no_informada` / `monto_no_informado`)."""
    conds = _monto_pasa(Licitacion.monto_clp, monto_min, monto_max)
    if regiones:
        conds.append(or_(Licitacion.region.is_(None), Licitacion.region.in_(list(regiones))))
    return conds


def _filtro_ca(
    regiones: Sequence[int] | None, monto_min: float | None, monto_max: float | None
) -> list[Any]:
    """Región (solo CA, regla 7) y monto de una Compra Ágil; monto no informado pasa."""
    conds = _monto_pasa(CompraAgil.monto_disponible_clp, monto_min, monto_max)
    if regiones:
        conds.append(CompraAgil.region.in_(list(regiones)))
    return conds


def _candidatos_licitaciones(
    session: Session,
    ahora: datetime,
    q: str | None,
    qx: str | None,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
    regiones: Sequence[int] | None = None,
    monto_min: float | None = None,
    monto_max: float | None = None,
) -> list[Licitacion]:
    """Candidatos por FTS, OR'd con recall aditivo de rubro UNSPSC y organismo seguido.

    Si no hay keywords (q=None) ni rubros/organismos, no se aplica filtro de
    inclusión (se conservan todas las licitaciones activas, como antes de F9b).
    Región y monto se filtran en SQL ANTES del tope de 500 (mismas condiciones
    que la limpieza), para que el tope no deje fuera lo que sí calza.
    """
    stmt = (
        select(Licitacion)
        .options(selectinload(Licitacion.items))
        .where(*_vigencia_lic(ahora))
        .where(*_criterio_lic(q, qx, categorias_unspsc, organismos_seguidos))
        .where(*_filtro_licitaciones(regiones, monto_min, monto_max))
        .order_by(Licitacion.fecha_cierre.asc().nulls_last(), Licitacion.codigo)
        .limit(_MAX_CANDIDATOS)
    )
    return list(session.execute(stmt).scalars())


def _candidatos_ca(
    session: Session,
    ahora: datetime,
    q: str | None,
    qx: str | None,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
    regiones: Sequence[int] | None = None,
    monto_min: float | None = None,
    monto_max: float | None = None,
) -> list[CompraAgil]:
    """Análogo a _candidatos_licitaciones para Compra Ágil (rubro vía ca_productos,
    organismo vía RUT normalizado: `organismos_seguidos` son RUT ya normalizados)."""
    stmt = (
        select(CompraAgil)
        .options(selectinload(CompraAgil.productos))
        .where(*_vigencia_ca(ahora))
        .where(*_criterio_ca(q, qx, categorias_unspsc, organismos_seguidos))
        .where(*_filtro_ca(regiones, monto_min, monto_max))
        .order_by(CompraAgil.fecha_cierre.asc().nulls_last(), CompraAgil.codigo)
        .limit(_MAX_CANDIDATOS)
    )
    return list(session.execute(stmt).scalars())


# ---------------------------------------------------------------------------
# Limpieza de matches que ya no calzan (F-guardar)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CriterioPerfil:
    """El criterio actual de un perfil, ya convertido a tsquery/listas."""

    perfil_id: int
    fuentes: tuple[str, ...]
    q: str | None
    qx: str | None
    categorias_unspsc: tuple[str, ...]
    organismos_seguidos: tuple[str, ...]  # codigo_entidad, tal como los guarda el perfil
    organismos_ruts: tuple[str, ...]  # los mismos organismos como RUT normalizado (CA)
    regiones: tuple[int, ...]
    monto_min: float | None
    monto_max: float | None


def ruts_de_organismos(session: Session, codigos: Sequence[str]) -> list[str]:
    """RUT normalizado de cada `codigo_entidad` del perfil, desde `instituciones_pac`
    (la CA identifica al organismo por RUT, no por código). Un código sin RUT en el
    catálogo no aporta nada."""
    numericos = [int(c) for c in codigos if str(c).strip().isdigit()]
    if not numericos:
        return []
    filas = session.execute(
        select(InstitucionPAC.rut).where(
            InstitucionPAC.codigo_entidad.in_(numericos), InstitucionPAC.rut.is_not(None)
        )
    ).scalars()
    return sorted({r for r in (normalizar_rut(x) for x in filas) if r})


def criterio_perfil(
    session: Session, perfil: PerfilBusqueda, excluir_extra: list[str] | None = None
) -> CriterioPerfil:
    """Criterio del perfil; `excluir_extra` suma exclusiones sin escribir nada
    (vista previa de "Descartar y excluir"). Lee `instituciones_pac` para traducir
    los organismos seguidos a RUT."""
    keywords = cast(list[str], list(perfil.keywords or []))
    excluir = cast(list[str], list(perfil.keywords_excluir or [])) + list(excluir_extra or [])
    organismos = cast(list[str], list(perfil.organismos_seguidos or []))
    return CriterioPerfil(
        perfil_id=perfil.id,
        fuentes=tuple(cast(list[str], list(perfil.fuentes or ["licitaciones", "compras_agiles"]))),
        q=build_tsquery(keywords) if keywords_validas(keywords) else None,
        qx=build_exclude_tsquery(excluir) if keywords_validas(excluir) else None,
        categorias_unspsc=tuple(cast(list[str], list(perfil.categorias_unspsc or []))),
        organismos_seguidos=tuple(organismos),
        organismos_ruts=tuple(ruts_de_organismos(session, organismos)),
        regiones=tuple(cast(list[int], list(perfil.regiones or []))),
        monto_min=perfil.monto_min_clp,
        monto_max=perfil.monto_max_clp,
    )


def _monto_pasa(col: Any, monto_min: float | None, monto_max: float | None) -> list[Any]:
    """Mismo filtro de monto que match_perfil: monto no informado pasa."""
    rango: list[Any] = []
    if monto_min is not None:
        rango.append(col >= monto_min)
    if monto_max is not None:
        rango.append(col <= monto_max)
    if not rango:
        return []
    return [or_(col.is_(None), and_(*rango))]


def _where_limpieza(c: CriterioPerfil, fuente: str, ahora: datetime) -> list[Any]:
    """WHERE sobre oportunidades_match: matches de ese perfil y fuente cuya
    oportunidad está vigente y NO pasa el criterio actual.

    Región y monto usan los mismos fragmentos que el recall (`_filtro_*`).
    "No pasa" = no está en el conjunto que el recall devolvería sin el tope de
    500 (mismos fragmentos de `_criterio_*`). Se expresa como NOT IN del
    conjunto que calza, no como NOT (criterio): así un NULL (organismo o región
    no informados) se trata igual que en el recall, donde un WHERE NULL no entra.
    """
    if fuente == "licitaciones":
        vigentes = select(Licitacion.codigo).where(*_vigencia_lic(ahora))
        calzan = select(Licitacion.codigo).where(
            *_vigencia_lic(ahora),
            *_criterio_lic(c.q, c.qx, list(c.categorias_unspsc), list(c.organismos_seguidos)),
            *_filtro_licitaciones(c.regiones, c.monto_min, c.monto_max),
        )
    else:
        vigentes = select(CompraAgil.codigo).where(*_vigencia_ca(ahora))
        calzan = select(CompraAgil.codigo).where(
            *_vigencia_ca(ahora),
            *_criterio_ca(c.q, c.qx, list(c.categorias_unspsc), list(c.organismos_ruts)),
            *_filtro_ca(c.regiones, c.monto_min, c.monto_max),
        )
    conds: list[Any] = [
        OportunidadMatch.perfil_id == c.perfil_id,
        OportunidadMatch.fuente == fuente,
        OportunidadMatch.codigo_oportunidad.in_(vigentes),
    ]
    # Fuente que el perfil ya no mira: nada de esa fuente calza.
    if fuente in c.fuentes:
        conds.append(OportunidadMatch.codigo_oportunidad.not_in(calzan))
    return conds


_FUENTES = ("licitaciones", "compras_agiles")


def contar_limpieza(session: Session, criterio: CriterioPerfil, ahora: datetime | None = None) -> int:
    """Cuántos matches borraría `limpiar_matches_perfil` con ese criterio (sin escribir)."""
    ahora = ahora or ahora_utc()
    total = 0
    for fuente in _FUENTES:
        total += int(
            session.execute(
                select(func.count())
                .select_from(OportunidadMatch)
                .where(*_where_limpieza(criterio, fuente, ahora))
            ).scalar_one()
        )
    return total


def limpiar_matches_perfil(
    session: Session, perfil: PerfilBusqueda, ahora: datetime | None = None
) -> int:
    """Borra los matches de ESE perfil cuya oportunidad sigue vigente pero ya no
    pasa su criterio actual (keyword quitada, exclusión agregada, región, monto,
    rubro, organismo o fuente cambiados). Devuelve cuántos borró. La exclusión se
    evalúa solo sobre el título (F-match-1): un ítem o la descripción con la
    palabra excluida no hacen borrar el match.

    Un DELETE por fuente, con los mismos fragmentos SQL del recall y sin el tope
    de 500. No toca matches de oportunidades terminales/vencidas (historial,
    competencia), ni seguidas, ni feedback; las alertas del match se van por
    cascade. "Vigente" es la regla oficial (`app/core/vigencia.py`): los matches de
    CA que dejan de serlo (p. ej. sin cierre y publicadas hace más de 7 días) NO se
    borran; quedan fuera del alcance de la limpieza como cualquier no vigente.
    Si una keyword se quita y se vuelve a poner, el match se recrea con
    `fecha_match` nueva y reaparece en el resumen (aceptado). No hace commit.
    """
    ahora = ahora or ahora_utc()
    criterio = criterio_perfil(session, perfil)
    borrados = 0
    for fuente in _FUENTES:
        r = session.execute(
            delete(OportunidadMatch)
            .where(*_where_limpieza(criterio, fuente, ahora))
            .execution_options(synchronize_session=False)
        )
        borrados += int(r.rowcount or 0)  # type: ignore[attr-defined]
    if borrados:
        _log.info("limpiar_matches_perfil id=%d: borrados=%d", perfil.id, borrados)
    return borrados


# ---------------------------------------------------------------------------
# Detección set-based de keywords_hit (Postgres FTS — misma tsquery que el recall)
# ---------------------------------------------------------------------------

# Para cada (codigo, keyword) se evalúa el hit por campo en una CTE y luego se
# agrega por codigo: una sola query por fuente cubre todos los candidatos y
# todas las keywords del perfil (sin N+1).
_HITS_LIC_SQL = text(
    f"""
    WITH pares AS (
        SELECT
            l.codigo AS codigo,
            kw.keyword AS keyword,
            to_tsvector('spanish', inmutable_unaccent(coalesce(l.nombre, '')))
                @@ {_tsq('kw.keyword')} AS hit_nombre,
            to_tsvector('spanish', inmutable_unaccent(coalesce(l.descripcion, '')))
                @@ {_tsq('kw.keyword')} AS hit_descripcion,
            EXISTS (
                SELECT 1 FROM licitacion_items li
                WHERE li.licitacion_codigo = l.codigo
                AND to_tsvector('spanish', inmutable_unaccent(li.nombre))
                    @@ {_tsq('kw.keyword')}
            ) AS hit_producto
        FROM licitaciones l
        CROSS JOIN unnest(:keywords) AS kw(keyword)
        WHERE l.codigo = ANY(:codigos)
    )
    SELECT
        codigo,
        array_agg(keyword) FILTER (WHERE hit_nombre OR hit_descripcion OR hit_producto)
            AS keywords_hit,
        bool_or(hit_nombre) AS hit_nombre,
        bool_or(hit_descripcion) AS hit_descripcion,
        bool_or(hit_producto) AS hit_producto
    FROM pares
    GROUP BY codigo
    """
).bindparams(
    bindparam("keywords", type_=ARRAY(String)),
    bindparam("codigos", type_=ARRAY(String)),
)

_HITS_CA_SQL = text(
    f"""
    WITH pares AS (
        SELECT
            c.codigo AS codigo,
            kw.keyword AS keyword,
            to_tsvector('spanish', inmutable_unaccent(coalesce(c.nombre, '')))
                @@ {_tsq('kw.keyword')} AS hit_nombre,
            to_tsvector('spanish', inmutable_unaccent(coalesce(c.descripcion, '')))
                @@ {_tsq('kw.keyword')} AS hit_descripcion,
            EXISTS (
                SELECT 1 FROM ca_productos p
                WHERE p.ca_codigo = c.codigo
                AND {_FTS_CA_PRODUCTO}
                    @@ {_tsq('kw.keyword')}
            ) AS hit_producto
        FROM compras_agiles c
        CROSS JOIN unnest(:keywords) AS kw(keyword)
        WHERE c.codigo = ANY(:codigos)
    )
    SELECT
        codigo,
        array_agg(keyword) FILTER (WHERE hit_nombre OR hit_descripcion OR hit_producto)
            AS keywords_hit,
        bool_or(hit_nombre) AS hit_nombre,
        bool_or(hit_descripcion) AS hit_descripcion,
        bool_or(hit_producto) AS hit_producto
    FROM pares
    GROUP BY codigo
    """
).bindparams(
    bindparam("keywords", type_=ARRAY(String)),
    bindparam("codigos", type_=ARRAY(String)),
)

# Resultado por candidato sin ningún keyword_hit (perfil sin keywords, o
# candidato que entró solo por rubro/organismo seguido).
# El tercer valor es `hit_en_nombre` (base 50 de `relevancia`; si no, 35).
_SIN_HITS: tuple[list[str], str, bool] = ([], "desconocido", False)


def _campo_hit(hit_nombre: bool, hit_descripcion: bool, hit_producto: bool) -> str:
    """Precedencia nombre > descripcion > producto > desconocido."""
    if hit_nombre:
        return "nombre"
    if hit_descripcion:
        return "descripcion"
    if hit_producto:
        return "producto"
    return "desconocido"


def _hits_licitaciones(
    session: Session, codigos: list[str], keywords: list[str]
) -> dict[str, tuple[list[str], str, bool]]:
    """keywords_hit + campo_hit + hit_en_nombre por licitación, en una sola query set-based."""
    if not codigos or not keywords:
        return {}
    filas = session.execute(_HITS_LIC_SQL, {"codigos": codigos, "keywords": keywords}).all()
    return {
        codigo: (
            list(keywords_hit or []),
            _campo_hit(hit_nombre, hit_descripcion, hit_producto),
            bool(hit_nombre),
        )
        for codigo, keywords_hit, hit_nombre, hit_descripcion, hit_producto in filas
    }


def _hits_ca(
    session: Session, codigos: list[str], keywords: list[str]
) -> dict[str, tuple[list[str], str, bool]]:
    """keywords_hit + campo_hit + hit_en_nombre por Compra Ágil, en una sola query set-based."""
    if not codigos or not keywords:
        return {}
    filas = session.execute(_HITS_CA_SQL, {"codigos": codigos, "keywords": keywords}).all()
    return {
        codigo: (
            list(keywords_hit or []),
            _campo_hit(hit_nombre, hit_descripcion, hit_producto),
            bool(hit_nombre),
        )
        for codigo, keywords_hit, hit_nombre, hit_descripcion, hit_producto in filas
    }


# ---------------------------------------------------------------------------
# Upsert de matches
# ---------------------------------------------------------------------------


def _upsert_match(
    session: Session,
    perfil_id: int,
    fuente: str,
    codigo: str,
    score: float,
    razones: dict[str, Any],
    ahora: datetime,
) -> bool:
    """Upsert de OportunidadMatch. Retorna True si es nuevo.

    `fecha_match` significa "primera vez que este perfil matcheó esta
    oportunidad". Por eso solo se setea en INSERT; los re-matches actualizan
    score/razones, pero no re-tocan la fecha. El resumen de descubrimiento usa
    esa fecha para decidir qué oportunidades son realmente nuevas.
    """
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        # Una sola sentencia (sin SELECT previo). `fecha_match` NO está en el
        # SET: solo se fija al insertar. El WHERE evita reescribir filas sin
        # cambios; en ese caso no hay RETURNING y se trata como "no nuevo".
        ins = pg_insert(OportunidadMatch).values(
            perfil_id=perfil_id,
            fuente=fuente,
            codigo_oportunidad=codigo,
            score=score,
            razones=razones,
            fecha_match=ahora,
        )
        upsert: Any = ins.on_conflict_do_update(
            constraint="uq_match",
            set_={"score": ins.excluded.score, "razones": ins.excluded.razones},
            where=or_(
                OportunidadMatch.score.is_distinct_from(ins.excluded.score),
                OportunidadMatch.razones.is_distinct_from(ins.excluded.razones),
            ),
        ).returning(literal_column("(xmax = 0)").label("insertado"))
        fila = session.execute(upsert).first()
        return bool(fila is not None and fila.insertado)

    # SQLite (tests): camino SELECT + INSERT/UPDATE.
    existing = session.execute(
        select(OportunidadMatch).where(
            OportunidadMatch.perfil_id == perfil_id,
            OportunidadMatch.fuente == fuente,
            OportunidadMatch.codigo_oportunidad == codigo,
        )
    ).scalar_one_or_none()

    if existing is None:
        session.add(
            OportunidadMatch(
                perfil_id=perfil_id,
                fuente=fuente,
                codigo_oportunidad=codigo,
                score=score,
                razones=razones,
                fecha_match=ahora,
            )
        )
        return True

    existing.score = score
    existing.razones = razones
    return False


# ---------------------------------------------------------------------------
# Scoring por oportunidad
# ---------------------------------------------------------------------------


def _hit_nombre(campo_hit: str, hit_en_nombre: bool | None) -> bool:
    """Si el llamador no lo informa, se deduce de `campo_hit` (nombre tiene la
    máxima precedencia, así que `campo_hit == "nombre"` es exacto)."""
    return campo_hit == "nombre" if hit_en_nombre is None else hit_en_nombre


def _razones(
    keywords_hit: list[str],
    campo_hit: str,
    hit_en_nombre: bool,
    categorias_hit: list[str],
    organismo_seguido: bool,
    ofertas: int | None,
) -> dict[str, Any]:
    """`razones` del match. Sin `dias_al_cierre`: cambiaba en cada ciclo y hacía que
    el WHERE del upsert nunca ahorrara una escritura; la UI lo calcula de `fecha_cierre`."""
    razones: dict[str, Any] = {
        "keywords_hit": keywords_hit,
        "campo_hit": campo_hit,
        "hit_en_nombre": hit_en_nombre,
        "ofertas": ofertas,
    }
    if categorias_hit:
        razones["categorias_hit"] = categorias_hit
    if organismo_seguido:
        razones["organismo_seguido"] = True
    return razones


def _score_licitacion(
    lic: Licitacion,
    keywords_hit: list[str],
    campo_hit: str,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
    hit_en_nombre: bool | None = None,
) -> tuple[float, dict[str, Any]]:
    """Relevancia (`relevancia`) y razones de una licitación.

    keywords_hit/campo_hit vienen de _hits_licitaciones (FTS set-based) —
    misma tsquery que decidió el recall (invariante F9c).
    """
    hit_n = _hit_nombre(campo_hit, hit_en_nombre)
    categorias_hit = _rubros_hit(categorias_unspsc or [], [i.codigo_producto for i in lic.items])
    organismo_seguido = bool(lic.codigo_organismo) and lic.codigo_organismo in (
        organismos_seguidos or []
    )
    total = relevancia(keywords_hit, hit_n, bool(categorias_hit), organismo_seguido)
    return total, _razones(keywords_hit, campo_hit, hit_n, categorias_hit, organismo_seguido, None)


def _score_ca(
    ca: CompraAgil,
    keywords_hit: list[str],
    campo_hit: str,
    categorias_unspsc: list[str] | None = None,
    organismos_ruts: list[str] | None = None,
    hit_en_nombre: bool | None = None,
) -> tuple[float, dict[str, Any]]:
    """Análogo a _score_licitacion para Compra Ágil. `organismos_ruts` son los RUT de
    los organismos seguidos; se comparan normalizados (con o sin puntos y guion)."""
    hit_n = _hit_nombre(campo_hit, hit_en_nombre)
    categorias_hit = _rubros_hit(
        categorias_unspsc or [], [p.codigo_producto for p in ca.productos]
    )
    rut = normalizar_rut(ca.organismo_rut)
    organismo_seguido = bool(rut) and rut in {normalizar_rut(r) for r in organismos_ruts or []}
    total = relevancia(keywords_hit, hit_n, bool(categorias_hit), organismo_seguido)
    return total, _razones(
        keywords_hit, campo_hit, hit_n, categorias_hit, organismo_seguido, ca.total_ofertas
    )


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def match_perfil(
    perfil: PerfilBusqueda,
    session: Session,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """Ejecuta matching para un perfil. No llama a clientes HTTP.

    Devuelve conteos y listas sin_detalle_* para que el orchestrator
    decida qué detalles buscar respetando el presupuesto de cuota. Al final
    borra los matches vigentes que ya no calzan (`limpiar_matches_perfil`,
    F-guardar) y los cuenta en `borrados`.
    """
    if ahora is None:
        ahora = ahora_utc()

    criterio = criterio_perfil(session, perfil)
    kws_validas = keywords_validas(cast(list[str], list(perfil.keywords or [])))
    categorias = list(criterio.categorias_unspsc)
    ruts = list(criterio.organismos_ruts)

    nuevos = actualizados = 0
    sin_detalle_lic: list[str] = []
    sin_detalle_ca: list[str] = []

    if "licitaciones" in criterio.fuentes:
        lics = _candidatos_licitaciones(
            session,
            ahora,
            criterio.q,
            criterio.qx,
            categorias,
            list(criterio.organismos_seguidos),
            criterio.regiones,
            criterio.monto_min,
            criterio.monto_max,
        )
        hits_lic = _hits_licitaciones(session, [lic.codigo for lic in lics], kws_validas)
        for lic in lics:
            kw_hit, campo_hit, hit_n = hits_lic.get(lic.codigo, _SIN_HITS)
            sc, razones = _score_licitacion(
                lic, kw_hit, campo_hit, categorias, list(criterio.organismos_seguidos), hit_n
            )
            if lic.monto_clp is None:
                razones["monto_no_informado"] = True
            if criterio.regiones and lic.region is None:
                razones["region_no_informada"] = True

            es_nuevo = _upsert_match(
                session, perfil.id, "licitaciones", lic.codigo, sc, razones, ahora
            )
            nuevos += es_nuevo
            actualizados += not es_nuevo
            if lic.raw_json is None:
                sin_detalle_lic.append(lic.codigo)

    if "compras_agiles" in criterio.fuentes:
        cas = _candidatos_ca(
            session,
            ahora,
            criterio.q,
            criterio.qx,
            categorias,
            ruts,
            criterio.regiones,
            criterio.monto_min,
            criterio.monto_max,
        )
        hits_ca = _hits_ca(session, [ca.codigo for ca in cas], kws_validas)
        for ca in cas:
            kw_hit_ca, campo_hit_ca, hit_ni_ca = hits_ca.get(ca.codigo, _SIN_HITS)
            sc, razones = _score_ca(ca, kw_hit_ca, campo_hit_ca, categorias, ruts, hit_ni_ca)
            if ca.monto_disponible_clp is None:
                razones["monto_no_informado"] = True

            es_nuevo = _upsert_match(
                session, perfil.id, "compras_agiles", ca.codigo, sc, razones, ahora
            )
            nuevos += es_nuevo
            actualizados += not es_nuevo
            if ca.raw_json is None:
                sin_detalle_ca.append(ca.codigo)

    borrados = limpiar_matches_perfil(session, perfil, ahora)
    session.commit()
    _log.info(
        "match_perfil id=%d: nuevos=%d act=%d borrados=%d",
        perfil.id,
        nuevos,
        actualizados,
        borrados,
    )
    return {
        "nuevos": nuevos,
        "actualizados": actualizados,
        "borrados": borrados,
        "sin_detalle_licitaciones": sin_detalle_lic,
        "sin_detalle_ca": sin_detalle_ca,
    }


def match_todos(
    session: Session,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """Ejecuta match_perfil para todos los perfiles activos de usuarios activos."""
    perfiles = list(
        session.execute(
            select(PerfilBusqueda)
            .join(PerfilBusqueda.owner)
            .where(
                PerfilBusqueda.activo.is_(True),
                Usuario.activo.is_(True),
            )
        ).scalars()
    )

    total_nuevos = total_act = total_borrados = 0
    all_sin_lic: list[str] = []
    all_sin_ca: list[str] = []

    for perfil in perfiles:
        # El id se lee antes: tras el rollback el objeto queda expirado y leerlo
        # de nuevo puede fallar (o pegarle a la BD).
        perfil_id = perfil.id
        try:
            r = match_perfil(perfil, session, ahora)
            total_nuevos += r["nuevos"]
            total_act += r["actualizados"]
            total_borrados += r.get("borrados", 0)
            all_sin_lic.extend(r["sin_detalle_licitaciones"])
            all_sin_ca.extend(r["sin_detalle_ca"])
        except Exception:
            # Sin rollback, una sesión abortada (p. ej. un error de Postgres)
            # haría fallar a todos los perfiles siguientes.
            session.rollback()
            _log.error("match_todos: error en perfil_id=%d", perfil_id, exc_info=True)

    _log.info(
        "match_todos: perfiles=%d nuevos=%d act=%d borrados=%d",
        len(perfiles),
        total_nuevos,
        total_act,
        total_borrados,
    )
    return {
        "perfiles_procesados": len(perfiles),
        "nuevos": total_nuevos,
        "actualizados": total_act,
        "borrados": total_borrados,
        "sin_detalle_licitaciones": list(set(all_sin_lic)),
        "sin_detalle_ca": list(set(all_sin_ca)),
    }
