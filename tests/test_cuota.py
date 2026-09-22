"""Tests F-cuota: conteo real de requests, instrumentación del 429 y corte de loop.

Todo mockeado con respx: ninguna llamada real a la API.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.base import (
    BaseClient,
    MPAuthError,
    MPConcurrencyError,
    MPRateLimitError,
    MPServerError,
    QuotaExceededError,
    QuotaTracker,
    RateLimiter,
    _parse_retry_after,
)
from app.models.base import Base

_FAKE_TICKET = "ticket-de-test-1234"
_URL = "https://api.mercadopublico.cl/servicios/v1/publico/prueba"


@pytest.fixture()
def mem_engine():
    engine = create_engine("sqlite:///:memory:")
    yield engine
    engine.dispose()


@pytest.fixture()
def quota(mem_engine) -> QuotaTracker:
    return QuotaTracker(mem_engine, budget=100)


@pytest.fixture()
def client(quota) -> BaseClient:
    # rps alto y sin sleeps largos: el rate limiter no es lo que se prueba acá.
    return BaseClient(ticket=_FAKE_TICKET, rate_limiter=RateLimiter(rps=1000.0), quota=quota)


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch: pytest.MonkeyPatch):
    """Los backoff entre reintentos no aportan nada al test y lo hacen lento."""
    monkeypatch.setattr("app.clients.base.time.sleep", lambda _s: None)


# ---------------------------------------------------------------------------
# 2 — contar todas las requests, no solo las exitosas
# ---------------------------------------------------------------------------


@respx.mock
def test_una_request_exitosa_cuenta_una(client, quota) -> None:
    respx.get(_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    client._request("GET", _URL)
    assert quota.remaining() == 99


@respx.mock
def test_el_504_y_su_reintento_cuentan_por_separado(client, quota) -> None:
    respx.get(_URL).mock(
        side_effect=[
            httpx.Response(504, text="gateway timeout"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    client._request("GET", _URL)
    # Dos requests emitidas: la que falló y la del reintento.
    assert quota.remaining() == 98


@respx.mock
def test_el_429_cuenta_aunque_falle(client, quota) -> None:
    respx.get(_URL).mock(return_value=httpx.Response(429, text="limite"))
    with pytest.raises(MPRateLimitError):
        client._request("GET", _URL)
    assert quota.remaining() == 99


@respx.mock
def test_secuencia_completa_deja_el_contador_en_4(client, quota) -> None:
    """1 éxito + 1 error 504 con su reintento + 1 error 429 = 4 requests."""
    respx.get(_URL).mock(
        side_effect=[
            httpx.Response(200, json={"ok": True}),  # 1
            httpx.Response(504, text="boom"),  # 2
            httpx.Response(200, json={"ok": True}),  # 3 (reintento del 504)
            httpx.Response(429, text="limite"),  # 4
        ]
    )
    client._request("GET", _URL)
    client._request("GET", _URL)
    with pytest.raises(MPRateLimitError):
        client._request("GET", _URL)

    assert quota.remaining() == 96  # 100 - 4, no 100 - 1


@respx.mock
def test_el_timeout_y_sus_reintentos_tambien_cuentan(client, quota) -> None:
    respx.get(_URL).mock(side_effect=httpx.TimeoutException("agotado"))
    with pytest.raises(MPServerError):
        client._request("GET", _URL)
    # 3 intentos totales según la política de timeouts del cliente.
    assert quota.remaining() == 97


@respx.mock
def test_el_401_cuenta_la_request_emitida(client, quota) -> None:
    respx.get(_URL).mock(return_value=httpx.Response(401, text="no"))
    with pytest.raises(MPAuthError):
        client._request("GET", _URL)
    assert quota.remaining() == 99


def test_el_presupuesto_sigue_cortando_antes_de_emitir(client, quota) -> None:
    quota.consume(100)
    with pytest.raises(QuotaExceededError):
        client._request("GET", _URL)


# ---------------------------------------------------------------------------
# 3 — instrumentación del 429
# ---------------------------------------------------------------------------


@respx.mock
def test_el_429_loguea_headers_y_body(client, caplog) -> None:
    respx.get(_URL).mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "120", "X-RateLimit-Remaining": "0"},
            text="Se ha alcanzado el limite de consultas diarias",
        )
    )
    with caplog.at_level(logging.WARNING), pytest.raises(MPRateLimitError):
        client._request("GET", _URL)

    # httpx normaliza los nombres de cabecera a minúsculas.
    registro = "\n".join(r.getMessage() for r in caplog.records).lower()
    assert "429" in registro
    assert "retry-after" in registro
    assert "x-ratelimit-remaining" in registro
    assert "limite de consultas diarias" in registro


@respx.mock
def test_el_429_no_loguea_headers_de_request(client, caplog) -> None:
    """Regla 1: el ticket viaja en la request y no puede aparecer en el log.

    Se cubren los dos caminos por los que viaja: header (v2) y query param (v1).
    La garantía es estructural —solo se leen cabeceras de RESPUESTA y el body—,
    no depende del enmascaramiento del _SecretFilter.
    """
    url_v1 = f"{_URL}?ticket={_FAKE_TICKET}"
    respx.get(url_v1).mock(return_value=httpx.Response(429, text="limite"))
    with caplog.at_level(logging.WARNING), pytest.raises(MPRateLimitError):
        client._request("GET", url_v1, headers={"ticket": _FAKE_TICKET})

    del_429 = [r.getMessage() for r in caplog.records if "429" in r.getMessage()]
    assert del_429, "la rama del 429 debe dejar su registro"
    assert _FAKE_TICKET not in "\n".join(del_429)


@respx.mock
def test_retry_after_en_segundos_manda_sobre_el_fallback(client) -> None:
    respx.get(_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "90"}))
    with pytest.raises(MPRateLimitError) as exc:
        client._request("GET", _URL)
    assert exc.value.retry_after_seconds == 90
    assert "Retry-After" in str(exc.value)


@respx.mock
def test_sin_retry_after_se_mantiene_el_fallback_de_medianoche(client) -> None:
    respx.get(_URL).mock(return_value=httpx.Response(429, text="limite"))
    with pytest.raises(MPRateLimitError) as exc:
        client._request("GET", _URL)
    # Fallback a 00:01 Chile: siempre positivo y nunca más de ~25 h.
    assert 0 < exc.value.retry_after_seconds <= 25 * 3600
    assert "00:01 Chile" in str(exc.value)


@respx.mock
def test_el_429_sigue_sin_reintentarse(client, quota) -> None:
    """Regla 3: un 429 que NO es 10500 se trata como tope diario y no se reintenta.

    El 10500 (concurrencia) sí se reintenta: ver tests/test_429_concurrencia.py.
    """
    ruta = respx.get(_URL).mock(
        return_value=httpx.Response(429, json={"Codigo": 10501, "Mensaje": "limite"})
    )
    with pytest.raises(MPRateLimitError) as exc:
        client._request("GET", _URL)
    assert not isinstance(exc.value, MPConcurrencyError)
    assert ruta.call_count == 1


def test_parse_retry_after_segundos() -> None:
    assert _parse_retry_after("120") == 120
    assert _parse_retry_after("  0 ") == 0


def test_parse_retry_after_fecha_http() -> None:
    futuro = datetime.now(UTC) + timedelta(seconds=300)
    cabecera = futuro.strftime("%a, %d %b %Y %H:%M:%S GMT")
    segundos = _parse_retry_after(cabecera)
    assert segundos is not None
    assert 280 <= segundos <= 300


def test_parse_retry_after_fecha_pasada_no_es_negativa() -> None:
    pasado = datetime.now(UTC) - timedelta(seconds=300)
    assert _parse_retry_after(pasado.strftime("%a, %d %b %Y %H:%M:%S GMT")) == 0


def test_parse_retry_after_basura_o_ausente() -> None:
    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("mañana") is None
    assert _parse_retry_after("-5") is None


# ---------------------------------------------------------------------------
# 1 — cortar el loop ante un error del canal
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_engine():
    import app.models.tables  # noqa: F401

    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    yield e


class _V1Falso:
    """Cliente v1 que devuelve detalles hasta que le toca fallar."""

    def __init__(self, fallar_en: int, error: Exception) -> None:
        self._fallar_en = fallar_en
        self._error = error
        self.llamadas = 0

    def licitacion_detalle(self, codigo: str) -> Any:
        self.llamadas += 1
        if self.llamadas >= self._fallar_en:
            raise self._error
        from app.clients.types import LicitacionDetalle

        return LicitacionDetalle(
            codigo=codigo,
            nombre=f"Licitación {codigo}",
            descripcion="",
            estado=5,
            fecha_publicacion=None,
            fecha_cierre=None,
            tipo=None,
            codigo_organismo=None,
            moneda=None,
            monto_estimado=None,
            items=[],
        )


def _sembrar_pendientes(engine, n: int) -> None:
    from app.models.tables import Licitacion

    with Session(engine) as s:
        for i in range(n):
            s.add(
                Licitacion(
                    codigo=f"LIC-{i:03d}",
                    nombre="Servicio de aseo",
                    descripcion="",
                    estado="publicada",
                    detalle_obtenido=False,
                )
            )
        s.commit()


@pytest.mark.parametrize(
    "error",
    [
        MPRateLimitError("429", retry_after_seconds=60),
        QuotaExceededError("presupuesto agotado"),
        MPAuthError("ticket inválido"),
    ],
    ids=["429", "presupuesto", "401"],
)
def test_fetch_detalles_corta_ante_error_de_canal(db_engine, error) -> None:
    """El bug de producción: 60 requests seguidas contra una API que rechazaba todas."""
    from app.core.settings import Settings
    from app.ingest.licitaciones import fetch_detalles_pendientes

    _sembrar_pendientes(db_engine, 10)
    v1 = _V1Falso(fallar_en=3, error=error)
    settings = Settings(
        mp_ticket="T",
        database_url="sqlite:///:memory:",
        secret_key="clave-de-test-32bytesxxxxxxxxxx",
        jobs_token="token-de-test-jobs-abcdefgh1234",
    )

    with Session(db_engine) as session, pytest.raises(type(error)):
        fetch_detalles_pendientes(session, v1, settings, max_requests=10)

    # Cortó en la tercera: no siguió pidiendo las 7 restantes.
    assert v1.llamadas == 3


def test_fetch_detalles_guarda_el_progreso_parcial(db_engine) -> None:
    from app.core.settings import Settings
    from app.ingest.licitaciones import fetch_detalles_pendientes
    from app.models.tables import Licitacion, SyncState

    _sembrar_pendientes(db_engine, 10)
    v1 = _V1Falso(fallar_en=3, error=MPRateLimitError("429", retry_after_seconds=60))
    settings = Settings(
        mp_ticket="T",
        database_url="sqlite:///:memory:",
        secret_key="clave-de-test-32bytesxxxxxxxxxx",
        jobs_token="token-de-test-jobs-abcdefgh1234",
    )

    with Session(db_engine) as session, pytest.raises(MPRateLimitError):
        fetch_detalles_pendientes(session, v1, settings, max_requests=10)

    with Session(db_engine) as s:
        hechas = [
            lic.codigo
            for lic in s.query(Licitacion).all()
            if lic.detalle_obtenido
        ]
        estado = s.get(SyncState, "licitaciones_detalles")

    assert len(hechas) == 2, "las dos que alcanzaron a bajar deben quedar guardadas"
    assert estado is not None
    assert estado.ultima_ejecucion is not None
    assert estado.ultimo_ok is None, "un corte del canal no es una corrida OK"


@pytest.mark.parametrize(
    "error",
    [
        MPRateLimitError("429", retry_after_seconds=60),
        QuotaExceededError("presupuesto agotado"),
        MPAuthError("ticket inválido"),
    ],
    ids=["429", "presupuesto", "401"],
)
def test_lifecycle_corta_ante_error_de_canal(db_engine, error) -> None:
    from app.core.settings import Settings
    from app.ingest.lifecycle import refresh_estados
    from app.models.tables import Licitacion

    ahora = datetime.now()
    with Session(db_engine) as s:
        for i in range(10):
            s.add(
                Licitacion(
                    codigo=f"LIC-{i:03d}",
                    nombre="Servicio de aseo",
                    descripcion="",
                    estado="publicada",
                    fecha_cierre=ahora + timedelta(days=1),
                )
            )
        s.commit()

    v1 = _V1Falso(fallar_en=3, error=error)
    settings = Settings(
        mp_ticket="T",
        database_url="sqlite:///:memory:",
        secret_key="clave-de-test-32bytesxxxxxxxxxx",
        jobs_token="token-de-test-jobs-abcdefgh1234",
    )

    with Session(db_engine) as session, pytest.raises(type(error)):
        refresh_estados(session, v1, None, settings, max_requests=10)

    assert v1.llamadas == 3


# ---------------------------------------------------------------------------
# 4 — el advisory lock no se suelta solo
# ---------------------------------------------------------------------------


def test_la_conexion_del_lock_esta_en_autocommit(db_engine) -> None:
    """El bug: tras ejecutar `pg_try_advisory_lock`, la conexión quedaba dentro de
    una transacción abierta durante todo `fn()`. Neon la mataba por
    idle_in_transaction_session_timeout y el lock se soltaba a mitad de corrida.

    Nota: `in_transaction()` NO sirve de sonda — en AUTOCOMMIT SQLAlchemy sigue
    reportando True aunque el servidor no mantenga ninguna transacción abierta.
    Lo que se comprueba es la opción efectiva de la conexión.
    """
    from app.ingest.orchestrator import _run_with_lock

    vistas: list[str | None] = []

    def try_lock(conn: Any, key: int) -> bool:
        vistas.append(conn.get_execution_options().get("isolation_level"))
        return True

    _run_with_lock("test_autocommit", lambda: {"ok": 1}, db_engine, try_lock, lambda c, k: None)
    assert vistas == ["AUTOCOMMIT"]


def test_la_conexion_del_lock_no_retiene_una_transaccion_abierta(tmp_path) -> None:
    """Prueba de comportamiento: lo que escribe la conexión del lock mientras
    `fn()` corre ya está confirmado, o sea que no hay transacción retenida."""
    from sqlalchemy import text

    # Archivo en disco: con SQLite en memoria las conexiones no son independientes.
    import app.models.tables  # noqa: F401
    from app.ingest.orchestrator import _run_with_lock

    url = f"sqlite:///{tmp_path / 'lock.db'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)  # job_runs: la telemetría de _run_with_lock
    with engine.begin() as c:
        c.execute(text("CREATE TABLE marca (n INTEGER)"))

    capturada: list[Any] = []

    def try_lock(conn: Any, key: int) -> bool:
        conn.execute(text("INSERT INTO marca (n) VALUES (1)"))
        capturada.append(conn)
        return True

    visible_desde_afuera: list[int] = []

    def job_fn() -> dict[str, int]:
        # Mientras fn() corre, la conexión del lock está idle. Si retuviera una
        # transacción, esta fila no sería visible desde otra conexión.
        otro = create_engine(url)
        with otro.connect() as c2:
            visible_desde_afuera.append(
                c2.execute(text("SELECT count(*) FROM marca")).scalar_one()
            )
        otro.dispose()
        return {"ok": 1}

    try:
        _run_with_lock("test_sin_tx", job_fn, engine, try_lock, lambda c, k: None)
    finally:
        engine.dispose()

    assert visible_desde_afuera == [1], "la conexión del lock retuvo una transacción abierta"


def test_el_unlock_sigue_corriendo_en_autocommit(db_engine) -> None:
    """`pg_advisory_unlock` en el finally tiene que seguir ejecutándose."""
    from sqlalchemy import text

    from app.ingest.orchestrator import _run_with_lock

    ejecutadas: list[str] = []

    def try_lock(conn: Any, key: int) -> bool:
        conn.execute(text("SELECT 1"))
        ejecutadas.append("lock")
        return True

    def unlock(conn: Any, key: int) -> None:
        # Una sentencia real: si la conexión quedara inutilizable, fallaría acá.
        conn.execute(text("SELECT 1"))
        ejecutadas.append("unlock")

    _run_with_lock("test_unlock", lambda: {"ok": 1}, db_engine, try_lock, unlock)
    assert ejecutadas == ["lock", "unlock"]


def test_el_camino_de_lock_ocupado_no_cambia(db_engine) -> None:
    from app.ingest.orchestrator import _run_with_lock

    llamadas = 0

    def job_fn() -> dict[str, int]:
        nonlocal llamadas
        llamadas += 1
        return {}

    resultado = _run_with_lock(
        "test_ocupado", job_fn, db_engine, lambda c, k: False, lambda c, k: None
    )
    assert resultado is None
    assert llamadas == 0


def test_fetch_detalles_sigue_ante_un_error_de_una_licitacion(db_engine) -> None:
    """Un error por-licitación (parseo, código raro) no corta el loop."""
    from app.core.settings import Settings
    from app.ingest.licitaciones import fetch_detalles_pendientes

    _sembrar_pendientes(db_engine, 5)
    v1 = _V1Falso(fallar_en=3, error=ValueError("json corrupto"))
    settings = Settings(
        mp_ticket="T",
        database_url="sqlite:///:memory:",
        secret_key="clave-de-test-32bytesxxxxxxxxxx",
        jobs_token="token-de-test-jobs-abcdefgh1234",
    )

    with Session(db_engine) as session:
        r = fetch_detalles_pendientes(session, v1, settings, max_requests=5)

    assert v1.llamadas == 5, "los errores por-licitación no cortan el loop"
    assert r["errores"] == 3
    assert r["procesadas"] == 2
