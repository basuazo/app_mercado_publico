"""Tests F-observabilidad — historial de corridas y dead-man's switch.

Cobertura:
- _run_with_lock graba una fila en job_runs por corrida (ok | error | omitido).
- La telemetría nunca tumba el job: si la escritura lanza, el resultado no cambia.
- GET /api/salud/jobs: 200 con corridas frescas, 503 si un crítico está atrasado
  o nunca corrió; los alias satisfacen al canónico; un "error" no cuenta como OK.
- La retención purga job_runs viejas y conserva las recientes.

Todo corre offline sobre SQLite: sin Neon, sin red.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.main import create_app
from app.core.settings import Settings
from app.ingest.orchestrator import _run_with_lock
from app.models.base import Base
from app.models.tables import JobRun

# Locks inyectables: pg_try_advisory_lock solo existe en Postgres.
_LOCK_LIBRE = lambda conn, key: True  # noqa: E731
_LOCK_OCUPADO = lambda conn, key: False  # noqa: E731
_UNLOCK = lambda conn, key: None  # noqa: E731


def _ahora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@pytest.fixture()
def engine():
    import app.models.tables  # noqa: F401

    e = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(e)
    yield e
    e.dispose()


@pytest.fixture()
def settings():
    return Settings(
        mp_ticket="TICKET_TEST",
        database_url="sqlite:///:memory:",
        secret_key="secret-test-key-larga-32chars!!",
        jobs_token="jobs-token-secreto",
    )


@pytest.fixture()
def client(engine, settings):
    return TestClient(create_app(settings, engine), raise_server_exceptions=True)


def _corridas(engine) -> list[JobRun]:
    with Session(engine) as s:
        return list(s.execute(select(JobRun).order_by(JobRun.id)).scalars())


def _sembrar(engine, job: str, estado: str, horas_atras: float) -> None:
    """Inserta una corrida con `iniciado_en` desplazada al pasado."""
    inicio = _ahora() - timedelta(hours=horas_atras)
    with Session(engine) as s:
        s.add(JobRun(job=job, iniciado_en=inicio, terminado_en=inicio, estado=estado))
        s.commit()


# ---------------------------------------------------------------------------
# _run_with_lock graba una fila por corrida
# ---------------------------------------------------------------------------


class TestRegistroDeCorridas:
    def test_exito_graba_ok_con_resultado(self, engine):
        result = _run_with_lock(
            "sync_activas",
            lambda: {"nuevas": 3, "actualizadas": 1},
            engine,
            _LOCK_LIBRE,
            _UNLOCK,
        )

        assert result == {"nuevas": 3, "actualizadas": 1}

        (corrida,) = _corridas(engine)
        assert corrida.job == "sync_activas"
        assert corrida.estado == "ok"
        assert corrida.resultado_json == {"nuevas": 3, "actualizadas": 1}
        assert corrida.error is None
        assert corrida.terminado_en is not None
        assert corrida.terminado_en >= corrida.iniciado_en

    def test_excepcion_graba_error_con_traceback(self, engine):
        def job_fn() -> dict[str, int]:
            raise RuntimeError("fallo intencional")

        result = _run_with_lock("ca", job_fn, engine, _LOCK_LIBRE, _UNLOCK)

        # El contrato de retorno no cambia: error → None.
        assert result is None

        (corrida,) = _corridas(engine)
        assert corrida.estado == "error"
        assert corrida.resultado_json is None
        assert corrida.error is not None
        assert "fallo intencional" in corrida.error
        assert "Traceback" in corrida.error

    def test_lock_ocupado_graba_omitido(self, engine):
        """Lock tomado por otra instancia no es fallo: queda como 'omitido'."""
        llamadas = 0

        def job_fn() -> dict[str, int]:
            nonlocal llamadas
            llamadas += 1
            return {}

        result = _run_with_lock("ca", job_fn, engine, _LOCK_OCUPADO, _UNLOCK)

        assert result is None
        assert llamadas == 0

        (corrida,) = _corridas(engine)
        assert corrida.estado == "omitido"
        assert corrida.resultado_json is None
        assert corrida.error is None

    def test_una_sola_fila_por_corrida(self, engine):
        _run_with_lock("resumen", lambda: {"enviados": 2}, engine, _LOCK_LIBRE, _UNLOCK)
        _run_with_lock("resumen", lambda: {"enviados": 0}, engine, _LOCK_LIBRE, _UNLOCK)

        assert len(_corridas(engine)) == 2

    def test_resultado_no_serializable_no_rompe_el_registro(self, engine):
        """Un runner que devuelve objetos raros igual deja fila: el switch la necesita."""

        class Raro:
            pass

        result = _run_with_lock(
            "match",
            lambda: {"objeto": Raro()},  # type: ignore[dict-item]
            engine,
            _LOCK_LIBRE,
            _UNLOCK,
        )

        assert result is not None
        (corrida,) = _corridas(engine)
        assert corrida.estado == "ok"


class TestTelemetriaNoTumbaElJob:
    """La telemetría es best-effort: si falla, el job se comporta igual que sin ella."""

    def test_exito_conserva_resultado_si_falla_el_registro(self, engine):
        def explota(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("BD caída")

        with patch("app.ingest.orchestrator._registrar_corrida", side_effect=explota):
            result = _run_with_lock(
                "activas", lambda: {"nuevas": 7}, engine, _LOCK_LIBRE, _UNLOCK
            )

        assert result == {"nuevas": 7}
        assert _corridas(engine) == []

    def test_error_sigue_devolviendo_none_si_falla_el_registro(self, engine):
        def explota(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("BD caída")

        def job_fn() -> dict[str, int]:
            raise ValueError("fallo del job")

        with patch("app.ingest.orchestrator._registrar_corrida", side_effect=explota):
            result = _run_with_lock("activas", job_fn, engine, _LOCK_LIBRE, _UNLOCK)

        assert result is None

    def test_lock_ocupado_sigue_devolviendo_none_si_falla_el_registro(self, engine):
        def explota(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("BD caída")

        llamadas = 0

        def job_fn() -> dict[str, int]:
            nonlocal llamadas
            llamadas += 1
            return {}

        with patch("app.ingest.orchestrator._registrar_corrida", side_effect=explota):
            result = _run_with_lock("activas", job_fn, engine, _LOCK_OCUPADO, _UNLOCK)

        assert result is None
        assert llamadas == 0

    def test_el_unlock_se_llama_igual(self, engine):
        """El registro no debe interferir con la liberación del lock."""
        unlocked: list[bool] = []

        def explota(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("BD caída")

        with patch("app.ingest.orchestrator._registrar_corrida", side_effect=explota):
            _run_with_lock(
                "activas",
                lambda: {},
                engine,
                _LOCK_LIBRE,
                lambda conn, key: unlocked.append(True),
            )

        assert unlocked == [True]


# ---------------------------------------------------------------------------
# GET /api/salud/jobs — dead-man's switch
# ---------------------------------------------------------------------------

# Nombres tal como los graba cada disparador (endpoint / scheduler / nocturno).
_CRITICOS_FRESCOS = (
    ("activas", 2.0),
    ("ca", 0.5),
    ("datos-abiertos", 5.0),
    ("match", 0.5),
    ("alerts", 0.5),
)


def _sembrar_criticos_frescos(engine) -> None:
    for job, horas in _CRITICOS_FRESCOS:
        _sembrar(engine, job, "ok", horas)


class TestSaludJobs:
    def test_200_con_todos_los_criticos_frescos(self, client, engine):
        _sembrar_criticos_frescos(engine)

        r = client.get("/api/salud/jobs")

        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert {j["job"] for j in body["jobs"]} == {
            "activas",
            "ca",
            "datos-abiertos",
            "resumen",
            "match",
            "alerts",
        }
        criticos = [j for j in body["jobs"] if j["critico"]]
        assert len(criticos) == 5
        assert all(j["stale"] is False for j in criticos)

    def test_503_si_un_critico_esta_atrasado(self, client, engine):
        _sembrar(engine, "ca", "ok", 0.5)
        _sembrar(engine, "datos-abiertos", "ok", 5.0)
        _sembrar(engine, "activas", "ok", 72.0)  # umbral 30 h

        r = client.get("/api/salud/jobs")

        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "stale"
        activas = next(j for j in body["jobs"] if j["job"] == "activas")
        assert activas["stale"] is True
        assert activas["edad_horas"] == pytest.approx(72.0, abs=0.2)
        assert activas["umbral_horas"] == 30

    def test_503_si_un_critico_nunca_corrio(self, client, engine):
        """Base recién creada: ningún job tiene historial."""
        r = client.get("/api/salud/jobs")

        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "stale"
        for job in body["jobs"]:
            assert job["stale"] is True
            assert job["ultimo_ok"] is None
            assert job["edad_horas"] is None

    def test_alias_del_scheduler_satisface_al_canonico(self, client, engine):
        """Una corrida 'sync_activas' (scheduler) cuenta como 'activas' (endpoint)."""
        _sembrar(engine, "sync_activas", "ok", 1.0)
        _sembrar(engine, "ca_incremental", "ok", 0.3)
        _sembrar(engine, "datos_abiertos", "ok", 6.0)
        _sembrar(engine, "match_post_ca", "ok", 0.3)
        _sembrar(engine, "alerts_post_activas", "ok", 0.3)

        r = client.get("/api/salud/jobs")

        assert r.status_code == 200
        assert r.json()["status"] == "ok"

    def test_corrida_con_error_no_cuenta_como_ok(self, client, engine):
        _sembrar(engine, "ca", "ok", 0.5)
        _sembrar(engine, "datos-abiertos", "ok", 5.0)
        _sembrar(engine, "activas", "error", 0.1)  # reciente, pero falló

        r = client.get("/api/salud/jobs")

        assert r.status_code == 503
        activas = next(j for j in r.json()["jobs"] if j["job"] == "activas")
        assert activas["stale"] is True
        assert activas["ultimo_ok"] is None

    def test_corrida_omitida_no_cuenta_como_ok(self, client, engine):
        _sembrar(engine, "ca", "ok", 0.5)
        _sembrar(engine, "datos-abiertos", "ok", 5.0)
        _sembrar(engine, "activas", "omitido", 0.1)

        assert client.get("/api/salud/jobs").status_code == 503

    def test_job_informativo_atrasado_no_dispara_503(self, client, engine):
        """'resumen' se muestra pero no es crítico: no corta el switch."""
        _sembrar_criticos_frescos(engine)
        _sembrar(engine, "resumen", "ok", 500.0)

        r = client.get("/api/salud/jobs")

        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        resumen = next(j for j in r.json()["jobs"] if j["job"] == "resumen")
        assert resumen["critico"] is False
        assert resumen["stale"] is True

    def test_usa_la_corrida_ok_mas_reciente(self, client, engine):
        """Un OK viejo no debe opacar al fresco ni al revés."""
        _sembrar(engine, "ca", "ok", 0.5)
        _sembrar(engine, "datos-abiertos", "ok", 5.0)
        _sembrar(engine, "match", "ok", 0.5)
        _sembrar(engine, "alerts", "ok", 0.5)
        _sembrar(engine, "activas", "ok", 400.0)
        _sembrar(engine, "activas", "ok", 1.0)

        r = client.get("/api/salud/jobs")

        assert r.status_code == 200
        activas = next(j for j in r.json()["jobs"] if j["job"] == "activas")
        assert activas["edad_horas"] == pytest.approx(1.0, abs=0.2)

    def test_publico_y_sin_secretos(self, client, engine):
        """Sin cookie de sesión (regla: mismo criterio que /ping) y sin secretos."""
        _sembrar_criticos_frescos(engine)

        r = client.get("/api/salud/jobs")

        assert r.status_code == 200
        cuerpo = r.text.lower()
        for secreto in ("ticket_test", "secret-test-key", "jobs-token-secreto"):
            assert secreto not in cuerpo

    def test_el_ciclo_real_deja_el_switch_en_verde(self, client, engine):
        """De punta a punta: _run_with_lock graba y el endpoint lo lee."""
        assert client.get("/api/salud/jobs").status_code == 503

        for job in ("sync_activas", "ca_incremental", "datos_abiertos", "match", "alerts"):
            _run_with_lock(job, lambda: {"ok": 1}, engine, _LOCK_LIBRE, _UNLOCK)

        assert client.get("/api/salud/jobs").status_code == 200

    def test_match_cancelado_en_todas_las_corridas_dispara_503(self, client, engine):
        """El caso del canario (F-detalles-match): `ca` sale OK cada vez, pero
        `match` muere por el timeout del workflow y no deja fila. Antes el switch
        quedaba en verde para siempre."""
        for job, horas in _CRITICOS_FRESCOS:
            if job != "match":
                _sembrar(engine, job, "ok", horas)
        _sembrar(engine, "match", "ok", 40.0)  # último OK hace 40 h > 30 h

        r = client.get("/api/salud/jobs")

        assert r.status_code == 503
        match = next(j for j in r.json()["jobs"] if j["job"] == "match")
        assert match["critico"] is True and match["stale"] is True

    def test_detalles_match_no_se_vigila(self, client, engine):
        """Un día sin detalles no es una caída: no aparece en el watchlist."""
        _sembrar_criticos_frescos(engine)

        r = client.get("/api/salud/jobs")

        assert r.status_code == 200
        assert "detalles-match" not in {j["job"] for j in r.json()["jobs"]}


# ---------------------------------------------------------------------------
# Retención de job_runs
# ---------------------------------------------------------------------------


class TestRetencionJobRuns:
    def test_purga_viejas_y_conserva_recientes(self, engine):
        from app.core.retencion import purgar_job_runs

        _sembrar(engine, "activas", "ok", 24 * 200)  # ~200 días
        _sembrar(engine, "activas", "error", 24 * 91)
        _sembrar(engine, "ca", "ok", 24 * 10)
        _sembrar(engine, "ca", "ok", 1)

        with Session(engine) as s:
            borradas = purgar_job_runs(s, dias=90)
            s.commit()

        assert borradas == 2
        restantes = _corridas(engine)
        assert len(restantes) == 2
        assert {c.job for c in restantes} == {"ca"}

    def test_run_retencion_reporta_el_conteo_y_persiste(self, engine):
        """run_retencion debe commitear: sin eso la purga se revierte al cerrar."""
        from app.ingest.orchestrator import run_retencion

        _sembrar(engine, "activas", "ok", 24 * 200)
        _sembrar(engine, "ca", "ok", 1)

        resultado = run_retencion(engine)

        assert resultado["job_runs_borrados"] == 1
        assert [c.job for c in _corridas(engine)] == ["ca"]
