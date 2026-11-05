"""Avance visible de los jobs largos (F-plat-6-fix).

Cada etapa cerrada deja un log INFO y su duración en `resultado["segundos_por_etapa"]`: una
corrida larga se puede seguir y, si se corta, se sabe dónde iba.
"""

from __future__ import annotations

import logging
import time
from typing import Any

_log = logging.getLogger("app.avance")


class Etapas:
    def __init__(self, resultado: dict[str, Any], job: str) -> None:
        self.por_etapa: dict[str, float] = resultado.setdefault("segundos_por_etapa", {})
        self.job = job
        self._t = time.monotonic()

    def fin(self, etapa: str, detalle: str = "") -> float:
        """Cierra `etapa` (desde la anterior o la creación) y devuelve sus segundos."""
        ahora = time.monotonic()
        seg = round(ahora - self._t, 1)
        self.por_etapa[etapa] = round(self.por_etapa.get(etapa, 0.0) + seg, 1)
        self._t = ahora
        _log.info("%s %s: %.1f s%s", self.job, etapa, seg, f" ({detalle})" if detalle else "")
        return seg
