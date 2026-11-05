"""Análisis de competencia (F-plat-comp): quién gana qué, a qué precio relativo, con quién y dónde.

- **Portable a licitaciones (§1.5):** el cálculo recibe procesos genéricos (`Proceso` con sus
  `Oferta`s: proceso, comprador, familia, mes, rut, precio, ganó) desde un adaptador
  (`_filas_desde_ca`). Licitaciones solo tendrá que escribir otro adaptador; no se piden a la API
  (regla 24).
- **Reglas:** elección F-merc-1c (`eleccion.elegir_por_rut`: una oferta por RUT; n.º de oferentes
  = RUT distintos); `seleccionado` nulo no es derrota (`n_sin_dato`); el `log_ratio` solo existe en
  CA con ≥ 2 oferentes con precio; CA desiertas o canceladas (o leídas sin oferentes) cuentan para
  el comprador, no para las tasas de victoria.
- **Claves:** comprador = `comprador.clave_unidad` (la del modelo y la mesa); familia =
  `adjudicacion.familia_de_ca` (la de la mesa); RUT = `comprador.rut_competidor`.
- **Esquema:** `competencia_mes` (RUT × comprador × familia × mes) y `competencia_comprador_mes`
  (comprador × familia × mes). El job `mercado-competencia` borra y reinserta los meses que toca
  (idempotente), por lotes de `LOTE` CA (regla 12). Las consultas de la UI leen SOLO esas tablas:
  ningún request recorre `ca_cotizaciones`.
- **Modelo (§1.3):** `variables_competencia` se calcula con meses ANTERIORES al de la CA y queda
  registrada como grupo `competencia` en `adjudicacion.VARIABLES`; entra al modelo solo si mejora
  el Brier en el walk-forward de F-plat-4 (mismos meses elegibles; sin evaluación paralela).

Sin API ni red. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import logging
import math
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from operator import attrgetter
from typing import Any

from sqlalchemy import delete, func, insert, or_, select, text
from sqlalchemy.orm import Session

from app.core.tiempo import ahora_utc
from app.mercado.adjudicacion import (
    MESES_ENTRENAMIENTO,
    VARIABLES_BASE,
    ContextoOferta,
    brier,
    cargar_dataset,
    entrenar_logistico,
    familia_de_ca,
    registrar_variable,
)
from app.mercado.comprador import clave_unidad, rut_competidor
from app.mercado.eleccion import elegir_por_rut
from app.mercado.evaluacion import (
    MESES_EVALUADOS,
    MIN_CA_MES,
    MODELO_COMPETENCIA,
    cas_de_entrenamiento,
    elegibilidad,
)
from app.mercado.indice import (
    LOTE,
    _rut,
    mes_actual,
    mes_de,
    meses_con_cotizaciones,
    meses_entre,
    rango_mes_utc,
    sumar_meses,
)
from app.models.tables import (
    CaCotizacion,
    CaProducto,
    CompetenciaCompradorMes,
    CompetenciaMes,
    CompraAgil,
    Empresa,
    EvaluacionModelo,
    OcLineaCa,
)

_log = logging.getLogger(__name__)

ESTADOS_DESIERTA = frozenset({"desierta", "cancelada"})
MESES_UI = 12
VENTANA_HISTORIAL = 12  # meses ANTERIORES al de la CA para competidores esperados
TOP_ESPERADOS = 3
META_TOP1 = 0.60  # el top 1 esperado oferta en ≥ 60 % de las CA de su comprador × familia
TOLERANCIA_CUADRATURA = 0.005
LIMITE_LISTA = 300
# §1.1: si las dos tablas pasan de 20 MB, agregar por trimestre. Medido el 5-nov con volumen
# sintético: 241 B/fila en `competencia_mes` tras VACUUM (ver commit F-plat-comp).
MAX_BYTES = 20_000_000
_Z95 = 1.96


# ---------------------------------------------------------------------------
# Procesos genéricos (§1.5) y adaptador de Compra Ágil
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Oferta:
    rut: str
    nombre: str | None
    es_emt: bool | None
    precio: float | None  # neto; None = sin precio utilizable
    gano: bool | None  # None = la API no lo dijo


@dataclass(frozen=True)
class Proceso:
    proceso: str
    comprador: str  # clave de comprador
    organismo_rut: str
    comprador_nombre: str | None
    region: int | None
    familia: str
    mes: str
    desierta: bool  # desierta o cancelada
    leido: bool  # sus ofertas ya se leyeron (aunque vinieran vacías)
    ofertas: tuple[Oferta, ...]


@dataclass
class ConteoRuts:
    """RUT de oferentes vistos: cuántos no son válidos y cuántos cambian de forma entre
    `indice._rut` (forma laxa, la de la clave de comprador) y `proveedores.normalizar_rut`."""

    vistos: set[str] = field(default_factory=set)
    no_validos: set[str] = field(default_factory=set)
    forma_distinta: set[str] = field(default_factory=set)
    ofertas_sin_rut: int = 0

    def registrar(self, crudo: str | None) -> str:
        rut, valido = rut_competidor(crudo)
        if not rut:
            self.ofertas_sin_rut += 1
            return ""
        self.vistos.add(rut)
        if not valido:
            self.no_validos.add(rut)
        elif rut != _rut(crudo):
            self.forma_distinta.add(rut)
        return rut

    def como_dict(self) -> dict[str, int]:
        return {
            "ruts_vistos": len(self.vistos),
            "ruts_no_validos": len(self.no_validos),
            "ruts_forma_distinta": len(self.forma_distinta),
            "ofertas_sin_rut": self.ofertas_sin_rut,
        }


def _nombre_comprador(organismo: str | None, unidad: str | None) -> str | None:
    partes = [" ".join(t.split()) for t in (organismo, unidad) if t and t.strip()]
    return " · ".join(partes)[:600] if partes else None


def _filas_desde_ca(
    session: Session, regiones: list[int], mes: str, ahora: datetime, conteo: ConteoRuts
) -> Iterator[list[Proceso]]:
    """Adaptador de Compra Ágil: CA de `regiones` cerradas en `mes` (hora de Chile) antes de
    `ahora`, por lotes de `LOTE`, como `Proceso`s genéricos. Ofertas sin RUT: fuera (se cuentan)."""
    ini, fin = rango_mes_utc(mes)
    fin = min(fin, ahora)
    ultimo = ""
    while True:
        cas = session.execute(
            select(
                CompraAgil.codigo, CompraAgil.organismo_rut, CompraAgil.organismo_nombre,
                CompraAgil.unidad_compra, CompraAgil.region, CompraAgil.estado,
                CompraAgil.motivo_desierta, CompraAgil.cotizaciones_obtenidas_en,
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
        codigos = [c[0] for c in cas]
        ofertas: dict[str, list[Oferta]] = defaultdict(list)
        for ca, rut, nombre, emt, sel, valor in session.execute(
            select(
                CaCotizacion.ca_codigo, CaCotizacion.rut_proveedor, CaCotizacion.razon_social,
                CaCotizacion.es_emt, CaCotizacion.seleccionado, CaCotizacion.valor_neto,
            )
            .where(CaCotizacion.ca_codigo.in_(codigos))
            .order_by(CaCotizacion.ca_codigo, CaCotizacion.id)
        ):  # fmt: skip
            r = conteo.registrar(rut)
            if r:
                precio = float(valor) if valor is not None and valor > 0 else None
                ofertas[ca].append(Oferta(r, nombre, emt, precio, sel))
        productos: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
        for ca, cod, nombre in session.execute(
            select(CaProducto.ca_codigo, CaProducto.codigo_producto, CaProducto.nombre).where(
                CaProducto.ca_codigo.in_(codigos)
            )
        ):
            productos[ca].append((cod, nombre))
        yield [
            Proceso(
                proceso=codigo, comprador=clave_unidad(org, unidad), organismo_rut=_rut(org),
                comprador_nombre=_nombre_comprador(org_nombre, unidad), region=region,
                familia=familia_de_ca(productos.get(codigo, ())), mes=mes,
                desierta=(estado or "") in ESTADOS_DESIERTA or bool(motivo),
                leido=leidas is not None, ofertas=tuple(ofertas.get(codigo, ())),
            )
            for codigo, org, org_nombre, unidad, region, estado, motivo, leidas in cas
        ]  # fmt: skip


# ---------------------------------------------------------------------------
# Agregación (genérica: no sabe nada de Compra Ágil)
# ---------------------------------------------------------------------------


def elegidas_de(p: Proceso) -> dict[str, Oferta]:
    """Una oferta por RUT (regla F-merc-1c)."""
    return elegir_por_rut(p.ofertas, attrgetter("rut"), attrgetter("gano"), attrgetter("precio"))


@dataclass
class _AggRut:
    organismo_rut: str
    region: int | None
    razon_social: str | None = None
    es_emt: bool | None = None
    n_ca: int = 0
    n_ofertas: int = 0
    n_ganadas: int = 0
    n_sin_dato: int = 0
    log_ratios: list[float] = field(default_factory=list)
    monto: float = 0.0
    n_un: int = 0


@dataclass
class _AggComprador:
    organismo_rut: str
    nombre: str | None
    region: int | None
    n_ca: int = 0
    n_desiertas: int = 0
    oferentes: list[int] = field(default_factory=list)
    n_un: int = 0
    n_con_ganador: int = 0
    n_2mas_con_ganador: int = 0
    n_no_gano_mas_barato: int = 0
    monto: float = 0.0


Mercado = tuple[str, str]  # (clave_comprador, familia)


@dataclass
class Agregado:
    """Agregado de un mes: por (RUT, comprador, familia) y por (comprador, familia)."""

    por_rut: dict[tuple[str, str, str], _AggRut] = field(default_factory=dict)
    por_comprador: dict[Mercado, _AggComprador] = field(default_factory=dict)

    def acumular(self, p: Proceso) -> None:
        clave = (p.comprador, p.familia)
        c = self.por_comprador.get(clave)
        if c is None:
            c = self.por_comprador[clave] = _AggComprador(p.organismo_rut, p.comprador_nombre,
                                                          p.region)  # fmt: skip
        c.n_ca += 1
        if c.nombre is None:
            c.nombre = p.comprador_nombre
        elegidas = elegidas_de(p)
        if p.desierta or (p.leido and not elegidas):
            c.n_desiertas += 1  # sin oferentes o desierta/cancelada: fuera de las tasas
            return
        if not elegidas:
            return  # ofertas aún no leídas: no se sabe nada más
        n = len(elegidas)
        c.oferentes.append(n)
        c.n_un += n == 1
        ganadores = [o for o in elegidas.values() if o.gano is True]
        precios = [o.precio for o in elegidas.values() if o.precio is not None]
        if ganadores:
            c.n_con_ganador += 1
            c.monto += sum(o.precio or 0.0 for o in ganadores)
            if n >= 2 and len(precios) >= 2 and all(g.precio is not None for g in ganadores):
                c.n_2mas_con_ganador += 1
                if min(g.precio or 0.0 for g in ganadores) > min(precios) * (1 + 1e-9):
                    c.n_no_gano_mas_barato += 1
        mediana = statistics.median(precios) if n >= 2 and len(precios) >= 2 else None
        filas_por_rut = Counter(o.rut for o in p.ofertas)
        for rut, o in elegidas.items():
            a = self.por_rut.get((rut, p.comprador, p.familia))
            if a is None:
                a = self.por_rut[(rut, p.comprador, p.familia)] = _AggRut(p.organismo_rut,
                                                                          p.region)  # fmt: skip
            a.n_ca += 1
            a.n_ofertas += filas_por_rut[rut]
            if o.nombre:
                a.razon_social = " ".join(o.nombre.split())[:300]
            if o.es_emt is not None:
                a.es_emt = o.es_emt
            if o.gano is True:
                a.n_ganadas += 1
                a.monto += o.precio or 0.0
            elif o.gano is None:
                a.n_sin_dato += 1
            a.n_un += n == 1
            if mediana and mediana > 0 and o.precio is not None:
                a.log_ratios.append(math.log(o.precio / mediana))

    def filas(
        self, mes: str, ubicaciones: dict[str, tuple[str | None, int | None]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        filas_rut = []
        for (rut, comprador, familia), a in self.por_rut.items():
            comuna, region_prov = ubicaciones.get(rut, (None, None))
            filas_rut.append({
                "mes": mes, "region": a.region, "rut": rut, "clave_comprador": comprador,
                "organismo_rut": a.organismo_rut, "familia": familia,
                "razon_social": a.razon_social, "es_emt": a.es_emt, "comuna_proveedor": comuna,
                "region_proveedor": region_prov, "n_ca": a.n_ca, "n_ofertas": a.n_ofertas,
                "n_ganadas": a.n_ganadas, "n_sin_dato": a.n_sin_dato,
                "suma_log_ratio": sum(a.log_ratios), "n_log_ratio": len(a.log_ratios),
                "p50_log_ratio": statistics.median(a.log_ratios) if a.log_ratios else None,
                "monto_ganado": a.monto, "n_ca_un_oferente": a.n_un,
            })  # fmt: skip
        filas_comp = [
            {
                "mes": mes, "region": c.region, "clave_comprador": comprador,
                "organismo_rut": c.organismo_rut, "comprador_nombre": c.nombre,
                "familia": familia, "n_ca": c.n_ca, "n_desiertas": c.n_desiertas,
                "n_con_ofertas": len(c.oferentes), "suma_oferentes": sum(c.oferentes),
                "p50_oferentes": float(statistics.median(c.oferentes)) if c.oferentes else None,
                "n_un_oferente": c.n_un, "n_con_ganador": c.n_con_ganador,
                "n_2mas_con_ganador": c.n_2mas_con_ganador,
                "n_no_gano_mas_barato": c.n_no_gano_mas_barato, "monto_adjudicado": c.monto,
            }
            for (comprador, familia), c in self.por_comprador.items()
        ]  # fmt: skip
        return filas_rut, filas_comp


# ---------------------------------------------------------------------------
# Recálculo (job)
# ---------------------------------------------------------------------------


def mapa_ubicacion_proveedor(session: Session) -> dict[str, tuple[str | None, int | None]]:
    """RUT (forma de `rut_competidor`) → (comuna, región) más frecuente en `oc_lineas_ca`."""
    cuenta: dict[str, Counter[tuple[str | None, int | None]]] = defaultdict(Counter)
    for rut, comuna, region in session.execute(
        select(OcLineaCa.rut_proveedor, OcLineaCa.comuna_proveedor, OcLineaCa.region_proveedor)
        .where(OcLineaCa.rut_proveedor.isnot(None))
        .execution_options(yield_per=LOTE)
    ):
        r = rut_competidor(rut)[0]
        if r and (comuna or region is not None):
            nombre = " ".join(comuna.split()).title()[:100] if comuna else None
            cuenta[r][(nombre, int(region) if region is not None else None)] += 1
    return {
        r: sorted(c.items(), key=lambda kv: (-kv[1], str(kv[0])))[0][0] for r, c in cuenta.items()
    }


def cas_con_seleccionada(session: Session, mes: str, regiones: list[int], ahora: datetime) -> int:
    """Juez de cuadratura: CA del mes con alguna oferta seleccionada, contadas en SQL directo."""
    ini, fin = rango_mes_utc(mes)
    return int(
        session.execute(
            select(func.count(func.distinct(CaCotizacion.ca_codigo)))
            .join(CompraAgil, CompraAgil.codigo == CaCotizacion.ca_codigo)
            .where(
                CaCotizacion.seleccionado.is_(True), CompraAgil.region.in_(regiones),
                CompraAgil.fecha_cierre >= ini, CompraAgil.fecha_cierre < min(fin, ahora),
            )
        ).scalar_one()
    )  # fmt: skip


def recalcular_competencia(
    session: Session,
    desde_mes: str,
    regiones: list[int],
    *,
    hasta_mes: str | None = None,
    ahora: datetime | None = None,
    ubicaciones: dict[str, tuple[str | None, int | None]] | None = None,
) -> dict[str, Any]:
    """Reescribe `competencia_mes` y `competencia_comprador_mes` de `desde_mes` a `hasta_mes`
    (borra y reinserta; commit por mes). Idempotente. Devuelve conteos y la cuadratura por mes."""
    ahora = ahora or ahora_utc()
    hasta_mes = hasta_mes or mes_de(ahora) or mes_actual()
    ubicaciones = mapa_ubicacion_proveedor(session) if ubicaciones is None else ubicaciones
    conteo = ConteoRuts()
    total: dict[str, Any] = {"meses": [], "filas_rut": 0, "filas_comprador": 0, "procesos": 0,
                             "cuadratura": []}  # fmt: skip
    for mes in meses_entre(desde_mes, hasta_mes):
        agg = Agregado()
        for lote in _filas_desde_ca(session, regiones, mes, ahora, conteo):
            for p in lote:
                agg.acumular(p)
                total["procesos"] += 1
        filas_rut, filas_comp = agg.filas(mes, ubicaciones)
        session.execute(delete(CompetenciaMes).where(CompetenciaMes.mes == mes))
        session.execute(delete(CompetenciaCompradorMes).where(CompetenciaCompradorMes.mes == mes))
        for i in range(0, len(filas_rut), LOTE):
            session.execute(insert(CompetenciaMes), filas_rut[i : i + LOTE])
        for i in range(0, len(filas_comp), LOTE):
            session.execute(insert(CompetenciaCompradorMes), filas_comp[i : i + LOTE])
        session.commit()
        ganadas = sum(f["n_ganadas"] for f in filas_rut)
        directo = cas_con_seleccionada(session, mes, regiones, ahora)
        desvio = abs(ganadas - directo) / directo if directo else (0.0 if not ganadas else 1.0)
        total["cuadratura"].append({"mes": mes, "n_ganadas": ganadas, "cas_con_seleccionada": directo,
                                    "desvio": round(desvio, 5),
                                    "cumple": desvio <= TOLERANCIA_CUADRATURA})  # fmt: skip
        total["meses"].append(mes)
        total["filas_rut"] += len(filas_rut)
        total["filas_comprador"] += len(filas_comp)
    total.update(conteo.como_dict())
    _log.info("competencia_mes: %s", total)
    return total


def medir_bytes(session: Session) -> dict[str, Any] | None:
    """Tamaño total (tabla + índices) y bytes por fila de las dos tablas (regla 11). Solo Postgres."""
    if session.get_bind().dialect.name != "postgresql":
        return None
    out: dict[str, Any] = {}
    for tabla in ("competencia_mes", "competencia_comprador_mes"):
        tam, filas = session.execute(
            text(f"SELECT pg_total_relation_size('{tabla}'), (SELECT count(*) FROM {tabla})")
        ).one()  # nombres fijos de este módulo, no entrada de usuario
        out[tabla] = {"bytes": int(tam), "filas": int(filas),
                      "bytes_por_fila": round(tam / filas) if filas else None}  # fmt: skip
    return out


# ---------------------------------------------------------------------------
# Estadística chica
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tasa:
    """k de n con intervalo de Wilson al 95 % (n chico: el intervalo lo dice)."""

    k: int
    n: int
    bajo: float | None
    alto: float | None

    @property
    def p(self) -> float | None:
        return self.k / self.n if self.n else None


def wilson(k: int, n: int, z: float = _Z95) -> Tasa:
    if n <= 0:
        return Tasa(k, 0, None, None)
    p = k / n
    d = 1 + z * z / n
    centro = (p + z * z / (2 * n)) / d
    margen = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return Tasa(k, n, max(0.0, centro - margen), min(1.0, centro + margen))


def mediana_ponderada(pares: Iterable[tuple[float | None, float]]) -> float | None:
    """Mediana de valores con peso (la del mes ponderada por su n): aproximación de la mediana de
    varios meses sin guardar cada observación."""
    lista = sorted((v, w) for v, w in pares if v is not None and w > 0)
    tot = sum(w for _, w in lista)
    if not tot:
        return None
    acum = 0.0
    for v, w in lista:
        acum += w
        if acum >= tot / 2:
            return v
    return lista[-1][0]


def pct_relativo(log_ratio: float | None) -> float | None:
    """log(precio/mediana) → fracción sobre (+) o bajo (−) la mediana de la CA."""
    return None if log_ratio is None else math.exp(log_ratio) - 1.0


# ---------------------------------------------------------------------------
# Historial en memoria: competidores esperados y variables del modelo (sin fuga)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Esperado:
    rut: str
    n_ca: int
    n_ganadas: int
    p50_log_ratio: float | None


class HistorialCompetencia:
    """`competencia_mes` y la tasa de un oferente de `competencia_comprador_mes`, por mes.

    Toda consulta usa SOLO los `VENTANA_HISTORIAL` meses anteriores al mes pedido: nunca el mes
    de la CA ni posteriores (sin fuga temporal)."""

    def __init__(
        self,
        mercado: dict[Mercado, dict[str, list[tuple[str, int, int, float | None, int]]]],
        un_oferente: dict[str, dict[str, tuple[int, int]]],
    ) -> None:
        self.mercado = mercado
        self.un_oferente = un_oferente

    @classmethod
    def cargar(
        cls,
        session: Session,
        desde_mes: str,
        hasta_mes: str,
        *,
        regiones: list[int] | None = None,
        claves: Sequence[str] | None = None,
    ) -> HistorialCompetencia:
        cond_r: list[Any] = [CompetenciaMes.mes >= desde_mes, CompetenciaMes.mes <= hasta_mes]
        cond_c: list[Any] = [CompetenciaCompradorMes.mes >= desde_mes,
                             CompetenciaCompradorMes.mes <= hasta_mes]  # fmt: skip
        if regiones is not None:
            cond_r.append(CompetenciaMes.region.in_(regiones))
            cond_c.append(CompetenciaCompradorMes.region.in_(regiones))
        if claves is not None:
            cond_r.append(CompetenciaMes.clave_comprador.in_(list(claves)))
            cond_c.append(CompetenciaCompradorMes.clave_comprador.in_(list(claves)))
        mercado: dict[Mercado, dict[str, list[tuple[str, int, int, float | None, int]]]] = (
            defaultdict(lambda: defaultdict(list))
        )
        for clave, fam, mes, rut, n_ca, n_g, p50, n_lr in session.execute(
            select(
                CompetenciaMes.clave_comprador, CompetenciaMes.familia, CompetenciaMes.mes,
                CompetenciaMes.rut, CompetenciaMes.n_ca, CompetenciaMes.n_ganadas,
                CompetenciaMes.p50_log_ratio, CompetenciaMes.n_log_ratio,
            ).where(*cond_r).execution_options(yield_per=LOTE)
        ):  # fmt: skip
            mercado[(clave, fam)][mes].append((rut, n_ca, n_g, p50, n_lr))
        un: dict[str, dict[str, tuple[int, int]]] = defaultdict(dict)
        for clave, mes, n_un, n_con in session.execute(
            select(
                CompetenciaCompradorMes.clave_comprador,
                CompetenciaCompradorMes.mes,
                func.sum(CompetenciaCompradorMes.n_un_oferente),
                func.sum(CompetenciaCompradorMes.n_con_ofertas),
            )
            .where(*cond_c)
            .group_by(CompetenciaCompradorMes.clave_comprador, CompetenciaCompradorMes.mes)
        ):
            un[clave][mes] = (int(n_un or 0), int(n_con or 0))
        return cls(mercado, un)

    @staticmethod
    def _ventana(mes: str) -> list[str]:
        return [sumar_meses(mes, -k) for k in range(VENTANA_HISTORIAL, 0, -1)]

    def esperados(
        self, clave: str, familia: str, mes: str, k: int = TOP_ESPERADOS
    ) -> list[Esperado]:
        """Top `k` por participación (CA en que ofertó) en el comprador × familia, con los meses
        anteriores a `mes`. Empate: más CA ganadas, luego RUT."""
        por_mes = self.mercado.get((clave, familia), {})
        n_ca: Counter[str] = Counter()
        n_g: Counter[str] = Counter()
        lr: dict[str, list[tuple[float | None, float]]] = defaultdict(list)
        for m in self._ventana(mes):
            for rut, n, g, p50, n_lr in por_mes.get(m, ()):
                n_ca[rut] += n
                n_g[rut] += g
                lr[rut].append((p50, float(n_lr)))
        orden = sorted(n_ca, key=lambda r: (-n_ca[r], -n_g[r], r))[:k]
        return [Esperado(r, n_ca[r], n_g[r], mediana_ponderada(lr[r])) for r in orden]

    def tasa_un_oferente(self, clave: str, mes: str) -> tuple[int, int]:
        por_mes = self.un_oferente.get(clave, {})
        pares = [por_mes[m] for m in self._ventana(mes) if m in por_mes]
        return sum(u for u, _ in pares), sum(c for _, c in pares)

    def variables(self, clave: str, familia: str, mes: str) -> dict[str, float]:
        """Variables de competencia de una CA de `mes` (calculables antes del cierre)."""
        top = self.esperados(clave, familia, mes)
        un, con = self.tasa_un_oferente(clave, mes)
        if not top and not con:
            return {"comp_sin_historial:si": 1.0}
        agresividad = [e.p50_log_ratio for e in top if e.p50_log_ratio is not None]
        return {
            "comp_n_habituales": float(len(top)),
            "comp_agresividad": float(statistics.median(agresividad)) if agresividad else 0.0,
            "comp_tasa_un_oferente": un / con if con else 0.0,
        }


def _extractor_competencia(c: ContextoOferta) -> dict[str, float]:
    """Lee lo que dejó `variables_competencia` en `c.extra`; sin dato, solo la marca."""
    v = c.extra.get("competencia")
    return dict(v) if v else {"comp_sin_historial:si": 1.0}


registrar_variable("competencia", _extractor_competencia)
VARIABLES_CON_COMPETENCIA: tuple[str, ...] = (*VARIABLES_BASE, "competencia")


def variables_competencia(session: Session, ca: CompraAgil, familia: str) -> dict[str, float]:
    """Variables de competencia de UNA CA (antes del cierre): competidores habituales esperados
    del comprador × familia, su agresividad y la tasa de un oferente del comprador, con los 12
    meses anteriores al de la CA (sin el mes de la CA)."""
    mes = mes_de(ca.fecha_publicacion) or mes_de(ca.fecha_cierre) or mes_actual()
    clave = clave_unidad(ca.organismo_rut, ca.unidad_compra)
    hist = HistorialCompetencia.cargar(
        session, sumar_meses(mes, -VENTANA_HISTORIAL), sumar_meses(mes, -1), claves=[clave]
    )
    return hist.variables(clave, familia, mes)


# ---------------------------------------------------------------------------
# Jueces
# ---------------------------------------------------------------------------


def _fila_eval(mes: str, metrica: str, valor: float | None, n: int,
               detalle: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    return {"mes": mes, "modelo": MODELO_COMPETENCIA, "metrica": metrica,
            "valor": None if valor is None else round(valor, 6), "n": n, "detalle": detalle,
            "empresa_id": None}  # fmt: skip


_METRICAS_JUEZ = ("top1_oferta", "precision_top3", "recall_top3")
_METRICAS_APORTE = ("brier_2mas_sin", "brier_2mas_con", "aporte_sin_datos")


def juez_competidores(
    session: Session,
    regiones: list[int],
    meses: Sequence[str],
    historial: HistorialCompetencia,
    ahora: datetime,
    mes_hoy: str,
) -> list[dict[str, Any]]:
    """Walk-forward de la predicción de competidores: para cada CA cerrada del mes M con ≥ 2
    oferentes, los top 3 esperados (datos < M) contra los que ofertaron. Escribe
    `evaluacion_modelo` (modelo `competencia`) y devuelve el resumen por mes."""
    resumen: list[dict[str, Any]] = []
    for mes in meses:
        n2 = con_pred = top1 = inter = n_pred = n_act = 0
        for lote in _filas_desde_ca(session, regiones, mes, ahora, ConteoRuts()):
            for p in lote:
                if p.desierta:
                    continue
                ofertaron = set(elegidas_de(p))
                if len(ofertaron) < 2:
                    continue
                n2 += 1
                top = [e.rut for e in historial.esperados(p.comprador, p.familia, mes)]
                if not top:
                    continue
                con_pred += 1
                top1 += top[0] in ofertaron
                k = len(set(top) & ofertaron)
                inter += k
                n_pred += len(top)
                n_act += len(ofertaron)
        tasa = top1 / con_pred if con_pred else None
        det = {"cas_2mas": n2, "con_prediccion": con_pred,
               "datos_insuficientes": n2 < MIN_CA_MES, "parcial": mes >= mes_hoy,
               "meta": META_TOP1,
               "cumple": None if tasa is None else tasa >= META_TOP1}  # fmt: skip
        session.execute(
            delete(EvaluacionModelo).where(
                EvaluacionModelo.mes == mes, EvaluacionModelo.modelo == MODELO_COMPETENCIA,
                EvaluacionModelo.metrica.in_(_METRICAS_JUEZ),
            )
        )  # fmt: skip
        filas = [
            _fila_eval(mes, "top1_oferta", tasa, con_pred, det),
            _fila_eval(mes, "precision_top3", inter / n_pred if n_pred else None, n_pred, det),
            _fila_eval(mes, "recall_top3", inter / n_act if n_act else None, n_act, det),
        ]
        session.execute(insert(EvaluacionModelo), filas)
        session.commit()
        resumen.append({"mes": mes, "top1_oferta": None if tasa is None else round(tasa, 4),
                        "precision_top3": filas[1]["valor"], "recall_top3": filas[2]["valor"],
                        **det})  # fmt: skip
    return resumen


def aporte_al_modelo(
    session: Session,
    regiones: list[int],
    meses_eval: Sequence[str],
    historial: HistorialCompetencia,
) -> dict[str, Any]:
    """Brier (≥ 2 oferentes) del logístico de F-plat-4 con y sin las variables de competencia, en
    el MISMO walk-forward (`evaluacion.elegibilidad`, `cas_de_entrenamiento`). Ambos números van a
    `evaluacion_modelo`. Sin meses elegibles: «pendiente», nunca una evaluación más laxa."""
    ultimo = meses_eval[-1]
    cas = cargar_dataset(
        session, regiones, sumar_meses(meses_eval[0], -MESES_ENTRENAMIENTO), ultimo
    )
    for c in cas:
        v = historial.variables(c.unidad, c.familia, c.mes)
        for f in c.filas:
            f.contexto.extra["competencia"] = v
    evaluados: list[dict[str, Any]] = []
    tot_sin = tot_con = 0.0
    n_tot = 0
    for mes in meses_eval:
        ok, info = elegibilidad(cas, mes)
        session.execute(
            delete(EvaluacionModelo).where(
                EvaluacionModelo.mes == mes, EvaluacionModelo.modelo == MODELO_COMPETENCIA,
                EvaluacionModelo.metrica.in_(_METRICAS_APORTE),
            )
        )  # fmt: skip
        filas_train = [f for c in cas_de_entrenamiento(cas, mes) for f in c.filas]
        test = [f for c in cas if c.mes == mes and c.n_ofertas >= 2 for f in c.filas]
        if not ok or not filas_train or not test:
            session.execute(insert(EvaluacionModelo), [_fila_eval(mes, "aporte_sin_datos", None,
                                                                  info["cas_2mas"], info)])  # fmt: skip
            session.commit()
            evaluados.append({"mes": mes, "estado": "sin_datos_suficientes", **info})
            continue
        sin = entrenar_logistico(filas_train)
        con = entrenar_logistico(filas_train, variables=VARIABLES_CON_COMPETENCIA)
        b_sin, n = brier((sin.p(f.contexto), f.gano) for f in test)
        b_con, _ = brier((con.p(f.contexto), f.gano) for f in test)
        session.execute(insert(EvaluacionModelo), [
            _fila_eval(mes, "brier_2mas_sin", b_sin, n, info),
            _fila_eval(mes, "brier_2mas_con", b_con, n, info),
        ])  # fmt: skip
        session.commit()
        if b_sin is not None and b_con is not None:
            tot_sin += b_sin * n
            tot_con += b_con * n
            n_tot += n
        evaluados.append({"mes": mes, "estado": "evaluado", "brier_sin": b_sin, "brier_con": b_con,
                          "n": n, **info})  # fmt: skip
    brier_sin = tot_sin / n_tot if n_tot else None
    brier_con = tot_con / n_tot if n_tot else None
    n_meses = sum(1 for e in evaluados if e["estado"] == "evaluado")
    return {
        "meses": evaluados,
        "meses_evaluables": n_meses,
        "brier_sin": None if brier_sin is None else round(brier_sin, 4),
        "brier_con": None if brier_con is None else round(brier_con, 4),
        "mejora": None if brier_sin is None or brier_con is None else brier_con < brier_sin,
        # Fuera del modelo en esta fase: sin meses evaluables no hay evidencia de mejora.
        "en_modelo": False,
        "estado": f"Brier con/sin: pendiente, {n_meses} meses evaluables"
        if not n_meses
        else "evaluado",
    }


# ---------------------------------------------------------------------------
# Job `mercado-competencia`
# ---------------------------------------------------------------------------


def run_competencia_mercado(
    session: Session,
    regiones: list[int],
    *,
    completo: bool = False,
    mes_hoy: str | None = None,
    ahora: datetime | None = None,
) -> dict[str, Any]:
    """Recalcula los 2 últimos meses (o todo con `completo`), corre el juez de predicción de
    competidores (últimos 6 meses), mide el aporte al modelo (último mes cerrado, o 6 con
    `completo`) y el tamaño de las tablas. Sin API."""
    t0 = time.monotonic()
    resultado: dict[str, Any] = {"regiones": regiones, "completo": completo}
    if not regiones:
        return {**resultado, "sin_regiones": 1}
    ahora = ahora or ahora_utc()
    hoy = mes_hoy or mes_de(ahora) or mes_actual()
    if completo:
        meses = meses_con_cotizaciones(session, regiones)
        desde = meses[0] if meses else sumar_meses(hoy, -1)
    else:
        desde = sumar_meses(hoy, -1)
    t1 = time.monotonic()
    resultado["recalculo"] = recalcular_competencia(
        session, desde, regiones, hasta_mes=hoy, ahora=ahora
    )
    resultado["segundos_recalculo"] = round(time.monotonic() - t1, 1)
    avisos: list[str] = resultado.setdefault("avisos", [])
    for c in resultado["recalculo"]["cuadratura"]:
        if not c["cumple"]:
            avisos.append(f"{c['mes']}: cuadratura de n_ganadas {c['n_ganadas']} vs "
                          f"{c['cas_con_seleccionada']} CA con oferta seleccionada "
                          f"(desvío {c['desvio']:.2%} > 0,5 %)")  # fmt: skip

    ultimo = sumar_meses(hoy, -1)
    meses_juez = [sumar_meses(hoy, -k) for k in range(MESES_EVALUADOS - 1, -1, -1)]
    meses_aporte = (
        [sumar_meses(ultimo, -k) for k in range(MESES_EVALUADOS - 1, -1, -1)]
        if completo
        else [ultimo]
    )
    desde_hist = sumar_meses(
        min(meses_juez[0], meses_aporte[0]), -(MESES_ENTRENAMIENTO + VENTANA_HISTORIAL)
    )
    historial = HistorialCompetencia.cargar(session, desde_hist, hoy, regiones=regiones)
    resultado["juez_competidores"] = juez_competidores(
        session, regiones, meses_juez, historial, ahora, hoy
    )
    for j in resultado["juez_competidores"]:
        if j["cumple"] is False and not j["datos_insuficientes"]:
            avisos.append(f"{j['mes']}: el top 1 esperado ofertó en {j['top1_oferta']:.1%} de las "
                          f"CA (meta {META_TOP1:.0%})")  # fmt: skip
    if not any(
        j["con_prediccion"] and not j["datos_insuficientes"] for j in resultado["juez_competidores"]
    ):
        avisos.append(
            "predicción de competidores: ningún mes con ≥ 50 CA de ≥ 2 oferentes e historial"
        )
    resultado["aporte_modelo"] = aporte_al_modelo(session, regiones, meses_aporte, historial)
    if resultado["aporte_modelo"]["mejora"]:
        avisos.append("las variables de competencia mejoran el Brier walk-forward: activarlas en el "
                      "modelo requiere llenar `extra['competencia']` en p_ganar (pendiente)")  # fmt: skip
    resultado["bytes"] = medir_bytes(session)
    if resultado["bytes"]:
        total_bytes = sum(t["bytes"] for t in resultado["bytes"].values())
        if total_bytes > MAX_BYTES:
            avisos.append(f"competencia ocupa {total_bytes / 1e6:.1f} MB > {MAX_BYTES / 1e6:.0f} MB "
                          "(regla 11): pasar a agregado trimestral")  # fmt: skip
    resultado["duracion_s"] = round(time.monotonic() - t0, 1)
    _log.info("mercado-competencia: %s", resultado)
    return resultado


# ---------------------------------------------------------------------------
# Consultas de la UI (solo `competencia_mes` y `competencia_comprador_mes`)
# ---------------------------------------------------------------------------


def _desde(meses: int, mes_hoy: str | None) -> tuple[str, str]:
    hoy = mes_hoy or mes_actual()
    return sumar_meses(hoy, -(meses - 1)), hoy


def rut_empresa(session: Session, empresa_id: int | None) -> str | None:
    """RUT de la empresa en la forma de `competencia_mes` («nosotros»); None si no tiene."""
    empresa = session.get(Empresa, empresa_id) if empresa_id else None
    if empresa is None or not empresa.rut:
        return None
    return rut_competidor(empresa.rut)[0] or None


def regiones_empresa(session: Session, empresa_id: int) -> list[int]:
    empresa = session.get(Empresa, empresa_id)
    out: list[int] = []
    for r in (empresa.regiones_prioritarias if empresa else []) or []:
        try:
            out.append(int(r))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def nombres_comprador(session: Session, claves: Iterable[str]) -> dict[str, str]:
    lista = sorted(set(claves))
    if not lista:
        return {}
    out: dict[str, str] = {}
    for clave, nombre in session.execute(
        select(
            CompetenciaCompradorMes.clave_comprador,
            func.max(CompetenciaCompradorMes.comprador_nombre),
        )
        .where(CompetenciaCompradorMes.clave_comprador.in_(lista))
        .group_by(CompetenciaCompradorMes.clave_comprador)
    ):
        out[clave] = nombre or clave
    return out


def _filtro_rut(rut: str, regiones: list[int], desde: str, hasta: str) -> list[Any]:
    return [CompetenciaMes.rut == rut, CompetenciaMes.region.in_(regiones),
            CompetenciaMes.mes >= desde, CompetenciaMes.mes <= hasta]  # fmt: skip


@dataclass
class PerfilCompetidor:
    rut: str
    razon_social: str | None
    es_emt: bool | None
    comuna: str | None
    region_proveedor: int | None
    n_ca: int
    n_ofertas: int
    tasa: Tasa  # ganadas / (CA − sin dato)
    n_sin_dato: int
    precio_relativo: float | None  # mediana aproximada, fracción sobre/bajo la mediana de la CA
    n_precio: int
    monto_ganado: float
    n_ca_un_oferente: int
    compradores: list[dict[str, Any]]
    familias: list[dict[str, Any]]
    tendencia: list[dict[str, Any]]
    meses: int


def perfil_competidor(
    session: Session, rut: str, regiones: list[int], *, meses: int = MESES_UI,
    mes_hoy: str | None = None,
) -> PerfilCompetidor | None:  # fmt: skip
    rut = rut_competidor(rut)[0]
    desde, hasta = _desde(meses, mes_hoy)
    filas = list(
        session.execute(
            select(CompetenciaMes).where(*_filtro_rut(rut, regiones, desde, hasta))
            .order_by(CompetenciaMes.mes)
        ).scalars()
    )  # fmt: skip
    if not rut or not filas:
        return None
    n_ca = sum(f.n_ca for f in filas)
    n_g = sum(f.n_ganadas for f in filas)
    n_sd = sum(f.n_sin_dato for f in filas)
    nombre = next((f.razon_social for f in reversed(filas) if f.razon_social), None)
    emts = {f.es_emt for f in filas if f.es_emt is not None}
    ubic = next(((f.comuna_proveedor, f.region_proveedor) for f in reversed(filas)
                 if f.comuna_proveedor or f.region_proveedor is not None), (None, None))  # fmt: skip
    por_comp: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    por_fam: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    por_mes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for f in filas:
        for d, k in ((por_comp, f.clave_comprador), (por_fam, f.familia)):
            d[k][0] += f.n_ca
            d[k][1] += f.n_ganadas
            d[k][2] += f.n_sin_dato
        por_mes[f.mes][0] += f.n_ca
        por_mes[f.mes][1] += f.n_ganadas
    nombres = nombres_comprador(session, por_comp)

    def _top(
        d: dict[str, list[int]], etiqueta: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        orden = sorted(d.items(), key=lambda kv: (-kv[1][0], -kv[1][1], kv[0]))[:5]
        return [{"clave": k, "nombre": (etiqueta or {}).get(k, k), "n_ca": v[0],
                 "tasa": wilson(v[1], v[0] - v[2])} for k, v in orden]  # fmt: skip

    tope = max((v[0] for v in por_mes.values()), default=0)
    tendencia = [
        {"mes": m, "n_ca": por_mes[m][0], "n_ganadas": por_mes[m][1],
         "alto": round(100 * por_mes[m][0] / tope) if tope else 0,
         "alto_ganadas": round(100 * por_mes[m][1] / tope) if tope else 0}
        for m in meses_entre(desde, hasta)
    ]  # fmt: skip
    return PerfilCompetidor(
        rut=rut, razon_social=nombre, es_emt=(True in emts) if emts else None, comuna=ubic[0],
        region_proveedor=ubic[1], n_ca=n_ca, n_ofertas=sum(f.n_ofertas for f in filas),
        tasa=wilson(n_g, n_ca - n_sd), n_sin_dato=n_sd,
        precio_relativo=pct_relativo(mediana_ponderada((f.p50_log_ratio, float(f.n_log_ratio))
                                                       for f in filas)),
        n_precio=sum(f.n_log_ratio for f in filas), monto_ganado=sum(f.monto_ganado for f in filas),
        n_ca_un_oferente=sum(f.n_ca_un_oferente for f in filas),
        compradores=_top(por_comp, nombres), familias=_top(por_fam), tendencia=tendencia,
        meses=meses,
    )  # fmt: skip


def _agrupado_por_rut(cond: list[Any]) -> Any:
    return (
        select(
            CompetenciaMes.rut,
            func.max(CompetenciaMes.razon_social),
            func.sum(CompetenciaMes.n_ca),
            func.sum(CompetenciaMes.n_ganadas),
            func.sum(CompetenciaMes.n_sin_dato),
            func.sum(CompetenciaMes.suma_log_ratio),
            func.sum(CompetenciaMes.n_log_ratio),
            func.sum(CompetenciaMes.monto_ganado),
        )
        .where(*cond)
        .group_by(CompetenciaMes.rut)
    )


def _fila_competidor(fila: Any, nosotros: str | None, total: int | None = None) -> dict[str, Any]:
    rut, nombre, n_ca, n_g, n_sd, s_lr, n_lr, monto = fila
    n_ca, n_g, n_sd, n_lr = int(n_ca or 0), int(n_g or 0), int(n_sd or 0), int(n_lr or 0)
    return {
        "rut": rut, "nombre": nombre or rut, "n_ca": n_ca, "tasa": wilson(n_g, n_ca - n_sd),
        "precio_relativo": pct_relativo(float(s_lr) / n_lr) if n_lr else None, "n_precio": n_lr,
        "monto_ganado": float(monto or 0.0), "es_nosotros": bool(nosotros) and rut == nosotros,
        "participacion": n_ca / total if total else None,
    }  # fmt: skip


def ranking_territorio(
    session: Session, regiones: list[int], *, nosotros: str | None = None, meses: int = MESES_UI,
    mes_hoy: str | None = None, limite: int = 50,
) -> list[dict[str, Any]]:  # fmt: skip
    """Competidores del territorio por monto ganado (12 meses)."""
    desde, hasta = _desde(meses, mes_hoy)
    cond = [CompetenciaMes.region.in_(regiones), CompetenciaMes.mes >= desde,
            CompetenciaMes.mes <= hasta]  # fmt: skip
    filas = session.execute(
        _agrupado_por_rut(cond)
        .order_by(func.sum(CompetenciaMes.monto_ganado).desc(), func.sum(CompetenciaMes.n_ca).desc(),
                  CompetenciaMes.rut)
        .limit(limite)
    ).all()  # fmt: skip
    return [_fila_competidor(f, nosotros) for f in filas]


def _escapar_like(texto: str) -> str:
    return texto.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def buscar_competidores(
    session: Session, q: str, regiones: list[int], *, nosotros: str | None = None,
    meses: int = MESES_UI, mes_hoy: str | None = None, limite: int = 30,
) -> list[dict[str, Any]]:  # fmt: skip
    """Por RUT (prefijo, sin puntos) o razón social (contiene; ILIKE parametrizado)."""
    termino = " ".join(q.split())[:100]
    if not termino:
        return []
    desde, hasta = _desde(meses, mes_hoy)
    rut_q = termino.replace(".", "").replace(" ", "").upper()
    filtro = [CompetenciaMes.razon_social.ilike(f"%{_escapar_like(termino)}%", escape="\\")]
    if rut_q and all(ch.isdigit() or ch in "-K" for ch in rut_q):
        filtro.append(CompetenciaMes.rut.like(f"{_escapar_like(rut_q)}%", escape="\\"))
    cond = [CompetenciaMes.region.in_(regiones), CompetenciaMes.mes >= desde,
            CompetenciaMes.mes <= hasta, or_(*filtro)]  # fmt: skip
    filas = session.execute(
        _agrupado_por_rut(cond).order_by(func.sum(CompetenciaMes.n_ca).desc(), CompetenciaMes.rut)
        .limit(limite)
    ).all()  # fmt: skip
    return [_fila_competidor(f, nosotros) for f in filas]


def competidores_de(
    session: Session, clave_comprador: str, familia: str | None, regiones: list[int], *,
    nosotros: str | None = None, meses: int = MESES_UI, mes_hoy: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Ranking del comprador (× familia si viene): participación (CA en que ofertó / CA con
    ofertas del comprador), tasa de victoria y precio relativo; marca a «nosotros»."""
    desde, hasta = _desde(meses, mes_hoy)
    cond_r: list[Any] = [CompetenciaMes.clave_comprador == clave_comprador,
                         CompetenciaMes.region.in_(regiones), CompetenciaMes.mes >= desde,
                         CompetenciaMes.mes <= hasta]  # fmt: skip
    cond_c: list[Any] = [CompetenciaCompradorMes.clave_comprador == clave_comprador,
                         CompetenciaCompradorMes.region.in_(regiones),
                         CompetenciaCompradorMes.mes >= desde, CompetenciaCompradorMes.mes <= hasta]  # fmt: skip
    if familia:
        cond_r.append(CompetenciaMes.familia == familia)
        cond_c.append(CompetenciaCompradorMes.familia == familia)
    n_ca, n_con, n_des, n_un = session.execute(
        select(func.sum(CompetenciaCompradorMes.n_ca), func.sum(CompetenciaCompradorMes.n_con_ofertas),
               func.sum(CompetenciaCompradorMes.n_desiertas),
               func.sum(CompetenciaCompradorMes.n_un_oferente)).where(*cond_c)
    ).one()  # fmt: skip
    total = int(n_con or 0)
    filas = session.execute(
        _agrupado_por_rut(cond_r).order_by(func.sum(CompetenciaMes.n_ca).desc(),
                                           func.sum(CompetenciaMes.n_ganadas).desc(), CompetenciaMes.rut)
        .limit(LIMITE_LISTA)
    ).all()  # fmt: skip
    return {
        "clave": clave_comprador, "familia": familia or "",
        "nombre": nombres_comprador(session, [clave_comprador]).get(clave_comprador, clave_comprador),
        "n_ca": int(n_ca or 0), "n_con_ofertas": total, "n_desiertas": int(n_des or 0),
        "n_un_oferente": int(n_un or 0),
        "competidores": [_fila_competidor(f, nosotros, total) for f in filas],
    }  # fmt: skip


ORDENES_MAPA = {
    "monto": "monto", "n_ca": "n_ca", "un_oferente": "pct_un_oferente",
    "desiertas": "pct_desiertas", "no_barato": "pct_no_mas_barato", "oferentes": "oferentes",
}  # fmt: skip


@dataclass
class MapaMercado:
    filas: list[dict[str, Any]]
    familias: list[str]
    regiones: list[int]
    total: int


def mapa_mercado(
    session: Session, empresa_id: int, *, meses: int = MESES_UI, familia: str = "",
    orden: str = "monto", mes_hoy: str | None = None,
) -> MapaMercado:  # fmt: skip
    """Comprador × familia en las regiones de la empresa: monto, CA, oferentes medianos, % de un
    oferente, % desiertas, competidor dominante (mayor monto ganado) y % en que no ganó el más
    barato (señal de que el comprador premia otra cosa)."""
    regiones = regiones_empresa(session, empresa_id)
    desde, hasta = _desde(meses, mes_hoy)
    agg: dict[Mercado, dict[str, Any]] = {}
    for f in session.execute(
        select(CompetenciaCompradorMes).where(
            CompetenciaCompradorMes.region.in_(regiones), CompetenciaCompradorMes.mes >= desde,
            CompetenciaCompradorMes.mes <= hasta,
        )
    ).scalars():  # fmt: skip
        a = agg.setdefault((f.clave_comprador, f.familia), {
            "clave": f.clave_comprador, "familia": f.familia, "nombre": f.comprador_nombre,
            "n_ca": 0, "n_desiertas": 0, "n_con_ofertas": 0, "n_un": 0, "n_2mas_g": 0,
            "n_no_barato": 0, "monto": 0.0, "_oferentes": [],
        })  # fmt: skip
        a["nombre"] = a["nombre"] or f.comprador_nombre
        a["n_ca"] += f.n_ca
        a["n_desiertas"] += f.n_desiertas
        a["n_con_ofertas"] += f.n_con_ofertas
        a["n_un"] += f.n_un_oferente
        a["n_2mas_g"] += f.n_2mas_con_ganador
        a["n_no_barato"] += f.n_no_gano_mas_barato
        a["monto"] += f.monto_adjudicado
        a["_oferentes"].append((f.p50_oferentes, float(f.n_con_ofertas)))
    familias = sorted({k[1] for k in agg})
    dominante: dict[Mercado, tuple[float, str, str | None]] = {}
    for clave, fam, rut, nombre, monto in session.execute(
        select(CompetenciaMes.clave_comprador, CompetenciaMes.familia, CompetenciaMes.rut,
               func.max(CompetenciaMes.razon_social), func.sum(CompetenciaMes.monto_ganado))
        .where(CompetenciaMes.region.in_(regiones), CompetenciaMes.mes >= desde,
               CompetenciaMes.mes <= hasta, CompetenciaMes.n_ganadas > 0,
               *([CompetenciaMes.familia == familia] if familia else []))
        .group_by(CompetenciaMes.clave_comprador, CompetenciaMes.familia, CompetenciaMes.rut)
    ):  # fmt: skip
        m = float(monto or 0.0)
        act = dominante.get((clave, fam))
        if act is None or m > act[0] or (m == act[0] and rut < act[1]):
            dominante[(clave, fam)] = (m, rut, nombre)
    filas = []
    for k, a in agg.items():
        if familia and a["familia"] != familia:
            continue
        dom = dominante.get(k)
        filas.append({
            **{c: v for c, v in a.items() if not c.startswith("_")},
            "oferentes": mediana_ponderada(a.pop("_oferentes")),
            "pct_un_oferente": a["n_un"] / a["n_con_ofertas"] if a["n_con_ofertas"] else None,
            "pct_desiertas": a["n_desiertas"] / a["n_ca"] if a["n_ca"] else None,
            "pct_no_mas_barato": a["n_no_barato"] / a["n_2mas_g"] if a["n_2mas_g"] else None,
            "dominante": None if dom is None else {"rut": dom[1], "nombre": dom[2] or dom[1],
                                                   "monto": dom[0]},
        })  # fmt: skip
    campo = ORDENES_MAPA.get(orden, "monto")
    filas.sort(key=lambda f: (f[campo] is None, -(f[campo] or 0), f["nombre"] or "", f["familia"]))
    return MapaMercado(filas[:LIMITE_LISTA], familias, regiones, len(filas))


def cara_a_cara(
    session: Session, rut_a: str, rut_b: str, regiones: list[int], *, meses: int = MESES_UI,
    mes_hoy: str | None = None,
) -> dict[str, Any]:  # fmt: skip
    """Dos competidores en los mercados (comprador × familia × mes) donde ofertaron ambos: CA de
    cada uno, ganadas y precio relativo medio. Es a nivel de mercado, no CA a CA: el detalle por
    CA exige leer las ofertas crudas, y ningún request recorre `ca_cotizaciones` (§6.4)."""
    a, b = rut_competidor(rut_a)[0], rut_competidor(rut_b)[0]
    desde, hasta = _desde(meses, mes_hoy)
    datos: dict[str, dict[tuple[str, str, str], tuple[int, int, int, float, int]]] = {a: {}, b: {}}
    nombres: dict[str, str] = {}
    if a and b and a != b:
        for f in session.execute(
            select(CompetenciaMes).where(
                CompetenciaMes.rut.in_([a, b]), CompetenciaMes.region.in_(regiones),
                CompetenciaMes.mes >= desde, CompetenciaMes.mes <= hasta,
            )
        ).scalars():  # fmt: skip
            datos[f.rut][(f.clave_comprador, f.familia, f.mes)] = (
                f.n_ca,
                f.n_ganadas,
                f.n_sin_dato,
                f.suma_log_ratio,
                f.n_log_ratio,
            )
            if f.razon_social:
                nombres[f.rut] = f.razon_social
    comunes = sorted(set(datos[a]) & set(datos[b])) if a and b and a != b else []
    por_mercado: dict[Mercado, list[float]] = defaultdict(lambda: [0.0] * 10)
    for k in comunes:
        acc = por_mercado[(k[0], k[1])]
        for i, v in enumerate((*datos[a][k], *datos[b][k])):
            acc[i] += v
    nom_comp = nombres_comprador(session, {k[0] for k in por_mercado})

    def _lado(acc: list[float], o: int) -> dict[str, Any]:
        n_ca, n_g, n_sd, s_lr, n_lr = (acc[o + i] for i in range(5))
        return {"n_ca": int(n_ca), "tasa": wilson(int(n_g), int(n_ca - n_sd)),
                "precio_relativo": pct_relativo(s_lr / n_lr) if n_lr else None,
                "_lr": s_lr / n_lr if n_lr else None}  # fmt: skip

    filas = []
    tot = [0.0] * 10
    for (clave, fam), acc in sorted(por_mercado.items(), key=lambda kv: -(kv[1][0] + kv[1][5])):
        la, lb = _lado(acc, 0), _lado(acc, 5)
        filas.append({"clave": clave, "familia": fam, "nombre": nom_comp.get(clave, clave),
                      "a": la, "b": lb, "diferencia": _diferencia(la, lb)})  # fmt: skip
        tot = [x + y for x, y in zip(tot, acc, strict=True)]
    ta, tb = _lado(tot, 0), _lado(tot, 5)
    return {
        "a": {"rut": a, "nombre": nombres.get(a, a), **ta},
        "b": {"rut": b, "nombre": nombres.get(b, b), **tb},
        "mercados": filas,
        "diferencia": _diferencia(ta, tb),
    }


def _diferencia(a: dict[str, Any], b: dict[str, Any]) -> float | None:
    """Precio relativo de A respecto de B (fracción): exp(log_ratio A − log_ratio B) − 1."""
    if a["_lr"] is None or b["_lr"] is None:
        return None
    return pct_relativo(a["_lr"] - b["_lr"])
