"""Abastecimiento (F-plat-6): recomendaciones de la empresa y consultas de `/abastecimiento`.

- **Recomendaciones (§1.5)**, de la empresa (regla 26), solo en sus regiones y con sus comunas
  prioritarias primero: precio firme (ítem con demanda esperada ≥ umbral por base y proveedor con
  costo), hueco de proveedor (la misma demanda sin proveedor), próxima compra esperada (comprador
  × ítem el mes siguiente) y demanda desierta (≥ 2 CA desiertas en 6 meses, de
  `competencia_comprador_mes`). Upsert por la clave única que NUNCA pisa el `estado` puesto por
  una persona; para saber qué insertó, `RETURNING` (nunca `rowcount` tras `ON CONFLICT`).
- **Línea base:** por nivel, si el v1 no le gana a la línea base en el backtest, las
  recomendaciones y la UI usan la línea base (y la UI lo dice).
- **UI:** lee solo `demanda_series`, `pronosticos`, `demanda_nombres`, `recomendaciones`,
  `evaluacion_modelo` y lo de la empresa: ningún request recorre `oc_lineas_ca`.

Sin API ni red. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.avance import Etapas
from app.core.tiempo import ahora_utc
from app.mercado.competencia import mediana_ponderada, regiones_empresa
from app.mercado.demanda import (
    HORIZONTE,
    NIVELES,
    P_COMPRA,
    Veredicto,
    modo_de_clave,
    run_demanda,
    veredicto,
)
from app.mercado.indice import (
    IndiceMemoria,
    mes_actual,
    mes_de,
    meses_entre,
    nombre_comuna,
    sumar_meses,
)
from app.mercado.proveedores import (
    Candidato,
    MejorCosto,
    Variacion,
    mejor_costo,
    variaciones_de,
)
from app.models.tables import (
    CompetenciaCompradorMes,
    DemandaNombre,
    DemandaSerie,
    Empresa,
    ProductoCanonico,
    Pronostico,
    Proveedor,
    ProveedorProducto,
    Recomendacion,
)

_log = logging.getLogger(__name__)

TIPOS = ("precio_firme", "hueco_proveedor", "proxima_compra", "demanda_desierta")
ETIQUETAS = {
    "precio_firme": "Pedir precio firme",
    "hueco_proveedor": "Buscar proveedor",
    "proxima_compra": "Próxima compra esperada",
    "demanda_desierta": "Demanda desierta",
}
ESTADOS = ("nueva", "vista", "hecha", "descartada")
ABIERTOS = ("nueva", "vista")
UMBRALES_DEFAULT = {"kg": 200.0, "L": 300.0, "u": 500.0}
TOPE_PROXIMA = 60  # próximas compras por empresa (las de comunas prioritarias primero)
MESES_DESIERTAS = 6
MIN_DESIERTAS = 2
MESES_RETENCION = 3  # recomendaciones con mes objetivo más viejo se borran (cualquier estado)
MESES_UI = 12
MESES_SERIE = 24
LIMITE_LISTA = 200
_MESES_ES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
             "septiembre", "octubre", "noviembre", "diciembre")  # fmt: skip


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def nombre_mes(mes: str) -> str:
    """'2026-03' → 'marzo 2026'."""
    y, m = mes.split("-")
    return f"{_MESES_ES[int(m) - 1]} {y}"


def texto_meses(meses: Iterable[str]) -> str:
    lista = sorted(set(meses))
    if not lista:
        return ""
    if len(lista) == 1:
        return nombre_mes(lista[0])
    return f"{nombre_mes(lista[0])} a {nombre_mes(lista[-1])}"


def _cantidad(x: float, base: str | None) -> str:
    return f"{x:,.0f}".replace(",", ".") + (f" {base}" if base else "")


def usar_v1(ver: Mapping[str, Veredicto]) -> dict[str, bool]:
    return {n: bool(ver.get(n) and ver[n].gana) for n in NIVELES}


def esperado(p: Pronostico, v1: bool) -> float:
    """Cantidad esperada del mes (monto a nivel familia): p × cantidad con el v1; con la línea
    base, la cantidad de referencia si dice «compra»."""
    if v1:
        return (p.p_compra or 0.0) * (p.cantidad_esperada or 0.0) if p.metodo == "v1" else 0.0
    return (p.cantidad_linea_base or 0.0) if p.compra_base else 0.0


def monto_esperado(p: Pronostico, v1: bool) -> float:
    if v1:
        return (p.p_compra or 0.0) * (p.monto_esperado or 0.0) if p.metodo == "v1" else 0.0
    return (p.monto_linea_base or 0.0) if p.compra_base else 0.0


def compra_esperada(p: Pronostico, v1: bool) -> bool:
    if v1:
        return p.metodo == "v1" and (p.p_compra or 0.0) >= P_COMPRA
    return p.compra_base


def nombres(session: Session, tipo: str, claves: Iterable[str]) -> dict[str, DemandaNombre]:
    lista = sorted(set(claves))
    out: dict[str, DemandaNombre] = {}
    for i in range(0, len(lista), 1000):
        for n in session.execute(
            select(DemandaNombre).where(
                DemandaNombre.tipo == tipo, DemandaNombre.clave.in_(lista[i : i + 1000])
            )
        ).scalars():
            out[n.clave] = n
    return out


def clave_item(nivel: str, item: str) -> str:
    return f"{nivel}:{item.split('|')[0]}" if nivel == "unspsc" else f"{nivel}:{item}"


def nombre_item(nivel: str, item: str, mapa: Mapping[str, DemandaNombre]) -> str:
    if nivel == "familia":
        return item
    n = mapa.get(clave_item(nivel, item))
    if nivel == "unspsc":
        codigo, _, base = item.partition("|")
        return f"{n.nombre if n else codigo} ({codigo}, {base})"
    return n.nombre if n else f"Producto {item}"


def nombres_items(session: Session, pares: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
    pares = list(pares)
    mapa = nombres(session, "item", {clave_item(n, i) for n, i in pares if n != "familia"})
    return {(n, i): nombre_item(n, i, mapa) for n, i in pares}


def comuna_de(n: DemandaNombre | None) -> str | None:
    return (n.datos or {}).get("comuna") if n is not None else None


def comunas_prioritarias(empresa: Empresa | None) -> set[str]:
    if empresa is None:
        return set()
    return {c for c in (nombre_comuna(x) for x in (empresa.comunas_prioritarias or [])) if c}


# ---------------------------------------------------------------------------
# Costos de la empresa por ítem
# ---------------------------------------------------------------------------


def mejor_costo_item(
    session: Session, empresa_id: int, nivel: str, item: str, base: str | None
) -> MejorCosto | None:
    """Mejor costo hoy de la empresa para un ítem: por `producto_id` (nivel producto) o por
    `codigo_unspsc` + unidad base (nivel unspsc). Familia: no aplica."""
    if nivel == "producto":
        return mejor_costo(session, empresa_id, producto_id=int(item), unidad_base=base)
    if nivel != "unspsc" or not base:
        return None
    codigo = item.split("|")[0]
    cands: list[Candidato] = []
    for pp, prov in session.execute(
        select(ProveedorProducto, Proveedor)
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(
            Proveedor.empresa_id == empresa_id, Proveedor.activo.is_(True),
            ProveedorProducto.disponible.is_(True), ProveedorProducto.codigo_unspsc == codigo,
            ProveedorProducto.unidad_base == base, ProveedorProducto.factor_base > 0,
        )
    ).all():  # fmt: skip
        cands.append(Candidato(
            proveedor_id=prov.id, proveedor=prov.nombre, producto_id=pp.id,
            producto_texto=pp.producto_texto, formato_texto=pp.formato_texto, unidad_base=base,
            precio_neto=pp.precio_neto_actual,
            precio_por_unidad_base=pp.precio_neto_actual / (pp.factor_base or 1.0),
        ))  # fmt: skip
    if not cands:
        return None
    orden = sorted(cands, key=lambda c: c.precio_por_unidad_base)
    return MejorCosto(base, orden[0], orden)


def proveedores_item(
    session: Session, empresa_id: int, nivel: str, item: str, base: str | None
) -> list[tuple[Candidato, Variacion | None]]:
    """Proveedores de la empresa para el ítem, del más barato al más caro, con su variación."""
    mc = mejor_costo_item(session, empresa_id, nivel, item, base)
    if mc is None:
        return []
    pps = [session.get(ProveedorProducto, c.producto_id) for c in mc.candidatos]
    var = variaciones_de(session, [p for p in pps if p is not None])
    return [(c, var.get(c.producto_id)) for c in mc.candidatos]


def precio_referencia(
    session: Session, nivel: str, item: str, regiones: list[int], mes_hoy: str
) -> tuple[float | None, str]:
    """Precio de referencia por unidad base: `precio_indice` (producto) o la mediana ganadora de
    las OC del ítem en las regiones (UNSPSC), de los 3 meses anteriores."""
    if nivel == "producto":
        meses = [sumar_meses(mes_hoy, -k) for k in range(3, 0, -1)]
        ref = IndiceMemoria.cargar(session, meses, [int(item)]).referencia(
            int(item), None, mes_hoy, region=regiones[0] if regiones else None
        )
        return ref.mediana_ganador, "índice de precios"
    filas = session.execute(
        select(DemandaSerie.precio_mediana, DemandaSerie.n_oc).where(
            DemandaSerie.nivel == nivel, DemandaSerie.item == item,
            DemandaSerie.region.in_(regiones), DemandaSerie.mes >= sumar_meses(mes_hoy, -3),
            DemandaSerie.mes < mes_hoy,
        )
    ).all()  # fmt: skip
    return mediana_ponderada((p, float(n or 1)) for p, n in filas), "mediana de las OC"


# ---------------------------------------------------------------------------
# Recomendaciones
# ---------------------------------------------------------------------------


@dataclass
class _Demanda:
    base: str | None
    cantidad: float = 0.0
    monto: float = 0.0
    compradores: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    meses: set[str] = field(default_factory=set)
    debil: bool = False


def _prioritaria(comuna: str | None, prioritarias: set[str]) -> bool:
    return bool(comuna) and comuna in prioritarias


def recomendar(
    session: Session,
    empresa_id: int,
    *,
    mes_hoy: str | None = None,
    umbrales: Mapping[str, float] | None = None,
    ver: Mapping[str, Veredicto] | None = None,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """Genera y guarda (upsert) las recomendaciones de UNA empresa. Devuelve conteos."""
    ahora = ahora or ahora_utc()
    hoy = mes_hoy or mes_de(ahora) or mes_actual()
    umbrales = dict(umbrales or UMBRALES_DEFAULT)
    empresa = session.get(Empresa, empresa_id)
    regiones = regiones_empresa(session, empresa_id)
    if empresa is None or not regiones:
        return {"sin_regiones": 1}
    ver = ver if ver is not None else veredicto(session)
    v1 = usar_v1(ver)
    prioritarias = comunas_prioritarias(empresa)
    objetivos = [sumar_meses(hoy, k) for k in range(HORIZONTE)]
    pron = list(session.execute(
        select(Pronostico).where(
            Pronostico.region.in_(regiones), Pronostico.mes_objetivo.in_(objetivos),
            Pronostico.nivel.in_(("producto", "unspsc")),
        )
    ).scalars())  # fmt: skip
    comp_nom = nombres(session, "comprador", {p.comprador for p in pron})
    items_nom = nombres_items(session, {(p.nivel, p.item) for p in pron})
    info_prod = {
        str(pid): f"{(u or '').strip()}|{b}"
        for pid, u, b in session.execute(
            select(
                ProductoCanonico.id, ProductoCanonico.unspsc_sugerido, ProductoCanonico.unidad_base
            )
        )  # fmt: skip
    }
    recs: list[dict[str, Any]] = []

    def _rec(tipo: str, nivel: str, item: str, comprador: str, mes: str, cantidad: float | None,
             texto: str, datos: dict[str, Any]) -> None:  # fmt: skip
        recs.append({"empresa_id": empresa_id, "tipo": tipo, "nivel": nivel, "item": item[:100],
                     "comprador": comprador[:600], "mes_objetivo": mes, "cantidad": cantidad,
                     "texto": texto[:1000], "datos": datos, "generada_en": ahora,
                     "estado": "nueva"})  # fmt: skip

    # Precio firme / hueco de proveedor: demanda esperada por ítem en los 3 meses.
    demanda: dict[tuple[str, str], _Demanda] = {}
    for p in pron:
        e = esperado(p, v1[p.nivel])
        if e <= 0:
            continue
        d = demanda.setdefault((p.nivel, p.item), _Demanda(p.base))
        d.cantidad += e
        d.monto += monto_esperado(p, v1[p.nivel])
        d.compradores[p.comprador] += e
        d.meses.add(p.mes_objetivo)
        d.debil = d.debil or p.debil or p.metodo != "v1"
    sobre = {k: d for k, d in demanda.items()
             if d.base in umbrales and d.cantidad >= umbrales[d.base]}  # fmt: skip
    cubiertos = {info_prod.get(i) for (n, i) in sobre if n == "producto"}
    for (nivel, item), d in sorted(sobre.items(), key=lambda kv: (-kv[1].monto, kv[0])):
        if nivel == "unspsc" and item in cubiertos:
            continue  # ya va como producto (el nivel más fino)
        nombre = items_nom.get((nivel, item), item)
        top = [comp_nom[c].nombre if c in comp_nom else c
               for c, _ in sorted(d.compradores.items(), key=lambda kv: -kv[1])[:5]]  # fmt: skip
        prio = any(_prioritaria(comuna_de(comp_nom.get(c)), prioritarias) for c in d.compradores)
        datos: dict[str, Any] = {
            "item_nombre": nombre, "base": d.base, "cantidad": round(d.cantidad, 2),
            "monto": round(d.monto), "meses": sorted(d.meses), "compradores": top,
            "n_compradores": len(d.compradores), "debil": d.debil, "prioritaria": prio,
            "metodo": "v1" if v1[nivel] else "linea_base",
        }  # fmt: skip
        meses_txt = texto_meses(d.meses)
        mc = mejor_costo_item(session, empresa_id, nivel, item, d.base)
        if mc is None:
            _rec("hueco_proveedor", nivel, item, "", hoy, d.cantidad,
                 f"Buscar proveedor de {nombre}: se esperan {_cantidad(d.cantidad, d.base)} "
                 f"entre {meses_txt} y no tienes proveedor con costo cargado.", datos)  # fmt: skip
            continue
        g = mc.ganador
        pp = session.get(ProveedorProducto, g.producto_id)
        var = variaciones_de(session, [pp]).get(g.producto_id) if pp is not None else None
        ref, fuente_ref = precio_referencia(session, nivel, item, regiones, hoy)
        margen = (ref - g.precio_por_unidad_base) / ref if ref else None
        datos.update({
            "proveedor": g.proveedor, "proveedor_id": g.proveedor_id,
            "costo_unitario": round(g.precio_por_unidad_base, 2),
            "variacion_90": var.pct_vs_90 if var else None,
            "precio_referencia": None if ref is None else round(ref, 2),
            "fuente_referencia": fuente_ref,
            "margen_pct": None if margen is None else round(margen, 4),
        })  # fmt: skip
        _rec("precio_firme", nivel, item, "", hoy, d.cantidad,
             f"Pedir precio firme a {g.proveedor} por {_cantidad(d.cantidad, d.base)} de "
             f"{nombre} para {meses_txt}.", datos)  # fmt: skip

    # Próxima compra esperada: comprador × ítem el mes siguiente.
    siguiente = sumar_meses(hoy, 1)
    cand = [p for p in pron if p.mes_objetivo == siguiente and compra_esperada(p, v1[p.nivel])]
    con_producto = {(p.comprador, info_prod.get(p.item)) for p in cand if p.nivel == "producto"}
    filas_prox = []
    for p in cand:
        if p.nivel == "unspsc" and (p.comprador, p.item) in con_producto:
            continue
        comuna = comuna_de(comp_nom.get(p.comprador))
        filas_prox.append((not _prioritaria(comuna, prioritarias),
                           -monto_esperado(p, v1[p.nivel]), p.comprador, p.item, p, comuna))  # fmt: skip
    filas_prox.sort(key=lambda t: t[:4])
    for _, _, _, _, p, comuna in filas_prox[:TOPE_PROXIMA]:
        nombre = items_nom.get((p.nivel, p.item), p.item)
        comp = comp_nom[p.comprador].nombre if p.comprador in comp_nom else p.comprador
        cant = (p.cantidad_esperada if v1[p.nivel] else p.cantidad_linea_base) or 0.0
        _rec("proxima_compra", p.nivel, p.item, p.comprador, siguiente, cant,
             f"{comp} suele comprar {nombre} en {nombre_mes(siguiente)}: preparar oferta.", {
                 "item_nombre": nombre, "comprador_nombre": comp, "comuna": comuna,
                 "prioritaria": _prioritaria(comuna, prioritarias), "base": p.base,
                 "p_compra": None if p.p_compra is None else round(p.p_compra, 3),
                 "cantidad": round(cant, 2), "monto": round(monto_esperado(p, v1[p.nivel])),
                 "n_meses_con_compra": p.n_meses_con_compra, "debil": p.debil,
                 "metodo": "v1" if v1[p.nivel] else "linea_base",
             })  # fmt: skip

    # Demanda desierta: ≥ 2 CA desiertas en 6 meses por comprador × familia.
    desde = sumar_meses(hoy, -(MESES_DESIERTAS - 1))
    for clave, familia, rut, nombre_c, n in session.execute(
        select(CompetenciaCompradorMes.clave_comprador, CompetenciaCompradorMes.familia,
               func.max(CompetenciaCompradorMes.organismo_rut),
               func.max(CompetenciaCompradorMes.comprador_nombre),
               func.sum(CompetenciaCompradorMes.n_desiertas))
        .where(CompetenciaCompradorMes.region.in_(regiones),
               CompetenciaCompradorMes.mes >= desde, CompetenciaCompradorMes.mes <= hoy)
        .group_by(CompetenciaCompradorMes.clave_comprador, CompetenciaCompradorMes.familia)
        .having(func.sum(CompetenciaCompradorMes.n_desiertas) >= MIN_DESIERTAS)
    ):  # fmt: skip
        # La clave de competencia es `clave_unidad`; la de demanda puede ser esa o el RUT.
        noms = nombres(session, "comprador", [clave, rut])
        comuna = comuna_de(noms.get(clave) or noms.get(rut))
        _rec("demanda_desierta", "familia", familia, clave, hoy, None,
             f"{nombre_c or clave} declaró desiertas {int(n)} CA de {familia} en los últimos "
             f"{MESES_DESIERTAS} meses: oportunidad con poca competencia.", {
                 "item_nombre": familia, "comprador_nombre": nombre_c or clave,
                 "n_desiertas": int(n), "comuna": comuna,
                 "prioritaria": _prioritaria(comuna, prioritarias), "monto": 0,
             })  # fmt: skip

    stats = guardar_recomendaciones(session, empresa_id, recs, ahora=ahora, mes_hoy=hoy)
    stats["por_tipo"] = dict(Counter(r["tipo"] for r in recs))
    return stats


_CLAVE = ("empresa_id", "tipo", "nivel", "item", "comprador", "mes_objetivo")


def guardar_recomendaciones(
    session: Session, empresa_id: int, recs: list[dict[str, Any]], *, ahora: datetime,
    mes_hoy: str,
) -> dict[str, Any]:  # fmt: skip
    """Upsert que conserva `estado` y `usuario_id`; borra las abiertas que esta corrida no
    regeneró y las de mes objetivo vencido hace más de `MESES_RETENCION` meses."""
    unicas: dict[tuple[Any, ...], dict[str, Any]] = {}
    for r in recs:
        unicas.setdefault(tuple(r[k] for k in _CLAVE), r)
    antes = set(session.execute(
        select(Recomendacion.id).where(Recomendacion.empresa_id == empresa_id)
    ).scalars())  # fmt: skip
    crear = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    ids: list[int] = []
    filas = list(unicas.values())
    for i in range(0, len(filas), 500):
        ins = crear(Recomendacion).values(filas[i : i + 500])
        upsert = ins.on_conflict_do_update(
            index_elements=list(_CLAVE),
            set_={k: ins.excluded[k] for k in ("cantidad", "texto", "datos", "generada_en")},
        ).returning(Recomendacion.id)
        ids.extend(session.execute(upsert).scalars().all())
    obsoletas = session.execute(
        delete(Recomendacion).where(
            Recomendacion.empresa_id == empresa_id, Recomendacion.estado.in_(ABIERTOS),
            Recomendacion.generada_en < ahora,
        ).returning(Recomendacion.id)
    ).scalars().all()  # fmt: skip
    vencidas = session.execute(
        delete(Recomendacion).where(
            Recomendacion.empresa_id == empresa_id,
            Recomendacion.mes_objetivo < sumar_meses(mes_hoy, -MESES_RETENCION),
        ).returning(Recomendacion.id)
    ).scalars().all()  # fmt: skip
    session.commit()
    return {"generadas": len(ids), "nuevas": sum(1 for x in ids if x not in antes),
            "actualizadas": sum(1 for x in ids if x in antes),
            "obsoletas_borradas": len(obsoletas), "vencidas_borradas": len(vencidas)}  # fmt: skip


def run_mercado_demanda(
    session: Session,
    regiones: list[int],
    *,
    completo: bool = False,
    umbrales: Mapping[str, float] | None = None,
    mes_hoy: str | None = None,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """Job `mercado-demanda`: series, backtest, pronósticos y recomendaciones de cada empresa
    activa. Sin API."""
    ahora = ahora or ahora_utc()
    resultado = run_demanda(session, regiones, completo=completo, mes_hoy=mes_hoy, ahora=ahora)
    if resultado.get("sin_regiones"):
        return resultado
    t0 = time.monotonic()
    etapas = Etapas(resultado, "demanda")
    ver = veredicto(session)
    recs: dict[str, Any] = {}
    for (eid,) in session.execute(select(Empresa.id).where(Empresa.activa.is_(True))).all():
        recs[str(eid)] = recomendar(session, eid, mes_hoy=mes_hoy, umbrales=umbrales, ver=ver,
                                    ahora=ahora)  # fmt: skip
        etapas.fin("recomendaciones", f"empresa {eid}: {recs[str(eid)].get('generadas', 0)} generadas")
    resultado["recomendaciones"] = recs
    resultado["duracion_s"] = round(resultado.get("duracion_s", 0.0) + time.monotonic() - t0, 1)
    _log.info("mercado-demanda: %s", resultado)
    return resultado


# ---------------------------------------------------------------------------
# Estado de una recomendación (UI)
# ---------------------------------------------------------------------------


def recomendacion_de(session: Session, empresa_id: int, rec_id: int) -> Recomendacion | None:
    """La recomendación SOLO si es de la empresa (otra empresa → None → 404)."""
    return session.execute(
        select(Recomendacion).where(
            Recomendacion.id == rec_id, Recomendacion.empresa_id == empresa_id
        )
    ).scalar_one_or_none()


def cambiar_estado(rec: Recomendacion, estado: str, usuario_id: int | None) -> None:
    if estado not in ESTADOS:
        raise ValueError(f"estado inválido: {estado!r}")
    rec.estado = estado
    rec.usuario_id = usuario_id
    rec.actualizada_en = ahora_utc()


# ---------------------------------------------------------------------------
# Consultas de la UI
# ---------------------------------------------------------------------------


def _orden_rec(r: Recomendacion) -> tuple[Any, ...]:
    d = r.datos or {}
    return (not d.get("prioritaria"), -(d.get("monto") or 0), -(r.cantidad or 0), r.id)


def recomendaciones_ui(
    session: Session, empresa_id: int, *, cerradas: bool = False
) -> dict[str, list[Recomendacion]]:
    estados = ESTADOS if cerradas else ABIERTOS
    filas = session.execute(
        select(Recomendacion).where(
            Recomendacion.empresa_id == empresa_id, Recomendacion.estado.in_(estados)
        )
    ).scalars()
    por_tipo: dict[str, list[Recomendacion]] = {t: [] for t in TIPOS}
    for r in filas:
        por_tipo.setdefault(r.tipo, []).append(r)
    for lista in por_tipo.values():
        lista.sort(key=_orden_rec)
    return por_tipo


@dataclass
class Contexto:
    empresa_id: int
    regiones: list[int]
    prioritarias: set[str]
    hoy: str
    ver: dict[str, Veredicto]
    v1: dict[str, bool]


def contexto(session: Session, empresa_id: int, mes_hoy: str | None = None) -> Contexto:
    ver = veredicto(session)
    return Contexto(
        empresa_id, regiones_empresa(session, empresa_id),
        comunas_prioritarias(session.get(Empresa, empresa_id)), mes_hoy or mes_actual(), ver,
        usar_v1(ver),
    )  # fmt: skip


def calendario(session: Session, ctx: Contexto, nivel: str = "unspsc") -> dict[str, Any]:
    """Próximos 3 meses × ítem: cantidad (o monto) esperada, compradores y su p."""
    objetivos = [sumar_meses(ctx.hoy, k) for k in range(HORIZONTE)]
    v1 = ctx.v1.get(nivel, False)
    items: dict[str, dict[str, Any]] = {}
    for p in session.execute(
        select(Pronostico).where(
            Pronostico.region.in_(ctx.regiones), Pronostico.nivel == nivel,
            Pronostico.mes_objetivo.in_(objetivos),
        )
    ).scalars():  # fmt: skip
        e = esperado(p, v1)
        if e <= 0:
            continue
        it = items.setdefault(p.item, {"item": p.item, "base": p.base, "monto": 0.0,
                                       "meses": {m: {"cantidad": 0.0, "compradores": [],
                                                     "debil": False} for m in objetivos}})  # fmt: skip
        celda = it["meses"][p.mes_objetivo]
        celda["cantidad"] += e
        celda["debil"] = celda["debil"] or p.debil or p.metodo != "v1"
        celda["compradores"].append((p.comprador, p.p_compra, compra_esperada(p, v1)))
        it["monto"] += monto_esperado(p, v1)
    orden = sorted(items.values(), key=lambda x: (-x["monto"], x["item"]))[:LIMITE_LISTA]
    noms = nombres_items(session, [(nivel, x["item"]) for x in orden])
    comp_nom = nombres(session, "comprador",
                       {c for x in orden for m in x["meses"].values() for c, _, _ in m["compradores"]})  # fmt: skip
    for x in orden:
        x["nombre"] = noms.get((nivel, x["item"]), x["item"])
        for celda in x["meses"].values():
            lista = sorted(celda["compradores"], key=lambda t: -(t[1] or 0))
            celda["n_compran"] = sum(1 for _, _, c in lista if c)
            celda["n"] = len(lista)
            celda["compradores"] = [
                {"clave": c, "nombre": comp_nom[c].nombre if c in comp_nom else c, "p": p_,
                 "compra": cm} for c, p_, cm in lista[:5]
            ]  # fmt: skip
    return {"meses": objetivos, "items": orden, "nivel": nivel, "v1": v1}


def compradores_ui(session: Session, ctx: Contexto, q: str = "") -> list[dict[str, Any]]:
    """Compradores de las regiones de la empresa (12 meses): monto, cada cuánto compra, comuna."""
    desde = sumar_meses(ctx.hoy, -MESES_UI)
    filas = session.execute(
        select(DemandaSerie.comprador, func.sum(DemandaSerie.monto_neto),
               func.count(func.distinct(DemandaSerie.mes)), func.sum(DemandaSerie.n_oc))
        .where(DemandaSerie.region.in_(ctx.regiones), DemandaSerie.nivel == "familia",
               DemandaSerie.mes >= desde, DemandaSerie.mes < ctx.hoy)
        .group_by(DemandaSerie.comprador)
    ).all()  # fmt: skip
    noms = nombres(session, "comprador", [f[0] for f in filas])
    termino = " ".join(q.split()).lower()[:100]
    out = []
    for clave, monto, n_meses, n_oc in filas:
        n = noms.get(clave)
        nombre = n.nombre if n else clave
        if termino and termino not in nombre.lower() and termino not in clave.lower():
            continue
        comuna = comuna_de(n)
        out.append({
            "clave": clave, "nombre": nombre, "monto": float(monto or 0.0),
            "meses_con_compra": int(n_meses or 0), "n_oc": int(n_oc or 0),
            "recurrencia": int(n_meses or 0) / MESES_UI, "comuna": comuna,
            "prioritaria": _prioritaria(comuna, ctx.prioritarias),
            "proveedores": ((n.datos or {}).get("proveedores") or [])[:3] if n else [],
            "es_unidad": modo_de_clave(clave) == "unidad",
        })  # fmt: skip
    out.sort(key=lambda x: (not x["prioritaria"], -x["monto"], x["nombre"]))
    return out[:LIMITE_LISTA]


def comprador_ui(session: Session, ctx: Contexto, clave: str) -> dict[str, Any] | None:
    """Qué compra un comprador (12 meses), cada cuánto, a quién y a qué precio, y su pronóstico.
    None si no tiene compras en las regiones de la empresa (→ 404)."""
    desde = sumar_meses(ctx.hoy, -MESES_UI)
    filas = list(session.execute(
        select(DemandaSerie).where(
            DemandaSerie.comprador == clave, DemandaSerie.region.in_(ctx.regiones),
            DemandaSerie.mes >= desde, DemandaSerie.mes < ctx.hoy,
        )
    ).scalars())  # fmt: skip
    if not filas:
        return None
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for f in filas:
        a = agg.setdefault((f.nivel, f.item), {"nivel": f.nivel, "item": f.item, "base": f.base,
                                               "monto": 0.0, "cantidad": 0.0, "meses": set(),
                                               "precios": [], "n_oc": 0,
                                               "n_proveedores": 0})  # fmt: skip
        a["monto"] += f.monto_neto
        a["cantidad"] += f.cantidad_base or 0.0
        a["meses"].add(f.mes)
        a["n_oc"] += f.n_oc
        a["n_proveedores"] = max(a["n_proveedores"], f.n_proveedores)
        if f.precio_mediana is not None:
            a["precios"].append((f.precio_mediana, float(f.n_oc or 1)))
    pron: dict[tuple[str, str], list[Pronostico]] = defaultdict(list)
    for p in session.execute(
        select(Pronostico).where(
            Pronostico.comprador == clave, Pronostico.region.in_(ctx.regiones)
        )
    ).scalars():  # fmt: skip
        pron[(p.nivel, p.item)].append(p)
    noms = nombres_items(session, list(agg))
    items = []
    for k, a in agg.items():
        v1 = ctx.v1.get(a["nivel"], False)
        ps = sorted(pron.get(k, []), key=lambda p: p.mes_objetivo)
        items.append({
            **{c: v for c, v in a.items() if c not in ("meses", "precios")},
            "nombre": noms.get(k, a["item"]), "meses_con_compra": len(a["meses"]),
            "precio_mediana": mediana_ponderada(a["precios"]),
            "pronostico": [{"mes": p.mes_objetivo, "p": p.p_compra, "compra": compra_esperada(p, v1),
                            "cantidad": p.cantidad_esperada if v1 else p.cantidad_linea_base,
                            "debil": p.debil, "metodo": p.metodo} for p in ps],
        })  # fmt: skip
    items.sort(key=lambda x: (x["nivel"] != "familia", -x["monto"]))
    n = nombres(session, "comprador", [clave]).get(clave)
    comuna = comuna_de(n)
    return {
        "clave": clave, "nombre": n.nombre if n else clave, "comuna": comuna,
        "prioritaria": _prioritaria(comuna, ctx.prioritarias),
        "datos": (n.datos or {}) if n else {}, "es_unidad": modo_de_clave(clave) == "unidad",
        "familias": [i for i in items if i["nivel"] == "familia"],
        "items": [i for i in items if i["nivel"] != "familia"][:LIMITE_LISTA],
        "monto": sum(i["monto"] for i in items if i["nivel"] == "familia"),
        "meses_con_compra": len({f.mes for f in filas}),
    }  # fmt: skip


def item_ui(session: Session, ctx: Contexto, nivel: str, item: str) -> dict[str, Any] | None:
    """Serie de 24 meses del ítem en las regiones de la empresa, estacionalidad, compradores
    principales, pronóstico agregado y proveedores de la empresa con su variación de costo."""
    if nivel not in NIVELES:
        return None
    desde = sumar_meses(ctx.hoy, -MESES_SERIE)
    filas = list(session.execute(
        select(DemandaSerie).where(
            DemandaSerie.nivel == nivel, DemandaSerie.item == item,
            DemandaSerie.region.in_(ctx.regiones), DemandaSerie.mes >= desde,
        )
    ).scalars())  # fmt: skip
    if not filas:
        return None
    base = next((f.base for f in filas if f.base), None)
    por_mes: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    compradores: dict[str, float] = defaultdict(float)
    vistos: dict[str, set[str]] = defaultdict(set)
    for f in filas:
        valor = f.monto_neto if nivel == "familia" else (f.cantidad_base or 0.0)
        por_mes[f.mes][0] += valor
        por_mes[f.mes][1] += f.monto_neto
        vistos[f.mes].add(f.comprador)
        if f.mes >= sumar_meses(ctx.hoy, -MESES_UI):
            compradores[f.comprador] += f.monto_neto
    meses = meses_entre(desde, sumar_meses(ctx.hoy, -1))
    tope = max((por_mes[m][0] for m in meses), default=0.0)
    serie: list[dict[str, Any]] = [
        {"mes": m, "valor": por_mes[m][0], "monto": por_mes[m][1],
         "n_compradores": len(vistos[m]),
         "alto": round(100 * por_mes[m][0] / tope) if tope else 0}
        for m in meses
    ]  # fmt: skip
    valores = [por_mes[m][0] for m in meses]
    promedio = statistics.mean(valores) if valores else 0.0
    estacion = []
    for k in range(1, 13):
        vals = [por_mes[m][0] for m in meses if int(m[5:]) == k]
        prom = statistics.mean(vals) if vals else None
        indice = prom / promedio if promedio and prom is not None else None
        estacion.append({"mes": _MESES_ES[k - 1], "indice": indice, "n": len(vals),
                         "debil": len(vals) <= 1})  # fmt: skip
    noms = nombres(session, "comprador", list(compradores))
    top = [{"clave": c, "nombre": noms[c].nombre if c in noms else c, "monto": m,
            "es_unidad": modo_de_clave(c) == "unidad"} for c, m in sorted(compradores.items(), key=lambda kv: -kv[1])[:10]]  # fmt: skip
    objetivos = [sumar_meses(ctx.hoy, k) for k in range(HORIZONTE)]
    v1 = ctx.v1.get(nivel, False)
    prono = {m: {"cantidad": 0.0, "n_compran": 0, "n": 0, "debil": False} for m in objetivos}
    for p in session.execute(
        select(Pronostico).where(
            Pronostico.nivel == nivel, Pronostico.item == item,
            Pronostico.region.in_(ctx.regiones), Pronostico.mes_objetivo.in_(objetivos),
        )
    ).scalars():  # fmt: skip
        c = prono[p.mes_objetivo]
        c["cantidad"] += esperado(p, v1)
        c["n"] += 1
        c["n_compran"] += compra_esperada(p, v1)
        c["debil"] = c["debil"] or p.debil or p.metodo != "v1"
    return {
        "nivel": nivel, "item": item, "base": base,
        "nombre": nombres_items(session, [(nivel, item)])[(nivel, item)],
        "serie": serie, "estacionalidad": estacion, "compradores": top,
        "pronostico": [{"mes": m, **prono[m]} for m in objetivos], "v1": v1,
        "proveedores": proveedores_item(session, ctx.empresa_id, nivel, item, base),
    }  # fmt: skip


def resumen_backtest(ver: Mapping[str, Veredicto]) -> list[dict[str, Any]]:
    """Por nivel: métricas del v1 al lado de la línea base, cumple meta y si le gana."""
    return [
        {"nivel": n, "meses": list(v.meses), "elegibles": v.elegibles, **v.metricas,
         "cumple_meta": v.cumple_meta, "base_cumple_meta": v.base_cumple_meta, "gana": v.gana}
        for n, v in ver.items()
    ]  # fmt: skip
