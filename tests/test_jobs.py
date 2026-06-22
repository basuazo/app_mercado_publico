"""Tests para el mapeo de jobs en /api/jobs/run.

Prueba la lógica del dict _jobs directamente, sin levantar la app FastAPI
(que requiere psycopg2 en el entorno de dev).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


def _build_jobs_dict(settings, engine):
    """Replica la construcción del dict _jobs tal como lo hace jobs_run."""
    from app.ingest.orchestrator import (
        run_alerts,
        run_detalles,
        run_digest,
        run_lifecycle,
        run_match,
        run_sync_activas,
        run_sync_ca,
    )

    return {
        "ca":        lambda: run_sync_ca(settings, engine),
        "activas":   lambda: run_sync_activas(settings, engine),
        "detalles":  lambda: run_detalles(settings, engine),
        "lifecycle": lambda: run_lifecycle(settings, engine),
        "match":     lambda: run_match(settings, engine),
        "alerts":    lambda: run_alerts(settings, engine),
        "digest":    lambda: run_digest(settings, engine),
    }


def test_job_ca_invoca_run_sync_ca_no_activas():
    """job='ca' debe llamar run_sync_ca y nunca run_sync_activas."""
    settings = MagicMock()
    engine = MagicMock()

    with (
        patch("app.ingest.orchestrator.run_sync_ca", return_value={}) as mock_ca,
        patch("app.ingest.orchestrator.run_sync_activas", return_value={}) as mock_activas,
    ):
        jobs = _build_jobs_dict(settings, engine)
        jobs["ca"]()

    mock_ca.assert_called_once_with(settings, engine)
    mock_activas.assert_not_called()


def test_job_activas_invoca_run_sync_activas():
    """job='activas' debe llamar run_sync_activas."""
    settings = MagicMock()
    engine = MagicMock()

    with (
        patch("app.ingest.orchestrator.run_sync_activas", return_value={}) as mock_activas,
        patch("app.ingest.orchestrator.run_sync_ca", return_value={}) as mock_ca,
    ):
        jobs = _build_jobs_dict(settings, engine)
        jobs["activas"]()

    mock_activas.assert_called_once_with(settings, engine)
    mock_ca.assert_not_called()


def test_job_ca_y_activas_son_funciones_distintas():
    """'ca' y 'activas' deben apuntar a funciones distintas del orchestrator."""
    import inspect

    from app.api.routes.api import jobs_run  # solo para verificar el source
    source = inspect.getsource(jobs_run)
    # Verificar que run_sync_ca está importado y mapeado a "ca"
    assert "run_sync_ca" in source
    assert '"ca"' in source
    # Verificar que el mapeo de "ca" no menciona run_sync_activas en su línea
    for line in source.splitlines():
        if '"ca"' in line and "lambda" in line:
            assert "run_sync_activas" not in line, (
                f'El mapeo de "ca" usa run_sync_activas en lugar de run_sync_ca: {line!r}'
            )
