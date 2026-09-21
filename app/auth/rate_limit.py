"""Rate limiting de intentos de login en memoria.

NOTA: Con 2 instancias en Render (deploy rolling), el límite efectivo es
10 intentos por IP/15 min, ya que cada instancia mantiene su propio estado.
Aceptable para un equipo de 3–10 usuarios internos.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime, timedelta

from app.core.tiempo import ahora_utc

_lock = threading.Lock()
_attempts: dict[str, list[datetime]] = defaultdict(list)
_MAX_ATTEMPTS = 5
_WINDOW = timedelta(minutes=15)


def _cleanup(ip: str, now: datetime) -> None:
    _attempts[ip] = [t for t in _attempts[ip] if now - t < _WINDOW]


def is_rate_limited(ip: str) -> bool:
    with _lock:
        _cleanup(ip, ahora_utc())
        return len(_attempts[ip]) >= _MAX_ATTEMPTS


def record_failed_attempt(ip: str) -> None:
    with _lock:
        now = ahora_utc()
        _cleanup(ip, now)
        _attempts[ip].append(now)


def clear_attempts(ip: str) -> None:
    with _lock:
        _attempts.pop(ip, None)
