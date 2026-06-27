"""Lifecycle: refresca estados de oportunidades próximas a cierre."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.core.logging import get_logger
from app.core.settings import Settings
from app.ingest.compra_agil import upsert_ca_detalle
from app.ingest.licitaciones import upsert_detalle
from app.models.enums import ESTADOS_TERMINALES, EstadoOportunidad
from app.models.tables import CompraAgil, Licitacion, OportunidadSeguida

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

    También incluye, sin importar la fecha de cierre, las oportunidades
    seguidas (no archivadas) por algún usuario — el usuario las marcó como
    importantes y quiere detectar su avance aunque ya no sean match de ningún
    perfil (F-seguir). Son pocas, así que no comprometen el presupuesto diario
    (regla 3).

    Prioriza por cercanía de cierre (las más urgentes primero).
    Respeta max_requests (1 req por oportunidad).
    """
    ahora = _ahora()
    ventana_inicio = ahora - timedelta(days=7)
    ventana_fin = ahora + timedelta(days=3)
    budget_restante = max_requests
    actualizadas_lic = actualizadas_ca = errores = 0

    # --- Licitaciones no terminales próximas a cierre, o seguidas ---
    lics = list(
        session.execute(
            select(Licitacion)
            .where(
                Licitacion.estado.not_in([e.value for e in ESTADOS_TERMINALES]),
                or_(
                    Licitacion.fecha_cierre.between(ventana_inicio, ventana_fin),
                    exists().where(
                        OportunidadSeguida.codigo_oportunidad == Licitacion.codigo,
                        OportunidadSeguida.fuente == "licitaciones",
                        OportunidadSeguida.archivada.is_(False),
                    ),
                ),
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
            upsert_detalle(session, det, settings)
            session.commit()
            actualizadas_lic += 1
            budget_restante -= 1
        except Exception as exc:
            _log.warning("lifecycle: error lic %s: %s", lic.codigo, exc)
            session.rollback()
            errores += 1

    # --- Compras Ágiles no terminales próximas a cierre, o seguidas ---
    cas = list(
        session.execute(
            select(CompraAgil)
            .where(
                CompraAgil.estado.not_in([e.value for e in ESTADOS_TERMINALES]),
                or_(
                    CompraAgil.fecha_cierre.between(ventana_inicio, ventana_fin),
                    exists().where(
                        OportunidadSeguida.codigo_oportunidad == CompraAgil.codigo,
                        OportunidadSeguida.fuente == "compras_agiles",
                        OportunidadSeguida.archivada.is_(False),
                    ),
                ),
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
            upsert_ca_detalle(session, det_ca)
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
