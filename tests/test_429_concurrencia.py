"""Tests F-429-concurrencia: 10500 con backoff, enfriamiento tras 504/timeout.

Todo mockeado con respx y con el tiempo parchado (time.monotonic y time.sleep
sobre un reloj falso): ninguna espera real, ninguna llamada real a la API. Las
esperas se miden como tiempo simulado entre requests emitidas.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest
import respx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.clients.base import (
    BaseClient,
    MPConcurrencyError,
    MPRateLimitError,
    MPServerError,
    QuotaExceededError,
    QuotaTracker,
    RateLimiter,
    rate_limiter_compartido,
    reset_rate_limiter_compartido,
)
from app.core.settings import Settings
from app.models.base import Base

_FAKE_TICKET = "ticket-de-test-429"
_URL = "https://api.mercadopublico.cl/servicios/v1/publico/prueba"
_URL_V2 = "https://api2.mercadopublico.cl/v2/prueba"
_CUERPO_10500 = {
    "Codigo": 10500,
    "Mensaje": "Lo sentimos. Hemos detectado que existen peticiones simultáneas.",
}


class _Reloj:
    """Reloj monotónico falso: sleep() lo adelanta sin esperar."""

    def __init__(self) -> None:
        self.t = 1_000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, segundos: float) -> None:
        self.t += max(0.0, segundos)


@pytest.fixture(autouse=True)
def reloj(monkeypatch: pytest.MonkeyPatch) -> _Reloj:
    r = _Reloj()
    monkeypatch.setattr("app.clients.base.time.monotonic", r.monotonic)
    monkeypatch.setattr("app.clients.base.time.sleep", r.sleep)
    return r


@pytest.fixture()
def mem_engine():
    engine = create_engine("sqlite:///:memory:")
    yield engine
    engine.dispose()


@pytest.fixture()
def quota(mem_engine) -> QuotaTracker:
    return QuotaTracker(mem_engine, budget=100)


@pytest.fixture()
def limiter(reloj) -> RateLimiter:
    # rps alto: la espera propia del token bucket queda en milisegundos y no
    # se confunde con los backoff y enfriamientos que se miden.
    return RateLimiter(rps=1000.0)


@pytest.fixture()
def client(quota, limiter) -> BaseClient:
    return BaseClient(ticket=_FAKE_TICKET, rate_limiter=limiter, quota=quota)


def _secuencia(
    reloj: _Reloj, instantes: list[float], respuestas: list[httpx.Response | Exception]
) -> Callable[[httpx.Request], httpx.Response]:
    """side_effect de respx que anota el instante simulado de cada request."""
    pendientes = list(respuestas)

    def responder(request: httpx.Request) -> httpx.Response:
        instantes.append(reloj.t)
        r = pendientes.pop(0) if len(pendientes) > 1 else pendientes[0]
        if isinstance(r, Exception):
            raise r
        return r

    return responder


def _esperas(instantes: list[float]) -> list[float]:
    return [b - a for a, b in zip(instantes, instantes[1:], strict=False)]


def _settings() -> Settings:
    return Settings(
        mp_ticket=_FAKE_TICKET,
        database_url="sqlite:///:memory:",
        secret_key="clave-de-test-32bytesxxxxxxxxxx",
        jobs_token="token-de-test-jobs-abcdefgh1234",
        _env_file=None,  # type: ignore[call-arg]
    )


# ---------------------------------------------------------------------------
# 1 + 4 — distinguir el 10500 y reintentarlo con backoff 30/60/120
# ---------------------------------------------------------------------------


@respx.mock
def test_10500_y_luego_200_devuelve_los_datos(client, quota, reloj) -> None:
    instantes: list[float] = []
    respx.get(_URL).mock(
        side_effect=_secuencia(
            reloj,
            instantes,
            [httpx.Response(429, json=_CUERPO_10500), httpx.Response(200, json={"ok": True})],
        )
    )
    assert client._request("GET", _URL) == {"ok": True}
    assert quota.remaining() == 98, "las dos requests emitidas cuentan"
    (espera,) = _esperas(instantes)
    assert 30.0 <= espera <= 36.01, "primer backoff: 30 s + jitter de hasta 20 %"


@respx.mock
def test_10500_persistente_propaga_concurrency_error(client, quota, reloj) -> None:
    instantes: list[float] = []
    ruta = respx.get(_URL).mock(
        side_effect=_secuencia(reloj, instantes, [httpx.Response(429, json=_CUERPO_10500)])
    )
    with pytest.raises(MPConcurrencyError) as exc:
        client._request("GET", _URL)

    assert isinstance(exc.value, MPRateLimitError), "los runners lo cortan sin conocerlo"
    assert exc.value.retry_after_seconds == 900
    assert "Concurrencia (429/10500)" in str(exc.value)
    assert "Cuota agotada" not in str(exc.value)
    assert ruta.call_count == 4
    assert quota.remaining() == 96
    esperas = _esperas(instantes)
    assert len(esperas) == 3
    for espera, base in zip(esperas, (30.0, 60.0, 120.0), strict=True):
        assert base <= espera <= base * 1.2 + 0.01


@respx.mock
def test_las_esperas_del_10500_pasan_por_enfriar(client, limiter) -> None:
    """El backoff es un enfriamiento del limiter del proceso, no un sleep suelto:
    así frena también a cualquier otro cliente que comparta el limiter."""
    llamadas: list[tuple[float, str]] = []
    original = limiter.enfriar

    def espia(segundos: float, causa: str) -> None:
        llamadas.append((segundos, causa))
        original(segundos, causa)

    limiter.enfriar = espia  # type: ignore[method-assign]
    respx.get(_URL).mock(return_value=httpx.Response(429, json=_CUERPO_10500))
    with pytest.raises(MPConcurrencyError):
        client._request("GET", _URL)

    assert [c for _s, c in llamadas] == ["429/10500"] * 3
    for (segundos, _c), base in zip(llamadas, (30.0, 60.0, 120.0), strict=True):
        assert base <= segundos <= base * 1.2


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(429),
        httpx.Response(429, text="<html>Too Many Requests</html>"),
        httpx.Response(429, json={"Codigo": 10501, "Mensaje": "otro límite"}),
        httpx.Response(429, json={"Mensaje": "sin código"}),
        httpx.Response(429, json={"errors": "no es lista"}),
        httpx.Response(429, json=[10500]),
        httpx.Response(429, json={"Codigo": True}),
    ],
    ids=[
        "sin-cuerpo",
        "html",
        "otro-codigo",
        "json-sin-codigo",
        "errors-no-lista",
        "json-lista",
        "codigo-bool",
    ],
)
@respx.mock
def test_otro_429_sigue_sin_reintentarse(client, quota, reloj, respuesta) -> None:
    ruta = respx.get(_URL).mock(return_value=respuesta)
    antes = reloj.t
    with pytest.raises(MPRateLimitError) as exc:
        client._request("GET", _URL)

    assert not isinstance(exc.value, MPConcurrencyError)
    assert "Cuota agotada (429)" in str(exc.value)
    assert ruta.call_count == 1
    assert quota.remaining() == 99
    assert reloj.t - antes < 0.01, "sin espera"


@pytest.mark.parametrize(
    "cuerpo",
    [
        {"success": False, "payload": None, "errors": [{"codigo": 10500, "mensaje": "simult"}]},
        {"success": False, "errors": ["texto suelto", {"Codigo": "10500"}]},
        {"codigo": "10500"},
    ],
    ids=["errors-v2", "errors-mixto-y-string", "raiz-minuscula-string"],
)
@respx.mock
def test_el_10500_se_reconoce_en_otras_formas(client, cuerpo) -> None:
    respx.get(_URL).mock(
        side_effect=[httpx.Response(429, json=cuerpo), httpx.Response(200, json={"ok": 1})]
    )
    assert client._request("GET", _URL) == {"ok": 1}


@respx.mock
def test_el_warning_del_429_incluye_el_codigo(client, caplog) -> None:
    respx.get(_URL).mock(return_value=httpx.Response(429, json=_CUERPO_10500))
    with caplog.at_level(logging.WARNING), pytest.raises(MPConcurrencyError):
        client._request("GET", _URL)

    mensajes = [r.getMessage() for r in caplog.records]
    assert any("HTTP 429 codigo=10500" in m and "headers_cuota=" in m for m in mensajes)
    reintentos = [m for m in mensajes if "MPConcurrencyError" in m and "reintento" in m]
    assert len(reintentos) == 3
    assert _FAKE_TICKET not in "\n".join(mensajes)


# ---------------------------------------------------------------------------
# 2 + 3 — enfriamiento tras un 504 o un timeout
# ---------------------------------------------------------------------------


@respx.mock
def test_tras_un_504_la_request_de_otro_cliente_espera_60(mem_engine, reloj) -> None:
    """El caso del log: ca (v2) muere con 504 y la primera request de match (v1)."""
    from app.clients.mp_v1 import MercadoPublicoV1Client
    from app.clients.mp_v2 import MercadoPublicoV2Client

    settings = _settings()
    v2 = MercadoPublicoV2Client(settings, mem_engine)
    v1 = MercadoPublicoV1Client(settings, mem_engine)
    instantes_v2: list[float] = []
    instantes_v1: list[float] = []
    timeout = httpx.Response(504, json={"message": "Endpoint request timed out"})
    respx.get(_URL_V2).mock(side_effect=_secuencia(reloj, instantes_v2, [timeout]))
    respx.get(_URL).mock(
        side_effect=_secuencia(reloj, instantes_v1, [httpx.Response(200, json={"ok": 1})])
    )

    with pytest.raises(MPServerError):
        v2._client._request("GET", _URL_V2)
    v1._client._request("GET", _URL)

    assert len(instantes_v2) == 2, "la cantidad de reintentos del 5xx no cambia"
    assert instantes_v2[1] - instantes_v2[0] >= 60.0, "el reintento del 504 también enfría"
    assert instantes_v1[0] - instantes_v2[-1] >= 60.0


@respx.mock
def test_tras_un_timeout_la_request_de_otro_cliente_espera_60(quota, limiter, reloj) -> None:
    v2 = BaseClient(ticket=_FAKE_TICKET, rate_limiter=limiter, quota=quota)
    v1 = BaseClient(ticket=_FAKE_TICKET, rate_limiter=limiter, quota=quota)
    instantes_v2: list[float] = []
    instantes_v1: list[float] = []
    respx.get(_URL_V2).mock(
        side_effect=_secuencia(reloj, instantes_v2, [httpx.TimeoutException("agotado")])
    )
    respx.get(_URL).mock(
        side_effect=_secuencia(reloj, instantes_v1, [httpx.Response(200, json={"ok": 1})])
    )

    with pytest.raises(MPServerError):
        v2._request("GET", _URL_V2)
    v1._request("GET", _URL)

    assert len(instantes_v2) == 3, "la cantidad de reintentos del timeout no cambia"
    assert all(e >= 60.0 for e in _esperas(instantes_v2))
    assert instantes_v1[0] - instantes_v2[-1] >= 60.0


@pytest.mark.parametrize("status", [500, 502, 503])
@respx.mock
def test_los_otros_5xx_no_enfrian(client, reloj, status) -> None:
    instantes: list[float] = []
    respx.get(_URL).mock(
        side_effect=_secuencia(
            reloj,
            instantes,
            [httpx.Response(status, text="boom"), httpx.Response(200, json={"ok": 1})],
        )
    )
    client._request("GET", _URL)
    (espera,) = _esperas(instantes)
    assert 2.0 <= espera < 3.0, "conserva la espera actual de 2 s"


def test_enfriar_mas_corto_no_acorta_uno_en_curso(limiter, reloj) -> None:
    inicio = reloj.t
    limiter.enfriar(120, causa="test largo")
    limiter.enfriar(10, causa="test corto")
    limiter.acquire()
    assert reloj.t - inicio >= 120.0


def test_enfriar_mas_largo_extiende_el_en_curso(limiter, reloj) -> None:
    inicio = reloj.t
    limiter.enfriar(10, causa="test corto")
    limiter.enfriar(120, causa="test largo")
    limiter.acquire()
    assert reloj.t - inicio >= 120.0


def test_sin_enfriamiento_acquire_no_espera(limiter, reloj) -> None:
    inicio = reloj.t
    limiter.acquire()
    assert reloj.t - inicio < 0.01


# ---------------------------------------------------------------------------
# 5 — cada intento pasa por el presupuesto y el rate limiter
# ---------------------------------------------------------------------------


def _espiar_acquire(limiter: RateLimiter) -> list[int]:
    llamadas: list[int] = []
    original = limiter.acquire

    def acquire() -> None:
        llamadas.append(1)
        original()

    limiter.acquire = acquire  # type: ignore[method-assign]
    return llamadas


@pytest.mark.parametrize(
    ("respuestas", "esperadas"),
    [
        ([httpx.Response(504, text="boom"), httpx.Response(200, json={"ok": 1})], 2),
        ([httpx.Response(500, text="boom"), httpx.Response(200, json={"ok": 1})], 2),
        (
            [
                httpx.TimeoutException("t1"),
                httpx.TimeoutException("t2"),
                httpx.Response(200, json={"ok": 1}),
            ],
            3,
        ),
        (
            [
                httpx.Response(429, json=_CUERPO_10500),
                httpx.Response(429, json=_CUERPO_10500),
                httpx.Response(200, json={"ok": 1}),
            ],
            3,
        ),
    ],
    ids=["504", "500", "timeout", "10500"],
)
@respx.mock
def test_acquire_una_vez_por_intento(client, limiter, respuestas, esperadas) -> None:
    llamadas = _espiar_acquire(limiter)
    respx.get(_URL).mock(side_effect=respuestas)
    client._request("GET", _URL)
    assert len(llamadas) == esperadas


@respx.mock
def test_los_contadores_de_reintento_son_independientes(client) -> None:
    """Un 504 no gasta el reintento de 10500, ni un timeout el del 504."""
    ruta = respx.get(_URL).mock(
        side_effect=[
            httpx.Response(429, json=_CUERPO_10500),
            httpx.Response(504, text="boom"),
            httpx.TimeoutException("t"),
            httpx.Response(429, json=_CUERPO_10500),
            httpx.Response(200, json={"ok": 1}),
        ]
    )
    assert client._request("GET", _URL) == {"ok": 1}
    assert ruta.call_count == 5


@respx.mock
def test_presupuesto_agotado_entre_reintentos(mem_engine, limiter) -> None:
    quota = QuotaTracker(mem_engine, budget=2)
    client = BaseClient(ticket=_FAKE_TICKET, rate_limiter=limiter, quota=quota)
    ruta = respx.get(_URL).mock(return_value=httpx.Response(429, json=_CUERPO_10500))

    with pytest.raises(QuotaExceededError):
        client._request("GET", _URL)

    assert ruta.call_count == 2, "el tercer intento no se emite: no queda presupuesto"
    assert quota.remaining() == 0


# ---------------------------------------------------------------------------
# 2 — un solo rate limiter por proceso
# ---------------------------------------------------------------------------


def test_v1_y_v2_comparten_el_rate_limiter(mem_engine) -> None:
    from app.clients.mp_v1 import MercadoPublicoV1Client
    from app.clients.mp_v2 import MercadoPublicoV2Client

    settings = _settings()
    v1 = MercadoPublicoV1Client(settings, mem_engine)
    v2 = MercadoPublicoV2Client(settings, mem_engine)
    assert v1._client._rate_limiter is v2._client._rate_limiter


def test_con_otra_tasa_gana_la_primera_y_avisa(caplog) -> None:
    primero = rate_limiter_compartido(1.0)
    with caplog.at_level(logging.WARNING):
        segundo = rate_limiter_compartido(5.0)
    assert segundo is primero
    assert segundo.rps == 1.0
    assert any("se mantiene el primero" in r.getMessage() for r in caplog.records)


def test_el_reset_crea_un_limiter_nuevo() -> None:
    primero = rate_limiter_compartido(1.0)
    reset_rate_limiter_compartido()
    segundo = rate_limiter_compartido(3.0)
    assert segundo is not primero
    assert segundo.rps == 3.0


# ---------------------------------------------------------------------------
# Runner: 10500 persistente corta el loop y guarda el progreso parcial
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


@respx.mock
def test_fetch_detalles_corta_con_10500_persistente(db_engine) -> None:
    from app.clients.mp_v1 import MercadoPublicoV1Client
    from app.ingest.licitaciones import fetch_detalles_pendientes
    from app.models.tables import Licitacion, SyncState

    with Session(db_engine) as s:
        for i in range(10):
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

    # Las dos primeras bajan bien; desde la tercera la API rechaza por concurrencia.
    emitidas: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        codigo = request.url.params["codigo"]
        emitidas.append(codigo)
        if len(set(emitidas)) <= 2:
            return httpx.Response(
                200,
                json={"Listado": [{"CodigoExterno": codigo, "Nombre": "Aseo", "CodigoEstado": 5}]},
            )
        return httpx.Response(429, json=_CUERPO_10500)

    respx.get(url__startswith="https://api.mercadopublico.cl/servicios/v1/").mock(
        side_effect=responder
    )
    v1 = MercadoPublicoV1Client(_settings(), db_engine)

    with Session(db_engine) as session, pytest.raises(MPConcurrencyError):
        fetch_detalles_pendientes(session, v1, _settings(), max_requests=10)

    # 2 éxitos + 4 intentos de la tercera; no siguió con las 7 restantes.
    assert len(emitidas) == 6
    assert len(set(emitidas)) == 3

    with Session(db_engine) as s:
        hechas = [lic.codigo for lic in s.query(Licitacion).all() if lic.detalle_obtenido]
        estado = s.get(SyncState, "licitaciones_detalles")

    assert len(hechas) == 2, "las dos que alcanzaron a bajar deben quedar guardadas"
    assert estado is not None
    assert estado.ultimo_ok is None, "un corte del canal no es una corrida OK"
