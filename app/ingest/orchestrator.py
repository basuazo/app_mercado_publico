"""Orquestador de ingesta con APScheduler y pg_advisory_lock.

Reglas críticas:
- pg_advisory_lock adquirido al inicio de cada ciclo; liberado SIEMPRE en finally.
- Si otro proceso tiene el lock (Render levanta 2 instancias en deploy), se salta el ciclo.
- Backfill nocturno solo 22:00–07:00 hora Chile, validado con ZoneInfo.
- MPRateLimitError: aborta limpio, persiste progreso, agenda reintento post-medianoche.
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

from apscheduler.schedulers.blocking import BlockingScheduler
from sqlalchemy import (
    Engine,
    String,
    case,
    cast,
    exists,
    func,
    literal,
    or_,
    select,
    text,
    union_all,
)
from sqlalchemy.orm import Session

from app.clients.base import MPAuthError, MPRateLimitError, QuotaExceededError
from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.core.logging import get_logger
from app.core.retencion import purgar_terminales
from app.core.serializacion import dataclass_a_json
from app.core.settings import Settings
from app.core.tiempo import ahora_utc, en_ventana_nocturna
from app.ingest.catalogos import refresh_organismos
from app.ingest.compra_agil import sync_incremental, upsert_ca_detalle
from app.ingest.datos_abiertos import (
    CacheZipsDA,
    capturar_competencia,
    sync_items_datos_abiertos,
)
from app.ingest.licitaciones import (
    fetch_detalles_pendientes,
    sync_activas,
    sync_por_fecha,
    upsert_detalle,
)
from app.ingest.lifecycle import refresh_estados, refresh_estados_vencidos
from app.models.enums import EstadoOportunidad
from app.models.tables import CompraAgil, JobRun, Licitacion, OportunidadMatch

_log = get_logger(__name__)

# Clave para pg_advisory_lock — hash arbitrario de "mp_ingesta"
_LOCK_KEY = 7_891_011

# Errores que hablan del CANAL, no del ítem que se estaba pidiendo: ante
# cualquiera de ellos hay que cortar el loop en vez de seguir gastando cuota
# contra una API que ya nos está rechazando (regla 3).
_ERRORES_DE_CANAL = (MPRateLimitError, QuotaExceededError, MPAuthError)


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
# Runners de jobs
# ---------------------------------------------------------------------------


def _make_clients(settings: Settings, engine: Engine) -> tuple[MercadoPublicoV1Client, MercadoPublicoV2Client]:
    return MercadoPublicoV1Client(settings, engine), MercadoPublicoV2Client(settings, engine)


def run_sync_activas(settings: Settings, engine: Engine, limit: int | None = None) -> dict[str, int]:
    v1, _ = _make_clients(settings, engine)
    with Session(engine) as session:
        return sync_activas(session, v1, settings, limit=limit)


def run_sync_ca(settings: Settings, engine: Engine) -> dict[str, Any]:
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


def run_estados_vencidos(
    settings: Settings,
    engine: Engine,
    zips: CacheZipsDA | None = None,
    now_fn: Callable[..., datetime] | None = None,
) -> dict[str, int]:
    """Estados de licitaciones ya cerradas: datos abiertos y, de noche, API con tope."""
    v1, _ = _make_clients(settings, engine)
    with Session(engine) as session:
        return refresh_estados_vencidos(session, v1, settings, zips=zips, now_fn=now_fn)


def run_datos_abiertos(
    settings: Settings,
    engine: Engine,
    anio: int | None = None,
    mes: int | None = None,
    zips: CacheZipsDA | None = None,
) -> dict[str, int]:
    """Completa licitacion_items desde datos abiertos (sin cuota de API)."""
    with Session(engine) as session:
        return sync_items_datos_abiertos(session, settings, anio=anio, mes=mes, zips=zips)


def run_competencia(
    settings: Settings, engine: Engine, zips: CacheZipsDA | None = None
) -> dict[str, int]:
    """Captura ofertas (lic-da) de licitaciones seguidas y adjudicadas (sin cuota de API)."""
    with Session(engine) as session:
        return capturar_competencia(session, settings, zips=zips)


def run_catalogos(settings: Settings, engine: Engine) -> dict[str, int]:
    v1, _ = _make_clients(settings, engine)
    with Session(engine) as session:
        return refresh_organismos(session, v1)


def run_retencion(engine: Engine) -> dict[str, int]:
    with Session(engine) as session:
        resultado = purgar_terminales(session)
        # Sin commit, el cierre de la sesión revierte la purga entera.
        session.commit()
        return resultado


_FUENTE_LIC = "licitaciones"
_FUENTE_CA = "compras_agiles"


def _sin_detalle(col: Any, dialecto: str) -> Any:
    """`raw_json` sin detalle: NULL de SQL o JSON `null` guardado.

    [V] Hay licitaciones con JSON `null` en vez de NULL: un `raw_json = None`
    sobre JSONB puede escribir `null`. En SQLite (tests) no hay JSONB: basta con
    IS NULL o el texto 'null'.
    """
    if dialecto == "postgresql":
        return or_(col.is_(None), func.jsonb_typeof(col) == "null")
    return or_(col.is_(None), cast(col, String) == "null")


def _cola_detalles_match(session: Session, ahora: datetime) -> list[tuple[str, str]]:
    """Cola de `detalles-match` como pares (fuente, código), en UNA query parametrizada.

    Entran licitaciones y CA con al menos una fila en oportunidades_match, sin
    detalle, publicadas y sin cerrar (fecha_cierre nula o futura). Orden:
    fecha_cierre ascendente —lo que cierra antes, primero— y las sin fecha al
    final. No hay tope de cantidad: el tope es de tiempo (ver run_detalles_match).
    """
    dialecto = session.get_bind().dialect.name
    publicada = EstadoOportunidad.PUBLICADA.value

    def _con_match(fuente: str, codigo: Any) -> Any:
        return exists().where(
            OportunidadMatch.fuente == fuente,
            OportunidadMatch.codigo_oportunidad == codigo,
        )

    lic = select(
        literal(_FUENTE_LIC).label("fuente"),
        Licitacion.codigo.label("codigo"),
        Licitacion.fecha_cierre.label("fecha_cierre"),
    ).where(
        _sin_detalle(Licitacion.raw_json, dialecto),
        Licitacion.estado == publicada,
        or_(Licitacion.fecha_cierre.is_(None), Licitacion.fecha_cierre > ahora),
        _con_match(_FUENTE_LIC, Licitacion.codigo),
    )
    ca = select(
        literal(_FUENTE_CA).label("fuente"),
        CompraAgil.codigo.label("codigo"),
        CompraAgil.fecha_cierre.label("fecha_cierre"),
    ).where(
        _sin_detalle(CompraAgil.raw_json, dialecto),
        CompraAgil.estado == publicada,
        or_(CompraAgil.fecha_cierre.is_(None), CompraAgil.fecha_cierre > ahora),
        _con_match(_FUENTE_CA, CompraAgil.codigo),
    )
    sub = union_all(lic, ca).subquery()
    stmt = select(sub.c.fuente, sub.c.codigo).order_by(
        case((sub.c.fecha_cierre.is_(None), 1), else_=0),
        sub.c.fecha_cierre,
        sub.c.fuente,
        sub.c.codigo,
    )
    return [(fuente, codigo) for fuente, codigo in session.execute(stmt)]


def _bajar_detalle(
    settings: Settings,
    engine: Engine,
    v1: MercadoPublicoV1Client,
    v2: MercadoPublicoV2Client,
    fuente: str,
    codigo: str,
) -> None:
    """Baja un detalle y lo guarda —campos, ítems/productos y raw_json— en UN commit.

    Sin reintento de 5xx ni de timeout: si falla, el detalle sigue sin raw_json
    y vuelve solo a la cola de la corrida siguiente. El enfriamiento de 60 s
    tras un 504 o un timeout lo sigue aplicando el cliente (regla 3).
    """
    with Session(engine) as session:
        if fuente == _FUENTE_LIC:
            det = v1.licitacion_detalle(codigo, reintentar_transitorios=False)
            upsert_detalle(session, det, settings)
            lic = session.get(Licitacion, codigo)
            if lic:
                lic.raw_json = dataclass_a_json(det)
        else:
            det_ca = v2.detalle_compra_agil(codigo, reintentar_transitorios=False)
            upsert_ca_detalle(session, det_ca)
            ca = session.get(CompraAgil, codigo)
            if ca:
                ca.raw_json = dataclass_a_json(det_ca)
        session.commit()


def run_match(settings: Settings, engine: Engine) -> dict[str, Any]:
    """Calcula los matches de todos los perfiles. NO baja detalles.

    Los detalles de las oportunidades con match los baja `detalles-match`
    (:func:`run_detalles_match`), que corre DESPUÉS de `alerts`: bajar uno
    cuesta ~78 s de media por los 504 de la API y no debe demorar las alertas.

    El resultado lleva CONTEOS de lo que quedó sin detalle, no las listas de
    códigos: se guarda en job_runs y las listas lo inflaban en cada corrida.
    """
    from app.matching.engine import match_todos

    with Session(engine) as session:
        result = match_todos(session)

    result["sin_detalle_licitaciones"] = len(result.get("sin_detalle_licitaciones") or [])
    result["sin_detalle_ca"] = len(result.get("sin_detalle_ca") or [])
    return result


def run_detalles_match(
    settings: Settings,
    engine: Engine,
    reloj: Callable[[], float] = time.monotonic,
    now_fn: Callable[..., datetime] | None = None,
) -> dict[str, Any]:
    """Baja el detalle de las oportunidades con match que aún no lo tienen.

    Tope por TIEMPO, no por cantidad: antes de pedir cada detalle, si lo
    transcurrido ya alcanzó el presupuesto, corta y deja el resto para la
    corrida siguiente. Presupuesto: DETALLES_MINUTOS_NOCHE dentro de la ventana
    22:00–07:00 de Chile (regla 5), DETALLES_MINUTOS_DIA fuera de ella. Un
    detalle ya empezado termina: el corte real puede pasarse en lo que dure uno
    (~30 s de request + 60 s de enfriamiento si da 504).

    Un detalle que falla (504, timeout, parseo) se cuenta y se sigue con el
    próximo: vuelve solo a la cola. Un error del CANAL (429 que no es 10500,
    10500 con reintentos agotados, cuota, 401) corta el loop, igual que antes
    en run_match (regla 3). Idempotente: lo guardado ya no entra en la cola.

    `reloj` y `now_fn` son inyectables para tests.
    """
    nocturno = en_ventana_nocturna(now_fn)
    presupuesto_min = (
        settings.detalles_minutos_noche if nocturno else settings.detalles_minutos_dia
    )
    presupuesto_s = presupuesto_min * 60
    inicio = reloj()

    with Session(engine) as session:
        cola = _cola_detalles_match(session, ahora_utc())

    intentados = guardados = fallidos = 0
    result: dict[str, Any] = {}

    if cola:
        v1, v2 = _make_clients(settings, engine)
        try:
            for fuente, codigo in cola:
                if reloj() - inicio >= presupuesto_s:
                    result["cortado_por_tiempo"] = True
                    break
                intentados += 1
                try:
                    _bajar_detalle(settings, engine, v1, v2, fuente, codigo)
                    guardados += 1
                except _ERRORES_DE_CANAL:
                    fallidos += 1
                    raise
                except Exception:
                    fallidos += 1
                    _log.error(
                        "detalles-match: error bajando detalle %s %s", fuente, codigo, exc_info=True
                    )
        except _ERRORES_DE_CANAL as exc:
            # Insistir contra una API que nos rechaza solo quema cuota (regla 3).
            _log.warning("detalles-match: corte del canal — %s", exc)
            result["detalles_interrumpidos"] = True

    result.update(
        {
            "detalles_intentados": intentados,
            "detalles_guardados": guardados,
            "detalles_fallidos": fallidos,
            "detalles_pendientes": len(cola) - intentados,
            "minutos_usados": round((reloj() - inicio) / 60, 1),
            "presupuesto_minutos": presupuesto_min,
        }
    )
    return result


def run_alerts(settings: Settings, engine: Engine) -> dict[str, Any]:
    """Detecta eventos de seguidas y envía alertas inmediatas pendientes."""
    from app.alerts.detector import (
        detectar_cambio_estado_seguidas,
        detectar_recordatorio_cierre_seguidas,
    )
    from app.alerts.email import enviar_pendientes_inmediatas

    with Session(engine) as session:
        n_estado = detectar_cambio_estado_seguidas(session)
        n_cierre = detectar_recordatorio_cierre_seguidas(session)
        session.commit()

    with Session(engine) as session:
        result = enviar_pendientes_inmediatas(session, settings)

    return {
        "detectados_seguimiento_estado": n_estado,
        "detectados_seguimiento_cierre": n_cierre,
        **result,
    }


def run_resumen(settings: Settings, engine: Engine) -> dict[str, Any]:
    """Envía el resumen consolidado por usuario elegible."""
    from app.alerts.email import enviar_resumen

    with Session(engine) as session:
        return enviar_resumen(session, settings)


def run_backfill_fecha(settings: Settings, engine: Engine, fecha: date) -> dict[str, int]:
    """Backfill de una fecha concreta. Solo llamar dentro de ventana nocturna."""
    v1, _ = _make_clients(settings, engine)
    with Session(engine) as session:
        return sync_por_fecha(session, v1, settings, fecha)


# ---------------------------------------------------------------------------
# Ciclo con advisory lock
# ---------------------------------------------------------------------------


def _registrar_corrida(
    engine: Engine,
    job: str,
    iniciado_en: datetime,
    estado: str,
    resultado: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Graba UNA fila en job_runs con el desenlace de una corrida.

    Usa su PROPIA sesión de vida corta, NUNCA la `conn` del advisory lock:
    escribir ahí podría ensuciar su transacción, y un rollback soltaría el lock.
    """
    if resultado is not None:
        try:
            # Algunos runners devuelven valores no serializables; para el switch
            # importa más que la fila exista que el detalle exacto del resultado.
            resultado = json.loads(json.dumps(resultado, default=str))
        except (TypeError, ValueError):
            resultado = {"_no_serializable": True}

    with Session(engine) as session:
        session.add(
            JobRun(
                job=job,
                iniciado_en=iniciado_en,
                terminado_en=ahora_utc(),
                estado=estado,
                resultado_json=resultado,
                error=error,
            )
        )
        session.commit()


def _registrar_corrida_segura(
    engine: Engine,
    job: str,
    iniciado_en: datetime,
    estado: str,
    resultado: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Envoltorio que NUNCA propaga: esto es telemetría, no puede tumbar un job."""
    try:
        _registrar_corrida(engine, job, iniciado_en, estado, resultado, error)
    except Exception as _exc:
        _log.warning("job=%s: no se pudo registrar la corrida en job_runs: %s", job, _exc)


# Cada cuánto se reintenta pg_try_advisory_lock mientras se espera (F-actions-2).
_ESPERA_LOCK_INTERVALO_S = 30.0


def _adquirir_lock(
    job_name: str,
    engine: Engine,
    try_lock_fn: Callable[[Any, int], bool],
    esperar_lock_s: int,
    sleep_fn: Callable[[float], None],
    reloj_fn: Callable[[], float],
) -> Any | None:
    """Devuelve una conexión AUTOCOMMIT que YA tiene el lock, o None si venció el plazo.

    Con ``esperar_lock_s=0`` es un solo intento, como siempre. Con más, reintenta
    ``pg_try_advisory_lock`` cada 30 s hasta el plazo. No se usa el
    ``pg_advisory_lock`` bloqueante: sin timeout, un lock colgado dejaría la
    corrida esperando hasta que la mate su propio timeout.

    Mientras espera NO retiene ninguna conexión: cada intento abre la suya y la
    devuelve al pool si no consiguió el lock. Así no hay transacción abierta
    (Neon mata "idle in transaction", ver F-cuota) ni una conexión del pool
    (pool_size ≤ 5) ociosa durante media hora.
    """
    inicio = reloj_fn()
    esperando = False
    while True:
        # AUTOCOMMIT a propósito: pg_advisory_lock es de SESIÓN, no de transacción,
        # así que el lock se mantiene igual mientras la conexión viva. Con una
        # transacción abierta, en cambio, esta conexión quedaba "idle in
        # transaction" durante todo fn() y Neon la mataba por
        # idle_in_transaction_session_timeout: el lock se soltaba a mitad de la
        # corrida y la garantía de la regla 13 se perdía en los jobs largos.
        conn = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        try:
            acquired = try_lock_fn(conn, _LOCK_KEY)
        except BaseException:
            conn.close()
            raise
        esperado = reloj_fn() - inicio
        if acquired:
            if esperando:
                _log.info(
                    "job=%s: advisory lock conseguido tras %.0f s de espera", job_name, esperado
                )
            return conn
        conn.close()
        if esperado >= esperar_lock_s:
            if esperando:
                _log.info(
                    "job=%s: advisory lock sigue ocupado tras %.0f s de espera (plazo %d s)",
                    job_name,
                    esperado,
                    esperar_lock_s,
                )
            return None
        if not esperando:
            _log.info(
                "job=%s: advisory lock ocupado — esperando hasta %d s, reintento cada %.0f s",
                job_name,
                esperar_lock_s,
                _ESPERA_LOCK_INTERVALO_S,
            )
            esperando = True
        sleep_fn(min(_ESPERA_LOCK_INTERVALO_S, esperar_lock_s - esperado))


def _run_with_lock(
    job_name: str,
    fn: Callable[[], dict[str, int]],
    engine: Engine,
    try_lock_fn: Callable[[Any, int], bool] = _pg_try_lock,
    unlock_fn: Callable[[Any, int], None] = _pg_unlock,
    propagar: bool = False,
    esperar_lock_s: int = 0,
    sleep_fn: Callable[[float], None] = time.sleep,
    reloj_fn: Callable[[], float] = time.monotonic,
) -> dict[str, int] | None:
    """Ejecuta fn dentro de un pg_advisory_lock.

    Retorna None si el lock está ocupado (otro proceso en ejecución).
    El lock se libera SIEMPRE en finally.

    `esperar_lock_s` (F-actions-2, solo el CLI lo pasa > 0): en vez de omitir de
    inmediato, reintenta el lock hasta ese plazo (ver :func:`_adquirir_lock`).
    Con 0, el default, se comporta como siempre: scheduler, endpoint y Render no
    cambian. `sleep_fn` y `reloj_fn` son inyectables para tests.

    Si fn falla: con `propagar=False` (default: scheduler, endpoint y los pasos
    de `_ciclo_nocturno`) registra el error y devuelve None, para que un paso
    caído no corte la secuencia. Con `propagar=True` —solo el CLI— registra
    igual y re-lanza, para que el proceso salga ≠ 0 y GitHub Actions quede en
    rojo. Así el CLI distingue "omitido" (None, sin excepción) de "error".

    Cada corrida deja UNA fila en job_runs (ok | error | omitido). Este es el
    único camino por el que pasan todos los disparadores —scheduler, endpoint
    y CLI—, así que es el único lugar donde hay que instrumentar. La telemetría
    no cambia el valor de retorno ni puede hacer fallar el job.
    """
    iniciado_en = ahora_utc()
    conn = _adquirir_lock(job_name, engine, try_lock_fn, esperar_lock_s, sleep_fn, reloj_fn)
    if conn is None:
        _log.info("job=%s: advisory lock ocupado — ciclo omitido", job_name)
        # "omitido" NO es fallo: otra instancia está haciendo el trabajo.
        _registrar_corrida_segura(engine, job_name, iniciado_en, "omitido")
        return None
    with conn:
        try:
            _log.info("job=%s: iniciando", job_name)
            result = fn()
            _log.info("job=%s: OK %s", job_name, result)
            _registrar_corrida_segura(engine, job_name, iniciado_en, "ok", resultado=result)
            return result
        except Exception:
            tb = traceback.format_exc()
            _log.error("job=%s: ERROR\n%s", job_name, tb)
            _registrar_corrida_segura(engine, job_name, iniciado_en, "error", error=tb)
            if propagar:
                raise
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
    esperar_lock_s: int = 0,
) -> None:
    """Datos abiertos + estados vencidos + lifecycle + competencia + backfill del día anterior.

    Solo ejecuta en ventana 22:00–07:00. datos_abiertos va primero para que sus
    ítems UNSPSC estén disponibles antes del próximo ciclo de match (08:00).
    competencia va DESPUÉS de estados-vencidos y lifecycle: necesita que los
    estados (incl. las adjudicadas) estén al día (F-competencia).

    Los tres pasos que leen lic-da comparten una `CacheZipsDA`: cada mes se baja
    una sola vez por noche aunque lo usen ítems, estados y competencia.

    detalles-match va AL FINAL con el presupuesto nocturno (DETALLES_MINUTOS_NOCHE,
    120 min por defecto): es el paso que más dura y el único que se puede cortar
    sin perder nada. OJO al dimensionar el timeout del workflow nocturno
    (F-actions-2): tiene que cubrir ese presupuesto + lo que tarde el resto del
    ciclo + ~2 min de margen porque un detalle ya empezado termina. Con el
    default, no menos de 120 + el resto medido.

    `esperar_lock_s` se aplica a CADA paso (lo pasa el CLI, F-actions-2): entre
    un paso y otro otra corrida puede quedarse con el lock.
    """
    if not en_ventana_nocturna(now_fn):
        _log.warning("ciclo_nocturno: fuera de ventana horaria — abortando")
        return

    with CacheZipsDA(settings) as zips:
        _run_with_lock(
            "datos_abiertos",
            lambda: run_datos_abiertos(settings, engine, zips=zips),
            engine,
            esperar_lock_s=esperar_lock_s,
        )
        _run_with_lock(
            "estados-vencidos",
            lambda: run_estados_vencidos(settings, engine, zips=zips, now_fn=now_fn),
            engine,
            esperar_lock_s=esperar_lock_s,
        )
        _run_with_lock(
            "lifecycle",
            lambda: run_lifecycle(settings, engine),
            engine,
            esperar_lock_s=esperar_lock_s,
        )
        _run_with_lock(
            "competencia",
            lambda: run_competencia(settings, engine, zips=zips),
            engine,
            esperar_lock_s=esperar_lock_s,
        )

    # Backfill: ayer (simple, se puede extender a rangos mayores)
    ayer = (datetime.now(UTC) - timedelta(days=1)).date()
    _run_with_lock(
        "backfill_ayer",
        lambda: run_backfill_fecha(settings, engine, ayer),
        engine,
        esperar_lock_s=esperar_lock_s,
    )

    _run_with_lock(
        "detalles-match",
        lambda: run_detalles_match(settings, engine, now_fn=now_fn),
        engine,
        esperar_lock_s=esperar_lock_s,
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

    # Cada 30 min: CA incremental + match + alertas inmediatas + detalles de lo
    # que matcheó (al final: no demora las alertas; paridad con ciclo-ca).
    sched.add_job(
        lambda: (
            _run_with_lock("ca_incremental", lambda: run_sync_ca(settings, engine), engine),
            _run_with_lock("match_post_ca", lambda: run_match(settings, engine), engine),
            _run_with_lock("alerts_post_ca", lambda: run_alerts(settings, engine), engine),
            _run_with_lock(
                "detalles-match", lambda: run_detalles_match(settings, engine), engine
            ),
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

    # Diario: resumen consolidado por usuario elegible
    sched.add_job(
        lambda: _run_with_lock("resumen", lambda: run_resumen(settings, engine), engine),
        "cron",
        hour=settings.digest_hour,
        minute=5,
        timezone="America/Santiago",
        id="resumen",
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
