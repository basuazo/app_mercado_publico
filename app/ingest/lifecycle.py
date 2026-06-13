"""Lifecycle: refresca estados de oportunidades próximas a cierre."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.core.logging import get_logger
from app.core.settings import Settings
from app.ingest.compra_agil import _upsert_ca_detalle
from app.ingest.licitaciones import _upsert_basica
from app.models.enums import ESTADOS_TERMINALES, EstadoOportunidad
from app.models.tables import CompraAgil, Licitacion

_log = get_logger(__name__)

_ESTADOS_NO_TERMINALES = [
    e.value for e in EstadoOportunidad if e not in ESTADOS_TERMINALES and e != EstadoOportunidad.DESCONOCIDO
]


def _ahora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def refresh_estados(
    session: Session,
    v1_client: MercadoPublicoV1Client,
    v2_client: MercadoPublicoV2Client,
    settings: Settings,
    max_requests: int = 100,
) -> dict[str, int]:
    """Re-consulta oportunidades no terminales con fecha_cierre en ±7/+3 días.

    Prioriza por cercanía de cierre (las más urgentes primero).
    Respeta max_requests (1 req por oportunidad).
    """
    ahora = _ahora()
    ventana_inicio = ahora - timedelta(days=7)
    ventana_fin = ahora + timedelta(days=3)
    budget_restante = max_requests
    actualizadas_lic = actualizadas_ca = errores = 0

    # --- Licitaciones no terminales próximas a cierre ---
    lics = list(
        session.execute(
            select(Licitacion)
            .where(
                Licitacion.estado.not_in([e.value for e in ESTADOS_TERMINALES]),
                Licitacion.fecha_cierre >= ventana_inicio,
                Licitacion.fecha_cierre <= ventana_fin,
            )
            .order_by(Licitacion.fecha_cierre.asc())
            .limit(budget_restante)
        ).scalars()
    )

    for lic in lics:
        if budget_restante <= 0:
            break
        try:
            det = v1_client.licitacion_detalle(lic.codigo)
            _upsert_basica(session, det)
            session.commit()
            actualizadas_lic += 1
            budget_restante -= 1
        except Exception as exc:
            _log.warning("lifecycle: error lic %s: %s", lic.codigo, exc)
            session.rollback()
            errores += 1

    # --- Compras Ágiles no terminales próximas a cierre ---
    cas = list(
        session.execute(
            select(CompraAgil)
            .where(
                CompraAgil.estado.not_in([e.value for e in ESTADOS_TERMINALES]),
                CompraAgil.fecha_cierre >= ventana_inicio,
                CompraAgil.fecha_cierre <= ventana_fin,
            )
            .order_by(CompraAgil.fecha_cierre.asc())
            .limit(budget_restante)
        ).scalars()
    )

    for ca in cas:
        if budget_restante <= 0:
            break
        try:
            det_ca = v2_client.detalle_compra_agil(ca.codigo)
            _upsert_ca_detalle(session, det_ca)
            session.commit()
            actualizadas_ca += 1
            budget_restante -= 1
        except Exception as exc:
            _log.warning("lifecycle: error CA %s: %s", ca.codigo, exc)
            session.rollback()
            errores += 1

    _log.info(
        "refresh_estados: lic=%d ca=%d errores=%d",
        actualizadas_lic,
        actualizadas_ca,
        errores,
    )
    return {
        "actualizadas_licitaciones": actualizadas_lic,
        "actualizadas_ca": actualizadas_ca,
        "errores": errores,
    }
