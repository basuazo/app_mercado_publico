"""Orquestador de ingesta con APScheduler y pg_advisory_lock.

Reglas críticas:
- pg_advisory_lock adquirido al inicio de cada ciclo; liberado SIEMPRE en finally.
- Si otro proceso tiene el lock (Render levanta 2 instancias en deploy), se salta el ciclo.
- Backfill nocturno solo 22:00–07:00 hora Chile, validado con ZoneInfo.
- MPRateLimitError: aborta limpio, persiste progreso, agenda reintento post-medianoche.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.core.logging import get_logger
from app.core.retencion import purgar_terminales
from app.core.settings import Settings
from app.ingest.catalogos import refresh_organismos
from app.ingest.compra_agil import sync_incremental, upsert_ca_detalle
from app.ingest.licitaciones import (
    fetch_detalles_pendientes,
    sync_activas,
    sync_por_fecha,
    upsert_detalle,
)
from app.ingest.lifecycle import refresh_estados
from app.models.tables import CompraAgil, Licitacion

_log = get_logger(__name__)
_TZ_CHILE = ZoneInfo("America/Santiago")

# Clave para pg_advisory_lock — hash arbitrario de "mp_ingesta"
_LOCK_KEY = 7_891_011


# ---------------------------------------------------------------------------
# Advisory lock (mockeable en tests)
# ---------------------------------------------------------------------------


def _pg_try_lock(conn: Any, key: int) -> bool:
    """Intenta adquirir pg_advisory_lock. Retorna False si está ocupado."""
    row = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).fetchone()
    return bool(row[0]) if row else False


def _pg_unlock(conn: Any, key: int) -> None:
    """Libera pg_advisory_lock."""
    conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})


# ---------------------------------------------------------------------------
# Guard de ventana nocturna
# ---------------------------------------------------------------------------


def en_ventana_nocturna(now_fn: Callable[..., datetime] | None = None) -> bool:
    """True si la hora actual en Chile está entre 22:00 y 07:00.

    `now_fn` es inyectable para tests (ej. lambda tz: frozen_datetime).
    """
    ahora = now_fn(_TZ_CHILE) if now_fn is not None else datetime.now(_TZ_CHILE)
    hora = ahora.hour
    return hora >= 22 or hora < 7


# ---------------------------------------------------------------------------
# Runners de jobs
# ---------------------------------------------------------------------------


def _make_clients(settings: Settings, engine: Engine) -> tuple[MercadoPublicoV1Client, MercadoPublicoV2Client]:
    return MercadoPublicoV1Client(settings, engine), MercadoPublicoV2Client(settings, engine)


def run_sync_activas(settings: Settings, engine: Engine, limit: int | None = None) -> dict[str, int]:
    v1, _ = _make_clients(settings, engine)
    with Session(engine) as session:
        return sync_activas(session, v1, settings, limit=limit)


def run_sync_ca(settings: Settings, engine: Engine) -> dict[str, int]:
    _, v2 = _make_clients(settings, engine)
    with Session(engine) as session:
        return sync_incremental(session, v2, settings)


def run_detalles(settings: Settings, engine: Engine, max_requests: int = 200) -> dict[str, int]:
    v1, _ = _make_clients(settings, engine)
    with Session(engine) as session:
        return fetch_detalles_pendientes(session, v1, settings, max_requests)


def run_lifecycle(settings: Settings, engine: Engine) -> dict[str, int]:
    v1, v2 = _make_clients(settings, engine)
    with Session(engine) as session:
        return refresh_estados(session, v1, v2, settings)


def run_catalogos(settings: Settings, engine: Engine) -> dict[str, int]:
    v1, _ = _make_clients(settings, engine)
    with Session(engine) as session:
        return refresh_organismos(session, v1)


def run_retencion(engine: Engine) -> dict[str, int]:
    with Session(engine) as session:
        return purgar_terminales(session)


def run_match(settings: Settings, engine: Engine) -> dict[str, Any]:
    """Ejecuta match_todos y luego fetcha detalles de matches sin raw_json.

    El engine de matching NO llama clientes HTTP. Este runner toma la lista
    sin_detalle_* del resultado y fetcha los detalles respetando el presupuesto.
    """
    from dataclasses import asdict

    from app.matching.engine import match_todos

    with Session(engine) as session:
        result = match_todos(session)

    sin_lic: list[str] = result.get("sin_detalle_licitaciones", [])
    sin_ca: list[str] = result.get("sin_detalle_ca", [])

    if sin_lic or sin_ca:
        v1, v2 = _make_clients(settings, engine)

        for codigo in sin_lic:
            try:
                with Session(engine) as session:
                    det = v1.licitacion_detalle(codigo)
                    upsert_detalle(session, det, settings)
                    lic = session.get(Licitacion, codigo)
                    if lic:
                        lic.raw_json = asdict(det)
                    session.commit()
            except Exception:
                _log.error("run_match: error fetching detalle licitacion %s", codigo, exc_info=True)

        for codigo in sin_ca:
            try:
                with Session(engine) as session:
                    det_ca = v2.detalle_compra_agil(codigo)
                    upsert_ca_detalle(session, det_ca)
                    ca = session.get(CompraAgil, codigo)
                    if ca:
                        ca.raw_json = asdict(det_ca)
                    session.commit()
            except Exception:
                _log.error("run_match: error fetching detalle CA %s", codigo, exc_info=True)

    return result


def run_alerts(settings: Settings, engine: Engine) -> dict[str, Any]:
    """Detecta eventos y envía alertas inmediatas pendientes."""
    from app.alerts.detector import (
        detectar_cambio_estado,
        detectar_nuevo_match,
        detectar_recordatorios,
    )
    from app.alerts.email import enviar_pendientes_inmediatas

    with Session(engine) as session:
        n1 = detectar_nuevo_match(session)
        n2 = detectar_cambio_estado(session)
        n3 = detectar_recordatorios(session)
        session.commit()

    with Session(engine) as session:
        result = enviar_pendientes_inmediatas(session, settings)

    return {
        "detectados_nuevo": n1,
        "detectados_cambio": n2,
        "detectados_recordatorio": n3,
        **result,
    }


def run_digest(settings: Settings, engine: Engine) -> dict[str, Any]:
    """Envía el digest agrupado por usuario para alertas en modo 'digest'."""
    from app.alerts.email import enviar_digest

    with Session(engine) as session:
        return enviar_digest(session, settings)


def run_backfill_fecha(settings: Settings, engine: Engine, fecha: date) -> dict[str, int]:
    """Backfill de una fecha concreta. Solo llamar dentro de ventana nocturna."""
    v1, _ = _make_clients(settings, engine)
    with Session(engine) as session:
        return sync_por_fecha(session, v1, settings, fecha)


# ---------------------------------------------------------------------------
# Ciclo con advisory lock
# ---------------------------------------------------------------------------


def _run_with_lock(
    job_name: str,
    fn: Callable[[], dict[str, int]],
    engine: Engine,
    try_lock_fn: Callable[[Any, int], bool] = _pg_try_lock,
    unlock_fn: Callable[[Any, int], None] = _pg_unlock,
) -> dict[str, int] | None:
    """Ejecuta fn dentro de un pg_advisory_lock.

    Retorna None si el lock está ocupado (otro proceso en ejecución).
    El lock se libera SIEMPRE en finally.
    """
    with engine.connect() as conn:
        acquired = try_lock_fn(conn, _LOCK_KEY)
        if not acquired:
            _log.info("job=%s: advisory lock ocupado — ciclo omitido", job_name)
            return None
        try:
            _log.info("job=%s: iniciando", job_name)
            result = fn()
            _log.info("job=%s: OK %s", job_name, result)
            return result
        except Exception:
            _log.error("job=%s: ERROR\n%s", job_name, traceback.format_exc())
            return None
        finally:
            try:
                unlock_fn(conn, _LOCK_KEY)
            except Exception as _exc:
                # Neon puede terminar la conexión por idle_in_transaction_session_timeout.
                # pg_advisory_lock se libera automáticamente al cerrar la sesión,
                # así que este error es seguro de ignorar.
                _log.warning("No se pudo liberar advisory lock explícitamente: %s", _exc)


# ---------------------------------------------------------------------------
# Programación de trabajos nocturnos
# ---------------------------------------------------------------------------


def _ciclo_nocturno(
    settings: Settings,
    engine: Engine,
    now_fn: Callable[..., datetime] | None = None,
) -> None:
    """Lifecycle + backfill del día anterior. Solo ejecuta en ventana 22:00–07:00."""
    if not en_ventana_nocturna(now_fn):
        _log.warning("ciclo_nocturno: fuera de ventana horaria — abortando")
        return

    _run_with_lock("lifecycle", lambda: run_lifecycle(settings, engine), engine)

    # Backfill: ayer (simple, se puede extender a rangos mayores)
    ayer = (datetime.now(UTC) - timedelta(days=1)).date()
    _run_with_lock(
        "backfill_ayer",
        lambda: run_backfill_fecha(settings, engine, ayer),
        engine,
    )


# ---------------------------------------------------------------------------
# Scheduler principal
# ---------------------------------------------------------------------------


def build_scheduler(
    settings: Settings,
    engine: Engine,
    now_fn: Callable[..., datetime] | None = None,
) -> BlockingScheduler:
    """Construye el scheduler. Separado de start() para facilitar tests."""
    sched = BlockingScheduler(timezone="America/Santiago")

    # Cada 30 min: CA incremental + match + alertas inmediatas
    sched.add_job(
        lambda: (
            _run_with_lock("ca_incremental", lambda: run_sync_ca(settings, engine), engine),
            _run_with_lock("match_post_ca", lambda: run_match(settings, engine), engine),
            _run_with_lock("alerts_post_ca", lambda: run_alerts(settings, engine), engine),
        ),
        "interval",
        minutes=30,
        id="ca_incremental",
    )

    # 3 veces/día: licitaciones activas + detalles pendientes + match + alertas
    for hora in (8, 13, 18):
        sched.add_job(
            lambda h=hora: (
                _run_with_lock("sync_activas", lambda: run_sync_activas(settings, engine), engine),
                _run_with_lock("detalles", lambda: run_detalles(settings, engine), engine),
                _run_with_lock("match_post_activas", lambda: run_match(settings, engine), engine),
                _run_with_lock("alerts_post_activas", lambda: run_alerts(settings, engine), engine),
            ),
            "cron",
            hour=hora,
            minute=0,
            timezone="America/Santiago",
            id=f"activas_{hora}h",
        )

    # 23:30 Chile: lifecycle + backfill pesado
    sched.add_job(
        lambda: _ciclo_nocturno(settings, engine, now_fn),
        "cron",
        hour=23,
        minute=30,
        timezone="America/Santiago",
        id="nocturno",
    )

    # Diario: digest agrupado por usuario
    sched.add_job(
        lambda: _run_with_lock("digest", lambda: run_digest(settings, engine), engine),
        "cron",
        hour=settings.digest_hour,
        minute=5,
        timezone="America/Santiago",
        id="digest",
    )

    # Diario: purga de retención (03:00)
    sched.add_job(
        lambda: _run_with_lock("retencion", lambda: run_retencion(engine), engine),
        "cron",
        hour=3,
        minute=0,
        timezone="America/Santiago",
        id="retencion",
    )

    # Semanal: catálogos (lunes 02:00)
    sched.add_job(
        lambda: _run_with_lock("catalogos", lambda: run_catalogos(settings, engine), engine),
        "cron",
        day_of_week="mon",
        hour=2,
        minute=0,
        timezone="America/Santiago",
        id="catalogos",
    )

    return sched


def run_scheduler(settings: Settings, engine: Engine) -> None:
    """Inicia el scheduler bloqueante (producción)."""
    sched = build_scheduler(settings, engine)
    _log.info("Scheduler iniciado")
    sched.start()
