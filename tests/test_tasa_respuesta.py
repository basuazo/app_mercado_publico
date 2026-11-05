"""Tests F-tasa-respuesta: espera mínima 1/rps después de cada respuesta de la API.

Reloj y sleep falsos (ninguna espera real); red mockeada con respx.
"""

from __future__ import annotations

import contextlib
import random

import httpx
import pytest
import respx
from sqlalchemy import create_engine

from app.clients.base import BaseClient, QuotaTracker, RateLimiter

_URL = "https://api2.mercadopublico.cl/v2/compra-agil"


class _Reloj:
    def __init__(self) -> None:
        self.t = 1_000.0

    def monotonic(self) -> float:
        return self.t

    def sleep(self, segundos: float) -> None:
        self.t += max(0.0, segundos)


@pytest.fixture()
def reloj(monkeypatch: pytest.MonkeyPatch) -> _Reloj:
    r = _Reloj()
    monkeypatch.setattr("app.clients.base.time.monotonic", r.monotonic)
    monkeypatch.setattr("app.clients.base.time.sleep", r.sleep)
    # Sin jitter: las esperas medidas son exactas.
    monkeypatch.setattr(random, "uniform", lambda _a, _b: 0.0)
    return r


def test_respuesta_lenta_espera_desde_el_fin(reloj: _Reloj) -> None:
    rl = RateLimiter(rps=0.5)
    rl.acquire()  # primera: el bucket parte lleno
    reloj.t += 14.0  # respuesta de 14 s: el bucket se recarga entera
    rl.tras_respuesta()
    antes = reloj.t
    rl.acquire()
    assert reloj.t - antes >= 2.0  # 1/rps desde el fin de la respuesta


def test_respuesta_rapida_gobierna_el_bucket(reloj: _Reloj) -> None:
    rl = RateLimiter(rps=0.5)
    rl.acquire()
    inicio = reloj.t
    rl.tras_respuesta()  # respuesta instantánea: la espera nueva coincide con la del bucket
    rl.acquire()
    # Entre inicios de petición sigue habiendo 1/rps (el bucket, no la espera nueva).
    assert reloj.t - inicio == pytest.approx(2.0, abs=0.01)


def test_no_acorta_un_enfriamiento_en_curso(reloj: _Reloj) -> None:
    rl = RateLimiter(rps=0.5)
    rl.enfriar(1200, causa="test")
    rl.tras_respuesta()
    antes = reloj.t
    rl.acquire()
    assert reloj.t - antes >= 1200


@pytest.fixture()
def cliente(reloj: _Reloj) -> BaseClient:
    engine = create_engine("sqlite:///:memory:")
    quota = QuotaTracker(engine, budget=100)
    return BaseClient(ticket="t", rate_limiter=RateLimiter(rps=0.5), quota=quota)


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(200, json={"ok": True}),
        httpx.Response(429, json={"errors": [{"codigo": "429", "mensaje": "tasa"}]}),
        httpx.Response(504, text="timeout"),
        httpx.ConnectTimeout("t"),
    ],
    ids=["200", "429", "504", "timeout"],
)
def test_request_llama_tras_respuesta_con_cualquier_desenlace(
    cliente: BaseClient, monkeypatch: pytest.MonkeyPatch, respuesta: httpx.Response | Exception
) -> None:
    llamadas: list[int] = []
    monkeypatch.setattr(cliente._rate_limiter, "tras_respuesta", lambda: llamadas.append(1))
    with respx.mock:

        def responder(_request: httpx.Request) -> httpx.Response:
            if isinstance(respuesta, Exception):
                raise respuesta
            return respuesta

        respx.get(_URL).mock(side_effect=responder)
        with contextlib.suppress(Exception):  # el error de cada desenlace no importa acá
            cliente._request("GET", _URL, reintentar_transitorios=False)
    assert len(llamadas) == 1
