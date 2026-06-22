"""Motor de matching: score, candidatos FTS y match_perfil/match_todos.

Arquitectura:
- score_texto, score_urgencia, score_competencia son funciones puras sin DB.
- _candidatos_licitaciones/_candidatos_ca usan Postgres FTS (text() con bindparams).
- match_perfil no llama a ningún cliente HTTP; devuelve sin_detalle para que
  el orchestrator decida qué detalles buscar respetando el presupuesto de cuota.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import exists, or_, select, text
from sqlalchemy.orm import Session, selectinload

from app.core.logging import get_logger
from app.matching.text import build_exclude_tsquery, build_tsquery
from app.models.enums import EstadoOportunidad
from app.models.tables import (
    CaProducto,
    CompraAgil,
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


def score_texto(
    keywords: list[str],
    keywords_hit: list[str],
    hit_en_nombre: bool,
) -> float:
    """Score de relevancia textual, 0–60.

    proporcion_hit × 60 + bonus_nombre (5 si hit en nombre).
    Capped a 60.
    """
    if not keywords:
        return 0.0
    prop = len(keywords_hit) / len(keywords)
    bonus = 5.0 if hit_en_nombre else 0.0
    return min(60.0, prop * 60.0 + bonus)


def score_urgencia(dias_al_cierre: float) -> float:
    """Score de urgencia: 25 si 2–7 días, 10 si 8–30 días, 0 en otro caso."""
    if 2.0 <= dias_al_cierre <= 7.0:
        return 25.0
    if 8.0 <= dias_al_cierre <= 30.0:
        return 10.0
    return 0.0


def score_competencia(fuente: str, total_ofertas: int) -> float:
    """Score de competencia: 15/10/5 para CA según ofertas; 8 (neutro) para licitaciones."""
    if fuente == "compras_agiles":
        if total_ofertas == 0:
            return 15.0
        if total_ofertas <= 3:
            return 10.0
        return 5.0
    return 8.0


def score_estructural(rubro_hit: bool, organismo_seguido: bool) -> float:
    """Score por recall aditivo (F9b): +20 si hubo hit de rubro UNSPSC seguido,
    +15 si la oportunidad es de un organismo seguido. Permite que un match
    rubro/organismo-only (sin keywords) puntúe de forma razonable."""
    total = 0.0
    if rubro_hit:
        total += 20.0
    if organismo_seguido:
        total += 15.0
    return total


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
# Normalización para keyword matching en Python (tilde-tolerante)
# ---------------------------------------------------------------------------


def _norm(s: str) -> str:
    """Minúsculas + quitar diacríticos (NFD + filtrar categoría Mn)."""
    nfd = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn")


def _kw_bare(kw: str) -> str:
    """Quita comillas de frases y normaliza."""
    return _norm(kw.strip().strip('"'))


def _keywords_en_textos(keywords: list[str], textos: list[str]) -> list[str]:
    """Devuelve las keywords que aparecen en cualquiera de los textos (normalizado)."""
    textos_norm = [_norm(t) for t in textos if t]
    return [kw for kw in keywords if any(_kw_bare(kw) in t for t in textos_norm)]


# ---------------------------------------------------------------------------
# Fragmentos SQL para FTS (Postgres únicamente)
# Siempre usados como text().bindparams(q=...) — nunca interpolados.
# ---------------------------------------------------------------------------

_FTS_LIC_INCLUDE = (
    "licitaciones.tsv @@ websearch_to_tsquery('spanish', :q) "
    "OR EXISTS ("
    "SELECT 1 FROM licitacion_items li "
    "WHERE li.licitacion_codigo = licitaciones.codigo "
    "AND to_tsvector('spanish', inmutable_unaccent(li.nombre)) "
    "@@ websearch_to_tsquery('spanish', :q))"
)
_FTS_LIC_EXCLUDE = (
    "NOT (licitaciones.tsv @@ websearch_to_tsquery('spanish', :qx) "
    "OR EXISTS ("
    "SELECT 1 FROM licitacion_items li "
    "WHERE li.licitacion_codigo = licitaciones.codigo "
    "AND to_tsvector('spanish', inmutable_unaccent(li.nombre)) "
    "@@ websearch_to_tsquery('spanish', :qx)))"
)
_FTS_CA_INCLUDE = (
    "compras_agiles.tsv @@ websearch_to_tsquery('spanish', :q) "
    "OR EXISTS ("
    "SELECT 1 FROM ca_productos p "
    "WHERE p.ca_codigo = compras_agiles.codigo "
    "AND to_tsvector('spanish', inmutable_unaccent(p.nombre)) "
    "@@ websearch_to_tsquery('spanish', :q))"
)
_FTS_CA_EXCLUDE = (
    "NOT (compras_agiles.tsv @@ websearch_to_tsquery('spanish', :qx) "
    "OR EXISTS ("
    "SELECT 1 FROM ca_productos p "
    "WHERE p.ca_codigo = compras_agiles.codigo "
    "AND to_tsvector('spanish', inmutable_unaccent(p.nombre)) "
    "@@ websearch_to_tsquery('spanish', :qx)))"
)


# ---------------------------------------------------------------------------
# Queries de candidatos (requieren Postgres con tsv GENERATED)
# ---------------------------------------------------------------------------

# Protege RAM en Render (512 MB): suficiente para detectar matches relevantes.
_MAX_CANDIDATOS = 500


def _candidatos_licitaciones(
    session: Session,
    ahora: datetime,
    q: str | None,
    qx: str | None,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
) -> list[Licitacion]:
    """Candidatos por FTS, OR'd con recall aditivo de rubro UNSPSC y organismo seguido.

    Si no hay keywords (q=None) ni rubros/organismos, no se aplica filtro de
    inclusión (se conservan todas las licitaciones activas, como antes de F9b):
    el filtrado por región/monto sigue ocurriendo localmente en match_perfil.
    """
    stmt = (
        select(Licitacion)
        .options(selectinload(Licitacion.items))
        .where(
            Licitacion.estado == EstadoOportunidad.PUBLICADA.value,
            Licitacion.fecha_cierre > ahora,
        )
    )
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
        stmt = stmt.where(or_(*inclusion))
    if qx:
        stmt = stmt.where(text(_FTS_LIC_EXCLUDE).bindparams(qx=qx))
    stmt = stmt.limit(_MAX_CANDIDATOS)
    return list(session.execute(stmt).scalars())


def _candidatos_ca(
    session: Session,
    ahora: datetime,
    q: str | None,
    qx: str | None,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
) -> list[CompraAgil]:
    """Análogo a _candidatos_licitaciones para Compra Ágil (rubro vía ca_productos,
    organismo vía organismo_rut)."""
    stmt = (
        select(CompraAgil)
        .options(selectinload(CompraAgil.productos))
        .where(
            CompraAgil.estado == EstadoOportunidad.PUBLICADA.value,
            CompraAgil.fecha_cierre > ahora,
        )
    )
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
        inclusion.append(CompraAgil.organismo_rut.in_(organismos_seguidos))
    if inclusion:
        stmt = stmt.where(or_(*inclusion))
    if qx:
        stmt = stmt.where(text(_FTS_CA_EXCLUDE).bindparams(qx=qx))
    stmt = stmt.limit(_MAX_CANDIDATOS)
    return list(session.execute(stmt).scalars())


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
    """Upsert de OportunidadMatch. Retorna True si es nuevo."""
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
    existing.fecha_match = ahora
    return False


# ---------------------------------------------------------------------------
# Scoring por oportunidad
# ---------------------------------------------------------------------------


def _score_licitacion(
    lic: Licitacion,
    keywords: list[str],
    ahora: datetime,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
) -> tuple[float, dict[str, Any]]:
    nombres_items = [i.nombre for i in lic.items]
    kw_hit_nombre = _keywords_en_textos(keywords, [lic.nombre])
    kw_hit_desc = _keywords_en_textos(keywords, [lic.descripcion])
    kw_hit_prod = _keywords_en_textos(keywords, nombres_items)
    kw_hit = list(set(kw_hit_nombre) | set(kw_hit_desc) | set(kw_hit_prod))

    hit_en_nombre = bool(kw_hit_nombre)
    if kw_hit_nombre:
        campo_hit = "nombre"
    elif kw_hit_desc:
        campo_hit = "descripcion"
    elif kw_hit_prod:
        campo_hit = "producto"
    else:
        campo_hit = "desconocido"

    dias = 0.0
    if lic.fecha_cierre:
        delta = lic.fecha_cierre - ahora
        dias = max(0.0, delta.total_seconds() / 86400.0)

    categorias_hit = _rubros_hit(categorias_unspsc or [], [i.codigo_producto for i in lic.items])
    organismo_seguido = bool(lic.codigo_organismo) and lic.codigo_organismo in (
        organismos_seguidos or []
    )

    st = score_texto(keywords, kw_hit, hit_en_nombre)
    su = score_urgencia(dias)
    sc = score_competencia("licitaciones", 0)
    se = score_estructural(bool(categorias_hit), organismo_seguido)
    total = min(100.0, st + su + sc + se)

    razones: dict[str, Any] = {
        "keywords_hit": kw_hit,
        "campo_hit": campo_hit,
        "dias_al_cierre": round(dias, 1),
        "ofertas": None,
    }
    if categorias_hit:
        razones["categorias_hit"] = categorias_hit
    if organismo_seguido:
        razones["organismo_seguido"] = True
    return total, razones


def _score_ca(
    ca: CompraAgil,
    keywords: list[str],
    ahora: datetime,
    categorias_unspsc: list[str] | None = None,
    organismos_seguidos: list[str] | None = None,
) -> tuple[float, dict[str, Any]]:
    nombres_prods = [p.nombre for p in ca.productos]
    kw_hit_nombre = _keywords_en_textos(keywords, [ca.nombre])
    kw_hit_desc = _keywords_en_textos(keywords, [ca.descripcion])
    kw_hit_prod = _keywords_en_textos(keywords, nombres_prods)
    kw_hit = list(set(kw_hit_nombre) | set(kw_hit_desc) | set(kw_hit_prod))

    hit_en_nombre = bool(kw_hit_nombre)
    if kw_hit_nombre:
        campo_hit = "nombre"
    elif kw_hit_desc:
        campo_hit = "descripcion"
    elif kw_hit_prod:
        campo_hit = "producto"
    else:
        campo_hit = "desconocido"

    dias = 0.0
    if ca.fecha_cierre:
        delta = ca.fecha_cierre - ahora
        dias = max(0.0, delta.total_seconds() / 86400.0)

    categorias_hit = _rubros_hit(
        categorias_unspsc or [], [p.codigo_producto for p in ca.productos]
    )
    organismo_seguido = bool(ca.organismo_rut) and ca.organismo_rut in (organismos_seguidos or [])

    st = score_texto(keywords, kw_hit, hit_en_nombre)
    su = score_urgencia(dias)
    sc = score_competencia("compras_agiles", ca.total_ofertas)
    se = score_estructural(bool(categorias_hit), organismo_seguido)
    total = min(100.0, st + su + sc + se)

    razones: dict[str, Any] = {
        "keywords_hit": kw_hit,
        "campo_hit": campo_hit,
        "dias_al_cierre": round(dias, 1),
        "ofertas": ca.total_ofertas,
    }
    if categorias_hit:
        razones["categorias_hit"] = categorias_hit
    if organismo_seguido:
        razones["organismo_seguido"] = True
    return total, razones


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
    decida qué detalles buscar respetando el presupuesto de cuota.
    """
    if ahora is None:
        ahora = datetime.now(UTC).replace(tzinfo=None)

    keywords = cast(list[str], list(perfil.keywords or []))
    keywords_excluir = cast(list[str], list(perfil.keywords_excluir or []))
    fuentes = cast(list[str], list(perfil.fuentes or ["licitaciones", "compras_agiles"]))
    regiones = cast(list[int], list(perfil.regiones or []))
    categorias_unspsc = cast(list[str], list(perfil.categorias_unspsc or []))
    organismos_seguidos = cast(list[str], list(perfil.organismos_seguidos or []))

    q = build_tsquery(keywords) if keywords else None
    qx = build_exclude_tsquery(keywords_excluir) if keywords_excluir else None

    nuevos = actualizados = descartados = 0
    sin_detalle_lic: list[str] = []
    sin_detalle_ca: list[str] = []

    if "licitaciones" in fuentes:
        lics = _candidatos_licitaciones(
            session, ahora, q, qx, categorias_unspsc, organismos_seguidos
        )
        for lic in lics:
            # Filtro de monto (local)
            monto = lic.monto_clp
            razones_extra: dict[str, Any] = {}
            if monto is not None:
                if perfil.monto_min_clp is not None and monto < perfil.monto_min_clp:
                    descartados += 1
                    continue
                if perfil.monto_max_clp is not None and monto > perfil.monto_max_clp:
                    descartados += 1
                    continue
            else:
                razones_extra["monto_no_informado"] = True

            sc, razones = _score_licitacion(
                lic, keywords, ahora, categorias_unspsc, organismos_seguidos
            )
            razones.update(razones_extra)

            es_nuevo = _upsert_match(
                session, perfil.id, "licitaciones", lic.codigo, sc, razones, ahora
            )
            nuevos += es_nuevo
            actualizados += not es_nuevo
            if lic.raw_json is None:
                sin_detalle_lic.append(lic.codigo)

    if "compras_agiles" in fuentes:
        cas = _candidatos_ca(session, ahora, q, qx, categorias_unspsc, organismos_seguidos)
        for ca in cas:
            # Filtro de región (solo CA, local — spec regla 7)
            if regiones and ca.region not in regiones:
                descartados += 1
                continue
            # Filtro de monto (local)
            monto_ca = ca.monto_disponible_clp
            razones_extra_ca: dict[str, Any] = {}
            if monto_ca is not None:
                if perfil.monto_min_clp is not None and monto_ca < perfil.monto_min_clp:
                    descartados += 1
                    continue
                if perfil.monto_max_clp is not None and monto_ca > perfil.monto_max_clp:
                    descartados += 1
                    continue
            else:
                razones_extra_ca["monto_no_informado"] = True

            sc, razones = _score_ca(ca, keywords, ahora, categorias_unspsc, organismos_seguidos)
            razones.update(razones_extra_ca)

            es_nuevo = _upsert_match(
                session, perfil.id, "compras_agiles", ca.codigo, sc, razones, ahora
            )
            nuevos += es_nuevo
            actualizados += not es_nuevo
            if ca.raw_json is None:
                sin_detalle_ca.append(ca.codigo)

    session.commit()
    _log.info(
        "match_perfil id=%d: nuevos=%d act=%d desc=%d",
        perfil.id,
        nuevos,
        actualizados,
        descartados,
    )
    return {
        "nuevos": nuevos,
        "actualizados": actualizados,
        "descartados": descartados,
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

    total_nuevos = total_act = total_desc = 0
    all_sin_lic: list[str] = []
    all_sin_ca: list[str] = []

    for perfil in perfiles:
        try:
            r = match_perfil(perfil, session, ahora)
            total_nuevos += r["nuevos"]
            total_act += r["actualizados"]
            total_desc += r["descartados"]
            all_sin_lic.extend(r["sin_detalle_licitaciones"])
            all_sin_ca.extend(r["sin_detalle_ca"])
        except Exception:
            _log.error("match_todos: error en perfil_id=%d", perfil.id, exc_info=True)

    _log.info(
        "match_todos: perfiles=%d nuevos=%d act=%d desc=%d",
        len(perfiles),
        total_nuevos,
        total_act,
        total_desc,
    )
    return {
        "perfiles_procesados": len(perfiles),
        "nuevos": total_nuevos,
        "actualizados": total_act,
        "descartados": total_desc,
        "sin_detalle_licitaciones": list(set(all_sin_lic)),
        "sin_detalle_ca": list(set(all_sin_ca)),
    }
