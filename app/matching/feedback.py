"""CRUD de feedback explícito sobre matches (F10 parte 2), con ownership
obligatorio (regla 17 CLAUDE.md). Solo REGISTRA la señal — F11 es quien la
consumirá para reponderar el matching; este módulo no entrena ni reordena.

Un feedback por usuario por oportunidad (uq_match_feedback): alternar
actualiza el valor existente o lo borra, nunca duplica.

F-guardar: el feedback queda SOLO para el descarte. "Me sirve" se unificó con
Guardar (`app.matching.seguimiento`); `sirve` ya no se escribe.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.enums import ValorFeedback
from app.models.tables import MatchFeedback

_log = get_logger(__name__)


def obtener_feedback(
    session: Session,
    usuario_id: int,
    fuente: str,
    codigo: str,
) -> MatchFeedback | None:
    """Retorna el feedback del usuario para esa oportunidad, o None si no existe."""
    return session.execute(
        select(MatchFeedback).where(
            MatchFeedback.usuario_id == usuario_id,
            MatchFeedback.fuente == fuente,
            MatchFeedback.codigo_oportunidad == codigo,
        )
    ).scalar_one_or_none()


def listar_feedback_usuario(session: Session, usuario_id: int) -> dict[tuple[str, str], MatchFeedback]:
    """Mapa (fuente, codigo_oportunidad) -> MatchFeedback del usuario, para
    anotar el feed sin N+1 queries."""
    filas = session.execute(
        select(MatchFeedback).where(MatchFeedback.usuario_id == usuario_id)
    ).scalars()
    return {(f.fuente, f.codigo_oportunidad): f for f in filas}


def _marcar(
    session: Session,
    usuario_id: int,
    fuente: str,
    codigo: str,
    valor: ValorFeedback,
) -> MatchFeedback:
    """Crea o actualiza (upsert) el feedback del usuario para esa oportunidad."""
    existing = obtener_feedback(session, usuario_id, fuente, codigo)
    if existing is not None:
        existing.valor = valor.value
        return existing
    fb = MatchFeedback(usuario_id=usuario_id, fuente=fuente, codigo_oportunidad=codigo, valor=valor.value)
    session.add(fb)
    session.flush()
    return fb


def alternar_me_sirve(
    session: Session,
    usuario_id: int,
    fuente: str,
    codigo: str,
) -> bool:
    """Deprecated (F-guardar): alias de `seguimiento.alternar_guardada`.
    Devuelve True si quedó guardada."""
    from app.matching.seguimiento import alternar_guardada

    return alternar_guardada(session, usuario_id, fuente, codigo)


def descartar(session: Session, usuario_id: int, fuente: str, codigo: str) -> MatchFeedback:
    """Marca la oportunidad como descartada — el feed la excluye hasta deshacer.
    Si estaba guardada, la quita de guardadas (`dejar_de_seguir`, no archivar):
    nunca guardada y descartada a la vez."""
    from app.matching.seguimiento import dejar_de_seguir

    dejar_de_seguir(session, usuario_id, fuente, codigo)
    return _marcar(session, usuario_id, fuente, codigo, ValorFeedback.DESCARTE)


def deshacer_descarte(session: Session, usuario_id: int, fuente: str, codigo: str) -> bool:
    """Elimina el feedback (de cualquier valor), reincorporando la oportunidad
    al feed si estaba descartada. Retorna False si no existe."""
    existing = obtener_feedback(session, usuario_id, fuente, codigo)
    if existing is None:
        return False
    session.delete(existing)
    return True


def listar_descartadas(session: Session, usuario_id: int) -> list[MatchFeedback]:
    """Feedback de valor "descarte" del usuario, más recientes primero."""
    return list(
        session.execute(
            select(MatchFeedback)
            .where(MatchFeedback.usuario_id == usuario_id, MatchFeedback.valor == ValorFeedback.DESCARTE.value)
            .order_by(MatchFeedback.actualizado_en.desc())
        ).scalars()
    )
