"""CRUD de oportunidades seguidas, con ownership obligatorio (regla 17 CLAUDE.md).

Un usuario solo ve/edita/archiva sus propios seguimientos. Seguir es idempotente:
si ya existe un seguimiento (incluso archivado), no se duplica — se reactiva.

F-guardar: "Guardar" ES seguir. Guardada = OportunidadSeguida con
archivada=False: queda en el registro del usuario y le avisa de sus cambios. Guardar
y descartar se excluyen: guardar borra el descarte y descartar quita de guardadas
(ver `app.matching.feedback.descartar`).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.enums import ValorFeedback
from app.models.tables import CompraAgil, Licitacion, MatchFeedback, OportunidadSeguida

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


def estado_actual(session: Session, fuente: str, codigo: str) -> str:
    """Estado actual de la oportunidad ('' si no existe): el `estado_visto` con
    que nace una guardada, para que guardar no dispare una alerta de cambio."""
    op: Licitacion | CompraAgil | None
    op = session.get(Licitacion, codigo) if fuente == "licitaciones" else session.get(CompraAgil, codigo)
    return op.estado if op is not None else ""


def _borrar_descarte(session: Session, owner_id: int, fuente: str, codigo: str) -> None:
    fb = session.execute(
        select(MatchFeedback).where(
            MatchFeedback.usuario_id == owner_id,
            MatchFeedback.fuente == fuente,
            MatchFeedback.codigo_oportunidad == codigo,
            MatchFeedback.valor == ValorFeedback.DESCARTE.value,
        )
    ).scalar_one_or_none()
    if fb is not None:
        session.delete(fb)


def esta_guardada(session: Session, owner_id: int, fuente: str, codigo: str) -> bool:
    s = obtener_seguimiento(session, owner_id, fuente, codigo)
    return s is not None and not s.archivada


def guardar(session: Session, owner_id: int, fuente: str, codigo: str) -> OportunidadSeguida:
    """Guarda (= sigue) la oportunidad y borra un descarte previo. Idempotente;
    una archivada vuelve a guardadas."""
    _borrar_descarte(session, owner_id, fuente, codigo)
    return seguir_oportunidad(
        session, owner_id, fuente, codigo, estado_actual(session, fuente, codigo)
    )


def alternar_guardada(session: Session, owner_id: int, fuente: str, codigo: str) -> bool:
    """Toggle de Guardar. Devuelve True si quedó guardada. Quitar de guardadas =
    `dejar_de_seguir` (no archivar)."""
    if esta_guardada(session, owner_id, fuente, codigo):
        dejar_de_seguir(session, owner_id, fuente, codigo)
        return False
    guardar(session, owner_id, fuente, codigo)
    return True


def codigos_guardados(
    session: Session, owner_id: int, fuente: str, codigos: list[str]
) -> set[str]:
    """De `codigos`, los que el usuario tiene guardados — una sola query."""
    if not codigos:
        return set()
    return set(
        session.execute(
            select(OportunidadSeguida.codigo_oportunidad).where(
                OportunidadSeguida.owner_id == owner_id,
                OportunidadSeguida.fuente == fuente,
                OportunidadSeguida.archivada.is_(False),
                OportunidadSeguida.codigo_oportunidad.in_(codigos),
            )
        ).scalars()
    )


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
