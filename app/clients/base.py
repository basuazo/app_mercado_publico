"""Infraestructura compartida: excepciones, rate limiter, quota tracker y cliente base."""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import Engine, text
from tenacity import (
    RetryError,
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.core.logging import get_logger

_log = get_logger(__name__)
_TZ_CHILE = ZoneInfo("America/Santiago")


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class MPError(Exception):
    """Base para todos los errores de la API de Mercado Público."""


class MPAuthError(MPError):
    """Error 401 — ticket ausente o inválido."""


class MPRateLimitError(MPError):
    """Error 429 — cuota agotada; esperar hasta el día siguiente en TZ Chile."""

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class MPServerError(MPError):
    """Error 5xx — fallo transitorio del servidor."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class MPParseError(MPError):
    """JSON inválido o estructura de envelope inesperada."""


class QuotaExceededError(MPError):
    """El presupuesto local de requests/día se ha agotado."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seconds_until_next_day_chile() -> int:
    """Segundos hasta las 00:01 del día siguiente en America/Santiago + 60 s de margen."""
    now = datetime.now(_TZ_CHILE)
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return max(0, int((next_day - now).total_seconds())) + 60


# ---------------------------------------------------------------------------
# RateLimiter — token bucket con jitter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Token bucket síncrono; thread-safe."""

    def __init__(self, rps: float) -> None:
        self._rps = max(rps, 0.01)
        self._tokens: float = 1.0
        self._last: float = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(1.0, self._tokens + elapsed * self._rps)
            self._last = now
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self._rps
                time.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0
        jitter = random.uniform(0.0, 0.15 / self._rps)
        if jitter > 0:
            time.sleep(jitter)


# ---------------------------------------------------------------------------
# QuotaTracker — contador de requests persistido en Postgres
# ---------------------------------------------------------------------------

_CREATE_QUOTA_TABLE = """
CREATE TABLE IF NOT EXISTS quota_log (
    fecha DATE PRIMARY KEY,
    requests_usadas INTEGER NOT NULL DEFAULT 0
)
"""

_SELECT_QUOTA = "SELECT requests_usadas FROM quota_log WHERE fecha = :fecha"

# Postgres
_UPSERT_QUOTA_PG = """
INSERT INTO quota_log (fecha, requests_usadas)
VALUES (:fecha, :n)
ON CONFLICT (fecha) DO UPDATE
    SET requests_usadas = quota_log.requests_usadas + EXCLUDED.requests_usadas
"""

# SQLite (para tests en memoria)
_UPSERT_QUOTA_SQLITE = """
INSERT INTO quota_log (fecha, requests_usadas) VALUES (:fecha, :n)
ON CONFLICT(fecha) DO UPDATE SET requests_usadas = requests_usadas + :n
"""


class QuotaTracker:
    """Rastrea el uso diario de la cuota de la API persistido en Postgres."""

    def __init__(self, engine: Engine, budget: int) -> None:
        self._engine = engine
        self._budget = budget
        self._lock = threading.Lock()
        self._is_sqlite = engine.dialect.name == "sqlite"
        self._ensure_table()

    def _ensure_table(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(_CREATE_QUOTA_TABLE))

    def _today(self) -> str:
        return datetime.now(_TZ_CHILE).date().isoformat()

    def remaining(self) -> int:
        today = self._today()
        with self._engine.connect() as conn:
            row = conn.execute(text(_SELECT_QUOTA), {"fecha": today}).fetchone()
        used = int(row[0]) if row else 0
        return max(0, self._budget - used)

    def consume(self, n: int = 1) -> None:
        today = self._today()
        upsert = _UPSERT_QUOTA_SQLITE if self._is_sqlite else _UPSERT_QUOTA_PG
        with self._lock, self._engine.begin() as conn:
            conn.execute(text(upsert), {"fecha": today, "n": n})

    def check_budget(self, n: int = 1) -> None:
        rem = self.remaining()
        if rem < n:
            raise QuotaExceededError(f"Presupuesto diario agotado (quedan {rem} requests)")


# ---------------------------------------------------------------------------
# BaseClient
# ---------------------------------------------------------------------------


class BaseClient:
    """Cliente HTTP base con rate limiting, quota tracking y retries."""

    def __init__(
        self,
        ticket: str,
        rate_limiter: RateLimiter,
        quota: QuotaTracker,
        default_headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._ticket = ticket
        self._rate_limiter = rate_limiter
        self._quota = quota
        self._timeout = timeout
        self._http = httpx.Client(headers=default_headers or {}, timeout=timeout)

    def _handle_response(self, response: httpx.Response) -> dict[str, object]:
        if response.status_code == 401:
            raise MPAuthError("Ticket inválido o ausente (401)")
        if response.status_code == 429:
            secs = _seconds_until_next_day_chile()
            raise MPRateLimitError(
                f"Cuota agotada (429). Reintentar en {secs} s (00:01 Chile)",
                retry_after_seconds=secs,
            )
        if response.status_code >= 500:
            raise MPServerError(
                f"Error del servidor ({response.status_code})",
                status_code=response.status_code,
            )
        try:
            return response.json()  # type: ignore[no-any-return]
        except Exception as exc:
            raise MPParseError(f"Respuesta no es JSON válido: {exc}") from exc

    def _request(self, method: str, url: str, **kwargs: object) -> dict[str, object]:
        self._quota.check_budget()
        self._rate_limiter.acquire()
        try:
            for attempt in Retrying(
                retry=retry_if_exception_type((MPServerError, httpx.TimeoutException)),
                stop=stop_after_attempt(3),
                wait=wait_exponential(multiplier=1, min=2, max=30),
                reraise=True,
            ):
                with attempt:
                    _log.debug("HTTP %s %s", method, url)
                    response = self._http.request(
                        method,
                        url,
                        **kwargs,  # type: ignore[arg-type]
                    )
                    data = self._handle_response(response)
        except RetryError as exc:
            raise MPServerError("Agotados los reintentos", status_code=0) from exc
        except (MPAuthError, MPRateLimitError, MPParseError):
            raise
        except httpx.TimeoutException as exc:
            raise MPServerError("Timeout de red", status_code=0) from exc
        self._quota.consume()
        return data
