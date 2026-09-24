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
    _ciclo_nocturno,
    _run_with_lock,
    run_alerts,
    run_catalogos,
    run_competencia,
    run_datos_abiertos,
    run_detalles,
    run_detalles_match,
    run_estados_vencidos,
    run_lifecycle,
    run_match,
    run_resumen,
    run_retencion,
    run_scheduler,
    run_sync_activas,
    run_sync_ca,
)

_JOBS = (
    "activas",
    "ca",
    "detalles",
    "lifecycle",
    "catalogos",
    "retencion",
    "match",
    "alerts",
    "detalles-match",
    "resumen",
    "datos-abiertos",
    "competencia",
    "estados-vencidos",
    "nocturno",
)


def _make_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        connect_args={"sslmode": "require"} if "neon" in settings.database_url or "postgresql" in settings.database_url else {},
    )


def cmd_run_once(
    job: str,
    limit: int | None = None,
    anio: int | None = None,
    mes: int | None = None,
    esperar_lock_min: int = 0,
) -> None:
    setup_logging()
    settings = get_settings()
    engine = _make_engine(settings)
    esperar_lock_s = esperar_lock_min * 60

    def _locked(nombre: str, fn: Callable[[], Any]) -> Callable[[], Any]:
        """Envuelve un runner en el pg_advisory_lock (regla 13 de CLAUDE.md).

        El CLI es un camino de producción (cron externo), así que toma el mismo
        lock que el scheduler interno y que POST /api/jobs/run. Lock ocupado →
        espera hasta `--esperar-lock-min` (F-actions-2; 0 = no espera) y, si
        sigue ocupado, `_run_with_lock` devuelve None y el ciclo se omite.

        `propagar=True`: el error se re-lanza para que el proceso salga 1. Sin
        esto el CLI salía 0 aunque el job fallara y GitHub Actions quedaba verde
        los días sin ingesta. Solo el CLI lo pasa: endpoint y scheduler siguen
        tragándose el error para no cortar sus secuencias.
        """
        return lambda: _run_with_lock(
            nombre, fn, engine, propagar=True, esperar_lock_s=esperar_lock_s
        )

    dispatch: dict[str, Callable[[], Any]] = {
        "activas": _locked("activas", lambda: run_sync_activas(settings, engine, limit=limit)),
        "ca": _locked("ca", lambda: run_sync_ca(settings, engine)),
        "detalles": _locked("detalles", lambda: run_detalles(settings, engine)),
        "lifecycle": _locked("lifecycle", lambda: run_lifecycle(settings, engine)),
        "catalogos": _locked("catalogos", lambda: run_catalogos(settings, engine)),
        "retencion": _locked("retencion", lambda: run_retencion(engine)),
        "match": _locked("match", lambda: run_match(settings, engine)),
        "alerts": _locked("alerts", lambda: run_alerts(settings, engine)),
        "detalles-match": _locked(
            "detalles-match", lambda: run_detalles_match(settings, engine)
        ),
        "resumen": _locked("resumen", lambda: run_resumen(settings, engine)),
        "datos-abiertos": _locked(
            "datos-abiertos", lambda: run_datos_abiertos(settings, engine, anio=anio, mes=mes)
        ),
        "competencia": _locked("competencia", lambda: run_competencia(settings, engine)),
        # Datos abiertos siempre; la parte por API solo si cae en 22:00–07:00 Chile.
        "estados-vencidos": _locked(
            "estados-vencidos", lambda: run_estados_vencidos(settings, engine)
        ),
        # NO se envuelve: _ciclo_nocturno ya toma el lock por cada paso interno
        # (envolverlo por fuera volvería el ciclo entero un no-op silencioso) y
        # valida por sí mismo la ventana 22:00–07:00 de America/Santiago.
        "nocturno": lambda: _ciclo_nocturno(settings, engine, esperar_lock_s=esperar_lock_s),
    }

    if job not in dispatch:
        print(f"Job desconocido: {job}. Opciones: {', '.join(_JOBS)}", file=sys.stderr)
        sys.exit(1)

    try:
        result = dispatch[job]()
    except Exception as exc:
        # Solo el tipo, no el mensaje: el traceback completo ya quedó en el log
        # (con _SecretFilter) y en job_runs, mientras que print() no enmascara y
        # el mensaje de un error de httpx puede traer la URL v1 con el ticket.
        print(f"[{job}] ERROR: {type(exc).__name__} (detalle en el log y en job_runs)", file=sys.stderr)
        sys.exit(1)

    if result is None and job != "nocturno":
        # "omitido" no es fallo: otra corrida tenía el advisory lock. Sale 0 para
        # no dejar el workflow en rojo por una falsa alarma.
        print(f"[{job}] omitido: advisory lock ocupado por otra corrida")
        return
    print(f"[{job}] {result}")


def _minutos_no_negativos(valor: str) -> int:
    n = int(valor)
    if n < 0:
        raise argparse.ArgumentTypeError("debe ser ≥ 0")
    return n


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
    once.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limita cuántas licitaciones procesa (solo job=activas; pruebas locales acotadas)",
    )
    once.add_argument(
        "--anio",
        type=int,
        default=None,
        help="Año del archivo de datos abiertos a procesar (solo job=datos-abiertos; default: mes actual)",
    )
    once.add_argument(
        "--mes",
        type=int,
        default=None,
        help="Mes del archivo de datos abiertos a procesar (solo job=datos-abiertos; default: mes actual)",
    )

    once.add_argument(
        "--esperar-lock-min",
        type=_minutos_no_negativos,
        default=0,
        help=(
            "Si el advisory lock está ocupado, reintentar hasta N minutos antes de "
            "omitir (default 0: omite de inmediato). En `nocturno`, por cada paso"
        ),
    )

    sub.add_parser("run-scheduler", help="Inicia el scheduler APScheduler (bloqueante)")

    args = parser.parse_args()

    if args.cmd == "run-once":
        cmd_run_once(
            args.job,
            limit=args.limit,
            anio=args.anio,
            mes=args.mes,
            esperar_lock_min=args.esperar_lock_min,
        )
    elif args.cmd == "run-scheduler":
        cmd_run_scheduler()


if __name__ == "__main__":
    main()
