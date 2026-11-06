"""Demanda por comprador, pronóstico v1 y su juez (F-plat-6).

Qué va a comprar cada comprador, cuánto y cuándo, desde las órdenes de compra (`oc_lineas_ca`,
~26 meses): no depende del hueco de ofertas de 2026.

- **Medición (§1.0)** antes de agregar: (a) líneas de OC cuyo `rut_unidad` es `organismo_rut` de
  alguna CA; (b) unidades distintas por RUT; (c) CA con OC cuya `clave_unidad` coincide con la de
  la OC; (d) cobertura por nivel en % del monto; (e) cantidad en base: `cantidad × precio_neto /
  precio_base` contra `total / precio_base`. La clave del comprador sale de (c): ≥ 80 % →
  `clave_unidad`; si no (o con < 30 CA medibles) → el RUT (`indice._rut`). Nunca por nombre libre.
  Si la clave guardada no es la que toca, el job recalcula todo (nunca mezcla claves).
- **Series (§1.2):** comprador × nivel × ítem × mes. Niveles PARALELOS (una línea suma en todos
  los que alcanza): `producto` (catálogo canónico por `sugerencia_unica`, misma base), `unspsc`
  (`UNSPSC|base`, código de 8 dígitos) y `familia` (`familia_de_codigo`, solo monto). La cantidad
  solo se suma dentro de una base. Sin doble conteo: la OC cuenta; la CA cerrada sin OC cargada
  cuenta sus `ca_lineas` `ok`; la desierta no suma. Mes de la OC = mes de `fecha_creacion` (fecha
  local, sin hora [V dev 5-nov]); si falta, el `mes` del archivo.
- **Pronóstico v1 (§1.3)** sin dependencias: recurrencia, estacionalidad por mes calendario,
  tendencia acotada y p25–p75 escalado. Solo con ≥ 4 meses con compra.
- **Juez (§1.4):** backtest de los últimos 6 meses (cada mes M con datos < M), al lado de la
  línea base «compró el mismo mes del año anterior o el mes anterior». Va a `evaluacion_modelo`
  (`modelo = demanda`).

Sin API ni red. Por meses, con commit por mes (regla 12). Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, or_, select, text
from sqlalchemy.orm import Session

from app.catalogos.canonico import Catalogo, sugerencia_unica
from app.catalogos.mercado import FAMILIAS_ALIMENTOS, codigo_oc_normalizado, norm
from app.core.avance import Etapas
from app.core.lotes import insertar_por_lotes
from app.core.tiempo import ahora_utc
from app.mercado.adjudicacion import familia_de_codigo
from app.mercado.catalogo import cargar_catalogo
from app.mercado.competencia import ESTADOS_DESIERTA
from app.mercado.comprador import clave_unidad
from app.mercado.evaluacion import MODELO_DEMANDA
from app.mercado.indice import (
    LOTE,
    _rut,
    cuantil,
    mes_actual,
    mes_de,
    meses_entre,
    nombre_comuna,
    rango_mes_utc,
    sumar_meses,
)
from app.models.tables import (
    CaCotizacion,
    CaCotizacionProducto,
    CaLinea,
    CompraAgil,
    DemandaNombre,
    DemandaSerie,
    EvaluacionModelo,
    OcLineaCa,
    ProductoCanonico,
    Pronostico,
)

_log = logging.getLogger(__name__)

NIVELES = ("producto", "unspsc", "familia")
MODO_UNIDAD = "unidad"  # clave_unidad: «RUT|unidad»
MODO_RUT = "rut"  # solo el RUT; las unidades quedan como detalle
UMBRAL_CLAVE = 0.80  # (c) ≥ 80 % → clave_unidad
MIN_CA_CLAVE = 30  # con menos CA medibles no hay evidencia para clave_unidad
MIN_MESES_COMPRA = 4
HORIZONTE = 3
P_COMPRA = 0.60  # «compra en M» si p ≥ 0,6
P_GUARDAR = 0.20  # pronósticos con p menor no se guardan (salvo que la línea base diga compra)
K_PREVIA = 1.0  # peso de la recurrencia frente a los años observados del mismo mes calendario
MESES_INACTIVO = 12  # sin compra en más de 12 meses → p a la mitad
FACTOR_INACTIVO = 0.5
TOPE_MIN, TOPE_MAX = 0.5, 2.0  # tendencia y estacionalidad acotadas
META_PRECISION = 0.70
META_RECALL = 0.50
MESES_BACKTEST = 6
MESES_DIARIO = 3  # el job diario recalcula el mes en curso y los 2 anteriores
MESES_VIGENTE = 24  # una serie sin compra en 24 meses no se pronostica
MESES_PROVEEDORES = 12
TOP_PROVEEDORES = 5
TOP_UNIDADES = 10
TOLERANCIA_CANTIDAD = 0.01
MAX_BYTES = 40_000_000  # regla 11: aviso si las tablas de demanda pasan de 40 MB
TABLAS = ("demanda_series", "pronosticos", "demanda_nombres", "recomendaciones")
AVISO_CADA = 50_000  # log de avance del recorrido de OC cada tantas líneas

Clave = tuple[str, str, str]  # (comprador, nivel, item)


# ---------------------------------------------------------------------------
# Claves, meses y cantidades
# ---------------------------------------------------------------------------


def clave_comprador(rut: str | None, unidad: str | None, modo: str) -> str:
    """La clave elegida por la medición: `clave_unidad` o el RUT (`indice._rut`)."""
    if not _rut(rut):
        return ""
    return clave_unidad(rut, unidad) if modo == MODO_UNIDAD else _rut(rut)


def modo_de_clave(clave: str) -> str:
    return MODO_UNIDAD if "|" in clave else MODO_RUT


def mes_oc(fecha_creacion: datetime | None, mes_archivo: str | None) -> str | None:
    """Mes de compra de una línea de OC: `fecha_creacion` es fecha local sin hora (no UTC)."""
    if fecha_creacion is not None:
        return fecha_creacion.strftime("%Y-%m")
    return mes_archivo


def cantidad_en_base(
    cantidad: float | None,
    precio_neto: float | None,
    total: float | None,
    precio_base: float | None,
) -> float | None:
    """Cantidad en kg, L o u. `precio_base = precio_neto / factor` (`catalogos.mercado.normalizar`),
    así que `cantidad × precio_neto / precio_base = cantidad × factor`; sin cantidad, `total /
    precio_base`. Verificado en dev el 5-nov: ambas coinciden en 78.262 de 78.262 líneas."""
    if not precio_base or precio_base <= 0:
        return None
    if cantidad and cantidad > 0 and precio_neto and precio_neto > 0:
        return cantidad * precio_neto / precio_base
    if total and total > 0:
        return total / precio_base
    return None


def _codigo8(codigo: str | None) -> str | None:
    c = (codigo or "").strip()
    return c if len(c) == 8 and c.isdigit() else None


def es_alimento(nivel: str, item: str) -> bool:
    """Ítems de alimentos (la cobertura del juez se mide sobre ellos)."""
    if nivel == "producto":
        return True  # el catálogo canónico es la canasta de alimentos
    if nivel == "unspsc":
        return item.startswith("50") or item[:4] in FAMILIAS_ALIMENTOS
    return item in _FAMILIAS_ALIM or item.startswith("Otros alimentos")


_FAMILIAS_ALIM = frozenset(FAMILIAS_ALIMENTOS.values())


# ---------------------------------------------------------------------------
# Medición (§1.0) y directorio de nombres: una pasada liviana por OC y CA
# ---------------------------------------------------------------------------


@dataclass
class Medicion:
    lineas_oc: int = 0
    lineas_oc_rut_en_ca: int = 0
    unidades_por_rut: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    cas_con_oc: int = 0
    cas_con_oc_misma_clave: int = 0
    estados_oc: Counter[str] = field(default_factory=Counter)
    primer_mes: str | None = None

    @property
    def pct_rut_en_ca(self) -> float | None:
        return self.lineas_oc_rut_en_ca / self.lineas_oc if self.lineas_oc else None

    @property
    def pct_misma_clave(self) -> float | None:
        return self.cas_con_oc_misma_clave / self.cas_con_oc if self.cas_con_oc else None

    def modo(self) -> str:
        p = self.pct_misma_clave
        if p is None or self.cas_con_oc < MIN_CA_CLAVE:
            return MODO_RUT
        return MODO_UNIDAD if p >= UMBRAL_CLAVE else MODO_RUT

    def como_dict(self) -> dict[str, Any]:
        n_unid = [len(v) for v in self.unidades_por_rut.values()]
        p_a, p_c = self.pct_rut_en_ca, self.pct_misma_clave
        return {
            "a_lineas_oc": self.lineas_oc,
            "a_pct_rut_unidad_es_organismo_de_ca": None if p_a is None else round(p_a, 4),
            "b_ruts": len(n_unid),
            "b_ruts_con_varias_unidades": sum(1 for n in n_unid if n > 1),
            "b_max_unidades_por_rut": max(n_unid, default=0),
            "b_media_unidades_por_rut": round(statistics.mean(n_unid), 2) if n_unid else None,
            "c_cas_con_oc": self.cas_con_oc,
            "c_pct_misma_clave_unidad": None if p_c is None else round(p_c, 4),
            "estados_oc": dict(self.estados_oc.most_common(10)),
            "clave_elegida": self.modo(),
        }


@dataclass
class _Rut:
    organismo: Counter[str] = field(default_factory=Counter)
    region: int | None = None
    comunas: Counter[str] = field(default_factory=Counter)
    unidades: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    # (unidad normalizada) → RUT de proveedor → [monto, nombre] en los últimos 12 meses
    proveedores: dict[str, dict[str, list[Any]]] = field(default_factory=lambda: defaultdict(dict))


@dataclass
class Directorio:
    """Lo que la UI necesita para nombrar compradores e ítems (se guarda en `demanda_nombres`)."""

    ruts: dict[str, _Rut] = field(default_factory=lambda: defaultdict(_Rut))
    genericos: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    oc_codigos: set[str] = field(default_factory=set)  # `ID` de oc-da normalizado (`id_oc`)
    oc_a_ca: dict[str, str] = field(default_factory=dict)  # `ID` de la OC normalizado → código de CA


def oc_de_cas(session: Session, cas: Mapping[str, str | None]) -> dict[str, set[str]]:
    """OC (normalizadas) de cada CA de `cas` (código → `compras_agiles.id_orden_compra`).

    `id_orden_compra` solo lo llenaba el job viejo `ca` (apagado desde el 31-oct); la ingesta
    vigente (`ingest/mercado.py`) guarda la OC en `ca_cotizaciones.id_oc`. Si `id_orden_compra`
    es nulo, valen los `id_oc` de las ofertas seleccionadas (varias → todas). Una consulta por
    lote. CA sin OC no aparece en el resultado.

    Ambos campos traen el `ID` numérico de la OC en oc-da (p. ej. `55672374`) [V, 5-nov-2026],
    no su `Codigo` (`2724-973-AG26`): se cruzan con `oc_lineas_ca.id_oc` (F-plat-6-cruce-2)."""
    out: dict[str, set[str]] = {}
    faltan: list[str] = []
    for codigo, id_oc in cas.items():
        oc = codigo_oc_normalizado(id_oc)
        if oc:
            out[codigo] = {oc}
        else:
            faltan.append(codigo)
    if faltan:
        for ca, id_oc in session.execute(
            select(CaCotizacion.ca_codigo, CaCotizacion.id_oc).where(
                CaCotizacion.ca_codigo.in_(faltan),
                CaCotizacion.seleccionado.is_(True),
                CaCotizacion.id_oc.is_not(None),
            )
        ):
            oc = codigo_oc_normalizado(id_oc)
            if oc:
                out.setdefault(ca, set()).add(oc)
    return out


def recorrer(session: Session, regiones: list[int], mes_hoy: str) -> tuple[Medicion, Directorio]:
    """Una pasada por las OC (columnas livianas) y otra por las CA de `regiones`."""
    med, dir_ = Medicion(), Directorio()
    ruts_ca = {
        _rut(r)
        for r in session.execute(
            select(CompraAgil.organismo_rut).where(CompraAgil.region.in_(regiones)).distinct()
        ).scalars()
        if _rut(r)
    }
    oc_clave: dict[str, tuple[str, str]] = {}
    desde_prov = sumar_meses(mes_hoy, -(MESES_PROVEEDORES - 1))
    for (id_oc, rut_u, unidad, organismo, comuna, region, rut_p, prov, cod, generico, total,
         fecha, mes, estado, moneda) in session.execute(
        select(
            OcLineaCa.id_oc, OcLineaCa.rut_unidad, OcLineaCa.unidad_compra,
            OcLineaCa.organismo, OcLineaCa.comuna_compra, OcLineaCa.region_compra,
            OcLineaCa.rut_proveedor, OcLineaCa.proveedor, OcLineaCa.codigo_producto,
            OcLineaCa.producto_generico, OcLineaCa.total_linea_neto, OcLineaCa.fecha_creacion,
            OcLineaCa.mes, OcLineaCa.codigo_estado, OcLineaCa.moneda,
        )
        .where(OcLineaCa.region_compra.in_(regiones))
        .execution_options(yield_per=LOTE)
    ):  # fmt: skip
        rut = _rut(rut_u)
        un = norm(unidad)
        med.lineas_oc += 1
        if med.lineas_oc % AVISO_CADA == 0:
            _log.info("demanda recorrido OC: %d líneas, %d RUT", med.lineas_oc, len(dir_.ruts))
        med.estados_oc[estado or ""] += 1
        oc_norm = codigo_oc_normalizado(id_oc)  # el `ID` de oc-da; sin él la línea no cruza
        if oc_norm:
            dir_.oc_codigos.add(oc_norm)
        m = mes_oc(fecha, mes)
        if m and (med.primer_mes is None or m < med.primer_mes):
            med.primer_mes = m
        if not rut:
            continue
        med.lineas_oc_rut_en_ca += rut in ruts_ca
        med.unidades_por_rut[rut].add(un)
        if oc_norm:
            oc_clave.setdefault(oc_norm, (rut, un))
        r = dir_.ruts[rut]
        if organismo:
            r.organismo[" ".join(organismo.split())[:300]] += 1
        r.region = region if region is not None else r.region
        c = nombre_comuna(comuna)
        if c:
            r.comunas[c] += 1
        nombres_unidad = r.unidades[un]  # aunque venga sin texto: la clave «RUT|» también existe
        if unidad:
            nombres_unidad[" ".join(unidad.split())[:300]] += 1
        c8 = _codigo8(cod)
        if c8 and generico:
            dir_.genericos[c8][" ".join(generico.split())[:200]] += 1
        if m and m >= desde_prov and rut_p and (moneda or "CLP") == "CLP" and total:
            fila = r.proveedores[un].setdefault(rut_p, [0.0, None])
            fila[0] += float(total)
            fila[1] = fila[1] or (" ".join(prov.split())[:200] if prov else None)
    ultimo = ""
    while True:  # por lotes con keyset: `oc_de_cas` consulta entre lote y lote
        cas = session.execute(
            select(
                CompraAgil.codigo, CompraAgil.organismo_rut, CompraAgil.organismo_nombre,
                CompraAgil.unidad_compra, CompraAgil.region, CompraAgil.comuna_inferida,
                CompraAgil.id_orden_compra,
            )
            .where(CompraAgil.region.in_(regiones), CompraAgil.codigo > ultimo)
            .order_by(CompraAgil.codigo)
            .limit(LOTE)
        ).all()  # fmt: skip
        if not cas:
            break
        ultimo = cas[-1][0]
        ocs_lote = oc_de_cas(session, {fila[0]: fila[6] for fila in cas})
        for codigo, org_rut, org_nombre, unidad, region, comuna, _ in cas:
            rut = _rut(org_rut)
            if not rut:
                continue
            r = dir_.ruts[rut]
            if org_nombre and not r.organismo:
                r.organismo[" ".join(org_nombre.split())[:300]] += 1
            r.region = r.region if r.region is not None else region
            if comuna and not r.comunas:
                r.comunas[comuna] += 1
            un = norm(unidad)
            if un not in r.unidades:
                nombres_unidad = r.unidades[un]
                if unidad:
                    nombres_unidad[" ".join(unidad.split())[:300]] += 1
            cruzan = sorted(oc for oc in ocs_lote.get(codigo, ()) if oc in oc_clave)
            if not cruzan:
                continue
            for oc in cruzan:
                dir_.oc_a_ca[oc] = codigo
            med.cas_con_oc += 1
            clave_ca = clave_unidad(org_rut, unidad)
            med.cas_con_oc_misma_clave += any(
                clave_ca == f"{oc_clave[oc][0]}|{oc_clave[oc][1]}" for oc in cruzan
            )
    _log.info("demanda recorrido terminado: %d líneas de OC, %d RUT, %d CA con OC",
              med.lineas_oc, len(dir_.ruts), med.cas_con_oc)  # fmt: skip
    return med, dir_


def modo_guardado(session: Session, regiones: list[int]) -> str | None:
    """Clave con que están escritas las series de `regiones` (None si no hay series)."""
    clave = session.execute(
        select(DemandaSerie.comprador).where(DemandaSerie.region.in_(regiones)).limit(1)
    ).scalar_one_or_none()
    return None if clave is None else modo_de_clave(clave)


# ---------------------------------------------------------------------------
# Series (§1.2)
# ---------------------------------------------------------------------------


@dataclass
class _Agg:
    base: str | None
    ocs: set[str] = field(default_factory=set)
    cas: set[str] = field(default_factory=set)
    cantidad: float = 0.0
    con_cantidad: bool = False
    monto: float = 0.0
    precios: list[float] = field(default_factory=list)
    proveedores: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _InfoProducto:
    nombre: str
    unidad_base: str
    unspsc: str | None
    familia: str | None


class Productos:
    """Catálogo canónico para el nivel «producto»: sugerencia única y misma unidad base."""

    def __init__(self, session: Session, catalogo: Catalogo | None = None) -> None:
        self.catalogo = catalogo if catalogo is not None else cargar_catalogo(session)
        self.info = {
            pid: _InfoProducto(nombre, base, (unspsc or "").strip() or None, familia)
            for pid, nombre, base, unspsc, familia in session.execute(
                select(
                    ProductoCanonico.id,
                    ProductoCanonico.nombre,
                    ProductoCanonico.unidad_base,
                    ProductoCanonico.unspsc_sugerido,
                    ProductoCanonico.familia,
                )
            )  # fmt: skip
        }
        self._cache: dict[tuple[str, str | None], int | None] = {}

    def de_linea(self, texto: str | None, unspsc: str | None, base: str | None) -> int | None:
        if not texto or not base:
            return None
        k = (texto, unspsc)
        if k not in self._cache:
            s = sugerencia_unica(texto, unspsc, self.catalogo)
            self._cache[k] = s.producto_id if s is not None else None
        pid = self._cache[k]
        info = self.info.get(pid) if pid is not None else None
        return pid if info is not None and info.unidad_base == base else None

    def limpiar(self) -> None:
        self._cache.clear()


@dataclass
class ConteoSeries:
    meses: list[str] = field(default_factory=list)
    filas: int = 0
    lineas_oc: int = 0
    lineas_oc_otra_moneda: int = 0
    lineas_oc_sin_comprador: int = 0
    cas_desiertas: int = 0
    cas_con_oc: int = 0
    cas_sin_oc: int = 0
    lineas_ca_ok: int = 0
    cantidad_verificadas: int = 0
    cantidad_coinciden: int = 0
    monto: float = 0.0
    monto_por_nivel: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    monto_alim: float = 0.0
    monto_alim_por_nivel: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def como_dict(self) -> dict[str, Any]:
        def _pct(x: float, tot: float) -> float | None:
            return round(x / tot, 4) if tot else None

        return {
            "meses": self.meses, "filas": self.filas, "lineas_oc": self.lineas_oc,
            "lineas_oc_otra_moneda": self.lineas_oc_otra_moneda,
            "lineas_oc_sin_comprador": self.lineas_oc_sin_comprador,
            "cas_desiertas_no_suman": self.cas_desiertas, "cas_con_oc": self.cas_con_oc,
            "cas_sin_oc": self.cas_sin_oc, "lineas_ca_ok_sumadas": self.lineas_ca_ok,
            "d_cobertura_monto": {n: _pct(self.monto_por_nivel[n], self.monto) for n in NIVELES},
            "d_cobertura_monto_alimentos": {
                n: _pct(self.monto_alim_por_nivel[n], self.monto_alim) for n in NIVELES
            },
            "e_cantidad_verificadas": self.cantidad_verificadas,
            "e_pct_cantidad_coincide": _pct(self.cantidad_coinciden, self.cantidad_verificadas),
        }  # fmt: skip


class _Mes:
    """Agregado de un mes: (comprador, nivel, item, region) → _Agg."""

    def __init__(self, conteo: ConteoSeries) -> None:
        self.agg: dict[tuple[str, str, str, int | None], _Agg] = {}
        self.conteo = conteo

    def sumar(
        self, comprador: str, nivel: str, item: str, region: int | None, base: str | None, *,
        oc: str | None, ca: str | None, cantidad: float | None, monto: float,
        precio: float | None = None, proveedor: str | None = None,
    ) -> None:  # fmt: skip
        a = self.agg.get((comprador, nivel, item, region))
        if a is None:
            a = self.agg[(comprador, nivel, item, region)] = _Agg(base)
        if oc:
            a.ocs.add(oc)
        if ca:
            a.cas.add(ca)
        if cantidad is not None and nivel != "familia":
            a.cantidad += cantidad
            a.con_cantidad = True
        a.monto += monto
        if precio is not None and precio > 0:
            a.precios.append(precio)
        if proveedor:
            a.proveedores.add(proveedor)

    def filas(self, mes: str) -> list[dict[str, Any]]:
        return [
            {
                "comprador": comp, "nivel": nivel, "item": item[:100], "mes": mes,
                "region": region, "n_oc": len(a.ocs), "n_ca": len(a.cas),
                "cantidad_base": a.cantidad if a.con_cantidad else None,
                "base": a.base if nivel != "familia" else None, "monto_neto": a.monto,
                "precio_mediana": statistics.median(a.precios) if a.precios else None,
                "n_proveedores": len(a.proveedores),
            }
            for (comp, nivel, item, region), a in self.agg.items()
        ]  # fmt: skip

    def cobertura(self, nivel: str, item: str, monto: float) -> None:
        c = self.conteo
        c.monto_por_nivel[nivel] += monto
        if es_alimento(nivel, item):
            c.monto_alim_por_nivel[nivel] += monto


def _sumar_oc(
    session: Session, mes: str, regiones: list[int], modo: str, dir_: Directorio,
    productos: Productos, acc: _Mes,
) -> None:  # fmt: skip
    """Líneas de OC creadas en `mes` (por `fecha_creacion`, fecha local; sin fecha, el `mes` del
    archivo). Filtrar en SQL evita traer en cada mes todo lo de archivos posteriores."""
    c = acc.conteo
    y, m = (int(x) for x in mes.split("-"))
    ini = datetime(y, m, 1)
    y2, m2 = (int(x) for x in sumar_meses(mes, 1).split("-"))
    fin = datetime(y2, m2, 1)
    en_mes = or_(
        and_(OcLineaCa.fecha_creacion >= ini, OcLineaCa.fecha_creacion < fin),
        and_(OcLineaCa.fecha_creacion.is_(None), OcLineaCa.mes == mes),
    )
    for (codigo_oc, rut_u, unidad, region, rut_p, cod, generico, espec, cantidad, precio_neto,
         total, precio_base, base, fecha, mes_arch, moneda, id_oc) in session.execute(
        select(
            OcLineaCa.codigo_oc, OcLineaCa.rut_unidad, OcLineaCa.unidad_compra,
            OcLineaCa.region_compra, OcLineaCa.rut_proveedor, OcLineaCa.codigo_producto,
            OcLineaCa.producto_generico, OcLineaCa.especificacion, OcLineaCa.cantidad,
            OcLineaCa.precio_neto, OcLineaCa.total_linea_neto, OcLineaCa.precio_base,
            OcLineaCa.base, OcLineaCa.fecha_creacion, OcLineaCa.mes, OcLineaCa.moneda,
            OcLineaCa.id_oc,
        )
        .where(OcLineaCa.region_compra.in_(regiones), en_mes)
        .execution_options(yield_per=LOTE)
    ):  # fmt: skip
        if mes_oc(fecha, mes_arch) != mes:
            continue
        if (moneda or "CLP") != "CLP":
            c.lineas_oc_otra_moneda += 1
            continue
        comprador = clave_comprador(rut_u, unidad, modo)
        if not comprador:
            c.lineas_oc_sin_comprador += 1
            continue
        c.lineas_oc += 1
        if total and total > 0:
            monto = float(total)
        elif cantidad and precio_neto:
            monto = float(cantidad) * float(precio_neto)
        else:
            monto = 0.0
        c.monto += monto
        ca = dir_.oc_a_ca.get(codigo_oc_normalizado(id_oc))
        familia = familia_de_codigo(cod, generico)
        kw: dict[str, Any] = {"oc": codigo_oc, "ca": ca, "monto": monto, "proveedor": rut_p}
        acc.sumar(comprador, "familia", familia, region, None, cantidad=None, **kw)
        acc.cobertura("familia", familia, monto)
        if es_alimento("familia", familia):
            c.monto_alim += monto
        c8 = _codigo8(cod)
        if not (c8 and base):
            continue
        cant = cantidad_en_base(cantidad, precio_neto, total, precio_base)
        if cant is not None and cantidad and precio_neto and total and precio_base:
            c.cantidad_verificadas += 1
            alt = float(total) / float(precio_base)
            c.cantidad_coinciden += abs(cant - alt) <= TOLERANCIA_CANTIDAD * max(cant, alt)
        item = f"{c8}|{base}"
        acc.sumar(comprador, "unspsc", item, region, base, cantidad=cant, precio=precio_base, **kw)
        acc.cobertura("unspsc", item, monto)
        pid = productos.de_linea(espec or generico, c8, base)
        if pid is not None:
            acc.sumar(comprador, "producto", str(pid), region, base, cantidad=cant,
                      precio=precio_base, **kw)  # fmt: skip
            acc.cobertura("producto", str(pid), monto)


def _sumar_ca_sin_oc(
    session: Session, mes: str, regiones: list[int], modo: str, dir_: Directorio,
    productos: Productos, acc: _Mes, ahora: datetime,
) -> None:  # fmt: skip
    """CA cerradas en `mes`: con OC cargada → ya contó la OC; desierta → no suma; cerrada sin
    OC → sus líneas `ok` (cantidad en base; monto con el precio de la oferta seleccionada)."""
    c = acc.conteo
    ini, fin = rango_mes_utc(mes)
    fin = min(fin, ahora)
    ultimo = ""
    while True:
        cas = session.execute(
            select(
                CompraAgil.codigo, CompraAgil.organismo_rut, CompraAgil.unidad_compra,
                CompraAgil.region, CompraAgil.estado, CompraAgil.motivo_desierta,
                CompraAgil.id_orden_compra,
            )
            .where(
                CompraAgil.region.in_(regiones), CompraAgil.fecha_cierre >= ini,
                CompraAgil.fecha_cierre < fin, CompraAgil.codigo > ultimo,
            )
            .order_by(CompraAgil.codigo)
            .limit(LOTE)
        ).all()  # fmt: skip
        if not cas:
            return
        ultimo = cas[-1][0]
        sin_oc: dict[str, tuple[str, int | None]] = {}
        ocs_lote = oc_de_cas(session, {fila[0]: fila[6] for fila in cas})
        for codigo, org, unidad, region, estado, motivo, _ in cas:
            if (estado or "") in ESTADOS_DESIERTA or motivo:
                c.cas_desiertas += 1
            elif any(oc in dir_.oc_codigos for oc in ocs_lote.get(codigo, ())):
                c.cas_con_oc += 1
            else:
                comprador = clave_comprador(org, unidad, modo)
                if comprador:
                    c.cas_sin_oc += 1
                    sin_oc[codigo] = (comprador, region)
        if not sin_oc:
            continue
        codigos = list(sin_oc)
        precios: dict[tuple[str, str], float] = {}
        for ca, cod, precio in session.execute(
            select(CaCotizacion.ca_codigo, CaCotizacionProducto.codigo_producto,
                   CaCotizacionProducto.precio_unitario)
            .join(CaCotizacion, CaCotizacion.id == CaCotizacionProducto.cotizacion_id)
            .where(CaCotizacion.ca_codigo.in_(codigos), CaCotizacion.seleccionado.is_(True))
        ):  # fmt: skip
            if cod and precio and precio > 0:
                precios.setdefault((ca, cod), float(precio))
        for ca, pid, cantidad, cant_base, cod, texto in session.execute(
            select(CaLinea.ca_codigo, CaLinea.producto_id, CaLinea.cantidad,
                   CaLinea.cantidad_base, CaLinea.codigo_unspsc, CaLinea.texto_origen)
            .where(CaLinea.ca_codigo.in_(codigos), CaLinea.estado == "ok",
                   CaLinea.producto_id.isnot(None))
        ):  # fmt: skip
            info = productos.info.get(pid)
            if info is None:
                continue
            comprador, region = sin_oc[ca]
            c.lineas_ca_ok += 1
            precio = precios.get((ca, cod)) if cod else None
            monto = precio * cantidad if precio and cantidad else 0.0
            c.monto += monto
            unspsc = _codigo8(cod) or _codigo8(info.unspsc)
            familia = familia_de_codigo(unspsc, texto)
            kw: dict[str, Any] = {"oc": None, "ca": ca, "monto": monto}
            cb = float(cant_base) if cant_base and cant_base > 0 else None
            acc.sumar(comprador, "familia", familia, region, None, cantidad=None, **kw)
            acc.cobertura("familia", familia, monto)
            if es_alimento("familia", familia):
                c.monto_alim += monto
            if unspsc:
                item = f"{unspsc}|{info.unidad_base}"
                acc.sumar(comprador, "unspsc", item, region, info.unidad_base, cantidad=cb, **kw)
                acc.cobertura("unspsc", item, monto)
            acc.sumar(comprador, "producto", str(pid), region, info.unidad_base, cantidad=cb, **kw)
            acc.cobertura("producto", str(pid), monto)


def recalcular_series(
    session: Session,
    regiones: list[int],
    desde: str,
    hasta: str,
    modo: str,
    dir_: Directorio,
    *,
    ahora: datetime,
    productos: Productos | None = None,
) -> dict[str, Any]:
    """Reescribe `demanda_series` de `desde` a `hasta` SOLO en `regiones` (borra y reinserta;
    commit por mes). Idempotente."""
    productos = productos if productos is not None else Productos(session)
    conteo = ConteoSeries()
    for mes in meses_entre(desde, hasta):
        t_mes = time.monotonic()
        acc = _Mes(conteo)
        _sumar_oc(session, mes, regiones, modo, dir_, productos, acc)
        _sumar_ca_sin_oc(session, mes, regiones, modo, dir_, productos, acc, ahora)
        productos.limpiar()
        filas = acc.filas(mes)
        session.execute(
            delete(DemandaSerie).where(DemandaSerie.mes == mes, DemandaSerie.region.in_(regiones))
        )
        insertar_por_lotes(session, DemandaSerie, filas)
        session.commit()
        conteo.meses.append(mes)
        conteo.filas += len(filas)
        _log.info("demanda series %s: %d filas, %.1f s", mes, len(filas), time.monotonic() - t_mes)
    return conteo.como_dict()


# ---------------------------------------------------------------------------
# Nombres para la UI
# ---------------------------------------------------------------------------


def _mas_comun(c: Counter[str]) -> str | None:
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if c else None


def guardar_nombres(
    session: Session, regiones: list[int], dir_: Directorio, modo: str, productos: Productos
) -> int:
    """Reescribe `demanda_nombres`: compradores de `regiones` (con la clave vigente) e ítems."""
    filas: list[dict[str, Any]] = []
    for rut, r in dir_.ruts.items():
        organismo = _mas_comun(r.organismo) or rut
        comuna = _mas_comun(r.comunas)
        unidades = {un: _mas_comun(cs) or un for un, cs in r.unidades.items()}
        grupos = [(un, [un]) for un in unidades] if modo == MODO_UNIDAD else [("", list(unidades))]
        if not grupos:
            grupos = [("", [])]
        for un, miembros in grupos:
            clave = f"{rut}|{un}" if modo == MODO_UNIDAD else rut
            prov: dict[str, list[Any]] = {}
            for m in miembros:
                for rp, (monto, nombre) in r.proveedores.get(m, {}).items():
                    p = prov.setdefault(rp, [0.0, nombre])
                    p[0] += monto
                    p[1] = p[1] or nombre
            top = sorted(prov.items(), key=lambda kv: (-kv[1][0], kv[0]))[:TOP_PROVEEDORES]
            nombre = f"{organismo} · {unidades[un]}" if modo == MODO_UNIDAD and un else organismo
            filas.append({
                "tipo": "comprador", "clave": clave[:620], "nombre": nombre[:600],
                "region": r.region,
                "datos": {
                    "organismo_rut": rut, "comuna": comuna,
                    "unidades": sorted({unidades[m] for m in miembros})[:TOP_UNIDADES],
                    "n_unidades": len(miembros),
                    "proveedores": [{"rut": k, "nombre": v[1] or k, "monto": round(v[0])}
                                    for k, v in top],
                },
            })  # fmt: skip
    for codigo, cs in dir_.genericos.items():
        filas.append({"tipo": "item", "clave": f"unspsc:{codigo}", "nombre": _mas_comun(cs) or codigo,
                      "region": None, "datos": None})  # fmt: skip
    for pid, info in productos.info.items():
        filas.append({"tipo": "item", "clave": f"producto:{pid}", "nombre": info.nombre[:600],
                      "region": None, "datos": {"unidad_base": info.unidad_base,
                                                "unspsc": info.unspsc}})  # fmt: skip
    session.execute(
        delete(DemandaNombre).where(
            or_(DemandaNombre.tipo == "item",
                and_(DemandaNombre.tipo == "comprador",
                     or_(DemandaNombre.region.in_(regiones), DemandaNombre.region.is_(None))))
        )
    )  # fmt: skip
    vistos: set[tuple[str, str]] = set()
    unicas = []
    for f in filas:
        k = (f["tipo"], f["clave"])
        if k not in vistos:
            vistos.add(k)
            unicas.append(f)
    insertar_por_lotes(session, DemandaNombre, unicas)
    session.commit()
    return len(unicas)


# ---------------------------------------------------------------------------
# Pronóstico v1 (§1.3)
# ---------------------------------------------------------------------------


@dataclass
class Serie:
    region: int | None
    base: str | None
    valores: dict[str, float] = field(default_factory=dict)  # cantidad (monto en familia)
    montos: dict[str, float] = field(default_factory=dict)


def cargar_series(session: Session, regiones: list[int]) -> dict[Clave, Serie]:
    """Todas las series de `regiones` en memoria (sumando regiones si un comprador está en dos)."""
    series: dict[Clave, Serie] = {}
    for comp, nivel, item, mes, region, cant, base, monto in session.execute(
        select(DemandaSerie.comprador, DemandaSerie.nivel, DemandaSerie.item, DemandaSerie.mes,
               DemandaSerie.region, DemandaSerie.cantidad_base, DemandaSerie.base,
               DemandaSerie.monto_neto)
        .where(DemandaSerie.region.in_(regiones))
        .execution_options(yield_per=LOTE)
    ):  # fmt: skip
        s = series.get((comp, nivel, item))
        if s is None:
            s = series[(comp, nivel, item)] = Serie(region, base)
        valor = float(monto or 0.0) if nivel == "familia" else float(cant or 0.0)
        s.valores[mes] = s.valores.get(mes, 0.0) + valor
        s.montos[mes] = s.montos.get(mes, 0.0) + float(monto or 0.0)
        s.base = s.base or base
    return series


@dataclass(frozen=True)
class PronosticoMes:
    mes_objetivo: str
    metodo: str  # v1 | insuficiente
    n_meses_con_compra: int
    compra_base: bool
    cantidad_linea_base: float | None
    monto_linea_base: float | None = None
    p_compra: float | None = None
    cantidad: float | None = None  # si compra (monto a nivel familia)
    monto: float | None = None
    intervalo: tuple[float, float] | None = None
    debil: bool = False


def _acotar(x: float) -> float:
    return max(TOPE_MIN, min(TOPE_MAX, x))


def pronosticar(
    valores: Mapping[str, float],
    corte: str,
    objetivos: Sequence[str],
    montos: Mapping[str, float] | None = None,
) -> list[PronosticoMes]:
    """Pronóstico de cada mes de `objetivos` con SOLO los meses anteriores a `corte`.

    - recurrencia = meses con compra / meses observados desde la primera compra;
    - p_compra = recurrencia ajustada por los años en que se observó ese mes calendario:
      (compras en ese mes calendario + K × recurrencia) / (años observados + K); sin compra en
      más de 12 meses, a la mitad;
    - cantidad (si compra) = promedio de los meses con compra × estacionalidad × tendencia, con
      estacionalidad = promedio de ese mes calendario / promedio (sin años con compra → 1; un
      año o menos observado → `debil`) y tendencia = media de las compras de los últimos 3 meses
      / de los últimos 12, DESESTACIONALIZADAS (cada compra / la estacionalidad de su mes; sin
      compras en 3 meses → 1), ambas acotadas a [0,5; 2]. Se usan los meses con compra y no los
      ceros, y se desestacionaliza, porque si no el que compra el triple en marzo que en
      septiembre «caería» a la mitad cada septiembre (y con ceros, a 0 en febrero);
    - intervalo = p25–p75 histórico de los meses con compra × el mismo factor.
    Línea base al lado: compró el mismo mes del año anterior o el último mes observado.
    """
    hist = {m: v for m, v in valores.items() if m < corte and v > 0}
    if not hist:
        return []
    ultimo = sumar_meses(corte, -1)
    obs = meses_entre(min(hist), ultimo)
    orden = sorted(hist)
    compras = [hist[m] for m in orden]
    n = len(compras)
    ult_compra = orden[-1]
    precio_unit = None
    if montos is not None:
        tot = sum(hist.values())
        precio_unit = sum(montos.get(m, 0.0) for m in hist) / tot if tot else None
    promedio = statistics.mean(compras)
    por_cal: dict[str, list[float]] = defaultdict(list)
    for m in orden:
        por_cal[m[5:]].append(hist[m])
    estac = {c: _acotar(statistics.mean(v) / promedio) for c, v in por_cal.items()}
    ult3 = [hist[m] / estac[m[5:]] for m in obs[-3:] if m in hist]
    ult12 = [hist[m] / estac[m[5:]] for m in obs[-12:] if m in hist]
    tendencia = _acotar(statistics.mean(ult3) / statistics.mean(ult12)) if ult3 and ult12 else 1.0
    inactivo = len(meses_entre(ult_compra, ultimo)) - 1 > MESES_INACTIVO
    p25, p75 = cuantil(compras, 0.25), cuantil(compras, 0.75)
    salida: list[PronosticoMes] = []
    for obj in objetivos:
        ref = sumar_meses(obj, -12)
        compra_base = ref in hist or ultimo in hist
        q_base = hist.get(ref) or hist.get(ultimo) or hist[ult_compra]
        m_base = q_base * precio_unit if precio_unit is not None else None
        if n < MIN_MESES_COMPRA:
            salida.append(PronosticoMes(obj, "insuficiente", n, compra_base, q_base, m_base))
            continue
        recurrencia = n / len(obs)
        meses_cal = [m for m in obs if m[5:] == obj[5:]]
        compras_cal = [hist[m] for m in meses_cal if m in hist]
        p = (len(compras_cal) + K_PREVIA * recurrencia) / (len(meses_cal) + K_PREVIA)
        if inactivo:
            p *= FACTOR_INACTIVO
        est = estac.get(obj[5:], 1.0)
        f = est * tendencia
        cantidad = promedio * f
        monto = cantidad * precio_unit if precio_unit is not None else None
        salida.append(PronosticoMes(
            obj, "v1", n, compra_base, q_base, m_base, p_compra=min(1.0, p), cantidad=cantidad,
            monto=monto, intervalo=(p25 * f, p75 * f), debil=len(meses_cal) <= 1,
        ))  # fmt: skip
    return salida


def guardar_pronosticos(
    session: Session, series: Mapping[Clave, Serie], regiones: list[int], mes_hoy: str,
    ahora: datetime,
) -> dict[str, int]:  # fmt: skip
    """Reescribe `pronosticos` de `regiones`: meses objetivo = el en curso y los 2 siguientes,
    con datos hasta el último mes cerrado. Guarda los de p ≥ 0,2 o con la línea base en «compra»."""
    objetivos = [sumar_meses(mes_hoy, k) for k in range(HORIZONTE)]
    vigente = sumar_meses(mes_hoy, -MESES_VIGENTE)
    filas: list[dict[str, Any]] = []
    stats = Counter[str]()
    for (comp, nivel, item), s in series.items():
        compras = [m for m, v in s.valores.items() if v > 0 and m < mes_hoy]
        if not compras or max(compras) < vigente:
            continue
        stats["series"] += 1
        for f in pronosticar(s.valores, mes_hoy, objetivos, s.montos):
            stats[f.metodo] += 1
            guardar = f.compra_base or (f.p_compra is not None and f.p_compra >= P_GUARDAR)
            if not guardar:
                continue
            filas.append({
                "comprador": comp, "nivel": nivel, "item": item, "region": s.region,
                "base": s.base if nivel != "familia" else None, "mes_objetivo": f.mes_objetivo,
                "p_compra": f.p_compra, "cantidad_esperada": f.cantidad,
                "monto_esperado": f.monto if nivel != "familia" else f.cantidad,
                "intervalo_min": f.intervalo[0] if f.intervalo else None,
                "intervalo_max": f.intervalo[1] if f.intervalo else None, "debil": f.debil,
                "compra_base": f.compra_base, "cantidad_linea_base": f.cantidad_linea_base,
                "monto_linea_base": f.monto_linea_base if nivel != "familia" else
                f.cantidad_linea_base,
                "n_meses_con_compra": f.n_meses_con_compra, "metodo": f.metodo,
                "generado_en": ahora,
            })  # fmt: skip
    session.execute(
        delete(Pronostico).where(or_(Pronostico.region.in_(regiones), Pronostico.region.is_(None)))
    )
    insertar_por_lotes(session, Pronostico, filas)
    session.commit()
    return {**stats, "filas": len(filas)}


# ---------------------------------------------------------------------------
# Juez: backtest (§1.4)
# ---------------------------------------------------------------------------


@dataclass
class _Conteo:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tp_base: int = 0
    fp_base: int = 0
    fn_base: int = 0
    elegibles: int = 0
    pares_mape: int = 0
    suma_ape: float = 0.0
    suma_ape_base: float = 0.0
    monto_alim: float = 0.0
    monto_alim_cubierto: float = 0.0

    def sumar(self, otro: Mapping[str, Any]) -> None:
        for k in self.__dataclass_fields__:
            setattr(self, k, getattr(self, k) + (otro.get(k) or 0))

    def metricas(self) -> dict[str, float | None]:
        def _div(a: float, b: float) -> float | None:
            return a / b if b else None

        prec, rec = _div(self.tp, self.tp + self.fp), _div(self.tp, self.tp + self.fn)
        pb, rb = (
            _div(self.tp_base, self.tp_base + self.fp_base),
            _div(self.tp_base, self.tp_base + self.fn_base),
        )
        return {
            "precision": prec, "recall": rec, "f1": _f1(prec, rec),
            "precision_base": pb, "recall_base": rb, "f1_base": _f1(pb, rb),
            "mape": _div(self.suma_ape, self.pares_mape),
            "mape_base": _div(self.suma_ape_base, self.pares_mape),
            "cobertura": _div(self.monto_alim_cubierto, self.monto_alim),
        }  # fmt: skip


def _f1(p: float | None, r: float | None) -> float | None:
    if p is None or r is None:
        return None
    return 2 * p * r / (p + r) if p + r else 0.0


def backtest(series: Mapping[Clave, Serie], meses: Sequence[str]) -> dict[str, dict[str, _Conteo]]:
    """Para cada mes M: pronostica M con los datos < M (`pronosticar` corta en M) y compara con lo
    comprado en M. Población: series con pronóstico v1 (≥ 4 meses con compra antes de M); la
    línea base se mide en la MISMA población. MAPE en los pares que compraron en M."""
    salida: dict[str, dict[str, _Conteo]] = {}
    for mes in meses:
        por_nivel = {n: _Conteo() for n in NIVELES}
        for (_, nivel, item), s in series.items():
            c = por_nivel[nivel]
            real = s.valores.get(mes, 0.0)
            alim = es_alimento(nivel, item)
            if alim:
                c.monto_alim += s.montos.get(mes, 0.0)
            f = pronosticar(s.valores, mes, [mes])
            if not f or f[0].metodo != "v1":
                continue
            p = f[0]
            c.elegibles += 1
            if alim:
                c.monto_alim_cubierto += s.montos.get(mes, 0.0)
            pred, base, compro = (p.p_compra or 0.0) >= P_COMPRA, p.compra_base, real > 0
            c.tp += pred and compro
            c.fp += pred and not compro
            c.fn += compro and not pred
            c.tp_base += base and compro
            c.fp_base += base and not compro
            c.fn_base += compro and not base
            if compro and p.cantidad is not None:
                c.pares_mape += 1
                c.suma_ape += abs(p.cantidad - real) / real
                c.suma_ape_base += abs((p.cantidad_linea_base or 0.0) - real) / real
        salida[mes] = por_nivel
    return salida


_METRICAS = ("precision", "recall", "precision_base", "recall_base", "mape", "mape_base",
             "cobertura")  # fmt: skip


def guardar_backtest(
    session: Session, resultado: Mapping[str, Mapping[str, _Conteo]], ahora: datetime
) -> None:
    """`evaluacion_modelo` (modelo `demanda`): borra solo las filas de ese modelo y esos meses."""
    for mes, por_nivel in resultado.items():
        session.execute(
            delete(EvaluacionModelo).where(
                EvaluacionModelo.mes == mes, EvaluacionModelo.modelo == MODELO_DEMANDA
            )
        )
        filas = []
        for nivel, c in por_nivel.items():
            met = c.metricas()
            detalle = {**{k: getattr(c, k) for k in c.__dataclass_fields__}, "nivel": nivel}
            for nombre in _METRICAS:
                v = met[nombre]
                filas.append({
                    "mes": mes, "modelo": MODELO_DEMANDA, "metrica": f"{nombre}_{nivel}",
                    "valor": None if v is None else round(v, 6),
                    "n": c.pares_mape if nombre.startswith("mape") else c.elegibles,
                    "detalle": detalle, "empresa_id": None, "calculado_en": ahora,
                })  # fmt: skip
        insertar_por_lotes(session, EvaluacionModelo, filas)
        session.commit()
        _log.info("demanda backtest %s guardado (%d métricas)", mes, len(filas))


@dataclass(frozen=True)
class Veredicto:
    nivel: str
    meses: tuple[str, ...]
    elegibles: int
    metricas: dict[str, float | None]

    @property
    def cumple_meta(self) -> bool:
        return _cumple(self.metricas.get("precision"), self.metricas.get("recall"))

    @property
    def base_cumple_meta(self) -> bool:
        return _cumple(self.metricas.get("precision_base"), self.metricas.get("recall_base"))

    @property
    def gana(self) -> bool:
        """El v1 se usa solo si cumple la meta (precisión ≥ 70 % y recall ≥ 50 %) Y le gana a la
        línea base en la misma población: la base no cumple la meta, o la cumple con menor F1.
        Si no, la UI muestra la línea base y lo dice."""
        if not self.cumple_meta:
            return False
        f1, f1b = self.metricas.get("f1"), self.metricas.get("f1_base")
        return not self.base_cumple_meta or (f1 is not None and (f1b is None or f1 > f1b))


def _cumple(p: float | None, r: float | None) -> bool:
    return p is not None and r is not None and p >= META_PRECISION and r >= META_RECALL


def veredicto(session: Session, meses: int = MESES_BACKTEST) -> dict[str, Veredicto]:
    """Backtest agregado de los últimos `meses` meses evaluados, por nivel (sumando conteos)."""
    filas = session.execute(
        select(EvaluacionModelo.mes, EvaluacionModelo.detalle).where(
            EvaluacionModelo.modelo == MODELO_DEMANDA,
            EvaluacionModelo.metrica.like("precision\\_%", escape="\\"),
            EvaluacionModelo.metrica.notlike("precision\\_base%", escape="\\"),
        )
    ).all()
    ultimos = sorted({m for m, _ in filas})[-meses:]
    acum = {n: _Conteo() for n in NIVELES}
    for mes, det in filas:
        if mes in ultimos and det and det.get("nivel") in acum:
            acum[det["nivel"]].sumar(det)
    return {n: Veredicto(n, tuple(ultimos), c.elegibles, c.metricas()) for n, c in acum.items()}


# ---------------------------------------------------------------------------
# Job (sin recomendaciones: las agrega `abastecimiento.run_mercado_demanda`)
# ---------------------------------------------------------------------------


def medir_bytes(session: Session) -> dict[str, Any] | None:
    """Tamaño (tabla + índices) y bytes por fila de las tablas de demanda (regla 11)."""
    if session.get_bind().dialect.name != "postgresql":
        return None
    out: dict[str, Any] = {}
    for tabla in TABLAS:
        tam, filas = session.execute(
            text(f"SELECT pg_total_relation_size('{tabla}'), (SELECT count(*) FROM {tabla})")
        ).one()  # nombres fijos de este módulo, no entrada de usuario
        out[tabla] = {"bytes": int(tam), "filas": int(filas),
                      "bytes_por_fila": round(tam / filas) if filas else None}  # fmt: skip
    return out


def run_demanda(
    session: Session,
    regiones: list[int],
    *,
    completo: bool = False,
    mes_hoy: str | None = None,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """Medición, series (3 últimos meses, o todo con `completo` o si cambió la clave), nombres,
    backtest de 6 meses y pronósticos a 3 meses. Sin API."""
    t0 = time.monotonic()
    resultado: dict[str, Any] = {"regiones": regiones, "completo": completo}
    if not regiones:
        return {**resultado, "sin_regiones": 1}
    ahora = ahora or ahora_utc()
    hoy = mes_hoy or mes_de(ahora) or mes_actual()
    avisos: list[str] = []
    etapas = Etapas(resultado, "demanda")
    _log.info("demanda: iniciando (regiones=%s, completo=%s)", regiones, completo)
    med, dir_ = recorrer(session, regiones, hoy)
    etapas.fin("recorrido")
    modo = med.modo()
    resultado["medicion"] = med.como_dict()
    if med.lineas_oc and not med.cas_con_oc:
        avisos.append("cruce CA↔OC no disponible: ninguna CA cruzó con su OC (se cruza el `ID` de "
                      "la OC con `oc_lineas_ca.id_oc`: ¿falta la recarga `mercado-oc --forzar`?); "
                      "(c) queda en None y la clave en RUT")  # fmt: skip
    guardado = modo_guardado(session, regiones)
    cambio_clave = guardado is not None and guardado != modo
    if cambio_clave:
        avisos.append(f"la clave del comprador pasó de «{guardado}» a «{modo}»: se recalcula todo")
    if completo or cambio_clave or guardado is None:
        desde = min(med.primer_mes or hoy, sumar_meses(hoy, -(MESES_DIARIO - 1)))
    else:
        desde = sumar_meses(hoy, -(MESES_DIARIO - 1))
    productos = Productos(session)
    _log.info("demanda: clave %s, series de %s a %s", modo, desde, hoy)
    resultado["series"] = recalcular_series(
        session, regiones, desde, hoy, modo, dir_, ahora=ahora, productos=productos
    )
    etapas.fin("series")
    e = resultado["series"]["e_pct_cantidad_coincide"]
    if e is not None and e < 0.99:
        avisos.append(f"cantidad en base: total/precio_base coincide solo en {e:.1%} de las líneas")
    resultado["nombres"] = guardar_nombres(session, regiones, dir_, modo, productos)
    etapas.fin("nombres", f"{resultado['nombres']} filas")
    series = cargar_series(session, regiones)
    etapas.fin("carga_series", f"{len(series)} series")
    con_datos = {m for s in series.values() for m, v in s.valores.items() if v > 0}
    meses_bt = [m for m in (sumar_meses(hoy, -k) for k in range(MESES_BACKTEST, 0, -1))
                if m in con_datos]  # fmt: skip
    bt = backtest(series, meses_bt)
    guardar_backtest(session, bt, ahora)
    etapas.fin("backtest", f"{len(meses_bt)} meses")
    ver = veredicto(session)
    resultado["backtest"] = {
        n: {"meses": list(v.meses), "elegibles": v.elegibles,
            **{k: None if x is None else round(x, 4) for k, x in v.metricas.items()},
            "cumple_meta": v.cumple_meta, "base_cumple_meta": v.base_cumple_meta,
            "gana_a_linea_base": v.gana}
        for n, v in ver.items()
    }  # fmt: skip
    for n, v in ver.items():
        if v.elegibles and not v.cumple_meta:
            avisos.append(f"demanda ({n}): el v1 no cumple la meta (precisión ≥ 70 %, recall "
                          "≥ 50 %); la UI muestra la línea base")  # fmt: skip
        elif v.elegibles and not v.gana:
            avisos.append(f"demanda ({n}): el v1 no le gana a la línea base; la UI muestra la base")
    resultado["pronosticos"] = guardar_pronosticos(session, series, regiones, hoy, ahora)
    etapas.fin("pronosticos", f"{resultado['pronosticos']['filas']} filas")
    resultado["bytes"] = medir_bytes(session)
    if resultado["bytes"]:
        total = sum(t["bytes"] for t in resultado["bytes"].values())
        if total > MAX_BYTES:
            avisos.append(
                f"demanda ocupa {total / 1e6:.1f} MB > {MAX_BYTES / 1e6:.0f} MB (regla 11)"
            )
    resultado["avisos"] = avisos
    resultado["duracion_s"] = round(time.monotonic() - t0, 1)
    return resultado


def series_de(
    session: Session, regiones: Iterable[int], *, nivel: str | None = None,
    item: str | None = None, comprador: str | None = None, desde: str | None = None,
) -> list[DemandaSerie]:  # fmt: skip
    """Filas de `demanda_series` acotadas (para la UI)."""
    cond: list[Any] = [DemandaSerie.region.in_(list(regiones))]
    if nivel is not None:
        cond.append(DemandaSerie.nivel == nivel)
    if item is not None:
        cond.append(DemandaSerie.item == item)
    if comprador is not None:
        cond.append(DemandaSerie.comprador == comprador)
    if desde is not None:
        cond.append(DemandaSerie.mes >= desde)
    return list(session.execute(select(DemandaSerie).where(*cond)).scalars())
