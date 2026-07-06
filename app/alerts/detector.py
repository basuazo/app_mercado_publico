"""Detección de eventos para alertas inmediatas de oportunidades seguidas."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.tables import Alerta, CompraAgil, Licitacion, OportunidadSeguida

_log = get_logger(__name__)

def _ahora_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _ya_existe_seguimiento(session: Session, seguimiento_id: int, tipo: str) -> bool:
    """True si ya hay alerta de ese tipo (pendiente o enviada) para el seguimiento."""
    from sqlalchemy import func

    count = session.execute(
        select(func.count()).select_from(Alerta).where(
            Alerta.seguimiento_id == seguimiento_id,
            Alerta.tipo == tipo,
            Alerta.estado.in_(["pendiente", "enviada"]),
        )
    ).scalar_one()
    return count > 0


def detectar_cambio_estado_seguidas(session: Session) -> int:
    """Crea alertas 'seguimiento_estado:<estado>' para seguimientos no archivados
    cuyo estado actual difiere de OportunidadSeguida.estado_visto.

    A diferencia de detectar_cambio_estado (que solo dispara para una lista
    acotada de estados notables), aquí CUALQUIER cambio de estado alerta: el
    usuario siguió esta oportunidad específica para no perderse su avance.
    Idempotente por construcción: estado_visto se actualiza en el mismo paso,
    así que una transición nunca genera dos alertas.
    """
    creados = 0

    rows_lic = list(
        session.execute(
            select(OportunidadSeguida, Licitacion.estado)
            .join(Licitacion, OportunidadSeguida.codigo_oportunidad == Licitacion.codigo)
            .where(
                OportunidadSeguida.fuente == "licitaciones",
                OportunidadSeguida.archivada.is_(False),
                Licitacion.estado != OportunidadSeguida.estado_visto,
            )
        ).all()
    )
    for seguimiento, estado_nuevo in rows_lic:
        session.add(Alerta(seguimiento_id=seguimiento.id, tipo=f"seguimiento_estado:{estado_nuevo}"))
        seguimiento.estado_visto = estado_nuevo
        seguimiento.actualizado_en = _ahora_utc()
        creados += 1

    rows_ca = list(
        session.execute(
            select(OportunidadSeguida, CompraAgil.estado)
            .join(CompraAgil, OportunidadSeguida.codigo_oportunidad == CompraAgil.codigo)
            .where(
                OportunidadSeguida.fuente == "compras_agiles",
                OportunidadSeguida.archivada.is_(False),
                CompraAgil.estado != OportunidadSeguida.estado_visto,
            )
        ).all()
    )
    for seguimiento, estado_nuevo in rows_ca:
        session.add(Alerta(seguimiento_id=seguimiento.id, tipo=f"seguimiento_estado:{estado_nuevo}"))
        seguimiento.estado_visto = estado_nuevo
        seguimiento.actualizado_en = _ahora_utc()
        creados += 1

    _log.info("detectar_cambio_estado_seguidas: %d alertas creadas", creados)
    return creados


def detectar_recordatorio_cierre_seguidas(
    session: Session,
    ahora: datetime | None = None,
) -> int:
    """Crea alertas 'seguimiento_cierre' para seguidas con cierre dentro de 48 h.

    Idempotente por seguimiento: no duplica si ya existe una alerta pendiente o
    enviada de cierre para esa oportunidad seguida.
    """
    if ahora is None:
        ahora = _ahora_utc()
    limite = ahora + timedelta(hours=48)
    creados = 0

    rows_lic = list(
        session.execute(
            select(OportunidadSeguida)
            .join(Licitacion, OportunidadSeguida.codigo_oportunidad == Licitacion.codigo)
            .where(
                OportunidadSeguida.fuente == "licitaciones",
                OportunidadSeguida.archivada.is_(False),
                Licitacion.fecha_cierre.between(ahora, limite),
            )
        ).scalars()
    )
    for seguimiento in rows_lic:
        if not _ya_existe_seguimiento(session, seguimiento.id, "seguimiento_cierre"):
            session.add(Alerta(seguimiento_id=seguimiento.id, tipo="seguimiento_cierre"))
            creados += 1

    rows_ca = list(
        session.execute(
            select(OportunidadSeguida)
            .join(CompraAgil, OportunidadSeguida.codigo_oportunidad == CompraAgil.codigo)
            .where(
                OportunidadSeguida.fuente == "compras_agiles",
                OportunidadSeguida.archivada.is_(False),
                CompraAgil.fecha_cierre.between(ahora, limite),
            )
        ).scalars()
    )
    for seguimiento in rows_ca:
        if not _ya_existe_seguimiento(session, seguimiento.id, "seguimiento_cierre"):
            session.add(Alerta(seguimiento_id=seguimiento.id, tipo="seguimiento_cierre"))
            creados += 1

    _log.info("detectar_recordatorio_cierre_seguidas: %d alertas creadas", creados)
    return creados
