"""Sincronización semanal de catálogos: organismos."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.clients.mp_v1 import MercadoPublicoV1Client
from app.core.logging import get_logger
from app.models.tables import Organismo

_log = get_logger(__name__)


def _ahora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def refresh_organismos(
    session: Session,
    v1_client: MercadoPublicoV1Client,
) -> dict[str, int]:
    """Actualiza el catálogo de organismos compradores (consume 1 request).

    Idempotente: upsert por código.
    """
    compradores = v1_client.listar_compradores()
    nuevos = actualizados = 0

    for c in compradores:
        if not c.codigo:
            continue
        existing = session.get(Organismo, c.codigo)
        if existing is None:
            session.add(
                Organismo(
                    codigo=c.codigo,
                    nombre=c.nombre,
                    rut=c.rut,
                    actualizado_en=_ahora(),
                )
            )
            nuevos += 1
        else:
            existing.nombre = c.nombre
            existing.rut = c.rut
            existing.actualizado_en = _ahora()
            actualizados += 1

    session.commit()
    _log.info("refresh_organismos: %d nuevos, %d actualizados", nuevos, actualizados)
    return {"nuevos": nuevos, "actualizados": actualizados}
