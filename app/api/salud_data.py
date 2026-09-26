"""Datos de salud del sistema — compartidos entre /salud (HTML) y /api/salud (JSON).

IMPORTANTE: get_salud_data() NUNCA debe incluir MP_TICKET, SECRET_KEY ni JOBS_TOKEN.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.retencion import tamano_bd, tamano_tabla
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE
from app.models.tables import PlanCompraLinea, SyncState

# Clave del advisory lock (igual que orchestrator._LOCK_KEY)
_LOCK_KEY = 7_891_011


def _advisory_lock_activo(session: Session) -> bool:
    try:
        count = session.execute(
            text(
                "SELECT COUNT(*) FROM pg_locks "
                "WHERE locktype='advisory' AND objid=:k"
            ),
            {"k": _LOCK_KEY},
        ).scalar_one()
        return int(count) > 0
    except Exception:
        return False


def get_salud_data(session: Session, settings: Settings) -> dict[str, Any]:
    """Agrega el estado del sistema. No incluye secretos."""
    fuentes = list(session.execute(select(SyncState)).scalars())

    sync_info = [
        {
            "fuente": f.fuente,
            "ultima_ejecucion": f.ultima_ejecucion.isoformat() if f.ultima_ejecucion else None,
            "ultimo_ok": f.ultimo_ok.isoformat() if f.ultimo_ok else None,
            "cursor": f.cursor,
            "requests_usadas_hoy": f.requests_usadas_hoy,
            "notas": f.notas,
        }
        for f in fuentes
    ]

    email_sync = next((f for f in fuentes if f.fuente == "alerts_email"), None)
    correos_hoy = email_sync.requests_usadas_hoy if email_sync else 0

    total_api_hoy = sum(
        f.requests_usadas_hoy for f in fuentes if f.fuente != "alerts_email"
    )

    tam = tamano_bd(session)
    limite_bytes = 500 * 1024 * 1024

    errores = [
        {"fuente": f.fuente, "nota": f.notas}
        for f in fuentes
        if f.notas
    ][-10:]

    return {
        "sync_state": sync_info,
        "cuota_api": {
            "usadas_hoy": total_api_hoy,
            "presupuesto": settings.api_daily_budget,
        },
        "correos": {
            "enviados_hoy": correos_hoy,
            "limite_diario": settings.email_daily_limit,
        },
        "base_datos": {
            "tamano_bytes": tam,
            "limite_bytes": limite_bytes,
            "porcentaje": round(tam / limite_bytes * 100, 1) if tam else None,
        },
        "lock_activo": _advisory_lock_activo(session),
        "errores_recientes": errores,
        "plan_anual": _plan_anual_salud(session),
    }


def _plan_anual_salud(session: Session) -> dict[str, Any]:
    """F-plan-busqueda: filas del PAC completo cargadas, año, `Last-Modified`
    de la fuente y tamaño de `plan_compra_lineas` (regla 11: Neon 0,5 GB)."""
    agno = datetime.now(TZ_CHILE).year
    state = session.get(SyncState, f"plan_compra_anual_{agno}")
    n_filas = session.execute(
        select(func.count()).select_from(PlanCompraLinea).where(PlanCompraLinea.agno == agno)
    ).scalar_one()
    return {
        "agno": agno,
        "filas_cargadas": n_filas,
        "actualizado_al": state.cursor if state else None,
        "ultimo_ok": state.ultimo_ok.isoformat() if state and state.ultimo_ok else None,
        "tamano_tabla_bytes": tamano_tabla(session, "plan_compra_lineas"),
    }
