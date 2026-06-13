"""Ingesta de Compras Ágiles desde la API v2 de Mercado Público."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from app.clients.base import MPRateLimitError
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.clients.types import CompraAgilBasica, CompraAgilDetalle
from app.core.logging import get_logger
from app.core.settings import Settings
from app.models.enums import estado_ca
from app.models.tables import CaProducto, CompraAgil, SyncState

_log = get_logger(__name__)

_FUENTE = "compra_agil"
# Estados que se ingestan; el resto se descarta localmente (spec: filtrar después del API)
_ESTADOS_VALIDOS = {"publicada", "cerrada", "proveedor_seleccionado"}


def _ahora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _upsert_ca_basica(session: Session, item: CompraAgilBasica) -> tuple[CompraAgil, bool]:
    """Upsert básico de Compra Ágil. Devuelve (objeto, es_nueva)."""
    existing = session.get(CompraAgil, item.codigo)
    es_nueva = existing is None

    if existing is None:
        ca = CompraAgil(codigo=item.codigo, creado_en=_ahora())
        session.add(ca)
    else:
        ca = existing

    ca.nombre = item.nombre
    ca.estado = estado_ca(item.estado).value
    ca.fecha_publicacion = item.fecha_publicacion
    ca.fecha_cierre = item.fecha_cierre
    ca.fecha_ultimo_cambio = item.fecha_ultimo_cambio
    ca.monto_disponible_clp = item.monto_clp
    ca.region = item.region
    ca.organismo_nombre = item.organismo_nombre
    ca.organismo_rut = item.organismo_rut
    ca.total_ofertas = item.total_ofertas
    ca.actualizado_en = _ahora()
    return ca, es_nueva


def _upsert_ca_detalle(session: Session, det: CompraAgilDetalle) -> None:
    """Actualiza una CA con datos de detalle y reemplaza sus productos."""
    ca, _ = _upsert_ca_basica(session, det)
    ca.descripcion = det.descripcion
    ca.id_orden_compra = det.id_orden_compra
    ca.estado_convocatoria = det.estado_convocatoria
    ca.actualizado_en = _ahora()

    for prod in ca.productos:
        session.delete(prod)
    session.flush()

    for p in det.productos:
        session.add(
            CaProducto(
                ca_codigo=ca.codigo,
                codigo_producto=p.codigo_producto,
                nombre=p.nombre,
                descripcion="",
                cantidad=p.cantidad,
                unidad=p.unidad,
            )
        )


def _leer_cursor(session: Session) -> datetime | None:
    """Lee el cursor de sync_state como datetime UTC, None si no existe."""
    state = session.get(SyncState, _FUENTE)
    if state is None or not state.cursor:
        return None
    try:
        return datetime.fromisoformat(state.cursor).replace(tzinfo=UTC)
    except ValueError:
        _log.warning("Cursor de %s inválido: %r — empezando desde cero", _FUENTE, state.cursor)
        return None


def _guardar_cursor(session: Session, nuevo_cursor_dt: datetime, ok: bool) -> None:
    state = session.get(SyncState, _FUENTE)
    if state is None:
        state = SyncState(fuente=_FUENTE)
        session.add(state)
    ahora = _ahora()
    state.ultima_ejecucion = ahora
    if ok:
        state.cursor = nuevo_cursor_dt.replace(tzinfo=None).isoformat()
        state.ultimo_ok = ahora


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def sync_incremental(
    session: Session,
    v2_client: MercadoPublicoV2Client,
    settings: Settings,
) -> dict[str, int]:
    """Sincronización incremental de Compras Ágiles.

    Lee cursor desde sync_state (ISO-8601 UTC), aplica solapamiento de 5 min,
    pagina de 50, filtra localmente por estado, hace commit por página.
    Cursor avanza SOLO si la corrida completa fue exitosa.
    """
    cursor_dt = _leer_cursor(session)
    cambio_desde: datetime | None = None
    if cursor_dt is not None:
        # solapamiento de 5 min para no perder cambios en el borde
        cambio_desde = cursor_dt - timedelta(minutes=5)
        # quitar tzinfo: el cliente serializa como ISO sin zona
        cambio_desde = cambio_desde.replace(tzinfo=None)

    nuevo_cursor_dt: datetime | None = None
    nuevas = actualizadas = descartadas = 0
    exitoso = False

    try:
        pagina = 1
        while True:
            resp = v2_client.listar_compra_agil(
                cambio_desde=cambio_desde,
                tamano_pagina=50,
                numero_pagina=pagina,
            )

            for ca in resp.items:
                # Filtro local por estado (spec: sin filtro en el endpoint)
                if ca.estado not in _ESTADOS_VALIDOS:
                    descartadas += 1
                    continue

                _, es_nueva = _upsert_ca_basica(session, ca)
                if es_nueva:
                    nuevas += 1
                else:
                    actualizadas += 1

                if ca.fecha_ultimo_cambio is not None and (
                    nuevo_cursor_dt is None or ca.fecha_ultimo_cambio > nuevo_cursor_dt
                ):
                    nuevo_cursor_dt = ca.fecha_ultimo_cambio

            # Commit por página → progreso persiste incluso ante 429 en pág siguiente
            session.commit()

            if pagina >= resp.paginacion.total_paginas:
                break
            pagina += 1

        exitoso = True

    except MPRateLimitError:
        _log.warning(
            "429 recibido en pág %d de CA incremental — progreso parcial guardado, cursor intacto",
            pagina,
        )
        raise
    except Exception:
        _log.error("Error en sync_incremental CA (pág %d)", pagina, exc_info=True)
        raise
    finally:
        # Cursor avanza SOLO en éxito total
        if exitoso and nuevo_cursor_dt is not None:
            _guardar_cursor(session, nuevo_cursor_dt, ok=True)
            session.commit()
        elif not exitoso:
            # Registrar que hubo un intento (sin avanzar cursor)
            state = session.get(SyncState, _FUENTE)
            if state is None:
                state = SyncState(fuente=_FUENTE)
                session.add(state)
            state.ultima_ejecucion = _ahora()
            try:
                session.commit()
            except Exception:
                session.rollback()

    _log.info(
        "sync_incremental CA: nuevas=%d act=%d desc=%d",
        nuevas,
        actualizadas,
        descartadas,
    )
    return {
        "nuevas": nuevas,
        "actualizadas": actualizadas,
        "descartadas": descartadas,
    }


def fetch_detalle(
    session: Session,
    v2_client: MercadoPublicoV2Client,
    codigo: str,
) -> bool:
    """Descarga y persiste el detalle de una CA por código. Retorna True si OK."""
    try:
        det = v2_client.detalle_compra_agil(codigo)
        _upsert_ca_detalle(session, det)
        session.commit()
        return True
    except Exception as exc:
        _log.warning("Error al pedir detalle CA %s: %s", codigo, exc)
        session.rollback()
        return False
