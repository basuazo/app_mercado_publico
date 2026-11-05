"""Evaluación walk-forward del índice y del modelo de adjudicación, y job `mercado-indices` (F-plat-4).

Para cada mes M evaluado: se entrena SOLO con CA cerradas antes de M (18 meses) y el índice de
los 3 meses anteriores a M; se predice lo cerrado en M y se compara con lo que pasó (juez externo:
las adjudicaciones reales, nunca el propio modelo). Un mes sin datos suficientes se informa como
tal, no como fallo (el backfill aún llena la historia).

Metas (peldaño 2, `16-pivote` §2): intervalo 80 % acierta ≥ 70 %; Brier ≤ 0,20; ganables ≥ 10 %.
Si no se cumplen se reporta tal cual (`avisos` → `::warning::` y `/salud`); no se afloja la meta.

Sin API ni red. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.core.tiempo import ahora_utc
from app.mercado.adjudicacion import (
    MARGEN_GANABLE,
    MESES_ENTRENAMIENTO,
    P_GANABLE,
    CaDataset,
    ContextoOferta,
    CostosEmpresa,
    Modelo,
    ModeloBase,
    ModeloLogistico,
    TablaN,
    brier,
    cargar_dataset,
    cargar_tarifas,
    curva,
    entrenar_base,
    entrenar_logistico,
    logistica,
    precios_de_lineas,
    tabla_n,
)
from app.mercado.indice import (
    ConteoUnion,
    IndiceMemoria,
    inferir_comunas,
    intervalo_80,
    lotes_cas_del_mes,
    mes_actual,
    meses_con_cotizaciones,
    meses_entre,
    observaciones_de_cas,
    rango_mes_utc,
    recalcular_indice,
    sumar_meses,
)
from app.models.tables import Empresa, EvaluacionModelo, ModeloAdjudicacion, ProductoCanonico

_log = logging.getLogger(__name__)

MESES_EVALUADOS = 6
MIN_CA_MES = 50  # CA cerradas con ganador y ≥ 2 oferentes en el mes evaluado
MIN_CA_MES_PREVIO = 20  # un mes anterior "con datos"
MIN_MESES_PREVIOS = 3
META_INTERVALO = 0.70
META_BRIER = 0.20
META_GANABLES = 0.10
DIAS_MODELOS_VIEJOS = 30


# ---------------------------------------------------------------------------
# Entrenamiento por mes (sin fuga: solo CA de meses anteriores)
# ---------------------------------------------------------------------------


@dataclass
class Entrenados:
    logistico: ModeloLogistico | None
    base: ModeloBase
    tabla: TablaN
    n_filas: int
    segundos: float


def cas_de_entrenamiento(cas: Sequence[CaDataset], mes: str) -> list[CaDataset]:
    """CA cerradas en los `MESES_ENTRENAMIENTO` meses ANTERIORES a `mes` (nunca `mes` ni después)."""
    desde = sumar_meses(mes, -MESES_ENTRENAMIENTO)
    return [c for c in cas if desde <= c.mes < mes]


def entrenar(cas_train: Sequence[CaDataset]) -> Entrenados:
    t0 = time.monotonic()
    filas = [f for c in cas_train for f in c.filas]
    logistico = entrenar_logistico(filas) if filas else None
    return Entrenados(
        logistico, entrenar_base(filas), tabla_n(cas_train), len(filas), time.monotonic() - t0
    )


def entrenar_para_mes(cas: Sequence[CaDataset], mes: str) -> Entrenados:
    return entrenar(cas_de_entrenamiento(cas, mes))


# ---------------------------------------------------------------------------
# Métricas
# ---------------------------------------------------------------------------


def confiabilidad(pares: Sequence[tuple[float, bool]]) -> list[dict[str, Any]]:
    """Curva de confiabilidad por decil de p predicha: n, p media y tasa observada."""
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(10)]
    for p, y in pares:
        bins[min(9, int(p * 10))].append((p, y))
    return [
        {
            "decil": i,
            "n": len(b),
            "p_media": round(sum(p for p, _ in b) / len(b), 4),
            "observada": round(sum(1 for _, y in b if y) / len(b), 4),
        }
        for i, b in enumerate(bins)
        if b
    ]


def _fila(mes: str, modelo: str, metrica: str, valor: float | None, n: int,
          detalle: dict[str, Any] | None = None, empresa_id: int | None = None) -> dict[str, Any]:  # fmt: skip
    return {"mes": mes, "modelo": modelo, "metrica": metrica,
            "valor": None if valor is None else round(valor, 6), "n": n, "detalle": detalle,
            "empresa_id": empresa_id}  # fmt: skip


@dataclass
class EmpresaEval:
    id: int
    costos: CostosEmpresa
    tarifas: Any
    tiene_costos: bool


def _ex_ante(
    c: CaDataset, memoria: IndiceMemoria, tabla: TablaN
) -> tuple[ContextoOferta, float, Any] | None:
    """(contexto base, mediana estimada, precios) de una CA como si fuera nueva; None sin cobertura."""
    if not c.lineas:
        return None
    refs = [(cb, memoria.referencia(pid, c.comuna, c.mes, region=c.region)) for pid, cb in c.lineas]
    precios = precios_de_lineas(refs)
    if precios is None or precios.mediana_estimada <= 0:
        return None
    base = ContextoOferta(
        ca_codigo=c.codigo, mes=c.mes, region=c.region, familia=c.familia, unidad=c.unidad,
        n_ofertas=tabla.esperado(c.unidad, c.familia), log_ratio=0.0,
    )  # fmt: skip
    return base, precios.mediana_estimada, precios


def evaluar_mes(
    session: Session,
    mes: str,
    cas: Sequence[CaDataset],
    regiones: list[int],
    empresas: Sequence[EmpresaEval],
    unidades: dict[int, str],
    tasa_financiera: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filas de `evaluacion_modelo` del mes y un resumen. No escribe."""
    ent = entrenar_para_mes(cas, mes)
    test = [c for c in cas if c.mes == mes]
    memoria = IndiceMemoria.cargar(session, [sumar_meses(mes, -k) for k in (3, 2, 1)])
    filas: list[dict[str, Any]] = []
    resumen: dict[str, Any] = {"mes": mes, "n_filas_entrenamiento": ent.n_filas,
                               "segundos_entrenamiento": round(ent.segundos, 1)}  # fmt: skip
    modelos: dict[str, Modelo] = {"base": ent.base}
    if ent.logistico is not None:
        modelos["logistico"] = ent.logistico
    ex_ante = {c.codigo: _ex_ante(c, memoria, ent.tabla) for c in test}

    for nombre, modelo in modelos.items():
        pares = [(modelo.p(f.contexto), f.gano) for c in test for f in c.filas]
        pares2 = [(modelo.p(f.contexto), f.gano) for c in test if c.n_ofertas >= 2 for f in c.filas]
        b, n = brier(pares)
        b2, n2 = brier(pares2)
        filas.append(_fila(mes, nombre, "brier", b, n))
        filas.append(
            _fila(mes, nombre, "brier_2mas", b2, n2, {"confiabilidad": confiabilidad(pares2)})
        )
        pares_ex: list[tuple[float, bool]] = []
        for c in test:
            ea = ex_ante.get(c.codigo)
            if ea is None or c.n_ofertas < 2:
                continue
            base, med, _ = ea
            for f in c.filas:
                ctx = ContextoOferta(**{**base.__dict__, "log_ratio": math.log(f.valor_neto / med),
                                        "es_local": f.contexto.es_local, "es_emt": f.contexto.es_emt})  # fmt: skip
                pares_ex.append((modelo.p(ctx), f.gano))
        bx, nx = brier(pares_ex)
        filas.append(_fila(mes, nombre, "brier_ex_ante", bx, nx))
        resumen[f"brier_2mas_{nombre}"] = None if b2 is None else round(b2, 4)
        resumen[f"brier_ex_ante_{nombre}"] = None if bx is None else round(bx, 4)

        for emp in empresas:
            if not emp.tiene_costos:
                filas.append(
                    _fila(
                        mes, nombre, "ganables", None, len(test), {"estado": "sin_costos"}, emp.id
                    )
                )
                continue
            ganables = con_costo = 0
            for c in test:
                ea = ex_ante.get(c.codigo)
                if ea is None or not c.lineas:
                    continue
                costos_lineas = [
                    emp.costos.costo(pid, unidades.get(pid, "")) for pid, _ in c.lineas
                ]
                if any(x is None for x in costos_lineas):
                    continue
                con_costo += 1
                prod = sum(
                    x.precio_por_unidad_base * cb
                    for x, (_, cb) in zip(costos_lineas, c.lineas, strict=True)
                    if x
                )
                kg = sum(cb for pid, cb in c.lineas if unidades.get(pid) == "kg")
                log = logistica(emp.tarifas, c.comuna, kg)
                total = (prod + log.costo) * (1 + tasa_financiera)
                base, _, precios = ea
                sug = next(p for p in curva(modelo, base, precios, total) if p.nombre == "sugerido")
                if (
                    sug.precio > 0
                    and (sug.precio - total) / sug.precio >= MARGEN_GANABLE
                    and sug.p >= P_GANABLE
                ):
                    ganables += 1
            filas.append(_fila(mes, nombre, "ganables", ganables / len(test) if test else None, len(test),
                               {"ganables": ganables, "con_costo_completo": con_costo}, emp.id))  # fmt: skip

    # Índice: intervalo 80 % y error del precio ganador por línea; cobertura del monto.
    aciertos = con_intervalo = 0
    errores: list[float] = []
    monto_cubierto = 0.0
    conteo = ConteoUnion()
    for lote in lotes_cas_del_mes(session, mes, regiones):
        for ob in observaciones_de_cas(session, lote, conteo):
            if ob.ganador is None or ob.ganador <= 0:
                continue
            ref = memoria.referencia(ob.producto_id, ob.comuna, mes, region=ob.region)
            if ref.n == 0 or not ref.mediana_ganador:
                continue
            monto_cubierto += ob.monto_ganador or 0.0
            errores.append(abs(ob.ganador / ref.mediana_ganador - 1.0))
            iv = intervalo_80(ref)
            if iv is not None:
                con_intervalo += 1
                aciertos += iv[0] <= ob.ganador <= iv[1]
    monto_total = sum(c.monto_ganador for c in test)
    filas.append(
        _fila(
            mes,
            "indice",
            "intervalo_80",
            aciertos / con_intervalo if con_intervalo else None,
            con_intervalo,
        )
    )
    filas.append(
        _fila(
            mes,
            "indice",
            "error_mediano_precio",
            statistics.median(errores) if errores else None,
            len(errores),
        )
    )
    filas.append(_fila(mes, "indice", "cobertura_monto", monto_cubierto / monto_total if monto_total else None,
                       len(test), {"monto_cubierto": round(monto_cubierto), "monto_total": round(monto_total),
                                   **conteo.como_dict()}))  # fmt: skip
    resumen["intervalo_80"] = round(aciertos / con_intervalo, 4) if con_intervalo else None
    resumen["lineas_con_intervalo"] = con_intervalo
    resumen["cobertura_monto"] = round(monto_cubierto / monto_total, 4) if monto_total else None
    resumen["descartadas_anexo"] = conteo.descartadas_anexo
    resumen["descartadas_ambiguas"] = conteo.descartadas_ambiguas
    return filas, resumen


def elegibilidad(cas: Sequence[CaDataset], mes: str) -> tuple[bool, dict[str, Any]]:
    """≥ 50 CA con ≥ 2 oferentes en `mes` y ≥ 3 meses anteriores (de los 18) con ≥ 20 CA."""
    por_mes = Counter(c.mes for c in cas)
    n2 = sum(1 for c in cas if c.mes == mes and c.n_ofertas >= 2)
    previos = [m for m in meses_entre(sumar_meses(mes, -MESES_ENTRENAMIENTO), sumar_meses(mes, -1))
               if por_mes.get(m, 0) >= MIN_CA_MES_PREVIO]  # fmt: skip
    ok = n2 >= MIN_CA_MES and len(previos) >= MIN_MESES_PREVIOS
    return ok, {"cas_2mas": n2, "meses_previos_con_datos": len(previos)}


# ---------------------------------------------------------------------------
# Modelos guardados y activación
# ---------------------------------------------------------------------------


def brier_comparado(session: Session) -> dict[str, float | None]:
    """Brier (≥ 2 oferentes) ponderado por n en los últimos 6 meses evaluados con ambos modelos."""
    filas = session.execute(
        select(
            EvaluacionModelo.mes,
            EvaluacionModelo.modelo,
            EvaluacionModelo.valor,
            EvaluacionModelo.n,
        ).where(EvaluacionModelo.metrica == "brier_2mas", EvaluacionModelo.valor.isnot(None))
    ).all()
    por_mes: dict[str, dict[str, tuple[float, int]]] = {}
    for mes, modelo, valor, n in filas:
        por_mes.setdefault(mes, {})[modelo] = (valor, n)
    meses = sorted(m for m, d in por_mes.items() if {"logistico", "base"} <= d.keys())[
        -MESES_EVALUADOS:
    ]
    out: dict[str, float | None] = {}
    for modelo in ("logistico", "base"):
        tot = sum(por_mes[m][modelo][1] for m in meses)
        out[modelo] = (
            sum(por_mes[m][modelo][0] * por_mes[m][modelo][1] for m in meses) / tot if tot else None
        )
    return out


def elegir_activo(comparado: dict[str, float | None]) -> str:
    """El logístico solo si su Brier walk-forward es MENOR que el de la base; si no, la base."""
    lg, bs = comparado.get("logistico"), comparado.get("base")
    return "logistico" if lg is not None and bs is not None and lg < bs else "base"


def guardar_modelos(session: Session, ent: Entrenados, datos_hasta_mes: str, n_cas: int,
                    sin_dato: int) -> str:  # fmt: skip
    """Inserta logístico y base, activa uno (`elegir_activo`) y purga inactivos viejos."""
    comparado = brier_comparado(session)
    tipo_activo = elegir_activo(comparado) if ent.logistico is not None else "base"
    session.execute(update(ModeloAdjudicacion).values(activo=False))
    metricas = {"n_cas": n_cas, "ofertas_sin_dato": sin_dato,
                "brier_walk_forward": comparado, "segundos": round(ent.segundos, 1)}  # fmt: skip
    for modelo in (ent.logistico, ent.base):
        if modelo is None:
            continue
        session.add(
            ModeloAdjudicacion(
                tipo=modelo.tipo, entrenado_en=ahora_utc(), datos_hasta_mes=datos_hasta_mes,
                n_filas=ent.n_filas,
                coeficientes={"modelo": modelo.a_json(), "n_esperado": ent.tabla.a_json()},
                metricas=metricas, activo=modelo.tipo == tipo_activo,
            )
        )  # fmt: skip
    session.execute(
        delete(ModeloAdjudicacion).where(
            ModeloAdjudicacion.activo.is_(False),
            ModeloAdjudicacion.entrenado_en < ahora_utc() - timedelta(days=DIAS_MODELOS_VIEJOS),
        )
    )
    session.commit()
    return tipo_activo


# ---------------------------------------------------------------------------
# Metas
# ---------------------------------------------------------------------------


def metas_del_mes(session: Session, mes: str, modelo: str) -> dict[str, Any]:
    """Estado de las metas del mes con el modelo dado: {meta: {valor, meta, cumple}}."""
    # Columnas, no entidades: el job borra y reinserta el mes en la misma sesión.
    filas = session.execute(
        select(
            EvaluacionModelo.modelo,
            EvaluacionModelo.metrica,
            EvaluacionModelo.valor,
            EvaluacionModelo.empresa_id,
            EvaluacionModelo.detalle,
        ).where(EvaluacionModelo.mes == mes)
    ).all()

    def _v(mod: str, met: str) -> float | None:
        return next(
            (
                f.valor
                for f in filas
                if f.modelo == mod and f.metrica == met and f.empresa_id is None
            ),
            None,
        )

    out: dict[str, Any] = {}
    iv = _v("indice", "intervalo_80")
    out["intervalo_80"] = {
        "valor": iv,
        "meta": META_INTERVALO,
        "cumple": iv is not None and iv >= META_INTERVALO,
    }
    br = _v(modelo, "brier_2mas")
    out["brier"] = {"valor": br, "meta": META_BRIER, "cumple": br is not None and br <= META_BRIER}
    gan = [
        f
        for f in filas
        if f.modelo == modelo and f.metrica == "ganables" and f.empresa_id is not None
    ]
    out["ganables"] = [
        {"empresa_id": f.empresa_id, "valor": f.valor, "meta": META_GANABLES,
         "cumple": None if f.valor is None else f.valor >= META_GANABLES,
         "estado": (f.detalle or {}).get("estado")}
        for f in gan
    ]  # fmt: skip
    return out


def avisos_de_metas(mes: str, metas: dict[str, Any]) -> list[str]:
    avisos: list[str] = []
    for clave in ("intervalo_80", "brier"):
        m = metas[clave]
        if not m["cumple"]:
            valor = "sin datos" if m["valor"] is None else f"{m['valor']:.3f}"
            avisos.append(f"{mes}: {clave} = {valor} no cumple la meta {m['meta']}")
    for g in metas["ganables"]:
        if g["estado"] == "sin_costos":
            avisos.append(f"{mes}: ganables de la empresa {g['empresa_id']}: sin costos")
        elif not g["cumple"]:
            valor = "sin datos" if g["valor"] is None else f"{g['valor']:.3f}"
            avisos.append(
                f"{mes}: ganables de la empresa {g['empresa_id']} = {valor} < meta {g['meta']}"
            )
    return avisos


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------


def run_indices(
    session: Session,
    regiones: list[int],
    *,
    completo: bool = False,
    tasa_financiera: float = 0.015,
    mes_hoy: str | None = None,
) -> dict[str, Any]:
    """Job `mercado-indices`: comunas → índice (últimos 3 meses, o todo con `completo`) →
    evaluación walk-forward (último mes cerrado, o los últimos 6) → reentrena y activa."""
    t0 = time.monotonic()
    resultado: dict[str, Any] = {"regiones": regiones, "completo": completo}
    if not regiones:
        return {**resultado, "sin_regiones": 1}
    hoy = mes_hoy or mes_actual()
    ultimo = sumar_meses(hoy, -1)
    desde_comunas = None if completo else rango_mes_utc(sumar_meses(hoy, -3))[0]
    resultado["comunas"] = inferir_comunas(session, regiones, todas=completo, desde=desde_comunas)
    meses_indice = (
        meses_con_cotizaciones(session, regiones)
        if completo
        else [sumar_meses(hoy, -k) for k in (2, 1, 0)]
    )
    resultado["indice"] = recalcular_indice(session, meses_indice, regiones)

    evaluar = (
        [sumar_meses(ultimo, -k) for k in range(MESES_EVALUADOS - 1, -1, -1)]
        if completo
        else [ultimo]
    )
    desde = sumar_meses(evaluar[0], -MESES_ENTRENAMIENTO)
    cas = cargar_dataset(session, regiones, desde, ultimo)
    resultado["dataset"] = {"cas": len(cas), "ofertas": sum(len(c.filas) for c in cas),
                            "ofertas_sin_dato": sum(c.sin_dato for c in cas)}  # fmt: skip
    unidades = {
        pid: ub
        for pid, ub in session.execute(select(ProductoCanonico.id, ProductoCanonico.unidad_base))
    }
    empresas = []
    for e in session.execute(select(Empresa).where(Empresa.activa.is_(True))).scalars():
        costos = CostosEmpresa(session, e.id)
        empresas.append(
            EmpresaEval(e.id, costos, cargar_tarifas(session, e.id), costos.tiene_costos())
        )

    evaluados: list[dict[str, Any]] = []
    for mes in evaluar:
        ok, info = elegibilidad(cas, mes)
        session.execute(delete(EvaluacionModelo).where(EvaluacionModelo.mes == mes))
        if not ok:
            session.add(
                EvaluacionModelo(
                    **_fila(mes, "indice", "sin_datos_suficientes", None, info["cas_2mas"], info)
                )
            )
            session.commit()
            evaluados.append({"mes": mes, "estado": "sin_datos_suficientes", **info})
            continue
        filas, resumen = evaluar_mes(
            session, mes, cas, regiones, empresas, unidades, tasa_financiera
        )
        for f in filas:
            session.add(EvaluacionModelo(**f))
        session.commit()
        evaluados.append({"estado": "evaluado", **resumen, **info})
    resultado["evaluacion"] = evaluados

    final = entrenar(
        [c for c in cas if sumar_meses(ultimo, -MESES_ENTRENAMIENTO + 1) <= c.mes <= ultimo]
    )
    if final.n_filas:
        activo = guardar_modelos(session, final, ultimo, len(cas), sum(c.sin_dato for c in cas))
        resultado["modelo_activo"] = activo
        resultado["segundos_entrenamiento_final"] = round(final.segundos, 1)
        if final.logistico is not None and not final.logistico.convergio:
            resultado.setdefault("avisos", []).append(
                "la logística no convergió (se activa la base si no la supera)"
            )
    else:
        resultado["modelo_activo"] = None

    avisos: list[str] = resultado.setdefault("avisos", [])
    ultimos_eval = [e["mes"] for e in evaluados if e["estado"] == "evaluado"]
    if ultimos_eval:
        mes_metas = ultimos_eval[-1]
        metas = metas_del_mes(session, mes_metas, resultado.get("modelo_activo") or "base")
        resultado["metas"] = {"mes": mes_metas, **metas}
        avisos.extend(avisos_de_metas(mes_metas, metas))
    else:
        avisos.append("ningún mes con datos suficientes para la evaluación walk-forward")
    resultado["duracion_s"] = round(time.monotonic() - t0, 1)
    _log.info("mercado-indices: %s", resultado)
    return resultado
