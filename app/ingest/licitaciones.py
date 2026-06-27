"""Ingesta de licitaciones desde la API v1 de Mercado Público."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.types import LicitacionBasica, LicitacionDetalle
from app.core.db_retry import commit_con_retry
from app.core.logging import get_logger
from app.core.montos import normalizar_clp
from app.core.settings import Settings
from app.models.enums import estado_licitacion
from app.models.tables import Licitacion, LicitacionItem, SyncState

_log = get_logger(__name__)


def _ahora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _fecha_a_dt(d: date | None) -> datetime | None:
    if d is None:
        return None
    return datetime(d.year, d.month, d.day)


def upsert_basica(session: Session, item: LicitacionBasica) -> tuple[Licitacion, bool]:
    """Upsert básico de una licitación. Devuelve (objeto, es_nueva)."""
    existing = session.get(Licitacion, item.codigo)
    es_nueva = existing is None

    if existing is None:
        lic = Licitacion(
            codigo=item.codigo,
            creado_en=_ahora(),
        )
        session.add(lic)
    else:
        lic = existing

    lic.nombre = item.nombre
    lic.estado_codigo = item.estado
    lic.estado = estado_licitacion(item.estado).value
    lic.tipo = item.tipo
    lic.codigo_organismo = item.codigo_organismo
    lic.fecha_publicacion = _fecha_a_dt(item.fecha_publicacion)
    lic.fecha_cierre = _fecha_a_dt(item.fecha_cierre)
    lic.actualizado_en = _ahora()
    return lic, es_nueva


def upsert_detalle(
    session: Session, det: LicitacionDetalle, settings: Settings
) -> None:
    """Actualiza una licitación con datos de detalle e items."""
    lic, _ = upsert_basica(session, det)
    lic.descripcion = det.descripcion
    lic.moneda = det.moneda or None
    lic.monto_estimado = det.monto_estimado
    lic.monto_clp = normalizar_clp(det.monto_estimado, det.moneda, settings)
    lic.detalle_obtenido = True
    lic.actualizado_en = _ahora()

    # Reemplazar items
    for item in lic.items:
        session.delete(item)
    session.flush()

    for it in det.items:
        session.add(
            LicitacionItem(
                licitacion_codigo=lic.codigo,
                codigo_producto=it.codigo_producto,
                nombre=it.nombre,
                cantidad=it.cantidad,
                unidad=it.unidad,
            )
        )


def _cumple_prefilter(item: LicitacionBasica, keywords: list[str]) -> bool:
    """Pre-filtro barato: True si el nombre contiene alguna keyword (case-insensitive)."""
    if not keywords:
        return True
    nombre_lower = item.nombre.lower()
    return any(kw.lower() in nombre_lower for kw in keywords)


def _guardar_estado(
    session: Session,
    fuente: str,
    *,
    ok: bool,
    notas: str = "",
    requests_usadas: int = 0,
) -> None:
    state = session.get(SyncState, fuente)
    if state is None:
        state = SyncState(fuente=fuente)
        session.add(state)
    ahora = _ahora()
    state.ultima_ejecucion = ahora
    if ok:
        state.ultimo_ok = ahora
    state.requests_usadas_hoy = requests_usadas
    if notas:
        state.notas = notas


def _procesar_lote_licitaciones(session: Session, lote: list[LicitacionBasica], contexto: str) -> tuple[int, int]:
    """Aplica upsert_basica a un lote y comitea con reintento ante desconexión (regla 12).

    Si el lote falla tras los reintentos, queda logueado y se descarta (0, 0):
    la corrida sigue con el siguiente lote en vez de abortar todo.
    """
    contador = {"nuevas": 0, "actualizadas": 0}

    def _aplicar() -> None:
        contador["nuevas"] = 0
        contador["actualizadas"] = 0
        for item in lote:
            _, es_nueva = upsert_basica(session, item)
            if es_nueva:
                contador["nuevas"] += 1
            else:
                contador["actualizadas"] += 1

    ok = commit_con_retry(session, _aplicar, contexto=f"{contexto} (lote de {len(lote)})")
    if not ok:
        return 0, 0
    return contador["nuevas"], contador["actualizadas"]


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def sync_activas(
    session: Session,
    v1_client: MercadoPublicoV1Client,
    settings: Settings,
    limit: int | None = None,
) -> dict[str, int]:
    """Sincroniza licitaciones activas via API. Un request total.

    Upsert en lotes de settings.ingest_batch_size, commit por lote con
    reintento ante desconexión transitoria (regla 12: nunca una transacción
    larga ni todo el avance perdido por una caída).
    `limit`, si se especifica, acota cuántas licitaciones se procesan
    (para pruebas locales acotadas).
    """
    items = v1_client.licitaciones_activas()
    batch_size = settings.ingest_batch_size
    nuevas = actualizadas = 0
    lote: list[LicitacionBasica] = []

    for idx, item in enumerate(items):
        if limit is not None and idx >= limit:
            break
        lote.append(item)

        if len(lote) >= batch_size:
            n, a = _procesar_lote_licitaciones(session, lote, "sync_activas")
            nuevas += n
            actualizadas += a
            lote = []

    if lote:
        n, a = _procesar_lote_licitaciones(session, lote, "sync_activas")
        nuevas += n
        actualizadas += a

    _guardar_estado(session, "licitaciones_activas", ok=True, requests_usadas=1)
    session.commit()

    _log.info("sync_activas: %d nuevas, %d actualizadas", nuevas, actualizadas)
    return {"nuevas": nuevas, "actualizadas": actualizadas, "total": nuevas + actualizadas}


def fetch_detalles_pendientes(
    session: Session,
    v1_client: MercadoPublicoV1Client,
    settings: Settings,
    max_requests: int = 200,
) -> dict[str, int]:
    """Descarga detalle de licitaciones con detalle_obtenido=False.

    Aplica pre-filtro de keywords amplias si settings.prefilter_keywords está definido.
    Respeta max_requests (cada detalle consume 1 request).
    """
    from sqlalchemy import select

    pendientes = list(
        session.execute(
            select(Licitacion)
            .where(Licitacion.detalle_obtenido.is_(False))
            .order_by(Licitacion.fecha_cierre.asc())
            .limit(max_requests * 2)  # margen para el pre-filtro
        ).scalars()
    )

    keywords = settings.prefilter_keywords
    procesadas = descartadas = errores = 0

    for lic in pendientes:
        if procesadas >= max_requests:
            break

        # Pre-filtro barato por nombre
        item_basico = LicitacionBasica(
            codigo=lic.codigo,
            nombre=lic.nombre,
            estado=lic.estado_codigo,
            fecha_publicacion=None,
            fecha_cierre=None,
            tipo=lic.tipo,
            codigo_organismo=lic.codigo_organismo,
        )
        if not _cumple_prefilter(item_basico, keywords):
            descartadas += 1
            continue

        try:
            det = v1_client.licitacion_detalle(lic.codigo)
            upsert_detalle(session, det, settings)
            session.commit()
            procesadas += 1
        except Exception as exc:
            _log.warning("Error al pedir detalle %s: %s", lic.codigo, exc)
            session.rollback()
            errores += 1

    _guardar_estado(
        session,
        "licitaciones_detalles",
        ok=errores == 0,
        requests_usadas=procesadas,
    )
    session.commit()

    _log.info(
        "fetch_detalles: procesadas=%d descartadas=%d errores=%d",
        procesadas,
        descartadas,
        errores,
    )
    return {"procesadas": procesadas, "descartadas": descartadas, "errores": errores}


def sync_por_fecha(
    session: Session,
    v1_client: MercadoPublicoV1Client,
    settings: Settings,
    fecha: date,
) -> dict[str, int]:
    """Backfill: descarga licitaciones de una fecha concreta (sin guard de ventana).

    El guard 22:00–07:00 lo aplica el scheduler, no esta función.
    """
    items = v1_client.licitaciones_por_fecha(fecha)
    batch_size = settings.ingest_batch_size
    nuevas = actualizadas = 0
    lote: list[LicitacionBasica] = []

    for item in items:
        lote.append(item)

        if len(lote) >= batch_size:
            n, a = _procesar_lote_licitaciones(session, lote, "sync_por_fecha")
            nuevas += n
            actualizadas += a
            lote = []

    if lote:
        n, a = _procesar_lote_licitaciones(session, lote, "sync_por_fecha")
        nuevas += n
        actualizadas += a

    _log.info("sync_por_fecha(%s): %d nuevas, %d actualizadas", fecha, nuevas, actualizadas)
    return {"nuevas": nuevas, "actualizadas": actualizadas, "total": nuevas + actualizadas}
