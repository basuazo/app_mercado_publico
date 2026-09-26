"""Búsqueda inversa en el Plan Anual de Compra (F-plan-busqueda): "qué se
compra" en vez de "qué organismo". Postgres FTS sobre `descripcion_producto`,
mismo motor que app.matching (websearch_to_tsquery + inmutable_unaccent), pero
en un módulo propio: app/matching/* es de la fase paralela F-ca-rubro y no se
toca acá (ver docs/prompt-F-plan-busqueda.md).

`FiltrosPlanBusqueda.q_include`/`q_exclude` ya vienen como tsquery-string listos
para `websearch_to_tsquery('spanish', :q)` — este módulo no decide CÓMO se
arma esa query (texto libre vs keywords de perfil), solo la aplica. Quien
llama:
- Texto libre (una sola caja de búsqueda): pasa el texto tal cual como
  `q_include` — `websearch_to_tsquery` ya entiende comillas de frase, "OR" y el
  prefijo "-" de exclusión nativamente, sin necesidad de un parser propio.
- "Para mis perfiles": arma `q_include`/`q_exclude` con
  app.matching.text.build_tsquery/build_exclude_tsquery (reusadas tal cual,
  mismo criterio que el matching engine — solo se importan, no se modifican).

Siempre `text(...).bindparams(...)` — nunca interpolado (regla de arquitectura).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

from app.models.tables import InstitucionPAC, PlanCompraLinea

# Misma expresión que el índice GIN de la migración d7f2a4c8b6e1 — tienen que
# calzar exactamente para que Postgres use el índice en vez de recalcular un
# seq scan.
_TSV_DESCRIPCION = "to_tsvector('spanish', inmutable_unaccent(plan_compra_lineas.descripcion_producto))"
_FTS_INCLUDE = f"({_TSV_DESCRIPCION} @@ websearch_to_tsquery('spanish', :q))"
_FTS_EXCLUDE = f"(NOT {_TSV_DESCRIPCION} @@ websearch_to_tsquery('spanish', :qx))"
_TS_RANK = f"ts_rank({_TSV_DESCRIPCION}, websearch_to_tsquery('spanish', :q))"

# Protege RAM en Render (la ruta web corre ahí, regla 12): la agregación "por
# organismo" nunca devuelve más de esto (hay ~962 instituciones en el PAC
# completo, así que este tope no debería alcanzarse en la práctica).
_MAX_ORGANISMOS = 500
_EXPORT_MAX_FILAS = 5000


@dataclass
class FiltrosPlanBusqueda:
    agno: int
    organismos: list[int] | None = None
    sector: str | None = None
    desde_mes: int | None = None
    monto_min: float | None = None
    monto_max: float | None = None
    q_include: str | None = None
    q_exclude: str | None = None


@dataclass
class ResultadoOrganismo:
    codigo_entidad: int
    institucion_nombre: str
    n_lineas: int
    monto_total: float
    meses: list[int]


def _condiciones_base(filtros: FiltrosPlanBusqueda) -> list[Any]:
    conds: list[Any] = [PlanCompraLinea.agno == filtros.agno]
    if filtros.organismos:
        conds.append(PlanCompraLinea.codigo_entidad.in_(filtros.organismos))
    if filtros.desde_mes is not None:
        # Mes desconocido (NULL) se mantiene visible: no sabemos si ya pasó
        # (regla 6, no descartar silenciosamente por falta de dato).
        conds.append(
            (PlanCompraLinea.mes_estimado.is_(None))
            | (PlanCompraLinea.mes_estimado >= filtros.desde_mes)
        )
    if filtros.monto_min is not None:
        conds.append(PlanCompraLinea.monto_estimado_clp >= filtros.monto_min)
    if filtros.monto_max is not None:
        conds.append(PlanCompraLinea.monto_estimado_clp <= filtros.monto_max)
    if filtros.sector:
        subq = select(InstitucionPAC.codigo_entidad).where(InstitucionPAC.sector == filtros.sector)
        conds.append(PlanCompraLinea.codigo_entidad.in_(subq))
    return conds


def _aplicar_fts(stmt: Any, filtros: FiltrosPlanBusqueda) -> Any:
    if filtros.q_include:
        stmt = stmt.where(text(_FTS_INCLUDE).bindparams(q=filtros.q_include))
    if filtros.q_exclude:
        stmt = stmt.where(text(_FTS_EXCLUDE).bindparams(qx=filtros.q_exclude))
    return stmt


def buscar_por_organismo(session: Session, filtros: FiltrosPlanBusqueda) -> list[ResultadoOrganismo]:
    """Agregado por institución: n.º de líneas, monto total y meses involucrados.

    Agregación en SQL (no en Python): con ~300k filas del año, traer los
    candidatos a memoria para agrupar a mano violaría la regla 12 (Render
    512 MB) en la ruta web.
    """
    stmt = (
        select(
            PlanCompraLinea.codigo_entidad,
            PlanCompraLinea.institucion_nombre,
            func.count().label("n_lineas"),
            func.sum(PlanCompraLinea.monto_estimado_clp).label("monto_total"),
            func.array_agg(func.distinct(PlanCompraLinea.mes_estimado)).label("meses"),
        )
        .where(*_condiciones_base(filtros))
        .group_by(PlanCompraLinea.codigo_entidad, PlanCompraLinea.institucion_nombre)
        .order_by(func.sum(PlanCompraLinea.monto_estimado_clp).desc().nulls_last())
        .limit(_MAX_ORGANISMOS)
    )
    stmt = _aplicar_fts(stmt, filtros)
    filas = session.execute(stmt).all()
    return [
        ResultadoOrganismo(
            codigo_entidad=f.codigo_entidad,
            institucion_nombre=f.institucion_nombre,
            n_lineas=f.n_lineas,
            monto_total=f.monto_total or 0.0,
            meses=sorted(m for m in (f.meses or []) if m is not None),
        )
        for f in filas
    ]


def contar_plan(session: Session, filtros: FiltrosPlanBusqueda) -> dict[str, Any]:
    """Conteo en vivo (HTMX): solo números, sin filas — respeta todos los filtros."""
    stmt = select(
        func.count(),
        func.sum(PlanCompraLinea.monto_estimado_clp),
        func.count(func.distinct(PlanCompraLinea.codigo_entidad)),
    ).where(*_condiciones_base(filtros))
    stmt = _aplicar_fts(stmt, filtros)
    n_lineas, monto_total, n_organismos = session.execute(stmt).one()
    return {
        "n_lineas": n_lineas or 0,
        "monto_total": monto_total or 0.0,
        "n_organismos": n_organismos or 0,
    }


def buscar_lineas(
    session: Session,
    filtros: FiltrosPlanBusqueda,
    *,
    orden: str = "relevancia",
    pagina: int = 1,
    page_size: int = 50,
) -> tuple[list[PlanCompraLinea], int]:
    """Vista "Líneas": paginada en SQL (LIMIT/OFFSET), nunca trae todo a memoria."""
    conds = _condiciones_base(filtros)

    total = session.execute(_aplicar_fts(select(func.count()).where(*conds), filtros)).scalar_one()

    stmt = _aplicar_fts(select(PlanCompraLinea).where(*conds), filtros)
    if orden == "monto":
        stmt = stmt.order_by(PlanCompraLinea.monto_estimado_clp.desc().nulls_last())
    elif orden == "mes":
        stmt = stmt.order_by(PlanCompraLinea.mes_estimado.asc().nulls_last())
    elif filtros.q_include:
        stmt = stmt.order_by(desc(text(_TS_RANK).bindparams(q=filtros.q_include)))
    else:
        stmt = stmt.order_by(PlanCompraLinea.monto_estimado_clp.desc().nulls_last())

    pagina = max(1, pagina)
    stmt = stmt.offset((pagina - 1) * page_size).limit(page_size)
    lineas = list(session.execute(stmt).scalars())
    return lineas, total


def lineas_para_exportar(session: Session, filtros: FiltrosPlanBusqueda) -> list[PlanCompraLinea]:
    """Hasta `_EXPORT_MAX_FILAS` líneas para el CSV, en el mismo orden que la
    vista de líneas por defecto (monto descendente)."""
    stmt = _aplicar_fts(select(PlanCompraLinea).where(*_condiciones_base(filtros)), filtros)
    stmt = stmt.order_by(PlanCompraLinea.monto_estimado_clp.desc().nulls_last()).limit(_EXPORT_MAX_FILAS)
    return list(session.execute(stmt).scalars())
