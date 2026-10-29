"""Tests F-429-pausa-ca: un job con API que arranca en pausa espera o se omite, no falla.

Sin esperas reales: `sleep` avanza un reloj falso que también es el "ahora" del
chequeo. SQLite en archivo (en memoria, cada conexión vería una base distinta),
con el esquema completo: la pausa vive en sync_state y la telemetría en job_runs.
El advisory lock es SQL de Postgres, así que se reemplaza por un doble.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.clients.base import APIPausadaError, MPTasaError, QuotaTracker
from app.core.settings import Settings
from app.models.tables import JobRun

_ENV = {
    "MP_TICKET": "ticket-test-pausa-ca",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-pausa-ca-32bytesxxxxxx",
    "JOBS_TOKEN": "token-test-pausa-ca-xxxxxxxxxxxxx",
}
_T0 = datetime(2026, 10, 29, 13, 5, tzinfo=UTC)  # 10:05 Chile


@pytest.fixture()
def engine(tmp_path) -> Iterator[Engine]:
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine(f"sqlite:///{tmp_path / 'pausa.db'}")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class _Reloj:
    """`ahora` y `dormir` comparten el tiempo; `al_dormir` simula a otro proceso."""

    def __init__(self, engine: Engine) -> None:
        self.t = _T0
        self.engine = engine
        self.dormidas: list[float] = []
        self.conexiones_al_dormir: list[int] = []
        self.al_dormir: Any = None

    def ahora(self) -> datetime:
        return self.t

    def dormir(self, s: float) -> None:
        self.conexiones_al_dormir.append(self.engine.pool.checkedout())  # type: ignore[attr-defined]
        self.dormidas.append(s)
        self.t += timedelta(seconds=s)
        if self.al_dormir:
            self.al_dormir(self)


def _pausar(engine: Engine, hasta: datetime, causa: str = "429/429") -> None:
    QuotaTracker(engine, budget=9000, now_fn=lambda: _T0).pausar(hasta, causa)


def _lock_real_sin_pg(nombre: str, fn: Any, eng: Any, **kw: Any) -> Any:
    """El `_run_with_lock` de verdad (telemetría y omitido incluidos) con el lock simulado."""
    from app.ingest.orchestrator import _run_with_lock

    return _run_with_lock(
        nombre, fn, eng, try_lock_fn=lambda _c, _k: True, unlock_fn=lambda _c, _k: None, **kw
    )


def _cli(settings: Settings, engine: Engine, reloj: _Reloj, job: str, **runners: Any) -> Any:
    """Corre `cmd_run_once(job)` con el reloj falso y los runners reemplazados."""
    from app.ingest import __main__ as cli

    parches = [
        patch.object(cli, "setup_logging"),
        patch.object(cli, "get_settings", return_value=settings),
        patch.object(cli, "_make_engine", return_value=engine),
        patch.object(cli, "_run_with_lock", side_effect=_lock_real_sin_pg),
        patch.object(cli, "_ahora", side_effect=reloj.ahora),
        patch.object(cli.time, "sleep", side_effect=reloj.dormir),
    ] + [patch.object(cli, nombre, **conf) for nombre, conf in runners.items()]
    for p in parches:
        p.start()
    try:
        return cli.cmd_run_once(job)
    finally:
        for p in reversed(parches):
            p.stop()


def _filas(engine: Engine, job: str) -> list[JobRun]:
    with Session(engine) as s:
        return list(s.execute(select(JobRun).where(JobRun.job == job)).scalars())


# ---------------------------------------------------------------------------
# Chequeo antes del lock
# ---------------------------------------------------------------------------


def test_sin_pausa_corre_sin_dormir(settings, engine, capsys) -> None:
    reloj = _Reloj(engine)
    _cli(settings, engine, reloj, "ca", run_sync_ca={"return_value": {"ventanas_completadas": 1}})

    assert reloj.dormidas == []
    assert [f.estado for f in _filas(engine, "ca")] == ["ok"]
    assert "omitido" not in capsys.readouterr().out


def test_pausa_que_vence_pronto_espera_sin_retener_conexion_y_corre(
    settings, engine, capsys
) -> None:
    reloj = _Reloj(engine)
    _pausar(engine, _T0 + timedelta(minutes=10))
    vista_al_correr: list[datetime] = []

    def _ca(*_a: Any) -> dict[str, int]:
        vista_al_correr.append(reloj.t)
        return {"ventanas_completadas": 1}

    _cli(settings, engine, reloj, "ca", run_sync_ca={"side_effect": _ca})

    assert reloj.dormidas == [10 * 60 + 30]
    assert reloj.conexiones_al_dormir == [0], "no retiene conexión mientras duerme"
    assert vista_al_correr == [_T0 + timedelta(minutes=10, seconds=30)]
    assert [f.estado for f in _filas(engine, "ca")] == ["ok"]


def test_pausa_que_vence_tarde_se_omite_con_aviso(settings, engine, capsys) -> None:
    reloj = _Reloj(engine)
    _pausar(engine, _T0 + timedelta(minutes=40), "429/desconocido:9999")

    with patch("app.ingest.__main__.run_sync_ca") as ca:
        _cli(settings, engine, reloj, "ca")  # sin SystemExit: sale 0

    ca.assert_not_called()
    assert reloj.dormidas == []
    filas = _filas(engine, "ca")
    assert [f.estado for f in filas] == ["omitido"]
    assert filas[0].resultado_json["omitido_por"] == "api_pausa"
    assert "::warning::[ca] omitido: API en pausa por 429 hasta 10:45 Chile" in capsys.readouterr().out


def test_pausa_alargada_mientras_espera_recalcula_y_omite(settings, engine, capsys) -> None:
    """Primero vence en 10 min; al despertar otro proceso la alargó 10 más: el total
    pasaría de 15 min, así que se omite sin volver a dormir."""
    reloj = _Reloj(engine)
    _pausar(engine, _T0 + timedelta(minutes=10))
    reloj.al_dormir = lambda r: _pausar(engine, r.t + timedelta(minutes=10))

    with patch("app.ingest.__main__.run_sync_ca") as ca:
        _cli(settings, engine, reloj, "ca")

    ca.assert_not_called()
    assert reloj.dormidas == [10 * 60 + 30]
    assert [f.estado for f in _filas(engine, "ca")] == ["omitido"]
    assert "::warning::[ca] omitido" in capsys.readouterr().out


def test_pausa_alargada_dentro_del_maximo_vuelve_a_esperar(settings, engine) -> None:
    reloj = _Reloj(engine)
    _pausar(engine, _T0 + timedelta(minutes=5))
    alargada = [False]

    def _alargar(r: _Reloj) -> None:
        if not alargada[0]:
            alargada[0] = True
            _pausar(engine, r.t + timedelta(minutes=5))

    reloj.al_dormir = _alargar
    _cli(settings, engine, reloj, "ca", run_sync_ca={"return_value": {}})

    assert reloj.dormidas == [5 * 60 + 30, 5 * 60 + 30]
    assert [f.estado for f in _filas(engine, "ca")] == ["ok"]


def test_pausa_vencida_no_espera(settings, engine) -> None:
    reloj = _Reloj(engine)
    _pausar(engine, _T0 - timedelta(minutes=1))
    _cli(settings, engine, reloj, "ca", run_sync_ca={"return_value": {}})
    assert reloj.dormidas == []


# ---------------------------------------------------------------------------
# Pausa que aparece entre el chequeo y la primera request
# ---------------------------------------------------------------------------


def test_api_pausada_en_la_primera_request_es_omitido(settings, engine, capsys) -> None:
    reloj = _Reloj(engine)
    _cli(
        settings,
        engine,
        reloj,
        "ca",
        run_sync_ca={"side_effect": APIPausadaError("API en pausa por 429 hasta 10:25 Chile")},
    )  # sin SystemExit: sale 0

    filas = _filas(engine, "ca")
    assert [f.estado for f in filas] == ["omitido"]
    assert filas[0].resultado_json == {"omitido_por": "APIPausadaError"}
    salida = capsys.readouterr()
    assert "::warning::[ca] omitido: API en pausa por 429 hasta 10:25 Chile" in salida.out
    assert "ERROR" not in salida.err


def test_tasa_en_la_primera_request_sigue_siendo_error(settings, engine, capsys) -> None:
    reloj = _Reloj(engine)
    with pytest.raises(SystemExit) as exc:
        _cli(
            settings,
            engine,
            reloj,
            "ca",
            run_sync_ca={
                "side_effect": MPTasaError("tasa", retry_after_seconds=1200, causa="429/429")
            },
        )

    assert exc.value.code == 1
    assert [f.estado for f in _filas(engine, "ca")] == ["error"]
    assert "[ca] ERROR: MPTasaError" in capsys.readouterr().err


def test_sin_propagar_la_api_pausada_sigue_siendo_error(engine) -> None:
    """Scheduler, endpoint y pasos del nocturno no cambian."""
    from app.ingest.orchestrator import _run_with_lock

    def _pausada() -> dict[str, int]:
        raise APIPausadaError("API en pausa por 429 hasta 10:25 Chile")

    r = _run_with_lock("lifecycle", _pausada, engine, lambda _c, _k: True, lambda _c, _k: None)

    assert r is None
    assert [f.estado for f in _filas(engine, "lifecycle")] == ["error"]


# ---------------------------------------------------------------------------
# Jobs que no esperan
# ---------------------------------------------------------------------------


def test_nocturno_no_espera_ni_se_omite(settings, engine) -> None:
    reloj = _Reloj(engine)
    _pausar(engine, _T0 + timedelta(minutes=40))

    with patch("app.ingest.__main__._ciclo_nocturno", return_value={}) as nocturno:
        _cli(settings, engine, reloj, "nocturno")

    nocturno.assert_called_once()
    assert reloj.dormidas == []
    assert _filas(engine, "nocturno") == []


def test_nocturno_corre_sus_pasos_sin_api_con_pausa_vigente(settings, engine) -> None:
    """Los pasos del nocturno no pasan por el chequeo: datos abiertos y competencia
    corren aunque la API esté en pausa."""
    from app.ingest import orchestrator

    _pausar(engine, datetime.now(UTC) + timedelta(minutes=40))
    corridos: list[str] = []

    def _paso(nombre: str) -> Any:
        return lambda *_a, **_k: corridos.append(nombre) or {}

    with (
        patch.object(orchestrator, "_run_with_lock", side_effect=lambda n, fn, e, **kw: fn()),
        patch.object(orchestrator, "CacheZipsDA"),
        patch.object(orchestrator, "run_datos_abiertos", side_effect=_paso("datos_abiertos")),
        patch.object(orchestrator, "run_competencia", side_effect=_paso("competencia")),
        patch.object(orchestrator, "run_estados_vencidos", return_value={}),
        patch.object(orchestrator, "run_lifecycle", return_value={}),
        patch.object(orchestrator, "run_backfill_fecha", return_value={}),
        patch.object(orchestrator, "run_rellenar_organismo", return_value={}),
        patch.object(orchestrator, "run_detalles_match", return_value={}),
    ):
        orchestrator._ciclo_nocturno(
            settings, engine, now_fn=lambda tz: datetime(2026, 10, 29, 1, 30, tzinfo=tz)
        )

    assert corridos == ["datos_abiertos", "competencia"]


def test_job_sin_api_corre_normal_con_pausa(settings, engine) -> None:
    reloj = _Reloj(engine)
    _pausar(engine, _T0 + timedelta(minutes=40))

    _cli(settings, engine, reloj, "match", run_match={"return_value": {"matches": 0}})

    assert reloj.dormidas == []
    assert [f.estado for f in _filas(engine, "match")] == ["ok"]


def test_jobs_con_api_son_jobs_del_cli() -> None:
    from app.ingest import __main__ as cli

    assert set(cli._JOBS) >= cli._JOBS_CON_API
    assert {"nocturno", "estados-vencidos", "match", "plan-anual"}.isdisjoint(cli._JOBS_CON_API)
