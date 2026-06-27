"""Ingesta de licitacion_items (UNSPSC) desde datos abiertos de ChileCompra.

Complementa fetch_detalles_pendientes (app/ingest/licitaciones.py): esa función
gasta 1 request de cuota de la API por licitación solo para obtener sus ítems.
Esta ingesta lee el ZIP mensual público (sin ticket, sin cuota — ver
docs/04-datos-abiertos.md) y completa SOLO licitaciones activas que aún no
tienen ítems. No marca detalle_obtenido ni toca los demás campos del detalle
(Descripcion, MontoEstimado, etc.) — eso sigue viniendo de la API.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.clients.datos_abiertos import (
    ItemDA,
    descargar_zip,
    head_last_modified,
    stream_items,
    url_lic_da,
)
from app.core.db_retry import commit_con_retry
from app.core.logging import get_logger
from app.core.settings import Settings
from app.models.enums import EstadoOportunidad
from app.models.tables import Licitacion, LicitacionItem, SyncState

_log = get_logger(__name__)
_FUENTE = "datos_abiertos_lic"
_TZ_CHILE = ZoneInfo("America/Santiago")

_VACIO: dict[str, int] = {
    "licitaciones_tocadas": 0,
    "items_insertados": 0,
    "no_unspsc": 0,
    "descargado": 0,
}


def _ahora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _mes_actual_chile() -> tuple[int, int]:
    ahora = datetime.now(_TZ_CHILE)
    return ahora.year, ahora.month


def _leer_cursor(session: Session) -> str | None:
    state = session.get(SyncState, _FUENTE)
    return state.cursor if state else None


def _guardar_estado(session: Session, *, cursor: str | None, notas: str) -> None:
    state = session.get(SyncState, _FUENTE)
    if state is None:
        state = SyncState(fuente=_FUENTE)
        session.add(state)
    state.ultima_ejecucion = _ahora()
    if cursor is not None:
        state.cursor = cursor
        state.ultimo_ok = _ahora()
    state.notas = notas


def _codigos_objetivo(session: Session) -> set[str]:
    """Licitaciones activas (publicada) que todavía no tienen ítems."""
    tiene_items = exists().where(LicitacionItem.licitacion_codigo == Licitacion.codigo)
    rows = session.execute(
        select(Licitacion.codigo)
        .where(Licitacion.estado == EstadoOportunidad.PUBLICADA.value)
        .where(~tiene_items)
    ).scalars()
    return set(rows)


def _es_unspsc_estandar(codigo_producto: str) -> bool:
    return len(codigo_producto) == 8 and codigo_producto.isdigit()


def _insertar_lote(session: Session, lote: list[ItemDA], contexto: str) -> bool:
    def _aplicar() -> None:
        for it in lote:
            session.add(
                LicitacionItem(
                    licitacion_codigo=it.codigo_externo,
                    codigo_producto=it.codigo_producto,
                    nombre=it.nombre,
                    cantidad=it.cantidad,
                    unidad=it.unidad,
                )
            )

    return commit_con_retry(session, _aplicar, contexto=contexto)


def sync_items_datos_abiertos(
    session: Session,
    settings: Settings,
    anio: int | None = None,
    mes: int | None = None,
) -> dict[str, int]:
    """Completa licitacion_items de licitaciones activas desde el ZIP mensual.

    Cursor por Last-Modified del blob (regla: nunca re-descargar 14+ MB sin
    necesidad). El archivo del mes vigente se reescribe periódicamente (ver
    spike), así que comparar Last-Modified sí detecta cambios reales.

    Nota de diseño: si el archivo no cambió, se omite por completo aunque
    hayan aparecido licitaciones objetivo nuevas desde la última corrida —
    es el comportamiento pedido (evitar descargas innecesarias); la próxima
    vez que el blob se actualice (a más tardar al día siguiente, según lo
    observado) esas licitaciones quedan cubiertas.
    """
    if not settings.datos_abiertos_habilitado:
        return dict(_VACIO)

    if anio is None or mes is None:
        anio_def, mes_def = _mes_actual_chile()
        anio = anio if anio is not None else anio_def
        mes = mes if mes is not None else mes_def

    url = url_lic_da(anio, mes, settings.datos_abiertos_base_url)
    last_modified = head_last_modified(url)
    cursor_nuevo = last_modified.isoformat() if last_modified is not None else None

    if cursor_nuevo is not None and cursor_nuevo == _leer_cursor(session):
        _log.info("sync_items_datos_abiertos: sin cambios (Last-Modified=%s) — omitido", cursor_nuevo)
        return dict(_VACIO)

    objetivo = _codigos_objetivo(session)
    if not objetivo:
        _guardar_estado(session, cursor=cursor_nuevo, notas="sin licitaciones objetivo")
        session.commit()
        return dict(_VACIO)

    licitaciones_tocadas: set[str] = set()
    items_insertados = 0
    no_unspsc = 0
    vistos: set[tuple[str, str]] = set()
    lote: list[ItemDA] = []
    batch_size = settings.ingest_batch_size

    with tempfile.TemporaryDirectory(prefix="mp_datos_abiertos_") as tmp_dir:
        zip_path = Path(tmp_dir) / f"lic-{anio}-{mes}.zip"
        descargar_zip(url, str(zip_path))

        for item in stream_items(str(zip_path)):
            if item.codigo_externo not in objetivo:
                continue
            clave = (item.codigo_externo, item.codigo_item)
            if clave in vistos:
                continue
            vistos.add(clave)

            if not _es_unspsc_estandar(item.codigo_producto):
                no_unspsc += 1

            lote.append(item)
            if len(lote) >= batch_size:
                if _insertar_lote(session, lote, "datos_abiertos lote items"):
                    licitaciones_tocadas.update(i.codigo_externo for i in lote)
                    items_insertados += len(lote)
                lote = []

        if lote and _insertar_lote(session, lote, "datos_abiertos lote items"):
            licitaciones_tocadas.update(i.codigo_externo for i in lote)
            items_insertados += len(lote)

    notas = f"licitaciones={len(licitaciones_tocadas)} items={items_insertados} no_unspsc={no_unspsc}"
    _guardar_estado(session, cursor=cursor_nuevo, notas=notas)
    session.commit()

    _log.info(
        "sync_items_datos_abiertos: licitaciones=%d items=%d no_unspsc=%d",
        len(licitaciones_tocadas),
        items_insertados,
        no_unspsc,
    )
    return {
        "licitaciones_tocadas": len(licitaciones_tocadas),
        "items_insertados": items_insertados,
        "no_unspsc": no_unspsc,
        "descargado": 1,
    }
