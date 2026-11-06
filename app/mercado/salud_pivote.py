"""Sección «Pivote» de /salud (F-plat-7 §1.1). Solo admin; solo base de datos: sin red.

Todo sale de tablas que ya existen. La cuota por job sale de `job_runs.resultado_json['requests']`
(el delta de `quota_log` que graba `_run_with_lock`, ver `orchestrator._con_requests`): la alerta
roja de R-p1 («la cuota es solo para Compra Ágil») se enciende si un job fuera de `mercado-*`
gastó ticket hoy. Nada de lo que devuelve incluye secretos. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.presentacion import nombre_region
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.models.tables import (
    CaAnexo,
    CaLinea,
    CompraAgil,
    Empresa,
    EvaluacionModelo,
    JobRun,
    LlmUso,
    Prediccion,
    ProductoAlias,
    SyncState,
    Usuario,
)

PIVOTE_MARCA = "mercado-"  # los jobs del pivote; el resto no debería gastar ticket (regla 24)
FUENTE_PAUSA = "api_pausa"  # misma fila que `QuotaTracker.pausar`
UMBRAL_BD_PCT = 70.0
LIMITE_BD_BYTES = 500 * 1024 * 1024
DIAS_CATALOGO = 30
# Claves de la última corrida de `mercado-ca` que se muestran (las demás no).
_CLAVES_MERCADO_CA = (
    "requests",
    "ca_listadas",
    "detalles_guardados",
    "detalles_fallidos",
    "detalles_pendientes",
    "cotizaciones",
    "anexos_bajados",
    "anexos_procesados",
    "cortado_por",
)


def inicio_dia_chile_utc(ahora: datetime) -> datetime:
    """Medianoche de Chile del día de `ahora` (UTC naive, como se guardan las fechas)."""
    local = ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE)
    medianoche = datetime.combine(local.date(), time.min, tzinfo=TZ_CHILE)
    return medianoche.astimezone(UTC).replace(tzinfo=None)


def _entero(v: Any) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def cuota_por_job(session: Session, ahora: datetime) -> dict[str, Any]:
    """Requests de hoy (día de Chile) por job, sumando las corridas de `job_runs`.

    `alerta_no_mercado`: jobs que no son `mercado-*` y gastaron ticket (R-p1). Un job viejo
    (`ca`, `match`, `nocturno`...) sale acá si alguien lo reactiva por error."""
    por_job: dict[str, int] = {}
    corridas = session.execute(
        select(JobRun.job, JobRun.resultado_json).where(
            JobRun.iniciado_en >= inicio_dia_chile_utc(ahora)
        )
    ).all()
    for job, resultado in corridas:
        if isinstance(resultado, dict):
            n = _entero(resultado.get("requests"))
            if n:
                por_job[job] = por_job.get(job, 0) + n
    otros = {j: n for j, n in por_job.items() if j not in ("mercado-ca", "mercado-backfill")}
    return {
        "mercado_ca": por_job.get("mercado-ca", 0),
        "mercado_backfill": por_job.get("mercado-backfill", 0),
        "otros": dict(sorted(otros.items())),
        "total": sum(por_job.values()),
        "alerta_no_mercado": sorted(j for j in por_job if not j.startswith(PIVOTE_MARCA)),
    }


def pausa_429(session: Session, ahora: datetime) -> dict[str, Any] | None:
    """Pausa de la API vigente (la que dejó un 429), en hora de Chile; None si no hay."""
    fila = session.get(SyncState, FUENTE_PAUSA)
    if fila is None or not fila.cursor:
        return None
    try:
        hasta = datetime.fromisoformat(fila.cursor)
    except ValueError:
        return None
    if hasta.tzinfo is not None:
        hasta = hasta.astimezone(UTC).replace(tzinfo=None)
    if hasta <= ahora:
        return None
    return {
        "hasta_chile": hasta.replace(tzinfo=UTC).astimezone(TZ_CHILE).strftime("%H:%M"),
        "minutos": max(1, int((hasta - ahora).total_seconds() // 60)),
        "causa": fila.notas,
    }


def territorio(session: Session) -> dict[str, Any]:
    """Regiones por empresa activa y última corrida buena de `mercado-ca`."""
    empresas = []
    union: set[int] = set()
    for nombre, regiones in session.execute(
        select(Empresa.nombre, Empresa.regiones_prioritarias)
        .where(Empresa.activa.is_(True))
        .order_by(Empresa.id)
    ):
        rs = sorted({int(r) for r in (regiones or []) if str(r).isdigit()})
        union.update(rs)
        empresas.append({"empresa": nombre, "regiones": [nombre_region(r) or str(r) for r in rs]})
    ultima = session.execute(
        select(JobRun)
        .where(JobRun.job == "mercado-ca", JobRun.estado == "ok")
        .order_by(JobRun.iniciado_en.desc())
        .limit(1)
    ).scalar_one_or_none()
    resumen = None
    if ultima is not None:
        res = ultima.resultado_json if isinstance(ultima.resultado_json, dict) else {}
        resumen = {
            "iniciado_en": ultima.iniciado_en.isoformat(),
            **{k: res[k] for k in _CLAVES_MERCADO_CA if k in res},
        }
    return {
        "empresas": empresas,
        "regiones": [nombre_region(r) or str(r) for r in sorted(union)],
        "ultima_mercado_ca": resumen,
    }


def tamano_pivote(tablas: list[dict[str, Any]], tam_bd: int | None) -> dict[str, Any]:
    """Suma de las tablas del pivote y alerta al 70 % de 0,5 GB (regla 11), sobre las tablas y
    sobre la base entera (que incluye `licitaciones`, `job_runs`, etc.)."""
    umbral = LIMITE_BD_BYTES * UMBRAL_BD_PCT / 100
    suma = sum(t["tamano_bytes"] or 0 for t in tablas)
    return {
        "tablas_bytes": suma,
        "umbral_bytes": int(umbral),
        "alerta_tablas": suma >= umbral,
        "alerta_bd": tam_bd is not None and tam_bd >= umbral,
    }


def catalogo(session: Session, regiones: list[int], ahora: datetime) -> dict[str, Any]:
    """CA por bandera de detalle (últimos 30 días), líneas pendientes, alias y correcciones."""
    desde = ahora - timedelta(days=DIAS_CATALOGO)
    por_detalle = dict.fromkeys(("sin_detalle", "incompleto", "completo"), 0)
    for estado, n in session.execute(
        select(CompraAgil.detalle_lineas, func.count())
        .where(CompraAgil.region.in_(regiones), CompraAgil.fecha_publicacion >= desde)
        .group_by(CompraAgil.detalle_lineas)
    ):
        por_detalle[estado] = int(n)
    pendientes = session.execute(
        select(func.count())
        .select_from(CaLinea)
        .where(CaLinea.estado.in_(("pendiente", "dudosa")))
    ).scalar_one()
    alias = session.execute(
        select(func.count()).select_from(ProductoAlias).where(ProductoAlias.fuente == "humano")
    ).scalar_one()
    correcciones = [
        {"usuario": email, "correcciones": int(n)}
        for email, n in session.execute(
            select(Usuario.email, func.count())
            .select_from(CaLinea)
            .join(Usuario, Usuario.id == CaLinea.revisado_por)
            .where(CaLinea.revisado_en >= desde)
            .group_by(Usuario.email)
            .order_by(func.count().desc(), Usuario.email)
        )
    ]
    return {
        "dias": DIAS_CATALOGO,
        "ca": por_detalle,
        "lineas_pendientes": int(pendientes),
        "alias_aprendidos": int(alias),
        "correcciones": correcciones,
    }


def llm(session: Session, settings: Settings, ahora: datetime) -> dict[str, Any]:
    """Gasto del mes en OpenRouter vs. presupuesto, requests y % de extracciones desde caché."""
    mes = ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE).strftime("%Y-%m")
    uso = session.get(LlmUso, mes)
    hechas = session.execute(
        select(CaAnexo.extraccion_costo_usd).where(
            CaAnexo.extraccion_estado == "hecha",
            CaAnexo.subido_en >= _inicio_mes_utc(mes),
        )
    ).scalars()
    costos = list(hechas)
    desde_cache = sum(1 for c in costos if c is not None and c == 0.0)
    gasto = float(uso.costo_usd) if uso else 0.0
    return {
        "mes": mes,
        "gasto_usd": round(gasto, 4),
        "presupuesto_usd": settings.llm_presupuesto_mes_usd,
        "pct_presupuesto": round(gasto / settings.llm_presupuesto_mes_usd * 100, 1)
        if settings.llm_presupuesto_mes_usd
        else None,
        "requests": int(uso.requests) if uso else 0,
        "estimado": bool(uso.estimado) if uso else False,
        "extracciones": len(costos),
        "pct_cache": round(desde_cache / len(costos) * 100, 1) if costos else None,
        "modelo": settings.llm_modelo_extraccion or None,
    }


def _inicio_mes_utc(mes: str) -> datetime:
    a, m = (int(x) for x in mes.split("-"))
    return datetime(a, m, 1, tzinfo=TZ_CHILE).astimezone(UTC).replace(tzinfo=None)


def demanda(session: Session) -> dict[str, Any] | None:
    """Última evaluación (backtest) del pronóstico de demanda: sus métricas del último mes."""
    mes = session.execute(
        select(func.max(EvaluacionModelo.mes)).where(
            EvaluacionModelo.modelo == "demanda", EvaluacionModelo.metrica != "sin_datos_suficientes"
        )
    ).scalar_one_or_none()
    if mes is None:
        return None
    filas = session.execute(
        select(EvaluacionModelo.metrica, EvaluacionModelo.valor, EvaluacionModelo.n)
        .where(EvaluacionModelo.modelo == "demanda", EvaluacionModelo.mes == mes)
        .order_by(EvaluacionModelo.metrica)
    ).all()
    return {
        "mes": mes,
        "metricas": [{"metrica": m, "valor": v, "n": int(n)} for m, v, n in filas],
    }


def sombra(session: Session) -> list[dict[str, Any]]:
    """Predicciones abiertas/cerradas y acierto acumulado del intervalo 80 %, por empresa."""
    por: dict[int, dict[str, int]] = {}
    for empresa_id, estado, en_int, n in session.execute(
        select(
            Prediccion.empresa_id,
            Prediccion.estado,
            Prediccion.resultado_en_intervalo,
            func.count(),
        ).group_by(Prediccion.empresa_id, Prediccion.estado, Prediccion.resultado_en_intervalo)
    ):
        d = por.setdefault(
            empresa_id,
            {"abiertas": 0, "cerradas": 0, "sin_adjudicacion": 0, "con_intervalo": 0, "aciertos": 0},
        )
        n = int(n)
        if estado == "abierta":
            d["abiertas"] += n
        elif estado == "cerrada":
            d["cerradas"] += n
            if en_int is not None:
                d["con_intervalo"] += n
                d["aciertos"] += n if en_int else 0
        else:
            d["sin_adjudicacion"] += n
    nombres: dict[int, str] = {
        i: n for i, n in session.execute(select(Empresa.id, Empresa.nombre)).all()
    }
    salida = []
    for empresa_id, d in sorted(por.items()):
        salida.append(
            {
                "empresa": nombres.get(empresa_id, f"#{empresa_id}"),
                **d,
                "pct_acierto": round(d["aciertos"] / d["con_intervalo"] * 100, 1)
                if d["con_intervalo"]
                else None,
            }
        )
    return salida


def salud_pivote(
    session: Session,
    settings: Settings,
    regiones: list[int],
    tablas: list[dict[str, Any]],
    tam_bd: int | None,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """La sección completa. `tablas` es la lista de `_tablas_mercado_salud` (no se recalcula)."""
    ahora = ahora or ahora_utc()
    return {
        "cuota": cuota_por_job(session, ahora),
        "pausa": pausa_429(session, ahora),
        "territorio": territorio(session),
        "tamano": tamano_pivote(tablas, tam_bd),
        "catalogo": catalogo(session, regiones, ahora),
        "llm": llm(session, settings, ahora),
        "demanda": demanda(session),
        "sombra": sombra(session),
    }
