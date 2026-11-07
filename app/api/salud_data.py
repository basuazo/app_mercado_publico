"""Datos de salud del sistema — compartidos entre /salud (HTML) y /api/salud (JSON).

IMPORTANTE: get_salud_data() NUNCA debe incluir MP_TICKET, SECRET_KEY ni JOBS_TOKEN.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Integer, cast, func, select, text
from sqlalchemy.orm import Session

from app.api.query import resumen_evaluacion_modelo
from app.core.retencion import tamano_bd, tamano_tabla
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE
from app.mercado.salud_pivote import salud_pivote
from app.mercado.territorio import regiones_activas
from app.models.tables import (
    CaAnexo,
    CaComentario,
    CaCotizacion,
    CaCotizacionProducto,
    CaEvento,
    CaLinea,
    CaRequisito,
    CaTrabajo,
    CompetenciaCompradorMes,
    CompetenciaMes,
    DemandaNombre,
    DemandaSerie,
    EmpresaProducto,
    EmpresaProductoMovimiento,
    EmpresaRequisito,
    EvaluacionCache,
    LlmCache,
    OcLineaCa,
    OfertaPropia,
    PlanCompraLinea,
    PrecioIndice,
    PrecioReferencia,
    Prediccion,
    ProductoAlias,
    ProductoCanonico,
    ProductoFormato,
    Pronostico,
    Proveedor,
    ProveedorProducto,
    Recomendacion,
    RubroVocabulario,
    SyncState,
    Venta,
    VentaCompra,
)

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

    # `anexos_*` (F-plat-anexos-2) no son requests de la API: descargas directas, límite del
    # endpoint externo, pausas y cortes; van en su propia sección.
    total_api_hoy = sum(
        f.requests_usadas_hoy
        for f in fuentes
        if f.fuente != "alerts_email" and not f.fuente.startswith("anexos_")
    )

    tam = tamano_bd(session)
    limite_bytes = 500 * 1024 * 1024
    tablas = _tablas_mercado_salud(session)

    errores = [
        {"fuente": f.fuente, "nota": f.notas}
        for f in fuentes
        if f.notas and not f.fuente.startswith("anexos_")
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
        "vocabulario_rubros": _vocabulario_salud(session),
        "tablas_mercado": tablas,
        # F-plat-7: sección «Pivote» (cuota por job, territorio, catálogo, LLM, demanda, sombra).
        "pivote": salud_pivote(session, settings, regiones_activas(session, settings), tablas, tam),
        # F-plat-4: metas del peldaño 2 en verde/rojo (no se aflojan: se reportan tal cual).
        "modelo_adjudicacion": resumen_evaluacion_modelo(session),
        "anexos": _anexos_salud(session, settings),
    }


def _anexos_salud(session: Session, settings: Settings) -> dict[str, Any]:
    """Anexos y lectura automática (F-plat-anexos-2 §2.6–§2.7): gasto del mes, anexos leídos por
    modelo y cuántos cayeron al respaldo, por fuente, y el estado de la descarga directa."""
    from datetime import UTC

    from app.core.tiempo import ahora_utc
    from app.ingest import anexos_directo as ad
    from app.models.tables import CaAnexo, LlmUso

    ahora = ahora_utc()
    local = ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE)
    inicio_mes = (
        local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
        .replace(tzinfo=None)
    )
    uso = session.get(LlmUso, local.strftime("%Y-%m"))
    por_modelo = [
        {"modelo": m or "(sin modelo)", "anexos": int(n), "respaldo": int(r or 0)}
        for m, n, r in session.execute(
            select(
                CaAnexo.extraccion_modelo,
                func.count(),
                func.sum(cast(CaAnexo.extraccion_respaldo, Integer)),
            )
            .where(CaAnexo.subido_en >= inicio_mes, CaAnexo.extraccion_modelo.isnot(None))
            .group_by(CaAnexo.extraccion_modelo)
            .order_by(func.count().desc())
        ).tuples()
    ]
    por_fuente = {
        f: int(n)
        for f, n in session.execute(
            select(CaAnexo.fuente, func.count())
            .where(CaAnexo.subido_en >= inicio_mes)
            .group_by(CaAnexo.fuente)
        ).tuples()
    }
    pausa = ad.pausa_hasta(session)
    # F-plat-anexos-auto: documentos de CA vigentes que el job todavía tiene que bajar.
    from app.mercado import adjuntos

    pendientes = adjuntos.cola(session, regiones_activas(session, settings), ahora)
    # La bandera y la clave viven en los Secrets de Actions, no en Render (la web no descarga):
    # el estado real es la última corrida del job.
    from app.models.tables import JobRun

    ultima = session.execute(
        select(JobRun).where(JobRun.job == "mercado-anexos").order_by(JobRun.id.desc()).limit(1)
    ).scalar_one_or_none()
    res_ultima = ultima.resultado_json if ultima is not None else None
    return {
        "gasto_mes_usd": float(uso.costo_usd) if uso else 0.0,
        "requests_mes": int(uso.requests) if uso else 0,
        "presupuesto_usd": settings.llm_presupuesto_mes_usd,
        "modelo": settings.llm_modelo_extraccion or None,
        "modelo_respaldo": settings.llm_modelo_respaldo or None,
        "por_modelo": por_modelo,
        "respaldo_total": sum(m["respaldo"] for m in por_modelo),
        "por_fuente": por_fuente,
        "directa": {
            "habilitada": settings.anexos_descarga_directa,
            "ultima_corrida": (
                {
                    "iniciado_en": ultima.iniciado_en.isoformat(),
                    "estado": ultima.estado,
                    "omitido": (res_ultima or {}).get("omitido"),
                    "cortado_por": (res_ultima or {}).get("cortado_por"),
                    "requests": (res_ultima or {}).get("requests"),
                    "extraidos": (res_ultima or {}).get("extraidos"),
                }
                if ultima is not None
                else None
            ),
            "descargas_hoy": ad.descargas_del_dia(session, ahora),
            "tope_dia": settings.anexos_descarga_max,
            "ca_por_listar": sum(1 for p in pendientes if p.es_listado),
            "archivos_en_cola": sum(1 for p in pendientes if not p.es_listado),
            "ca_pendientes": len({p.codigo for p in pendientes}),
            "cortes_7_dias": ad.cortes_recientes(session, ahora),
            "pausa_hasta": pausa.isoformat() if pausa is not None and pausa > ahora else None,
        },
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


def _vocabulario_salud(session: Session) -> dict[str, Any]:
    """F-ca-explorar: qué tan al día está el vocabulario por rubro que usa el
    explorador de Compras Ágiles para marcar las CA como "posibles"."""
    filas, familias, actualizado = session.execute(
        select(
            func.count(),
            func.count(func.distinct(RubroVocabulario.prefijo)),
            func.max(RubroVocabulario.actualizado_en),
        )
    ).one()
    return {
        "filas": int(filas),
        "familias": int(familias),
        "actualizado_en": actualizado.isoformat() if actualizado else None,
    }


def _tablas_mercado_salud(session: Session) -> list[dict[str, Any]]:
    """F-merc-2: filas y tamaño de las tablas del piloto de mercado (excepción a la regla 11:
    no se purgan, así que hay que vigilar que Neon, 0,5 GB, no se llene). F-plat-7: todas las del
    pivote; una fase que agrega tabla suma UNA línea acá (y a `PURGAS` si tiene retención)."""
    modelos = (
        ("oc_lineas_ca", OcLineaCa),
        ("precio_referencia", PrecioReferencia),
        ("precio_indice", PrecioIndice),
        ("ca_cotizaciones", CaCotizacion),
        ("ca_cotizacion_productos", CaCotizacionProducto),
        # F-plat-6: demanda, pronósticos y recomendaciones (derivadas salvo las recomendaciones).
        ("demanda_series", DemandaSerie),
        ("pronosticos", Pronostico),
        ("recomendaciones", Recomendacion),
        # F-plat-7: el resto de las tablas del pivote (catálogo, proveedores, mesa, equipo, LLM).
        ("competencia_mes", CompetenciaMes),
        ("competencia_comprador_mes", CompetenciaCompradorMes),
        ("demanda_nombres", DemandaNombre),
        ("ca_lineas", CaLinea),
        ("ca_anexos", CaAnexo),
        ("producto_canonico", ProductoCanonico),
        ("producto_formato", ProductoFormato),
        ("producto_alias", ProductoAlias),
        ("proveedores", Proveedor),
        ("proveedor_productos", ProveedorProducto),
        ("predicciones", Prediccion),
        ("ofertas_propias", OfertaPropia),
        ("evaluaciones_cache", EvaluacionCache),
        ("ca_trabajo", CaTrabajo),
        ("ca_eventos", CaEvento),
        ("ca_comentarios", CaComentario),
        ("empresa_productos", EmpresaProducto),
        # F-plat-seguimiento: checklist, ventas y compras (las ventas no se purgan).
        ("empresa_requisitos", EmpresaRequisito),
        ("ca_requisitos", CaRequisito),
        ("ventas", Venta),
        ("venta_compras", VentaCompra),
        ("empresa_producto_movimientos", EmpresaProductoMovimiento),
        ("llm_cache", LlmCache),
    )
    return [
        {
            "tabla": nombre,
            "filas": int(session.execute(select(func.count()).select_from(modelo)).scalar_one()),
            "tamano_bytes": tamano_tabla(session, nombre),
        }
        for nombre, modelo in modelos
    ]
