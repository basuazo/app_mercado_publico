"""CRUD de oportunidades seguidas, con ownership obligatorio (regla 17 CLAUDE.md).

Un usuario solo ve/edita/archiva sus propios seguimientos. Seguir es idempotente:
si ya existe un seguimiento (incluso archivado), no se duplica — se reactiva.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.tables import OportunidadSeguida

_log = get_logger(__name__)


def obtener_seguimiento(
    session: Session,
    owner_id: int,
    fuente: str,
    codigo: str,
) -> OportunidadSeguida | None:
    """Retorna el seguimiento del owner para esa oportunidad, o None si no existe."""
    return session.execute(
        select(OportunidadSeguida).where(
            OportunidadSeguida.owner_id == owner_id,
            OportunidadSeguida.fuente == fuente,
            OportunidadSeguida.codigo_oportunidad == codigo,
        )
    ).scalar_one_or_none()


def seguir_oportunidad(
    session: Session,
    owner_id: int,
    fuente: str,
    codigo: str,
    estado_actual: str,
) -> OportunidadSeguida:
    """Crea el seguimiento si no existe; si ya existe (incluso archivado), lo
    reactiva sin duplicarlo ni resetear estado_visto."""
    existing = obtener_seguimiento(session, owner_id, fuente, codigo)
    if existing is not None:
        existing.archivada = False
        return existing
    seguimiento = OportunidadSeguida(
        owner_id=owner_id,
        fuente=fuente,
        codigo_oportunidad=codigo,
        estado_visto=estado_actual,
        archivada=False,
    )
    session.add(seguimiento)
    session.flush()
    return seguimiento


def archivar_seguimiento(
    session: Session,
    owner_id: int,
    fuente: str,
    codigo: str,
    *,
    archivada: bool,
) -> bool:
    """Marca archivada=True/False. Retorna False si no existe o no es del owner."""
    s = obtener_seguimiento(session, owner_id, fuente, codigo)
    if s is None:
        return False
    s.archivada = archivada
    return True


def dejar_de_seguir(
    session: Session,
    owner_id: int,
    fuente: str,
    codigo: str,
) -> bool:
    """Elimina el seguimiento. Retorna False si no existe o no es del owner."""
    s = obtener_seguimiento(session, owner_id, fuente, codigo)
    if s is None:
        return False
    session.delete(s)
    return True


def listar_seguidas(
    session: Session,
    owner_id: int,
    *,
    incluir_archivadas: bool = False,
) -> list[OportunidadSeguida]:
    """Devuelve los seguimientos del usuario, más recientes primero."""
    stmt = select(OportunidadSeguida).where(OportunidadSeguida.owner_id == owner_id)
    if not incluir_archivadas:
        stmt = stmt.where(OportunidadSeguida.archivada.is_(False))
    stmt = stmt.order_by(OportunidadSeguida.creado_en.desc())
    return list(session.execute(stmt).scalars())
