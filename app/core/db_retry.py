"""Reintento acotado de commits ante desconexiones transitorias (Neon remoto)."""

from __future__ import annotations

import time
from collections.abc import Callable

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.logging import get_logger

_log = get_logger(__name__)

MAX_INTENTOS = 3
BACKOFF_BASE_S = 0.5


def commit_con_retry(
    session: Session,
    aplicar: Callable[[], None],
    *,
    contexto: str,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Ejecuta `aplicar()` y comitea, reintentando ante OperationalError.

    `aplicar` debe ser idempotente y reaplicar TODO su trabajo en cada intento:
    tras un fallo se hace rollback, lo que descarta el estado pendiente del
    intento anterior. Devuelve True si el commit tuvo éxito, False si se
    agotaron los intentos (queda logueado; el caller decide cómo continuar).
    """
    for intento in range(1, MAX_INTENTOS + 1):
        try:
            aplicar()
            session.commit()
            return True
        except OperationalError as exc:
            session.rollback()
            if intento >= MAX_INTENTOS:
                _log.error("%s: commit falló tras %d intentos: %s", contexto, MAX_INTENTOS, exc)
                return False
            delay = BACKOFF_BASE_S * (2 ** (intento - 1))
            _log.warning(
                "%s: intento %d/%d falló, reintentando en %.1fs: %s",
                contexto,
                intento,
                MAX_INTENTOS,
                delay,
                exc,
            )
            sleep_fn(delay)
    return False
