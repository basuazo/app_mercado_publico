"""Tests F-actions-2 — esperar el advisory lock en vez de omitir de inmediato.

Sin esperas reales: `sleep_fn` avanza un reloj falso que también es `reloj_fn`.
El SQL del lock es de Postgres, así que se reemplaza por un doble; la base es
SQLite en archivo (en memoria, cada conexión vería una base distinta).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.settings import Settings
from app.ingest.orchestrator import _run_with_lock

_ENV = {
    "MP_TICKET": "ticket-test-lock-espera",
    "DATABASE_URL": "sqlite:///:memory:",
    "SECRET_KEY": "clave-test-lock-espera-32bytesxxxx",
    "JOBS_TOKEN": "token-test-lock-espera-xxxxxxxxxxx",
}


@pytest.fixture()
def engine(tmp_path):
    import app.models.tables  # noqa: F401
    from app.models.base import Base

    e = create_engine(f"sqlite:///{tmp_path / 'lock.db'}")
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)
    return Settings(_env_file=None)  # type: ignore[call-arg]


class _Reloj:
    """Reloj monotónico falso: `dormir` lo avanza y deja registro."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.dormidas: list[float] = []

    def __call__(self) -> float:
        return self.t

    def dormir(self, s: float) -> None:
        self.dormidas.append(s)
        self.t += s


class _Lock:
    """Doble del lock: ocupado hasta el intento `libre_desde` (1-based, None = nunca).

    Guarda cada conexión con la que se intentó, su isolation_level y si la
    conexión anterior ya estaba cerrada al momento del intento siguiente.
    """

    def __init__(self, libre_desde: int | None) -> None:
        self.libre_desde = libre_desde
        self.conexiones: list[Any] = []
        self.niveles: list[str | None] = []
        self.anterior_cerrada: list[bool] = []

    def __call__(self, conn: Any, key: int) -> bool:
        if self.conexiones:
            self.anterior_cerrada.append(self.conexiones[-1].closed)
        self.conexiones.append(conn)
        self.niveles.append(conn.get_execution_options().get("isolation_level"))
        return self.libre_desde is not None and len(self.conexiones) >= self.libre_desde


def _estados(engine) -> list[str]:
    from app.models.tables import JobRun

    with Session(engine) as s:
        return list(s.scalars(select(JobRun.estado)))


def _correr(engine, lock: _Lock, reloj: _Reloj, esperar_lock_s: int, llamadas: list[int]) -> Any:
    def job() -> dict[str, int]:
        llamadas.append(1)
        return {"ok": 1}

    return _run_with_lock(
        "t",
        job,
        engine,
        try_lock_fn=lock,
        unlock_fn=lambda c, k: None,
        esperar_lock_s=esperar_lock_s,
        sleep_fn=reloj.dormir,
        reloj_fn=reloj,
    )


class TestEsperaDelLock:
    def test_con_0_omite_de_inmediato_como_hoy(self, engine) -> None:
        lock, reloj, llamadas = _Lock(libre_desde=None), _Reloj(), []

        assert _correr(engine, lock, reloj, 0, llamadas) is None

        assert len(lock.conexiones) == 1
        assert reloj.dormidas == []
        assert llamadas == []
        assert _estados(engine) == ["omitido"]

    def test_el_default_no_espera(self, engine) -> None:
        """Scheduler, endpoint y Render no pasan esperar_lock_s: no cambian."""
        intentos: list[int] = []

        def ocupado(conn: Any, key: int) -> bool:
            intentos.append(1)
            return False

        with patch("app.ingest.orchestrator.time.sleep") as dormir:
            r = _run_with_lock("t", lambda: {"ok": 1}, engine, ocupado, lambda c, k: None)

        assert r is None and intentos == [1]
        dormir.assert_not_called()

    def test_consigue_el_lock_si_se_libera_dentro_del_plazo(self, engine, caplog) -> None:
        lock, reloj, llamadas = _Lock(libre_desde=3), _Reloj(), []

        with caplog.at_level(logging.INFO, logger="app.ingest.orchestrator"):
            r = _correr(engine, lock, reloj, 25 * 60, llamadas)

        assert r == {"ok": 1}
        assert llamadas == [1]
        assert reloj.dormidas == [30.0, 30.0]
        assert _estados(engine) == ["ok"]
        mensajes = [rec.getMessage() for rec in caplog.records]
        assert any("esperando hasta 1500 s" in m for m in mensajes)
        assert any("conseguido tras 60 s" in m for m in mensajes)

    def test_omite_si_vence_el_plazo(self, engine, caplog) -> None:
        lock, reloj, llamadas = _Lock(libre_desde=None), _Reloj(), []

        with caplog.at_level(logging.INFO, logger="app.ingest.orchestrator"):
            r = _correr(engine, lock, reloj, 90, llamadas)

        assert r is None
        assert llamadas == []
        assert reloj.dormidas == [30.0, 30.0, 30.0]
        assert len(lock.conexiones) == 4  # t = 0, 30, 60, 90
        assert _estados(engine) == ["omitido"]
        mensajes = [rec.getMessage() for rec in caplog.records]
        assert any("sigue ocupado tras 90 s" in m for m in mensajes)
        assert any("ciclo omitido" in m for m in mensajes)

    def test_la_ultima_espera_no_pasa_del_plazo(self, engine) -> None:
        lock, reloj, llamadas = _Lock(libre_desde=None), _Reloj(), []

        _correr(engine, lock, reloj, 45, llamadas)

        assert reloj.dormidas == [30.0, 15.0]
        assert len(lock.conexiones) == 3

    def test_mientras_espera_no_retiene_conexion_ni_transaccion(self, engine) -> None:
        """Cada intento abre su conexión AUTOCOMMIT y la suelta si no consiguió
        el lock: nada queda "idle in transaction" (F-cuota) durante la espera."""
        lock, reloj, llamadas = _Lock(libre_desde=4), _Reloj(), []

        _correr(engine, lock, reloj, 300, llamadas)

        assert lock.niveles == ["AUTOCOMMIT"] * 4
        assert lock.anterior_cerrada == [True, True, True]
        # La que consiguió el lock se cierra al terminar el job.
        assert lock.conexiones[-1].closed

    def test_un_error_del_job_tras_esperar_se_registra_y_relanza(self, engine) -> None:
        lock, reloj = _Lock(libre_desde=2), _Reloj()

        def falla() -> dict[str, int]:
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            _run_with_lock(
                "t",
                falla,
                engine,
                try_lock_fn=lock,
                unlock_fn=lambda c, k: None,
                propagar=True,
                esperar_lock_s=60,
                sleep_fn=reloj.dormir,
                reloj_fn=reloj,
            )
        assert _estados(engine) == ["error"]


# ---------------------------------------------------------------------------
# CLI: --esperar-lock-min llega a cada job, y a cada paso de `nocturno`
# ---------------------------------------------------------------------------


@contextmanager
def _cli(settings: Settings, engine):
    from app.ingest import __main__ as cli

    with (
        patch.object(cli, "get_settings", return_value=settings),
        patch.object(cli, "_make_engine", return_value=engine),
    ):
        yield cli


class TestCli:
    @pytest.mark.parametrize("minutos,segundos", [(0, 0), (25, 1500)])
    def test_run_once_pasa_la_espera_en_segundos(self, settings, engine, minutos, segundos):
        with (
            _cli(settings, engine) as cli,
            patch.object(cli, "_run_with_lock", return_value={}) as lock,
        ):
            cli.cmd_run_once("ca", esperar_lock_min=minutos)

        assert lock.call_args.kwargs["esperar_lock_s"] == segundos
        assert lock.call_args.kwargs["propagar"] is True

    def test_el_default_del_cli_es_no_esperar(self, settings, engine):
        with (
            _cli(settings, engine) as cli,
            patch.object(cli, "_run_with_lock", return_value={}) as lock,
        ):
            cli.cmd_run_once("resumen")

        assert lock.call_args.kwargs["esperar_lock_s"] == 0

    def test_nocturno_pasa_la_espera_a_cada_paso(self, settings, engine):
        vistos: list[tuple[str, int]] = []

        def _fake(job_name: str, fn: Any, eng: Any, **kw: Any) -> None:
            vistos.append((job_name, kw.get("esperar_lock_s", 0)))

        with (
            _cli(settings, engine) as cli,
            freeze_time("2026-06-14 03:00:00"),  # 23:00 Chile: dentro de la ventana
            patch("app.ingest.orchestrator._run_with_lock", side_effect=_fake),
        ):
            cli.cmd_run_once("nocturno", esperar_lock_min=30)

        assert [n for n, _ in vistos] == [
            "datos_abiertos",
            "lifecycle",
            "competencia",
            "backfill_ayer",
            "detalles-match",
        ]
        assert {s for _, s in vistos} == {1800}

    def test_argparse_acepta_el_flag_y_rechaza_negativos(self, monkeypatch):
        from app.ingest import __main__ as cli

        capturado: dict[str, Any] = {}

        def _fake_run_once(job: str, **kw: Any) -> None:
            capturado.update(job=job, **kw)

        monkeypatch.setattr(cli, "cmd_run_once", _fake_run_once)
        monkeypatch.setattr(
            "sys.argv", ["app.ingest", "run-once", "--job=ca", "--esperar-lock-min=25"]
        )
        cli.main()
        assert capturado["esperar_lock_min"] == 25

        monkeypatch.setattr(
            "sys.argv", ["app.ingest", "run-once", "--job=ca", "--esperar-lock-min=-1"]
        )
        with pytest.raises(SystemExit):
            cli.main()
