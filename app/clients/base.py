"""Infraestructura compartida: excepciones, rate limiter, quota tracker y cliente base."""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import Engine, text

from app.core.logging import get_logger
from app.core.tiempo import TZ_CHILE

_log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Excepciones
# ---------------------------------------------------------------------------


class MPError(Exception):
    """Base para todos los errores de la API de Mercado Público."""


class MPAuthError(MPError):
    """Error 401 — ticket ausente o inválido."""


class MPRateLimitError(MPError):
    """Error 429 — la API rechaza por límite.

    Salvo la subclase MPConcurrencyError, se trata como tope diario: no se
    reintenta hasta el cambio de día en TZ Chile (regla 3).
    """

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class MPConcurrencyError(MPRateLimitError):
    """Error 429 con Codigo 10500 — "peticiones simultáneas", transitorio.

    Subclase A PROPÓSITO: _request lo reintenta con backoff corto, y si los
    reintentos se agotan los runners lo cortan igual que a cualquier
    MPRateLimitError, sin tener que conocerlo.
    """


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
    now = datetime.now(TZ_CHILE)
    next_day = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    return max(0, int((next_day - now).total_seconds())) + 60


# Cabeceras que, si la API las manda, dicen cuánto esperar de verdad. Se
# registran para saber qué entrega realmente Mercado Público: hoy no lo sabemos.
_HEADERS_DE_CUOTA = ("retry-after", "x-ratelimit-", "ratelimit-", "x-rate-limit-")


def _headers_de_cuota(headers: object) -> dict[str, str]:
    """Subconjunto de cabeceras de RESPUESTA relacionadas con límites de tasa.

    Nunca se leen cabeceras de request: ahí viaja el ticket (regla 1).
    """
    try:
        items = list(headers.items())  # type: ignore[attr-defined]
    except Exception:
        return {}
    return {
        str(k): str(v)
        for k, v in items
        if str(k).lower().startswith(_HEADERS_DE_CUOTA)
    }


def _parse_retry_after(valor: str | None) -> int | None:
    """`Retry-After` en segundos o como fecha HTTP. None si no se puede leer.

    RFC 9110 admite las dos formas; la API todavía no sabemos cuál usa, si es
    que manda alguna.
    """
    if not valor:
        return None
    crudo = valor.strip()
    if crudo.isdigit():
        return max(0, int(crudo))
    try:
        cuando = parsedate_to_datetime(crudo)
    except (TypeError, ValueError):
        return None
    if cuando is None:
        return None
    if cuando.tzinfo is None:
        cuando = cuando.replace(tzinfo=TZ_CHILE)
    return max(0, int((cuando - datetime.now(cuando.tzinfo)).total_seconds()))


# Único código de 429 verificado (22-sep-2026): "Hemos detectado que existen
# peticiones simultáneas". Cualquier otro se sigue tratando como tope diario.
_CODIGO_CONCURRENCIA = 10500
_RETRY_AFTER_CONCURRENCIA = 900


def _codigo_de_error(response: httpx.Response) -> int | None:
    """`Codigo` del cuerpo de un error, o None. Nunca lanza.

    Se busca en la raíz (forma v1) y, si no está, en los elementos de la lista
    `errors` del envelope v2. Cuerpo vacío, HTML o JSON sin código → None.
    """

    def _leer(obj: object) -> int | None:
        if not isinstance(obj, dict):
            return None
        valor = obj.get("Codigo", obj.get("codigo"))
        if valor is None or isinstance(valor, bool):
            return None
        try:
            return int(str(valor).strip())
        except ValueError:
            return None

    try:
        cuerpo = response.json()
    except Exception:
        return None
    codigo = _leer(cuerpo)
    if codigo is not None:
        return codigo
    errores = cuerpo.get("errors") if isinstance(cuerpo, dict) else None
    if isinstance(errores, list):
        for err in errores:
            codigo = _leer(err)
            if codigo is not None:
                return codigo
    return None


# ---------------------------------------------------------------------------
# RateLimiter — token bucket con jitter
# ---------------------------------------------------------------------------


class RateLimiter:
    """Token bucket síncrono; thread-safe, con enfriamiento opcional."""

    def __init__(self, rps: float) -> None:
        self._rps = max(rps, 0.01)
        self._tokens: float = 1.0
        self._last: float = time.monotonic()
        self._no_antes_de: float = 0.0
        self._lock = threading.Lock()

    @property
    def rps(self) -> float:
        return self._rps

    def enfriar(self, segundos: float, causa: str) -> None:
        """Ningún acquire() vuelve antes de `segundos` desde ahora.

        Un enfriamiento más corto no acorta uno más largo que ya esté en curso.
        """
        with self._lock:
            hasta = time.monotonic() + max(0.0, segundos)
            if hasta <= self._no_antes_de:
                return
            self._no_antes_de = hasta
        _log.warning("RateLimiter: enfriamiento de %.1f s (%s)", segundos, causa)

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._no_antes_de:
                time.sleep(self._no_antes_de - now)
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


# v1 y v2 usan el MISMO ticket: si cada cliente tuviera su limiter, el proceso
# podría emitir 2 req/s. Es por proceso, no global: entre procesos serializan el
# advisory lock y el grupo `concurrency` de los workflows.
_limiter_compartido: RateLimiter | None = None
_limiter_compartido_lock = threading.Lock()


def rate_limiter_compartido(rps: float) -> RateLimiter:
    """El RateLimiter único del proceso; se crea en la primera llamada."""
    global _limiter_compartido
    with _limiter_compartido_lock:
        if _limiter_compartido is None:
            _limiter_compartido = RateLimiter(rps)
        elif _limiter_compartido.rps != max(rps, 0.01):
            _log.warning(
                "rate_limiter_compartido: se pidió %.3f req/s pero ya existe uno de %.3f; "
                "se mantiene el primero",
                rps,
                _limiter_compartido.rps,
            )
        return _limiter_compartido


def reset_rate_limiter_compartido() -> None:
    """Descarta el limiter del proceso. Solo para tests."""
    global _limiter_compartido
    with _limiter_compartido_lock:
        _limiter_compartido = None


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
        return datetime.now(TZ_CHILE).date().isoformat()

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
            # Instrumentación (F-cuota): se registra lo que llega de verdad.
            # F-429-concurrencia: los logs mostraron que la API distingue por
            # `Codigo` del cuerpo. El 10500 (peticiones simultáneas) es
            # transitorio y _request lo reintenta; cualquier otro 429 se sigue
            # tratando como tope diario, sin reintento (regla 3).
            cabeceras = _headers_de_cuota(response.headers)
            cuerpo = response.text[:500]
            codigo = _codigo_de_error(response)
            _log.warning(
                "HTTP 429 codigo=%s headers_cuota=%s body=%s",
                codigo if codigo is not None else "(ninguno)",
                cabeceras or "(ninguna)",
                cuerpo,
            )
            if codigo == _CODIGO_CONCURRENCIA:
                raise MPConcurrencyError(
                    f"Concurrencia (429/{_CODIGO_CONCURRENCIA}): peticiones simultáneas",
                    retry_after_seconds=_RETRY_AFTER_CONCURRENCIA,
                )
            secs = _parse_retry_after(response.headers.get("Retry-After"))
            if secs is None:
                secs = _seconds_until_next_day_chile()
                origen = "00:01 Chile"
            else:
                origen = "Retry-After"
            raise MPRateLimitError(
                f"Cuota agotada (429). Reintentar en {secs} s ({origen})",
                retry_after_seconds=secs,
            )
        if response.status_code >= 500:
            # Cuerpo crudo truncado: la API a veces trae un mensaje útil en el
            # envelope de error (ver docs/09-compra-agil-500.md). El ticket nunca
            # viaja en el body (es header de request), pero igual pasa por el
            # _SecretFilter del logger raíz como cualquier otro mensaje.
            cuerpo = response.text[:500]
            _log.warning("HTTP %s body=%s", response.status_code, cuerpo)
            raise MPServerError(
                f"Error del servidor ({response.status_code}): {cuerpo}",
                status_code=response.status_code,
            )
        try:
            return response.json()  # type: ignore[no-any-return]
        except Exception as exc:
            raise MPParseError(f"Respuesta no es JSON válido: {exc}") from exc

    def _request(
        self,
        method: str,
        url: str,
        *,
        reintentar_transitorios: bool = True,
        **kwargs: object,
    ) -> dict[str, object]:
        # MPServerError: máx 2 intentos totales (1 reintento) — absorbe errores transitorios
        # httpx.TimeoutException: máx 3 intentos totales (2 reintentos)
        # MPConcurrencyError (429/10500): máx 4 intentos totales, backoff 30/60/120 s + jitter
        # MPRateLimitError (cualquier otro 429): nunca reintentar
        # Los tres contadores son independientes.
        #
        # `reintentar_transitorios=False` (F-detalles-match): 5xx y timeout NO se
        # reintentan, se lanzan al primer fallo. Es para llamadores cuyo ítem
        # vuelve solo a la cola en la corrida siguiente (detalles de oportunidades
        # con match): ahí un reintento en la misma corrida gasta ~1,5 min por
        # fallo sin ganar nada. El enfriamiento de 60 s y la política del 429 no
        # cambian. El default conserva el comportamiento de siempre.
        #
        # 504 y timeout enfrían el limiter del proceso 60 s, se reintente o no:
        # [I] el gateway corta a ~30 s pero el backend de ChileCompra sigue con
        # la consulta, y cualquier request nuestra con el mismo ticket, de v1 o
        # de v2, puede contarse como simultánea (10500). Las esperas del 10500
        # también pasan por el limiter, para frenar a todo el proceso.
        _MAX_SERVER_ATTEMPTS = 2
        _MAX_TIMEOUT_ATTEMPTS = 3
        _CONCURRENCY_DELAYS = (30.0, 60.0, 120.0)
        _ENFRIAMIENTO_S = 60.0

        server_attempt = 0
        timeout_attempt = 0
        concurrency_retry = 0

        while True:
            # Antes de CADA intento, también los reintentos: si el presupuesto
            # se agota a mitad de camino, QuotaExceededError sin emitir nada.
            self._quota.check_budget()
            self._rate_limiter.acquire()
            try:
                _log.debug("HTTP %s %s", method, url)
                try:
                    response = self._http.request(method, url, **kwargs)  # type: ignore[arg-type]
                finally:
                    # Cuenta CADA request emitida, sea cual sea su desenlace:
                    # 429, 504, timeout y cada reintento por separado. El
                    # presupuesto de 9.000 es sobre lo que nosotros emitimos, no
                    # sobre lo que sale bien; contar solo los éxitos hacía que
                    # /api/salud informara un piso en vez del consumo real.
                    self._quota.consume()
                return self._handle_response(response)
            except MPConcurrencyError:
                if concurrency_retry >= len(_CONCURRENCY_DELAYS):
                    raise
                delay = _CONCURRENCY_DELAYS[concurrency_retry] * (1 + random.uniform(0.0, 0.2))
                concurrency_retry += 1
                _log.warning(
                    "MPConcurrencyError (429/10500) reintento %d/%d; reintentando en %.1f s",
                    concurrency_retry,
                    len(_CONCURRENCY_DELAYS),
                    delay,
                )
                self._rate_limiter.enfriar(delay, causa="429/10500")
            except (MPAuthError, MPRateLimitError, MPParseError):
                raise
            except MPServerError as exc:
                es_504 = exc.status_code == 504
                if es_504:
                    self._rate_limiter.enfriar(_ENFRIAMIENTO_S, causa="HTTP 504")
                server_attempt += 1
                if not reintentar_transitorios or server_attempt >= _MAX_SERVER_ATTEMPTS:
                    raise
                delay = min(2.0 * (2 ** (server_attempt - 1)), 30.0)
                _log.warning(
                    "MPServerError intento %d/%d; reintentando en %.1f s",
                    server_attempt,
                    _MAX_SERVER_ATTEMPTS,
                    max(delay, _ENFRIAMIENTO_S) if es_504 else delay,
                )
                if not es_504:
                    # 500/502/503 conservan su espera: el 500 de Compra Ágil es
                    # determinista (docs/09-compra-agil-500.md), no una consulta viva.
                    time.sleep(delay)
            except httpx.TimeoutException as exc:
                self._rate_limiter.enfriar(_ENFRIAMIENTO_S, causa="timeout")
                timeout_attempt += 1
                if not reintentar_transitorios or timeout_attempt >= _MAX_TIMEOUT_ATTEMPTS:
                    raise MPServerError("Timeout de red", status_code=0) from exc
                _log.warning(
                    "TimeoutException intento %d/%d; reintentando en %.1f s",
                    timeout_attempt,
                    _MAX_TIMEOUT_ATTEMPTS,
                    _ENFRIAMIENTO_S,
                )
