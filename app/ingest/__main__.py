"""CLI de ingesta: python -m app.ingest run-once --job JOB | run-scheduler."""

from __future__ import annotations

import argparse
import sys

from collections.abc import Callable
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.core.logging import setup_logging
from app.core.settings import Settings, get_settings
from app.ingest.orchestrator import (
    run_catalogos,
    run_detalles,
    run_lifecycle,
    run_match,
    run_retencion,
    run_scheduler,
    run_sync_activas,
    run_sync_ca,
)

_JOBS = ("activas", "ca", "detalles", "lifecycle", "catalogos", "retencion", "match")


def _make_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        connect_args={"sslmode": "require"} if "neon" in settings.database_url or "postgresql" in settings.database_url else {},
    )


def cmd_run_once(job: str) -> None:
    setup_logging()
    settings = get_settings()
    engine = _make_engine(settings)

    dispatch: dict[str, Callable[[], Any]] = {
        "activas": lambda: run_sync_activas(settings, engine),
        "ca": lambda: run_sync_ca(settings, engine),
        "detalles": lambda: run_detalles(settings, engine),
        "lifecycle": lambda: run_lifecycle(settings, engine),
        "catalogos": lambda: run_catalogos(settings, engine),
        "retencion": lambda: run_retencion(engine),
        "match": lambda: run_match(settings, engine),
    }

    if job not in dispatch:
        print(f"Job desconocido: {job}. Opciones: {', '.join(_JOBS)}", file=sys.stderr)
        sys.exit(1)

    result = dispatch[job]()
    print(f"[{job}] {result}")


def cmd_run_scheduler() -> None:
    setup_logging()
    settings = get_settings()
    engine = _make_engine(settings)
    run_scheduler(settings, engine)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.ingest",
        description="CLI de ingesta de Mercado Público",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    once = sub.add_parser("run-once", help="Ejecuta un job una sola vez")
    once.add_argument(
        "--job",
        choices=list(_JOBS),
        required=True,
        help="Job a ejecutar",
    )

    sub.add_parser("run-scheduler", help="Inicia el scheduler APScheduler (bloqueante)")

    args = parser.parse_args()

    if args.cmd == "run-once":
        cmd_run_once(args.job)
    elif args.cmd == "run-scheduler":
        cmd_run_scheduler()


if __name__ == "__main__":
    main()
