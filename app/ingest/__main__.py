"""CLI de ingesta: python -m app.ingest run-once --job JOB | run-scheduler."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app.clients.base import APIPausadaError, leer_pausa, texto_pausa
from app.core.logging import get_logger, setup_logging
from app.core.settings import Settings, get_settings
from app.core.tiempo import ahora_utc
from app.ingest.orchestrator import (
    _ciclo_nocturno,
    _registrar_corrida_segura,
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
    run_mercado_backfill,
    run_mercado_ca,
    run_mercado_catalogo,
    run_mercado_competencia,
    run_mercado_indices,
    run_mercado_mesa,
    run_mercado_oc,
    run_plan_anual,
    run_resumen,
    run_retencion,
    run_scheduler,
    run_sync_activas,
    run_sync_ca,
    run_vocabulario_rubros,
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
    "plan-anual",
    "vocabulario-rubros",
    "mercado-ca",
    "mercado-backfill",
    "mercado-oc",
    "mercado-catalogo",
    "mercado-indices",
    "mercado-competencia",
    "mercado-mesa",
)

_log = get_logger(__name__)

# Jobs cuyo runner llama a la API de Mercado Público (verificado contra cada
# runner, F-429-pausa-ca). Antes de tomar el lock esperan una pausa que vence
# pronto o se omiten. Fuera A PROPÓSITO `nocturno` y `estados-vencidos`: tienen
# pasos sin API (datos abiertos, competencia) que deben correr igual, y sus pasos
# con API ya cortan limpio ante APIPausadaError. El resto no usa la API.
_JOBS_CON_API = frozenset(
    {
        "ca",
        "activas",
        "detalles",
        "lifecycle",
        "detalles-match",
        "catalogos",
        "mercado-ca",
        "mercado-backfill",
    }
)

# Margen tras el fin de la pausa antes de correr.
_MARGEN_PAUSA_S = 30.0


def _ahora() -> datetime:
    return datetime.now(UTC)


def _esperar_pausa(
    job: str,
    engine: Engine,
    espera_max_s: float,
    sleep_fn: Callable[[float], None] | None = None,
    now_fn: Callable[[], datetime] | None = None,
) -> datetime | None:
    """Espera una pausa de la API que vence pronto. Devuelve None si el job puede
    correr, o el fin de la pausa si hay que omitirlo (F-429-pausa-ca).

    Sin pausa vigente, corre de inmediato. Si vence dentro de lo que queda de
    `espera_max_s` (contado desde el primer chequeo), duerme hasta el
    vencimiento + 30 s y relee: otro proceso pudo alargarla. Mientras duerme no
    retiene ninguna conexión (`leer_pausa` devuelve la suya al pool), mismo
    criterio que `_adquirir_lock`.
    """
    dormir = sleep_fn or time.sleep
    ahora = now_fn or _ahora
    inicio = ahora()
    while True:
        hasta = leer_pausa(engine)
        momento = ahora()
        if hasta is None or momento >= hasta:
            return None
        falta = (hasta - momento).total_seconds()
        if falta > espera_max_s - (momento - inicio).total_seconds():
            return hasta
        espera = falta + _MARGEN_PAUSA_S
        _log.info("job=%s: %s; espero %.1f min", job, texto_pausa(hasta), espera / 60)
        dormir(espera)


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
    desde: str | None = None,
    inicial: bool = False,
    completo: bool = False,
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
        "retencion": _locked("retencion", lambda: run_retencion(engine, settings)),
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
        "plan-anual": _locked("plan-anual", lambda: run_plan_anual(settings, engine)),
        "vocabulario-rubros": _locked(
            "vocabulario-rubros", lambda: run_vocabulario_rubros(settings, engine)
        ),
        # F-merc-1: MP_TICKET y el lock de siempre (corrección 31-oct). El backfill
        # valida por sí mismo la ventana 22:00–07:00 de Chile (regla 5).
        "mercado-ca": _locked("mercado-ca", lambda: run_mercado_ca(settings, engine)),
        "mercado-backfill": _locked(
            "mercado-backfill", lambda: run_mercado_backfill(settings, engine)
        ),
        # F-merc-2: datos abiertos, sin ticket ni cuota; lock de siempre. `--desde AAAA-MM`
        # es la carga inicial (desde el PC; el disparo diario va a las 07:20 Chile).
        "mercado-oc": _locked("mercado-oc", lambda: run_mercado_oc(settings, engine, desde=desde)),
        # F-plat-1: solo base de datos (sin ticket ni cuota). `--inicial` recorre todas las CA
        # (desde el PC con DATABASE_URL de producción); sin él, solo las que cambiaron.
        "mercado-catalogo": _locked(
            "mercado-catalogo", lambda: run_mercado_catalogo(settings, engine, inicial=inicial)
        ),
        # F-plat-4: solo base de datos. `--completo` recalcula todo el índice y evalúa 6 meses
        # (desde el PC con DATABASE_URL de producción); el disparo diario va a las 07:45 Chile.
        "mercado-indices": _locked(
            "mercado-indices", lambda: run_mercado_indices(settings, engine, completo=completo)
        ),
        # F-plat-comp: solo base de datos. Va después de `mercado-indices` en su workflow;
        # `--completo` recalcula todos los meses (desde el PC con DATABASE_URL de producción).
        "mercado-competencia": _locked(
            "mercado-competencia",
            lambda: run_mercado_competencia(settings, engine, completo=completo),
        ),
        # F-plat-5: solo base de datos. Va en `mercado.yml` después de `mercado-ca` y
        # `mercado-catalogo`: caché de evaluaciones, predicciones del modo sombra y su cierre.
        "mercado-mesa": _locked("mercado-mesa", lambda: run_mercado_mesa(settings, engine)),
        # NO se envuelve: _ciclo_nocturno ya toma el lock por cada paso interno
        # (envolverlo por fuera volvería el ciclo entero un no-op silencioso) y
        # valida por sí mismo la ventana 22:00–07:00 de America/Santiago.
        "nocturno": lambda: _ciclo_nocturno(settings, engine, esperar_lock_s=esperar_lock_s),
    }

    if job not in dispatch:
        print(f"Job desconocido: {job}. Opciones: {', '.join(_JOBS)}", file=sys.stderr)
        sys.exit(1)

    if job in _JOBS_CON_API:
        iniciado_en = ahora_utc()
        hasta = _esperar_pausa(job, engine, settings.espera_pausa_max_min * 60)
        if hasta is not None:
            # Antes del lock: un job que sale omitido no tiene por qué esperarlo.
            _registrar_corrida_segura(
                engine,
                job,
                iniciado_en,
                "omitido",
                resultado={"omitido_por": "api_pausa", "pausada_hasta": hasta.isoformat()},
            )
            print(f"::warning::[{job}] omitido: {texto_pausa(hasta)}")
            return

    try:
        result = dispatch[job]()
    except APIPausadaError as exc:
        # La pausa la puso otro proceso después del chequeo, antes de que el job
        # avanzara: `_run_with_lock` ya dejó la fila `omitido`. El mensaje es
        # texto_pausa, sin URL ni ticket.
        print(f"::warning::[{job}] omitido: {exc}")
        return
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
    for nombre, causa in _cortes(job, result):
        print(f"::warning::[{nombre}] cortado por {causa}; avance parcial guardado")
    # F-plat-4: metas no cumplidas y otros avisos se ven en Actions, sin poner el job en rojo.
    if isinstance(result, dict):
        for aviso in result.get("avisos") or []:
            print(f"::warning::[{job}] {aviso}")
    print(f"[{job}] {result}")


# Claves con las que un runner informa que cortó por el canal (cuota, pausa, 429).
_CLAVES_DE_CORTE = ("cortado_por_error", "cortado_por")


def _cortes(job: str, result: Any) -> list[tuple[str, str]]:
    """(paso, causa) de cada corte por canal en el resultado (F-429-tasa).

    Un corte así sale 0 —el avance parcial quedó guardado—, pero tiene que
    verse en Actions: antes los jobs salían verdes sin haber hecho nada. En
    `nocturno` se revisa el resultado de cada paso.
    """
    pasos = result.items() if job == "nocturno" and isinstance(result, dict) else [(job, result)]
    cortes: list[tuple[str, str]] = []
    for nombre, res in pasos:
        if not isinstance(res, dict):
            continue
        for clave in _CLAVES_DE_CORTE:
            if res.get(clave):
                cortes.append((nombre, str(res[clave])))
                break
    return cortes


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
        "--desde",
        default=None,
        help="Mes inicial AAAA-MM (solo job=mercado-oc; default: mes actual y 2 anteriores)",
    )
    once.add_argument(
        "--inicial",
        action="store_true",
        help="Recorre todas las CA, no solo las cambiadas (solo job=mercado-catalogo)",
    )
    once.add_argument(
        "--completo",
        action="store_true",
        help=(
            "mercado-indices: índice completo y evaluación de los últimos 6 meses; "
            "mercado-competencia: recalcula todos los meses"
        ),
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
            desde=args.desde,
            inicial=args.inicial,
            completo=args.completo,
        )
    elif args.cmd == "run-scheduler":
        cmd_run_scheduler()


if __name__ == "__main__":
    main()
