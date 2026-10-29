"""Infraestructura compartida: excepciones, rate limiter, quota tracker y cliente base."""

from __future__ import annotations

import random
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import DateTime, Engine, bindparam, inspect, text

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
    """Error 429 — la API rechaza por límite. Base de las dos clases de 429.

    Los runners cortan su loop ante cualquier MPRateLimitError (regla 3); qué
    hace el cliente antes de lanzarlo depende de la subclase.
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


class MPTasaError(MPRateLimitError):
    """Error 429 que no es 10500: "límite de tasa" (codigo 429) o código no visto.

    F-429-tasa: es transitorio [V, 29-oct-2026], no tope diario. _request no lo
    reintenta, persiste una pausa de la API (`retry_after_seconds`) y lo
    re-lanza. Subclase de MPRateLimitError A PROPÓSITO, como MPConcurrencyError:
    los runners que ya cortan ante MPRateLimitError cortan sin conocerla.
    `causa` va a sync_state: "429/429" o "429/desconocido:<código>".
    """

    def __init__(self, message: str, retry_after_seconds: int, causa: str) -> None:
        super().__init__(message, retry_after_seconds=retry_after_seconds)
        self.causa = causa


class MPServerError(MPError):
    """Error 5xx — fallo transitorio del servidor."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class MPTransportError(MPServerError):
    """Error de transporte (conexión rechazada o cortada) con los reintentos agotados.

    Subclase de MPServerError con `status_code=0`, como el timeout: los
    llamadores que ya cortan o siguen ante un 5xx hacen lo mismo sin conocerla.
    `detalles-match` la distingue para no culpar a la oportunidad (F-datos-1).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=0)


# Fallos del canal que no son timeout: conexión rechazada, cortada a mitad de la
# respuesta o con el protocolo roto. Se tratan como el timeout (F-datos-1, R3).
_ERRORES_TRANSPORTE = (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError)


class MPParseError(MPError):
    """JSON inválido o estructura de envelope inesperada."""


class QuotaExceededError(MPError):
    """El presupuesto local de requests/día se ha agotado."""


class APIPausadaError(QuotaExceededError):
    """La API está en pausa por un 429 de tasa reciente; no se emite nada.

    Subclase de QuotaExceededError A PROPÓSITO: todos los llamadores ya cortan
    limpio ante ella sin conocerla (F-429-tasa).
    """


# Cortes por pausa de la API (F-429-tasa): `ca` y `estados-vencidos` los cierran
# como corrida OK con avance parcial; el resto de errores de canal se re-lanza.
ERRORES_DE_PAUSA = (MPTasaError, APIPausadaError)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


# Códigos de 429 verificados. 10500 (22-sep-2026): "Hemos detectado que existen
# peticiones simultáneas". 429 (29-oct-2026): "Se ha superado el límite de tasa
# de solicitudes"; transitorio, la API vuelve a atender el mismo día Chile.
# No hay código de tope diario verificado: ningún 429 marca el día agotado.
_CODIGO_CONCURRENCIA = 10500
_RETRY_AFTER_CONCURRENCIA = 900
_CODIGO_TASA = 429
# Defaults de BaseClient; en producción mandan PAUSA_TASA_MIN y
# PAUSA_429_DESCONOCIDO_MIN de Settings.
_PAUSA_TASA_S = 20 * 60
_PAUSA_429_DESCONOCIDO_S = 60 * 60


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

# Día agotado (R1): el contador sube al presupuesto completo, nunca baja.
_AGOTAR_QUOTA_PG = """
INSERT INTO quota_log (fecha, requests_usadas)
VALUES (:fecha, :n)
ON CONFLICT (fecha) DO UPDATE
    SET requests_usadas = GREATEST(quota_log.requests_usadas, EXCLUDED.requests_usadas)
"""

_AGOTAR_QUOTA_SQLITE = """
INSERT INTO quota_log (fecha, requests_usadas) VALUES (:fecha, :n)
ON CONFLICT(fecha) DO UPDATE SET requests_usadas = MAX(requests_usadas, :n)
"""

# Pausa de la API por 429 de tasa (F-429-tasa): fila propia de sync_state, sin
# migración. `cursor` = "pausada hasta" en ISO-8601 UTC sin zona, siempre con
# segundos: así la comparación de texto ordena igual que la de fechas, y el
# WHERE del upsert impide que una pausa más corta acorte una más larga. Misma
# sintaxis en Postgres y SQLite (≥ 3.24).
_FUENTE_PAUSA = "api_pausa"

_SELECT_PAUSA = "SELECT cursor FROM sync_state WHERE fuente = :fuente"

_UPSERT_PAUSA = """
INSERT INTO sync_state (fuente, cursor, notas, ultima_ejecucion, requests_usadas_hoy)
VALUES (:fuente, :hasta, :causa, :ahora, 0)
ON CONFLICT (fuente) DO UPDATE
    SET cursor = excluded.cursor, notas = excluded.notas,
        ultima_ejecucion = excluded.ultima_ejecucion
    WHERE sync_state.cursor IS NULL OR sync_state.cursor < excluded.cursor
"""


def _iso_pausa(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _ahora_chile() -> datetime:
    return datetime.now(TZ_CHILE)


class QuotaTracker:
    """Rastrea el uso diario de la cuota de la API persistido en Postgres.

    `reserva` (F-datos-1, R2): requests del presupuesto que este tracker no
    puede usar, guardadas para `ca`. El contador es uno solo; lo que cambia es
    el tope efectivo, `budget − reserva`. `now_fn` es inyectable para tests.
    """

    def __init__(
        self,
        engine: Engine,
        budget: int,
        reserva: int = 0,
        now_fn: Callable[[], datetime] = _ahora_chile,
    ) -> None:
        self._engine = engine
        self._budget = budget
        self._reserva = max(0, min(reserva, budget))
        self._now_fn = now_fn
        self._lock = threading.Lock()
        self._is_sqlite = engine.dialect.name == "sqlite"
        self._ensure_table()
        # sync_state la define app/models (migración). Siempre existe en
        # producción; algunos tests usan una base solo con quota_log, y ahí la
        # pausa simplemente no aplica.
        self._con_pausa = inspect(engine).has_table("sync_state")

    def _ensure_table(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(text(_CREATE_QUOTA_TABLE))

    def _today(self) -> str:
        return self._now_fn().astimezone(TZ_CHILE).date().isoformat()

    def remaining(self) -> int:
        today = self._today()
        with self._engine.connect() as conn:
            row = conn.execute(text(_SELECT_QUOTA), {"fecha": today}).fetchone()
        used = int(row[0]) if row else 0
        return max(0, self._budget - self._reserva - used)

    def ahora(self) -> datetime:
        return self._now_fn()

    def marcar_agotado(self) -> None:
        """Da por agotado el día calendario de Chile para TODOS los procesos.

        SIN USO desde F-429-tasa, hasta verificar un código de 429 de tope
        diario: el único 429 que llamaba acá resultó transitorio y un falso
        positivo cuesta un día entero de ingesta. Si se usa, el contador sube al
        presupuesto completo, así que `check_budget` de cualquier tracker —con o
        sin reserva, de esta corrida o de la siguiente— corta sin emitir. El día
        siguiente parte en cero.
        """
        today = self._today()
        agotar = _AGOTAR_QUOTA_SQLITE if self._is_sqlite else _AGOTAR_QUOTA_PG
        with self._lock, self._engine.begin() as conn:
            conn.execute(text(agotar), {"fecha": today, "n": self._budget})
        _log.warning(
            "Cuota: 429 de tope diario; día %s (Chile) marcado como agotado hasta el cambio de día",
            today,
        )

    def consume(self, n: int = 1) -> None:
        today = self._today()
        upsert = _UPSERT_QUOTA_SQLITE if self._is_sqlite else _UPSERT_QUOTA_PG
        with self._lock, self._engine.begin() as conn:
            conn.execute(text(upsert), {"fecha": today, "n": n})

    def pausar(self, hasta: datetime, causa: str) -> None:
        """Pone la API en pausa hasta `hasta` para TODOS los procesos (F-429-tasa).

        Vive en Postgres (regla 10). Idempotente; una pausa más corta nunca
        acorta una más larga que siga vigente.
        """
        if not self._con_pausa:
            _log.warning("Cuota: sin tabla sync_state; la pausa de la API no se persiste")
            return
        with self._lock, self._engine.begin() as conn:
            conn.execute(
                # DateTime explícito: en SQLite el adaptador por defecto de
                # datetime está deprecado; el tipo de SQLAlchemy lo serializa.
                text(_UPSERT_PAUSA).bindparams(bindparam("ahora", type_=DateTime())),
                {
                    "fuente": _FUENTE_PAUSA,
                    "hasta": _iso_pausa(hasta),
                    "causa": causa,
                    "ahora": self._now_fn().astimezone(UTC).replace(tzinfo=None),
                },
            )
        _log.warning(
            "Cuota: API en pausa hasta %s Chile (%s)",
            hasta.astimezone(TZ_CHILE).strftime("%Y-%m-%d %H:%M"),
            causa,
        )

    def pausada_hasta(self) -> datetime | None:
        """Fin de la última pausa registrada (UTC, con zona), vigente o no. None si no hay."""
        if not self._con_pausa:
            return None
        with self._engine.connect() as conn:
            row = conn.execute(text(_SELECT_PAUSA), {"fuente": _FUENTE_PAUSA}).fetchone()
        if row is None or not row[0]:
            return None
        try:
            return datetime.fromisoformat(row[0]).replace(tzinfo=UTC)
        except ValueError:
            _log.warning("Cuota: pausa ilegible en sync_state: %r — se ignora", row[0])
            return None

    def check_budget(self, n: int = 1) -> None:
        # La pausa va ANTES del presupuesto: tras un 429 de tasa no se emite
        # nada hasta que venza, aunque quede cuota (F-429-tasa).
        hasta = self.pausada_hasta()
        if hasta is not None and self._now_fn() < hasta:
            raise APIPausadaError(
                f"API en pausa por 429 hasta {hasta.astimezone(TZ_CHILE):%H:%M} Chile"
            )
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
        pausa_tasa_s: int = _PAUSA_TASA_S,
        pausa_429_desconocido_s: int = _PAUSA_429_DESCONOCIDO_S,
    ) -> None:
        self._ticket = ticket
        self._rate_limiter = rate_limiter
        self._quota = quota
        self._timeout = timeout
        self._pausa_tasa_s = pausa_tasa_s
        self._pausa_429_desconocido_s = pausa_429_desconocido_s
        self._http = httpx.Client(headers=default_headers or {}, timeout=timeout)

    def _handle_response(self, response: httpx.Response) -> dict[str, object]:
        if response.status_code == 401:
            raise MPAuthError("Ticket inválido o ausente (401)")
        if response.status_code == 429:
            # Instrumentación (F-cuota): se registra lo que llega de verdad.
            # F-429-concurrencia: los logs mostraron que la API distingue por
            # `Codigo` del cuerpo. El 10500 (peticiones simultáneas) es
            # transitorio y _request lo reintenta. F-429-tasa: cualquier otro
            # 429 es MPTasaError, sin reintento y con pausa persistida; el
            # codigo 429 ("límite de tasa") pausa PAUSA_TASA_MIN, uno no visto
            # PAUSA_429_DESCONOCIDO_MIN (regla 3).
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
            if codigo == _CODIGO_TASA:
                pausa = self._pausa_tasa_s
                causa = "429/429"
            else:
                pausa = self._pausa_429_desconocido_s
                causa = f"429/desconocido:{codigo if codigo is not None else 'ninguno'}"
                _log.error(
                    "429 con código no visto: %s — pausa de %d min; revisar el body",
                    codigo if codigo is not None else "(ninguno)",
                    pausa // 60,
                )
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            if retry_after is not None:
                pausa = max(retry_after, pausa)
            raise MPTasaError(
                f"Límite de tasa ({causa}); API en pausa {pausa} s",
                retry_after_seconds=pausa,
                causa=causa,
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
        # httpx.TimeoutException y errores de transporte: máx 3 intentos totales
        # (2 reintentos), con un contador compartido
        # MPConcurrencyError (429/10500): máx 4 intentos totales, backoff 30/60/120 s + jitter
        # MPTasaError (cualquier otro 429): nunca reintentar; persiste una pausa
        # de la API y re-lanza (F-429-tasa). No marca el día agotado.
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
            except MPTasaError as exc:
                # Transitorio, no tope diario (F-429-tasa, verificado 29-oct-2026).
                # La pausa se persiste para que ninguna corrida emita hasta que
                # venza (check_budget). No se duerme esperándola: dormir 20 min
                # quema minutos de Actions; la corrida corta y la siguiente
                # retoma. El limiter del proceso se enfría igual: con la pausa
                # persistida check_budget corta antes de llegar al limiter; si no
                # se pudo persistir, el limiter es lo que frena al proceso (y ahí
                # sí espera) si sigue con otro paso del nocturno.
                hasta = self._quota.ahora() + timedelta(seconds=exc.retry_after_seconds)
                try:
                    self._quota.pausar(hasta, exc.causa)
                except Exception:
                    _log.warning("Cuota: no pude persistir la pausa de la API", exc_info=True)
                self._rate_limiter.enfriar(exc.retry_after_seconds, causa=exc.causa)
                raise
            except (MPAuthError, MPParseError):
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
            except (httpx.TimeoutException, *_ERRORES_TRANSPORTE) as exc:
                # Timeout y errores de transporte comparten enfriamiento,
                # contador y bandera: en los dos la request pudo llegar y el
                # backend puede seguir procesándola (F-datos-1, R3).
                es_timeout = isinstance(exc, httpx.TimeoutException)
                causa = "timeout" if es_timeout else type(exc).__name__
                self._rate_limiter.enfriar(_ENFRIAMIENTO_S, causa=causa)
                timeout_attempt += 1
                if not reintentar_transitorios or timeout_attempt >= _MAX_TIMEOUT_ATTEMPTS:
                    if es_timeout:
                        raise MPServerError("Timeout de red", status_code=0) from exc
                    raise MPTransportError(f"Error de transporte ({causa})") from exc
                _log.warning(
                    "%s intento %d/%d; reintentando en %.1f s",
                    causa,
                    timeout_attempt,
                    _MAX_TIMEOUT_ATTEMPTS,
                    _ENFRIAMIENTO_S,
                )
