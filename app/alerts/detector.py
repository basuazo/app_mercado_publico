"""Detección de eventos para alertas de Mercado Público.

Tres tipos de eventos:
- nuevo_match: primera vez que un perfil captura una oportunidad.
- cambio_estado:<estado>: estado de la oportunidad cambió a uno notable.
- recordatorio_cierre: fecha_cierre dentro de las próximas 48 h.

Deduplicación a nivel aplicación: antes de crear la alerta se verifica
que no exista ya una (pendiente o enviada) del mismo tipo para el mismo match.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import not_, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.tables import Alerta, CompraAgil, Licitacion, OportunidadMatch

_log = get_logger(__name__)

# Estados que generan alerta de cambio
_ESTADOS_NOTIFICAR: frozenset[str] = frozenset(
    {"cerrada", "adjudicada", "cancelada", "proveedor_seleccionado"}
)


def _ahora_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _ya_existe(session: Session, match_id: int, tipo: str) -> bool:
    """True si ya hay alerta de ese tipo (pendiente o enviada) para el match."""
    from sqlalchemy import func

    count = session.execute(
        select(func.count()).select_from(Alerta).where(
            Alerta.match_id == match_id,
            Alerta.tipo == tipo,
            Alerta.estado.in_(["pendiente", "enviada"]),
        )
    ).scalar_one()
    return count > 0


def detectar_nuevo_match(session: Session) -> int:
    """Crea alertas 'nuevo_match' para matches sin alerta existente de ese tipo.

    Usa NOT EXISTS en SQL para eficiencia; idempotente dentro de la transacción.
    Retorna cantidad de alertas creadas.
    """
    any_sub = (
        select(Alerta.id)
        .where(
            Alerta.match_id == OportunidadMatch.id,
            Alerta.tipo == "nuevo_match",
        )
        .exists()
    )
    matches = list(
        session.execute(select(OportunidadMatch).where(~any_sub)).scalars()
    )
    for m in matches:
        session.add(Alerta(match_id=m.id, tipo="nuevo_match"))
    _log.info("detectar_nuevo_match: %d alertas creadas", len(matches))
    return len(matches)


def detectar_cambio_estado(session: Session) -> int:
    """Crea alertas 'cambio_estado:<estado>' para oportunidades en estado notable.

    El tipo codifica el estado concreto para que cada transición genere
    a lo sumo una alerta. No emite duplicados si ya existe alerta del mismo tipo.
    """
    creados = 0

    # Licitaciones
    rows_lic = list(
        session.execute(
            select(OportunidadMatch, Licitacion.estado)
            .join(Licitacion, OportunidadMatch.codigo_oportunidad == Licitacion.codigo)
            .where(
                OportunidadMatch.fuente == "licitaciones",
                Licitacion.estado.in_(list(_ESTADOS_NOTIFICAR)),
            )
        ).all()
    )
    for match, estado in rows_lic:
        tipo = f"cambio_estado:{estado}"
        if not _ya_existe(session, match.id, tipo):
            session.add(Alerta(match_id=match.id, tipo=tipo))
            creados += 1

    # Compras Ágiles
    rows_ca = list(
        session.execute(
            select(OportunidadMatch, CompraAgil.estado)
            .join(CompraAgil, OportunidadMatch.codigo_oportunidad == CompraAgil.codigo)
            .where(
                OportunidadMatch.fuente == "compras_agiles",
                CompraAgil.estado.in_(list(_ESTADOS_NOTIFICAR)),
            )
        ).all()
    )
    for match, estado in rows_ca:
        tipo = f"cambio_estado:{estado}"
        if not _ya_existe(session, match.id, tipo):
            session.add(Alerta(match_id=match.id, tipo=tipo))
            creados += 1

    _log.info("detectar_cambio_estado: %d alertas creadas", creados)
    return creados


def detectar_recordatorios(
    session: Session,
    ahora: datetime | None = None,
) -> int:
    """Crea alertas 'recordatorio_cierre' para oportunidades con cierre ≤ 48 h.

    Solo si no existe ya una alerta de ese tipo (pendiente o enviada) para el match.
    """
    if ahora is None:
        ahora = _ahora_utc()
    limite = ahora + timedelta(hours=48)

    creados = 0

    # Licitaciones
    matches_lic = list(
        session.execute(
            select(OportunidadMatch)
            .join(Licitacion, OportunidadMatch.codigo_oportunidad == Licitacion.codigo)
            .where(
                OportunidadMatch.fuente == "licitaciones",
                Licitacion.fecha_cierre.between(ahora, limite),
                not_(
                    select(Alerta.id)
                    .where(
                        Alerta.match_id == OportunidadMatch.id,
                        Alerta.tipo == "recordatorio_cierre",
                    )
                    .exists()
                ),
            )
        ).scalars()
    )
    for m in matches_lic:
        session.add(Alerta(match_id=m.id, tipo="recordatorio_cierre"))
        creados += 1

    # Compras Ágiles
    matches_ca = list(
        session.execute(
            select(OportunidadMatch)
            .join(CompraAgil, OportunidadMatch.codigo_oportunidad == CompraAgil.codigo)
            .where(
                OportunidadMatch.fuente == "compras_agiles",
                CompraAgil.fecha_cierre.between(ahora, limite),
                not_(
                    select(Alerta.id)
                    .where(
                        Alerta.match_id == OportunidadMatch.id,
                        Alerta.tipo == "recordatorio_cierre",
                    )
                    .exists()
                ),
            )
        ).scalars()
    )
    for m in matches_ca:
        session.add(Alerta(match_id=m.id, tipo="recordatorio_cierre"))
        creados += 1

    _log.info("detectar_recordatorios: %d alertas creadas", creados)
    return creados
