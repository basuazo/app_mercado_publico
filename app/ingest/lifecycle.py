"""Lifecycle: refresca estados de oportunidades próximas a cierre y de las ya vencidas."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session

from app.clients.base import MPAuthError, MPRateLimitError, QuotaExceededError
from app.clients.mp_v1 import MercadoPublicoV1Client
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import ahora_utc, en_ventana_nocturna
from app.ingest.compra_agil import upsert_ca_detalle
from app.ingest.datos_abiertos import CacheZipsDA, sync_estados_datos_abiertos
from app.ingest.licitaciones import upsert_detalle
from app.models.enums import ESTADOS_TERMINALES, EstadoOportunidad
from app.models.tables import (
    CompraAgil,
    Licitacion,
    OportunidadMatch,
    OportunidadSeguida,
    SyncState,
)

_log = get_logger(__name__)

_ESTADOS_NO_TERMINALES = [
    e.value for e in EstadoOportunidad if e not in ESTADOS_TERMINALES and e != EstadoOportunidad.DESCONOCIDO
]


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
    ahora = ahora_utc()
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
        except (MPRateLimitError, QuotaExceededError, MPAuthError):
            # Error del canal: cortar. Lo ya comiteado queda; seguir el loop
            # solo gastaría cuota contra una API que nos rechaza (regla 3).
            session.rollback()
            _log.warning(
                "lifecycle: corte del canal tras lic=%d ca=%d — progreso parcial guardado",
                actualizadas_lic,
                actualizadas_ca,
            )
            raise
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
        except (MPRateLimitError, QuotaExceededError, MPAuthError):
            session.rollback()
            _log.warning(
                "lifecycle: corte del canal tras lic=%d ca=%d — progreso parcial guardado",
                actualizadas_lic,
                actualizadas_ca,
            )
            raise
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


# ---------------------------------------------------------------------------
# Estados vencidos (F-estados-vencidos)
# ---------------------------------------------------------------------------

_FUENTE_VENCIDOS = "estados_vencidos"
_DIAS_VENCIDA = 7


def _guardar_vencidos(session: Session, resultado: dict[str, int], *, ok: bool) -> None:
    state = session.get(SyncState, _FUENTE_VENCIDOS)
    if state is None:
        state = SyncState(fuente=_FUENTE_VENCIDOS)
        session.add(state)
    state.ultima_ejecucion = ahora_utc()
    if ok:
        state.ultimo_ok = state.ultima_ejecucion
    state.notas = (
        f"datos_abiertos={resultado['actualizadas_da']} consultadas_api={resultado['consultadas_api']} "
        f"cambiadas_api={resultado['cambiadas_api']} rezagadas={resultado['rezagadas']} "
        f"errores_api={resultado['errores_api']}"
        + ("" if ok else " (cortado por el canal)")
    )


def _rezagadas(session: Session, excluir: set[str]) -> list[str]:
    """Licitaciones con match, no terminales, cerradas hace más de 7 días.

    La menos refrescada primero (`actualizado_en`), y entre iguales la que cerró
    más tarde: una que sigue legítimamente `cerrada` esperando adjudicación queda
    al fondo tras consultarla, y el tope va rotando por todas en noches seguidas.
    Fuera las que datos abiertos dejó terminales en esta corrida.
    """
    tiene_match = exists().where(
        OportunidadMatch.fuente == "licitaciones",
        OportunidadMatch.codigo_oportunidad == Licitacion.codigo,
    )
    codigos = session.execute(
        select(Licitacion.codigo)
        .where(
            Licitacion.estado.not_in([e.value for e in ESTADOS_TERMINALES]),
            Licitacion.fecha_cierre < ahora_utc() - timedelta(days=_DIAS_VENCIDA),
            tiene_match,
        )
        .order_by(Licitacion.actualizado_en.asc(), Licitacion.fecha_cierre.desc())
    ).scalars()
    return [c for c in codigos if c not in excluir]


def refresh_estados_vencidos(
    session: Session,
    v1_client: MercadoPublicoV1Client,
    settings: Settings,
    zips: CacheZipsDA | None = None,
    now_fn: Callable[..., datetime] | None = None,
) -> dict[str, int]:
    """Pone al día licitaciones que cerraron y quedaron con su último estado.

    `sync_activas` solo ve lo que sigue en el listado de activas y
    `refresh_estados` mira −7/+3 días del cierre: lo que cerró antes y nadie
    sigue quedaba "publicada" para siempre. Dos pasos:

    1. Datos abiertos (cuota 0): todas las no terminales que aparezcan en lic-da.
    2. API, con tope ESTADOS_VENCIDOS_MAX_REQUESTS: las que tienen match y lic-da
       no dejó terminales. Solo dentro de 22:00–07:00 Chile (regla 5), validado acá y no
       en el cron. Ante 429/cuota/auth corta igual que `refresh_estados`
       (regla 3), dejando grabado lo avanzado.
    """
    da, resueltas_da = sync_estados_datos_abiertos(session, settings, zips)
    resultado = {
        "actualizadas_da": da["actualizadas"],
        "vistas_da": da["vistas"],
        "desconocidos_da": da["desconocidos"],
        "meses_da": da["meses_escaneados"],
        "meses_fallidos_da": da["meses_fallidos"],
        # Consultadas ≠ cambiadas: muchas siguen `cerrada` esperando adjudicación.
        "consultadas_api": 0,
        "cambiadas_api": 0,
        "errores_api": 0,
        "rezagadas": 0,
        "api_fuera_de_ventana": 0,
    }

    pendientes = _rezagadas(session, resueltas_da)
    if not en_ventana_nocturna(now_fn):
        _log.info("refresh_estados_vencidos: fuera de 22:00–07:00 Chile — sin API")
        resultado["api_fuera_de_ventana"] = 1
    else:
        tope = max(settings.estados_vencidos_max_requests, 0)
        for codigo in pendientes[:tope]:
            try:
                antes = session.get(Licitacion, codigo)
                estado_antes = antes.estado if antes is not None else None
                det = v1_client.licitacion_detalle(codigo)
                upsert_detalle(session, det, settings)
                session.commit()
                resultado["consultadas_api"] += 1
                despues = session.get(Licitacion, codigo)
                if despues is not None and despues.estado != estado_antes:
                    resultado["cambiadas_api"] += 1
            except (MPRateLimitError, QuotaExceededError, MPAuthError):
                session.rollback()
                resultado["rezagadas"] = len(pendientes) - resultado["consultadas_api"]
                _log.warning(
                    "refresh_estados_vencidos: corte del canal tras api=%d — progreso parcial guardado",
                    resultado["consultadas_api"],
                )
                _guardar_vencidos(session, resultado, ok=False)
                session.commit()
                raise
            except Exception as exc:
                _log.warning("refresh_estados_vencidos: error lic %s: %s", codigo, exc)
                session.rollback()
                resultado["errores_api"] += 1

    resultado["rezagadas"] = len(pendientes) - resultado["consultadas_api"]
    _guardar_vencidos(session, resultado, ok=True)
    session.commit()
    _log.info(
        "refresh_estados_vencidos: datos_abiertos=%d consultadas_api=%d cambiadas_api=%d "
        "rezagadas=%d errores_api=%d",
        resultado["actualizadas_da"],
        resultado["consultadas_api"],
        resultado["cambiadas_api"],
        resultado["rezagadas"],
        resultado["errores_api"],
    )
    return resultado
