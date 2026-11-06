"""Modo sombra (F-plat-5): evaluaciones en caché, predicciones inmutables y su cierre.

Job `mercado-mesa` (solo base de datos: sin ticket, sin cuota, sin red). Por empresa activa y CA
vigente de sus regiones, por lotes (regla 12):
(a) reescribe `evaluaciones_cache` con `adjudicacion.evaluar_ca` (la mesa solo lee esa tabla);
(b) si la cobertura de costos es ≥ 0,5, hay modelo y faltan ≥ 2 h para el cierre, crea la
    predicción (una por CA y empresa, `ON CONFLICT DO NOTHING`: re-ejecutar no duplica ni cambia);
(c) cierra las predicciones `abierta` cuyo cierre pasó y cuyas ofertas ya están leídas: precio
    ganador (la seleccionada con `elegir_por_rut`; varias → suma y `detalle.multi_ganador`),
    acierto del intervalo 80 % y, si la CA estuvo `enviada`, el resultado propio de F-equipo
    (`equipo.resultado_de`). Desierta, cancelada o sin seleccionada al terminar →
    `sin_adjudicacion`;
(d) con oferta propia ganada: `precio_ganador_real` y `margen_realizado` (funciones de F-plat-4).

Una CA cerrada cuya selección no llega en `DIAS_ESPERA` días queda `sin_adjudicacion` (con su
motivo en `detalle`): sin ese tope, una predicción podría quedar abierta para siempre.
Ownership (regla 26): todo se filtra por `empresa_id`. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session, defer

from app.core.logging import get_logger
from app.core.tiempo import ahora_utc
from app.mercado import adjudicacion as adj
from app.mercado import equipo as eq
from app.mercado import oferta as of
from app.mercado.comprador import rut_competidor
from app.mercado.eleccion import elegir_por_rut
from app.mercado.mesa_dia import condiciones_vigente
from app.models.enums import EstadoOportunidad
from app.models.tables import (
    CaCotizacion,
    CaTrabajo,
    CompraAgil,
    Empresa,
    EvaluacionCache,
    OfertaPropia,
    Prediccion,
)

_log = get_logger(__name__)

ABIERTA = "abierta"
CERRADA = "cerrada"
SIN_ADJUDICACION = "sin_adjudicacion"
COBERTURA_MIN = 0.5
HORAS_MIN_CIERRE = 2
DIAS_ESPERA = 30
LOTE_MESA = 50
SEMANAS_RESULTADOS = 8
DIAS_AVISO_SIN_RESULTADO = 14  # F-plat-7 §1.3
MAX_AVISO_SIN_RESULTADO = 20
_SIN_ADJUDICACION = (
    EstadoOportunidad.DESIERTA.value,
    EstadoOportunidad.CANCELADA.value,
    EstadoOportunidad.REVOCADA.value,
)
_TERMINALES = (*_SIN_ADJUDICACION, EstadoOportunidad.ADJUDICADA.value)


# ---------------------------------------------------------------------------
# (a) y (b): caché y predicción
# ---------------------------------------------------------------------------


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(float(v), nd)


def datos_cache(ev: adj.EvaluacionCA) -> dict[str, Any]:
    """Lo que la tarjeta de la mesa necesita (sin líneas: la ficha evalúa en vivo)."""
    oks = [e for e in ev.lineas if e.estado == "ok"]
    return {
        "precio_sugerido": _r(ev.precio_sugerido),
        "intervalo_80": [_r(ev.intervalo_80[0]), _r(ev.intervalo_80[1])]
        if ev.intervalo_80
        else None,
        "costo_productos": _r(ev.costo_productos),
        "logistica": _r(ev.logistica.costo) if ev.logistica else None,
        "tarifa": ev.logistica.tarifa if ev.logistica else None,
        "costo_financiero": _r(ev.costo_financiero),
        "costo_total": _r(ev.costo_total),
        "margen_esperado": _r(ev.margen_esperado),
        "margen_pct": _r(ev.margen_pct, 4),
        "p_ganar": {k: _r(v, 4) for k, v in ev.p_ganar.items()},
        "curva": [{"nombre": p.nombre, "precio": _r(p.precio), "p": _r(p.p, 4)} for p in ev.curva],
        "cobertura": _r(ev.cobertura, 4),
        "completa": ev.completa,
        "modelo": ev.modelo,
        "avisos": list(ev.avisos),
        "n_lineas": len(ev.lineas),
        "n_ok": len(oks),
        "n_sin_costo": sum(1 for e in oks if e.costo is None),
        # F-plat-lote §1.4: el costo más viejo entre las líneas que solo tienen precio vencido.
        "costo_vencido_dias": max(
            (e.costo.dias or 0 for e in oks if e.costo is not None and e.costo.vencido),
            default=None,
        ),
    }


def detalle_prediccion(ev: adj.EvaluacionCA) -> dict[str, Any]:
    """Por línea, «así se vio al predecir» (congelado)."""
    return {
        "lineas": [
            {
                "n": e.n,
                "producto_id": e.producto_id,
                "producto": e.producto,
                "unidad_base": e.unidad_base,
                "cantidad_base": e.cantidad_base,
                "ambito": e.referencia.ambito if e.referencia else None,
                "n_ref": e.referencia.n if e.referencia else None,
                "mediana_ganador": _r(e.referencia.mediana_ganador) if e.referencia else None,
                "intervalo_80": [_r(e.intervalo_80[0]), _r(e.intervalo_80[1])]
                if e.intervalo_80
                else None,
                "costo_por_base": _r(e.costo.precio_por_unidad_base) if e.costo else None,
                "proveedor": e.costo.proveedor if e.costo else None,
                "costo_total": _r(e.costo_total),
            }  # fmt: skip
            for e in ev.lineas
        ],
        "costo_total": _r(ev.costo_total),
        "logistica": _r(ev.logistica.costo) if ev.logistica else None,
        "curva": [{"nombre": p.nombre, "precio": _r(p.precio), "p": _r(p.p, 4)} for p in ev.curva],
        "margen_pct": _r(ev.margen_pct, 4),
        "mes_referencia": ev.mes_referencia,
    }


def debe_predecir(
    ev: adj.EvaluacionCA, cierre: datetime | None, ahora: datetime, activo: adj.ModeloActivo | None
) -> bool:
    return (
        activo is not None
        and ev.precio_sugerido is not None
        and ev.cobertura is not None
        and ev.cobertura >= COBERTURA_MIN
        and cierre is not None
        and cierre - ahora >= timedelta(hours=HORAS_MIN_CIERRE)
    )


def _insert(session: Session, tabla: Any) -> Any:
    dialecto = session.get_bind().dialect.name
    return postgresql.insert(tabla) if dialecto == "postgresql" else sqlite.insert(tabla)


def insertar_prediccion(
    session: Session,
    ev: adj.EvaluacionCA,
    empresa_id: int,
    cierre: datetime | None,
    activo: adj.ModeloActivo,
    ahora: datetime,
) -> bool:
    """Inserción idempotente: si ya hay una predicción de la CA para la empresa, nada (ni la
    cambia). True si la creó."""
    valores = {
        "ca_codigo": ev.ca_codigo, "empresa_id": empresa_id, "creada_en": ahora,
        "cierre_ca": cierre, "modelo_id": activo.fila_id, "modelo_tipo": activo.modelo.tipo,
        "precio_sugerido": float(ev.precio_sugerido or 0.0),
        "intervalo_80_min": ev.intervalo_80[0] if ev.intervalo_80 else None,
        "intervalo_80_max": ev.intervalo_80[1] if ev.intervalo_80 else None,
        "p_ganar_sugerido": ev.p_ganar.get("sugerido"), "margen_esperado": ev.margen_esperado,
        "cobertura": ev.cobertura, "detalle": detalle_prediccion(ev), "estado": ABIERTA,
    }  # fmt: skip
    r = session.execute(
        _insert(session, Prediccion.__table__)
        .values(**valores)
        .on_conflict_do_nothing(index_elements=["ca_codigo", "empresa_id"])
        # RETURNING y no `rowcount`: tras ON CONFLICT DO NOTHING el rowcount de Postgres no es
        # confiable. Sin fila devuelta = ya existía (SQLite ≥ 3.35 también soporta RETURNING).
        .returning(Prediccion.__table__.c.id)
    )
    return r.first() is not None


def _guardar_cache(
    session: Session, ev: adj.EvaluacionCA, empresa_id: int, modelo_id: int | None, ahora: datetime
) -> None:
    fila = session.get(EvaluacionCache, (ev.ca_codigo, empresa_id))
    if fila is None:
        session.add(
            EvaluacionCache(
                ca_codigo=ev.ca_codigo,
                empresa_id=empresa_id,
                evaluada_en=ahora,
                modelo_id=modelo_id,
                datos=datos_cache(ev),
            )  # fmt: skip
        )
    else:
        fila.evaluada_en, fila.modelo_id, fila.datos = ahora, modelo_id, datos_cache(ev)


def _lotes(xs: list[str], n: int) -> Iterable[list[str]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def refrescar_empresa(
    session: Session,
    empresa_id: int,
    regiones: list[int],
    ahora: datetime,
    *,
    activo: adj.ModeloActivo | None,
    tasa: float = adj.COSTO_FINANCIERO_DEFAULT,
) -> dict[str, int]:
    """(a) y (b) para UNA empresa. Confirma por lote y suelta los objetos (regla 12)."""
    cuenta = {"evaluadas": 0, "predicciones_nuevas": 0, "cache_borradas": 0}
    vigentes = [
        (c, cierre)
        for c, cierre in session.execute(
            select(CompraAgil.codigo, CompraAgil.fecha_cierre)
            .where(*condiciones_vigente(regiones, ahora))
            .order_by(CompraAgil.codigo)
        )
    ]
    cierres = dict(vigentes)
    codigos = [c for c, _ in vigentes]
    for lote in _lotes(codigos, LOTE_MESA):
        con_pred = set(
            session.execute(
                select(Prediccion.ca_codigo).where(
                    Prediccion.empresa_id == empresa_id, Prediccion.ca_codigo.in_(lote)
                )
            ).scalars()
        )
        for codigo in lote:
            ev = adj.evaluar_ca(
                session, codigo, empresa_id, costo_financiero_mensual=tasa, activo=activo
            )
            if ev is None:
                continue
            _guardar_cache(session, ev, empresa_id, activo.fila_id if activo else None, ahora)
            cuenta["evaluadas"] += 1
            if (
                codigo not in con_pred
                and activo is not None
                and debe_predecir(ev, cierres.get(codigo), ahora, activo)
                and insertar_prediccion(session, ev, empresa_id, cierres.get(codigo), activo, ahora)
            ):
                cuenta["predicciones_nuevas"] += 1
        session.commit()
        session.expunge_all()
    # La caché es solo de vigentes: lo que ya cerró se borra (regla 11).
    viejas = select(EvaluacionCache.ca_codigo).where(EvaluacionCache.empresa_id == empresa_id)
    sobran = [c for c in session.execute(viejas).scalars() if c not in cierres]
    for lote in _lotes(sobran, 500):
        r = session.execute(
            delete(EvaluacionCache).where(
                EvaluacionCache.empresa_id == empresa_id, EvaluacionCache.ca_codigo.in_(lote)
            )
        )
        cuenta["cache_borradas"] += int(r.rowcount or 0)  # type: ignore[attr-defined]
    session.commit()
    return cuenta


# ---------------------------------------------------------------------------
# (c) y (d): cierre
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Desenlace:
    estado: str  # cerrada | sin_adjudicacion
    precio_ganador: float | None = None
    n_ganadores: int = 0
    motivo: str | None = None


def _elegidas(session: Session, codigo: str) -> dict[str, tuple[str, bool | None, float | None]]:
    """Una oferta por RUT (regla F-merc-1c, la misma de `equipo.resultado_de`)."""
    ofertas = [
        (rut_competidor(r)[0], sel, float(v) if v is not None else None)
        for r, sel, v in session.execute(
            select(
                CaCotizacion.rut_proveedor, CaCotizacion.seleccionado, CaCotizacion.valor_neto
            ).where(CaCotizacion.ca_codigo == codigo)
        )
    ]
    return elegir_por_rut(
        [o for o in ofertas if o[0]], lambda o: o[0], lambda o: o[1], lambda o: o[2]
    )


def desenlace(session: Session, ca: CompraAgil, ahora: datetime) -> Desenlace | None:
    """Cómo terminó la CA, o None si todavía no se puede decir."""
    if ca.estado in _SIN_ADJUDICACION:
        return Desenlace(SIN_ADJUDICACION, motivo=ca.estado)
    if ca.fecha_cierre is None or ca.fecha_cierre > ahora:
        return None
    vencida = ahora - ca.fecha_cierre > timedelta(days=DIAS_ESPERA)
    if ca.cotizaciones_obtenidas_en is None:
        return Desenlace(SIN_ADJUDICACION, motivo="sin_ofertas_leidas") if vencida else None
    ganadoras = [o for o in _elegidas(session, ca.codigo).values() if o[1] is True]
    if ganadoras:
        total = sum(o[2] or 0.0 for o in ganadoras)
        return Desenlace(CERRADA, total, len(ganadoras))
    if ca.estado in _TERMINALES or vencida:
        return Desenlace(SIN_ADJUDICACION, motivo="sin_seleccionada")
    return None


def _resultado_propio(
    session: Session, empresa_id: int, ca: CompraAgil, ahora: datetime, ruts: dict[int, str | None]
) -> str | None:
    t = eq.trabajo_de(session, empresa_id, ca.codigo)
    if t is None or t.estado != eq.ENVIADA:
        return None
    if empresa_id not in ruts:
        ruts[empresa_id] = eq.rut_empresa(session.get(Empresa, empresa_id))
    rut = ruts[empresa_id]
    return eq.resultado_de(session, rut, ca, ahora) if rut else None


def cerrar_predicciones(session: Session, ahora: datetime) -> dict[str, int]:
    """(c) para todas las empresas. Solo llena `estado`, `resultado_*` y `cerrada_en`.

    Además completa `resultado_propio` de las ya cerradas en los últimos `DIAS_ESPERA` días que
    lo tengan vacío: el equipo suele marcar «Enviada» después del cierre (sube la oferta a última
    hora y la registra luego), y sin esto /mesa/resultados perdería esa oferta para siempre."""
    cuenta = {"cerradas": 0, "sin_adjudicacion": 0, "resultado_propio_tardio": 0}
    ruts: dict[int, str | None] = {}
    filas = session.execute(
        select(Prediccion, CompraAgil)
        .join(CompraAgil, CompraAgil.codigo == Prediccion.ca_codigo)
        .where(Prediccion.estado == ABIERTA, CompraAgil.fecha_cierre <= ahora)
        .options(defer(CompraAgil.raw_json))  # regla 12: nunca raw_json en el job
        .order_by(Prediccion.id)
    ).all()
    for pred, ca in filas:
        d = desenlace(session, ca, ahora)
        if d is None:
            continue
        pred.estado = d.estado
        pred.cerrada_en = ahora
        if d.estado == CERRADA:
            pred.resultado_precio_ganador = d.precio_ganador
            if pred.intervalo_80_min is not None and pred.intervalo_80_max is not None:
                pred.resultado_en_intervalo = (
                    pred.intervalo_80_min <= (d.precio_ganador or 0.0) <= pred.intervalo_80_max
                )
            if d.n_ganadores > 1:
                pred.detalle = {**(pred.detalle or {}), "multi_ganador": d.n_ganadores}
            cuenta["cerradas"] += 1
        else:
            pred.detalle = {**(pred.detalle or {}), "motivo_cierre": d.motivo}
            cuenta["sin_adjudicacion"] += 1
        pred.resultado_propio = _resultado_propio(session, pred.empresa_id, ca, ahora, ruts)
    session.flush()
    tardias = session.execute(
        select(Prediccion, CompraAgil)
        .join(CompraAgil, CompraAgil.codigo == Prediccion.ca_codigo)
        .where(
            Prediccion.estado != ABIERTA,
            Prediccion.resultado_propio.is_(None),
            Prediccion.cerrada_en >= ahora - timedelta(days=DIAS_ESPERA),
            Prediccion.cerrada_en < ahora,  # las recién cerradas ya se calcularon arriba
        )
        .options(defer(CompraAgil.raw_json))
        .order_by(Prediccion.id)
    ).all()
    for pred, ca in tardias:
        res = _resultado_propio(session, pred.empresa_id, ca, ahora, ruts)
        if res is not None:
            pred.resultado_propio = res
            cuenta["resultado_propio_tardio"] += 1
    session.commit()
    return cuenta


def cerrar_ofertas(
    session: Session, ahora: datetime, tasa: float = adj.COSTO_FINANCIERO_DEFAULT
) -> dict[str, int]:
    """(d): ofertas propias ganadas → precio real y margen realizado (haya o no predicción).

    Solo CA cerradas hace ≤ `DIAS_ESPERA` días: una oferta que no ganó no se vuelve a revisar
    para siempre cada 15 min."""
    cuenta = {"ofertas_ganadas": 0}
    filas = session.execute(
        select(OfertaPropia, CompraAgil)
        .join(CompraAgil, CompraAgil.codigo == OfertaPropia.ca_codigo)
        .where(
            OfertaPropia.precio_ganador_real.is_(None),
            CompraAgil.cotizaciones_obtenidas_en.is_not(None),
            CompraAgil.fecha_cierre <= ahora,
            CompraAgil.fecha_cierre >= ahora - timedelta(days=DIAS_ESPERA),
        )
        .options(defer(CompraAgil.raw_json))  # regla 12
    ).all()
    for o, ca in filas:
        rut = eq.rut_empresa(session.get(Empresa, o.empresa_id))
        if not rut or eq.resultado_de(session, rut, ca, ahora) != eq.GANADA:
            continue
        nuestra = _elegidas(session, ca.codigo).get(rut)
        precio = nuestra[2] if nuestra and nuestra[2] is not None else o.total_neto
        o.precio_ganador_real = precio
        # F-plat-seguimiento: si la venta ya tiene compras registradas, el margen es el REAL
        # (ingreso − costo real) y esta estimación no lo pisa.
        from app.mercado import ventas

        if not ventas.tiene_costo_real(session, o.empresa_id, ca.codigo):
            o.margen_realizado = of.margen_realizado(session, o.empresa_id, ca, o, precio, tasa)
        cuenta["ofertas_ganadas"] += 1
    session.commit()
    return cuenta


def run_mesa(
    session: Session, *, tasa: float = adj.COSTO_FINANCIERO_DEFAULT, ahora: datetime | None = None
) -> dict[str, Any]:
    """Job `mercado-mesa`. Idempotente: re-ejecutar no duplica predicciones ni las cambia."""
    inicio = time.monotonic()
    ahora = ahora or ahora_utc()
    activo = adj.cargar_modelo_activo(session)
    total: dict[str, Any] = {"empresas": 0, "evaluadas": 0, "predicciones_nuevas": 0,
                             "cache_borradas": 0}  # fmt: skip
    empresas = [
        (e.id, [int(r) for r in (e.regiones_prioritarias or []) if str(r).isdigit()])
        for e in session.execute(select(Empresa).where(Empresa.activa.is_(True))).scalars()
    ]
    for empresa_id, regiones in empresas:
        if not regiones:
            continue
        total["empresas"] += 1
        for k, v in refrescar_empresa(
            session, empresa_id, regiones, ahora, activo=activo, tasa=tasa
        ).items():
            total[k] += v
    total.update(cerrar_predicciones(session, ahora))
    total.update(cerrar_ofertas(session, ahora, tasa))
    total["modelo"] = activo.modelo.tipo if activo else None
    total["duracion_s"] = round(time.monotonic() - inicio, 1)
    if activo is None:
        total["avisos"] = ["Sin modelo de adjudicación activo: no se crean predicciones"]
    _log.info("mercado-mesa: %s", total)
    return total


# ---------------------------------------------------------------------------
# /mesa/resultados (el juez del peldaño 2)
# ---------------------------------------------------------------------------


def semana_de(momento: datetime | None) -> str:
    if momento is None:
        return ""
    a, s, _ = momento.isocalendar()
    return f"{a}-S{s:02d}"


def resultados(
    session: Session, empresa_id: int, ahora: datetime, semana: str = ""
) -> dict[str, Any]:
    """Predicciones cerradas de la empresa (últimas 8 semanas por cierre de la CA)."""
    desde = ahora - timedelta(weeks=SEMANAS_RESULTADOS)
    filas = session.execute(
        select(Prediccion, CompraAgil.nombre, OfertaPropia)
        .join(CompraAgil, CompraAgil.codigo == Prediccion.ca_codigo)
        .outerjoin(
            OfertaPropia,
            (OfertaPropia.ca_codigo == Prediccion.ca_codigo)
            & (OfertaPropia.empresa_id == Prediccion.empresa_id),
        )
        .where(
            Prediccion.empresa_id == empresa_id,
            Prediccion.estado != ABIERTA,
            Prediccion.cierre_ca >= desde,
        )
        .order_by(Prediccion.cierre_ca.desc(), Prediccion.id.desc())
    ).all()
    semanas = sorted({semana_de(p.cierre_ca) for p, _, _ in filas}, reverse=True)
    if semana:
        filas = [f for f in filas if semana_de(f[0].cierre_ca) == semana]
    items: list[dict[str, Any]] = []
    for p, nombre, o in filas:
        error = None
        if p.estado == CERRADA and p.resultado_precio_ganador:
            error = (p.precio_sugerido - p.resultado_precio_ganador) / p.resultado_precio_ganador
        items.append({
            "codigo": p.ca_codigo, "nombre": nombre, "cierre": p.cierre_ca, "estado": p.estado,
            "semana": semana_de(p.cierre_ca), "predicho": p.precio_sugerido,
            "intervalo": (p.intervalo_80_min, p.intervalo_80_max)
            if p.intervalo_80_min is not None else None,
            "ganador": p.resultado_precio_ganador, "en_intervalo": p.resultado_en_intervalo,
            "error": error, "p_ganar": p.p_ganar_sugerido, "modelo": p.modelo_tipo,
            "multi_ganador": (p.detalle or {}).get("multi_ganador"),
            "resultado_propio": p.resultado_propio,
            "oferta_total": o.total_neto if o else None,
            "margen_realizado": o.margen_realizado if o else None,
        })  # fmt: skip
    cerradas = [i for i in items if i["estado"] == CERRADA]
    con_int = [i for i in cerradas if i["en_intervalo"] is not None]
    errores = [abs(i["error"]) for i in cerradas if i["error"] is not None]
    ofertamos = [
        (i["p_ganar"], i["resultado_propio"] == eq.GANADA)
        for i in items
        if i["resultado_propio"] in (eq.GANADA, eq.PERDIDA) and i["p_ganar"] is not None
    ]
    brier, n_brier = adj.brier(ofertamos)
    con_oferta = [i for i in items if i["oferta_total"] is not None]
    margenes = [i["margen_realizado"] for i in items if i["margen_realizado"] is not None]
    return {
        "filas": items,
        "semanas": semanas,
        "semana": semana,
        "n": len(items),
        "n_cerradas": len(cerradas),
        "n_sin_adjudicacion": len(items) - len(cerradas),
        "n_intervalo": len(con_int),
        "aciertos_intervalo": sum(1 for i in con_int if i["en_intervalo"]),
        "pct_intervalo": (sum(1 for i in con_int if i["en_intervalo"]) / len(con_int))
        if con_int else None,
        "n_error": len(errores),
        "error_mediano": statistics.median(errores) if errores else None,
        "brier": brier,
        "n_brier": n_brier,
        "n_ofertas": len(con_oferta),
        "n_ganadas": sum(1 for i in items if i["resultado_propio"] == eq.GANADA),
        "n_margen": len(margenes),
        "margen_realizado": sum(margenes) if margenes else None,
        "desde": desde,
    }  # fmt: skip


def ofertas_sin_resultado(
    session: Session, empresa_id: int, ahora: datetime
) -> dict[str, Any]:
    """F-plat-7 §1.3: CA que la empresa marcó `enviada` y cerraron hace más de 14 días sin que el
    sistema haya podido decir si ganó (`ca_trabajo.resultado` vacío: la selección no llega o las
    ofertas no se leyeron). `/mesa/resultados` las avisa para que el usuario revise el portal y lo
    registre a mano. Solo de la empresa (regla 26). Devuelve `{"total", "filas"}`; las filas, las
    más recientes primero, topadas a `MAX_AVISO_SIN_RESULTADO`."""
    corte = ahora - timedelta(days=DIAS_AVISO_SIN_RESULTADO)
    cond = (
        CaTrabajo.empresa_id == empresa_id,
        CaTrabajo.estado == eq.ENVIADA,
        CaTrabajo.resultado.is_(None),
        CompraAgil.fecha_cierre < corte,
    )
    total = session.execute(
        select(func.count())
        .select_from(CaTrabajo)
        .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
        .where(*cond)
    ).scalar_one()
    filas = session.execute(
        select(CompraAgil.codigo, CompraAgil.nombre, CompraAgil.fecha_cierre, OfertaPropia.total_neto)
        .select_from(CaTrabajo)
        .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
        .outerjoin(
            OfertaPropia,
            (OfertaPropia.ca_codigo == CaTrabajo.ca_codigo)
            & (OfertaPropia.empresa_id == CaTrabajo.empresa_id),
        )
        .where(*cond)
        .order_by(CompraAgil.fecha_cierre.desc(), CompraAgil.codigo)
        .limit(MAX_AVISO_SIN_RESULTADO)
    ).all()
    return {
        "total": int(total),
        "filas": [
            {"codigo": c, "nombre": n, "cierre": f, "oferta_total": t, "dias": (ahora - f).days}
            for c, n, f, t in filas
        ],
    }
