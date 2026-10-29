"""Tests F-429-tasa: el 429 "límite de tasa" pausa la API en vez de agotar el día.

El cuerpo del 429/429 es el que llegó de verdad en Actions (18–29 oct 2026).
Red mockeada con respx; SQLite en memoria con el esquema completo, porque la
pausa vive en sync_state. El reloj del QuotaTracker se inyecta.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import respx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.clients.base import (
    APIPausadaError,
    BaseClient,
    MPConcurrencyError,
    MPRateLimitError,
    MPTasaError,
    QuotaExceededError,
    QuotaTracker,
    RateLimiter,
)
from app.core.tiempo import TZ_CHILE

_URL = "https://api2.mercadopublico.cl/v2/compra-agil"
_CUERPO_429_TASA = {
    "errors": [
        {
            "codigo": "429",
            "mensaje": "Se ha superado el límite de tasa de solicitudes. "
            "Intente nuevamente más tarde.",
        }
    ]
}
_HOY = datetime(2026, 10, 29, 10, 0, tzinfo=TZ_CHILE)


@pytest.fixture()
def engine() -> Iterator[Engine]:
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture(autouse=True)
def sin_esperas(monkeypatch: pytest.MonkeyPatch) -> None:
    # El enfriamiento del limiter tras el 429 es de 20 min: ningún test lo espera.
    monkeypatch.setattr("app.clients.base.time.sleep", lambda _s: None)


def _tracker(engine: Engine, cuando: datetime = _HOY, **kw: Any) -> QuotaTracker:
    return QuotaTracker(engine, budget=100, now_fn=lambda: cuando, **kw)


def _cliente(quota: QuotaTracker) -> BaseClient:
    return BaseClient(ticket="t", rate_limiter=RateLimiter(rps=1000.0), quota=quota)


def _pausa(engine: Engine) -> tuple[str, str] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT cursor, notas FROM sync_state WHERE fuente = 'api_pausa'")
        ).fetchone()
    return (row[0], row[1]) if row else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Clasificación y pausa
# ---------------------------------------------------------------------------


@respx.mock
def test_429_tasa_pausa_20_min_sin_reintento_ni_dia_agotado(engine) -> None:
    ruta = respx.get(_URL).mock(return_value=httpx.Response(429, json=_CUERPO_429_TASA))
    q = _tracker(engine)

    with pytest.raises(MPTasaError) as exc:
        _cliente(q)._request("GET", _URL)

    assert isinstance(exc.value, MPRateLimitError), "los runners lo cortan sin conocerlo"
    assert not isinstance(exc.value, MPConcurrencyError)
    assert exc.value.causa == "429/429"
    assert exc.value.retry_after_seconds == 20 * 60
    assert ruta.call_count == 1  # sin reintento
    assert q.remaining() == 99  # cuenta 1; el día NO sube a 9.000
    assert _pausa(engine) == (_iso(_HOY + timedelta(minutes=20)), "429/429")


@respx.mock
def test_otra_corrida_en_la_pausa_no_emite_y_al_vencer_si(engine) -> None:
    ruta = respx.get(_URL).mock(
        side_effect=[
            httpx.Response(429, json=_CUERPO_429_TASA),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    with pytest.raises(MPTasaError):
        _cliente(_tracker(engine))._request("GET", _URL)

    dentro = _tracker(engine, _HOY + timedelta(minutes=5))
    with pytest.raises(APIPausadaError) as exc:
        dentro.check_budget()
    assert isinstance(exc.value, QuotaExceededError), "los llamadores ya la cortan"
    assert "10:20 Chile" in str(exc.value)
    with pytest.raises(APIPausadaError):
        _cliente(dentro)._request("GET", _URL)
    assert ruta.call_count == 1  # no salió nada

    # 21 min después, mismo día Chile: emite normal.
    despues = _tracker(engine, _HOY + timedelta(minutes=21))
    assert _cliente(despues)._request("GET", _URL) == {"ok": True}
    assert ruta.call_count == 2


@pytest.mark.parametrize(
    ("respuesta", "causa"),
    [
        (httpx.Response(429, json={"errors": [{"codigo": "9999"}]}), "429/desconocido:9999"),
        (httpx.Response(429), "429/desconocido:ninguno"),
    ],
    ids=["codigo-raro", "sin-codigo"],
)
@respx.mock
def test_429_desconocido_pausa_60_min_y_log_error(engine, caplog, respuesta, causa) -> None:
    respx.get(_URL).mock(return_value=respuesta)
    q = _tracker(engine)

    with caplog.at_level(logging.WARNING), pytest.raises(MPTasaError) as exc:
        _cliente(q)._request("GET", _URL)

    assert exc.value.causa == causa
    assert exc.value.retry_after_seconds == 60 * 60
    assert q.remaining() == 99
    assert _pausa(engine) == (_iso(_HOY + timedelta(minutes=60)), causa)
    errores = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errores and "429 con código no visto" in errores[0].getMessage()
    assert causa.split(":")[1].replace("ninguno", "(ninguno)") in errores[0].getMessage()


@pytest.mark.parametrize(
    ("retry_after", "minutos"), [("3600", 60), ("5", 20)], ids=["manda-retry-after", "manda-la-pausa"]
)
@respx.mock
def test_retry_after_solo_alarga_la_pausa(engine, retry_after, minutos) -> None:
    respx.get(_URL).mock(
        return_value=httpx.Response(
            429, headers={"Retry-After": retry_after}, json=_CUERPO_429_TASA
        )
    )
    with pytest.raises(MPTasaError) as exc:
        _cliente(_tracker(engine))._request("GET", _URL)

    assert exc.value.retry_after_seconds == minutos * 60
    assert _pausa(engine) == (_iso(_HOY + timedelta(minutes=minutos)), "429/429")


def test_una_pausa_mas_corta_no_acorta_una_mas_larga(engine) -> None:
    q = _tracker(engine)
    q.pausar(_HOY + timedelta(minutes=60), "429/desconocido:9999")
    q.pausar(_HOY + timedelta(minutes=20), "429/429")

    assert _pausa(engine) == (_iso(_HOY + timedelta(minutes=60)), "429/desconocido:9999")
    # Una más larga sí la reemplaza; y es idempotente.
    q.pausar(_HOY + timedelta(minutes=90), "429/429")
    q.pausar(_HOY + timedelta(minutes=90), "429/429")
    assert _pausa(engine) == (_iso(_HOY + timedelta(minutes=90)), "429/429")
    assert q.pausada_hasta() == (_HOY + timedelta(minutes=90)).astimezone(UTC)


def test_pausa_vencida_no_corta(engine) -> None:
    _tracker(engine).pausar(_HOY - timedelta(minutes=1), "429/429")
    _tracker(engine).check_budget()


def test_el_429_tasa_enfria_el_limiter_del_proceso(engine) -> None:
    llamadas: list[tuple[float, str]] = []
    limiter = RateLimiter(rps=1000.0)
    limiter.enfriar = lambda s, causa: llamadas.append((s, causa))  # type: ignore[method-assign]
    cliente = BaseClient(ticket="t", rate_limiter=limiter, quota=_tracker(engine))

    with respx.mock:
        respx.get(_URL).mock(return_value=httpx.Response(429, json=_CUERPO_429_TASA))
        with pytest.raises(MPTasaError):
            cliente._request("GET", _URL)

    assert llamadas == [(1200, "429/429")]


def test_las_pausas_vienen_de_settings(monkeypatch, engine) -> None:
    from app.clients.mp_v2 import MercadoPublicoV2Client
    from app.core.settings import Settings

    for k, v in {
        "MP_TICKET": "ticket-test-429-tasa",
        "DATABASE_URL": "sqlite:///:memory:",
        "SECRET_KEY": "clave-test-429-tasa-32bytesxxxxxx",
        "JOBS_TOKEN": "token-test-429-tasa-xxxxxxxxxxxxx",
        "PAUSA_TASA_MIN": "7",
        "PAUSA_429_DESCONOCIDO_MIN": "45",
    }.items():
        monkeypatch.setenv(k, v)
    cliente = MercadoPublicoV2Client(Settings(_env_file=None), engine)  # type: ignore[call-arg]

    assert cliente._client._pausa_tasa_s == 7 * 60
    assert cliente._client._pausa_429_desconocido_s == 45 * 60


# ---------------------------------------------------------------------------
# CLI: el corte se ve en Actions
# ---------------------------------------------------------------------------


def _correr_cli(job: str, resultado: Any, capsys) -> tuple[str, str]:
    from app.ingest import __main__ as cli

    def _lock_transparente(_nombre: str, fn: Any, _eng: Any, **_kw: Any) -> Any:
        return fn()

    with (
        patch.object(cli, "setup_logging"),
        patch.object(cli, "get_settings"),
        patch.object(cli, "_make_engine"),
        patch.object(cli, "_run_with_lock", side_effect=_lock_transparente),
        patch.object(cli, "run_sync_ca", return_value=resultado),
        patch.object(cli, "run_detalles_match", return_value=resultado),
        patch.object(cli, "_ciclo_nocturno", return_value=resultado),
    ):
        cli.cmd_run_once(job)  # sale 0: no lanza SystemExit
    salida = capsys.readouterr()
    return salida.out, salida.err


def test_cli_avisa_el_corte_de_ca(capsys) -> None:
    out, _ = _correr_cli("ca", {"ventanas_completadas": 2, "cortado_por_error": "MPTasaError"}, capsys)
    assert "::warning::[ca] cortado por MPTasaError; avance parcial guardado" in out


def test_cli_avisa_el_corte_de_detalles_match(capsys) -> None:
    out, _ = _correr_cli(
        "detalles-match", {"detalles_interrumpidos": True, "cortado_por": "APIPausadaError"}, capsys
    )
    assert "::warning::[detalles-match] cortado por APIPausadaError" in out


def test_cli_avisa_cada_paso_cortado_del_nocturno(capsys) -> None:
    out, _ = _correr_cli(
        "nocturno",
        {
            "datos_abiertos": {"actualizadas": 3},
            "estados-vencidos": {"cortado_por": "MPTasaError"},
            "lifecycle": None,
            "detalles-match": {"cortado_por": "APIPausadaError"},
        },
        capsys,
    )
    avisos = [linea for linea in out.splitlines() if linea.startswith("::warning::")]
    assert avisos == [
        "::warning::[estados-vencidos] cortado por MPTasaError; avance parcial guardado",
        "::warning::[detalles-match] cortado por APIPausadaError; avance parcial guardado",
    ]


def test_cli_sin_corte_no_avisa(capsys) -> None:
    out, _ = _correr_cli("ca", {"ventanas_completadas": 3, "cortado_por_error": None}, capsys)
    assert "::warning::" not in out


# ---------------------------------------------------------------------------
# Postgres (Neon dev): el upsert de la pausa es SQL crudo, se prueba en el motor real
# ---------------------------------------------------------------------------

_DB_URL = os.environ.get("DATABASE_URL", "")


@pytest.mark.skipif(not _DB_URL.startswith("postgres"), reason="requiere DATABASE_URL de Postgres")
def test_pausa_en_postgres_no_se_acorta_y_corta_check_budget() -> None:
    """Fechas de enero: nunca deja una pausa vigente en la base de dev. La fila
    previa de `api_pausa`, si había, se restaura."""
    from app.core.db import normalizar_url_driver

    e = create_engine(normalizar_url_driver(_DB_URL), pool_pre_ping=True)
    with e.connect() as conn:
        previa = conn.execute(
            text("SELECT cursor, notas FROM sync_state WHERE fuente = 'api_pausa'")
        ).fetchone()
    base = datetime(2026, 1, 15, 10, 0, tzinfo=TZ_CHILE)
    try:
        with e.begin() as conn:
            conn.execute(text("DELETE FROM sync_state WHERE fuente = 'api_pausa'"))
        q = QuotaTracker(e, budget=9000, now_fn=lambda: base)
        q.pausar(base + timedelta(minutes=60), "429/desconocido:9999")
        q.pausar(base + timedelta(minutes=20), "429/429")
        assert _pausa(e) == (_iso(base + timedelta(minutes=60)), "429/desconocido:9999")
        q.pausar(base + timedelta(minutes=90), "429/429")
        assert _pausa(e) == (_iso(base + timedelta(minutes=90)), "429/429")

        with pytest.raises(APIPausadaError):
            QuotaTracker(e, budget=9000, now_fn=lambda: base + timedelta(minutes=30)).check_budget()
        QuotaTracker(e, budget=9000, now_fn=lambda: base + timedelta(minutes=91)).check_budget()
    finally:
        with e.begin() as conn:
            conn.execute(text("DELETE FROM sync_state WHERE fuente = 'api_pausa'"))
            if previa is not None:
                conn.execute(
                    text(
                        "INSERT INTO sync_state (fuente, cursor, notas, requests_usadas_hoy) "
                        "VALUES ('api_pausa', :c, :n, 0)"
                    ),
                    {"c": previa[0], "n": previa[1]},
                )
        e.dispose()
