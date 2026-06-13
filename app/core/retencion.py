"""Política de retención de datos — límite Neon 0.5 GB.

Regla: raw_json se guarda SOLO cuando la oportunidad tiene al menos un match.
purgar_terminales() limpia raw_json e items/productos de oportunidades terminales
antiguas, pero nunca toca filas vigentes ni matches con alertas pendientes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, text, update
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.enums import ESTADOS_TERMINALES
from app.models.tables import (
    Alerta,
    CaProducto,
    CompraAgil,
    Licitacion,
    LicitacionItem,
    OportunidadMatch,
)

_log = get_logger(__name__)


def purgar_terminales(session: Session, dias: int = 90) -> dict[str, int]:
    """Purga raw_json e items de oportunidades terminales con más de `dias` días sin actualizar.

    No toca oportunidades vigentes ni aquellas con alertas pendientes.
    Devuelve dict con conteos de filas afectadas por tipo.
    """
    corte = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=dias)
    estados_str = [e.value for e in ESTADOS_TERMINALES]

    # IDs de matches que tienen alertas pendientes → protegerlos
    matches_con_alerta_pendiente = select(Alerta.match_id).where(Alerta.estado == "pendiente")
    codigos_licitacion_protegidos = select(OportunidadMatch.codigo_oportunidad).where(
        OportunidadMatch.fuente == "licitaciones",
        OportunidadMatch.id.in_(matches_con_alerta_pendiente),
    )
    codigos_ca_protegidos = select(OportunidadMatch.codigo_oportunidad).where(
        OportunidadMatch.fuente == "compras_agiles",
        OportunidadMatch.id.in_(matches_con_alerta_pendiente),
    )

    # -- Licitaciones terminales antiguas --
    # Ejecutar UNA sola vez para evitar subquery duplicada en DELETE y UPDATE.
    codigos_lic = list(
        session.execute(
            select(Licitacion.codigo).where(
                Licitacion.estado.in_(estados_str),
                Licitacion.actualizado_en < corte,
                Licitacion.codigo.not_in(codigos_licitacion_protegidos),
            )
        ).scalars()
    )

    if codigos_lic:
        r = session.execute(
            delete(LicitacionItem).where(LicitacionItem.licitacion_codigo.in_(codigos_lic))
        )
        items_borrados: int = r.rowcount  # type: ignore[attr-defined]
        r = session.execute(
            update(Licitacion).where(Licitacion.codigo.in_(codigos_lic)).values(raw_json=None)
        )
        lic_purgadas: int = r.rowcount  # type: ignore[attr-defined]
    else:
        items_borrados = 0
        lic_purgadas = 0

    # -- Compras Ágiles terminales antiguas --
    codigos_ca = list(
        session.execute(
            select(CompraAgil.codigo).where(
                CompraAgil.estado.in_(estados_str),
                CompraAgil.actualizado_en < corte,
                CompraAgil.codigo.not_in(codigos_ca_protegidos),
            )
        ).scalars()
    )

    if codigos_ca:
        r = session.execute(delete(CaProducto).where(CaProducto.ca_codigo.in_(codigos_ca)))
        prods_borrados: int = r.rowcount  # type: ignore[attr-defined]
        r = session.execute(
            update(CompraAgil).where(CompraAgil.codigo.in_(codigos_ca)).values(raw_json=None)
        )
        ca_purgadas: int = r.rowcount  # type: ignore[attr-defined]
    else:
        prods_borrados = 0
        ca_purgadas = 0

    _log.info(
        "purgar_terminales(dias=%d): licitaciones=%d items=%d ca=%d productos=%d",
        dias,
        lic_purgadas,
        items_borrados,
        ca_purgadas,
        prods_borrados,
    )
    return {
        "licitaciones_purgadas": lic_purgadas,
        "items_borrados": items_borrados,
        "ca_purgadas": ca_purgadas,
        "productos_borrados": prods_borrados,
    }


def tamano_bd(session: Session) -> int | None:
    """Retorna el tamaño de la BD en bytes usando pg_database_size().

    Devuelve None si no está disponible (ej. SQLite en tests).
    """
    try:
        row = session.execute(text("SELECT pg_database_size(current_database())")).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None
