"""Índice de precios por producto canónico × ámbito × mes y referencia jerárquica (F-plat-4).

Precio NETO por unidad base (kg, L o u). Comparación pareada dentro de la CA: todos los que
cotizan una línea ofertan el mismo producto, cantidad y comuna (`16-pivote` §5).

- Cotizaciones (`ca_cotizacion_productos`) unidas a `ca_lineas` `ok` por `(ca_codigo,
  codigo_producto)`; `ca_lineas.codigo_unspsc` guarda el `codigo_producto` de la API [V,
  `catalogo._procesar_lote`]. Si dos líneas de la CA comparten código, la oferta no se puede
  atribuir: esas líneas se descartan y se cuentan (`descartadas_ambiguas`). Las líneas leídas de un
  anexo no tienen código (sus ofertas van contra el producto «según anexo»): `descartadas_anexo`.
- Regla de elección (F-merc-1c): una oferta por RUT; el ganador con su oferta seleccionada, cada
  competidor con su precio más bajo.
- Respaldo: `oc_lineas_ca` (`precio_base`/`base`) da el precio ganador de un producto × ámbito ×
  mes sin ninguna cotización con ganador. Una línea de OC va a un producto solo si un único
  producto canónico activo tiene ese UNSPSC y esa unidad base (si no, se cuenta y se descarta).

Sin API ni red. Por lotes de `LOTE` CA (regla 12) con commit por mes.
Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import logging
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from operator import itemgetter
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from app.catalogos.territorio_nuble import comuna_canonica, comuna_en_texto, provincia_de
from app.core.tiempo import TZ_CHILE
from app.mercado.eleccion import elegir_por_rut
from app.models.tables import (
    CaCotizacion,
    CaCotizacionProducto,
    CaLinea,
    CompraAgil,
    OcLineaCa,
    PrecioIndice,
    PrecioReferencia,
    ProductoCanonico,
)

_log = logging.getLogger(__name__)

LOTE = 2000
MIN_N = 5  # caída jerárquica cuando n < 5
VENTANA_MESES = 3  # meses ANTERIORES al de la CA que forman la referencia
_Z80 = 1.2816  # cuantil 0,90 de la normal
_IQR_SIGMA = 1.349  # IQR de la normal en sigmas


# ---------------------------------------------------------------------------
# Meses (hora de Chile; las columnas DateTime son UTC naive, ver app/core/tiempo.py)
# ---------------------------------------------------------------------------


def mes_de(dt: datetime | None) -> str | None:
    """'AAAA-MM' de un instante UTC naive, en hora de Chile."""
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(TZ_CHILE).strftime("%Y-%m")


def sumar_meses(mes: str, k: int) -> str:
    y, m = (int(x) for x in mes.split("-"))
    i = y * 12 + (m - 1) + k
    return f"{i // 12:04d}-{i % 12 + 1:02d}"


def meses_entre(desde: str, hasta: str) -> list[str]:
    """Meses de `desde` a `hasta`, ambos incluidos."""
    out, m = [], desde
    while m <= hasta:
        out.append(m)
        m = sumar_meses(m, 1)
    return out


def mes_actual() -> str:
    return datetime.now(TZ_CHILE).strftime("%Y-%m")


def rango_mes_utc(mes: str) -> tuple[datetime, datetime]:
    """[inicio, fin) del mes de Chile, en UTC naive (para filtrar `fecha_cierre`)."""

    def _borde(m: str) -> datetime:
        y, mm = (int(x) for x in m.split("-"))
        return datetime(y, mm, 1, tzinfo=TZ_CHILE).astimezone(UTC).replace(tzinfo=None)

    return _borde(mes), _borde(sumar_meses(mes, 1))


# ---------------------------------------------------------------------------
# Estadística
# ---------------------------------------------------------------------------


def cuantil(xs: list[float], q: float) -> float:
    """Cuantil con interpolación lineal (el mismo de `scripts/mercado/bandas_precio.py`)."""
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * q
    i = int(k)
    j = min(i + 1, len(s) - 1)
    return s[i] + (s[j] - s[i]) * (k - i)


def _resumen(xs: list[float]) -> tuple[int, float | None, float | None, float | None]:
    if not xs:
        return 0, None, None, None
    return len(xs), cuantil(xs, 0.25), cuantil(xs, 0.5), cuantil(xs, 0.75)


# ---------------------------------------------------------------------------
# Territorio y comuna de una CA
# ---------------------------------------------------------------------------


def nombre_comuna(texto: str | None) -> str | None:
    """Nombre canónico si es de Ñuble; si no, el texto con mayúscula inicial; vacío → None."""
    canon = comuna_canonica(texto)
    if canon:
        return canon
    t = " ".join((texto or "").split())
    return t.title()[:100] if t else None


def ambitos(comuna: str | None, region: int | None) -> list[str]:
    """Ámbitos de una observación, del más fino al más grueso (sin país)."""
    out: list[str] = []
    if comuna:
        out.append(f"comuna:{comuna}")
        prov = provincia_de(comuna)
        if prov:
            out.append(f"provincia:{prov}")
    if region is not None:
        out.append(f"region:{region}")
    return out


def _rut(valor: str | None) -> str:
    return (valor or "").replace(".", "").replace(" ", "").upper()


def mapa_comunas_oc(session: Session, regiones: Iterable[int]) -> dict[str, str]:
    """`rut_unidad` → comuna de compra más frecuente en `oc_lineas_ca` (empate: alfabético)."""
    cuenta: dict[str, Counter[str]] = defaultdict(Counter)
    filas = session.execute(
        select(OcLineaCa.rut_unidad, OcLineaCa.comuna_compra)
        .where(
            OcLineaCa.region_compra.in_(list(regiones)),
            OcLineaCa.rut_unidad.isnot(None),
            OcLineaCa.comuna_compra.isnot(None),
        )
        .execution_options(yield_per=LOTE)
    )
    for rut_unidad, comuna in filas:
        nombre = nombre_comuna(comuna)
        if nombre:
            cuenta[_rut(rut_unidad)][nombre] += 1
    return {r: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for r, c in cuenta.items()}


def comuna_de_ca(
    organismo_rut: str | None,
    organismo_nombre: str | None,
    direccion_entrega: str | None,
    comunas_oc: Mapping[str, str],
) -> tuple[str | None, str | None]:
    """(comuna, fuente) de una CA. La API no trae comuna [V]:

    1. la más frecuente de `oc_lineas_ca.comuna_compra` del mismo comprador (`rut_unidad` =
       `organismo_rut`) → `oc`;
    2. una comuna de Ñuble nombrada en la dirección de entrega o en el organismo → `texto`
       (la dirección primero: es donde se entrega);
    3. si no, (None, None): la referencia cae a provincia o región.
    """
    rut = _rut(organismo_rut)
    if rut and rut in comunas_oc:
        return comunas_oc[rut], "oc"
    for texto in (direccion_entrega, organismo_nombre):
        c = comuna_en_texto(texto)
        if c:
            return c, "texto"
    return None, None


def inferir_comunas(
    session: Session, regiones: list[int], *, todas: bool = False, desde: datetime | None = None
) -> dict[str, int]:
    """Persiste `comuna_inferida`/`comuna_fuente` de las CA de `regiones`.

    Sin `todas`, solo las que no salieron de una OC (una OC nueva del comprador puede resolverlas)
    y, si viene `desde`, publicadas desde esa fecha o aún sin fecha. Core UPDATE con
    `actualizado_en` fijo: no es un cambio de la CA (el cursor de `mercado-catalogo` lo lee).
    Commit por lote.
    """
    comunas_oc = mapa_comunas_oc(session, regiones)
    stats = {"cas_revisadas": 0, "comuna_oc": 0, "comuna_texto": 0, "sin_comuna": 0, "cambiadas": 0}
    ultimo = ""
    while True:
        cond: list[Any] = [CompraAgil.region.in_(regiones), CompraAgil.codigo > ultimo]
        if not todas:
            cond.append((CompraAgil.comuna_fuente.is_(None)) | (CompraAgil.comuna_fuente != "oc"))
            if desde is not None:
                cond.append(
                    CompraAgil.fecha_publicacion.is_(None) | (CompraAgil.fecha_publicacion >= desde)
                )
        filas = session.execute(
            select(
                CompraAgil.codigo,
                CompraAgil.organismo_rut,
                CompraAgil.organismo_nombre,
                CompraAgil.direccion_entrega,
                CompraAgil.comuna_inferida,
                CompraAgil.comuna_fuente,
            )
            .where(*cond)
            .order_by(CompraAgil.codigo)
            .limit(LOTE)
        ).all()
        if not filas:
            break
        for codigo, org_rut, org_nombre, direccion, actual, fuente_actual in filas:
            comuna, fuente = comuna_de_ca(org_rut, org_nombre, direccion, comunas_oc)
            stats["cas_revisadas"] += 1
            stats[f"comuna_{fuente}" if fuente else "sin_comuna"] += 1
            if (comuna, fuente) != (actual, fuente_actual):
                session.execute(
                    update(CompraAgil)
                    .where(CompraAgil.codigo == codigo)
                    .values(
                        comuna_inferida=comuna,
                        comuna_fuente=fuente,
                        actualizado_en=CompraAgil.actualizado_en,
                    )
                    .execution_options(synchronize_session=False)
                )
                stats["cambiadas"] += 1
        session.commit()
        ultimo = filas[-1][0]
    return stats


# ---------------------------------------------------------------------------
# Observaciones: una por línea `ok` atribuible de una CA cerrada en el mes
# ---------------------------------------------------------------------------


@dataclass
class Observacion:
    ca_codigo: str
    producto_id: int
    cantidad_base: float
    comuna: str | None
    region: int | None
    ganador: float | None  # precio neto por unidad base de la oferta seleccionada
    ofertas: list[float]  # una por RUT (regla de elección), ganador incluido
    monto_ganador: float | None  # monto neto de la línea en la oferta ganadora


@dataclass
class ConteoUnion:
    lineas_ok: int = 0
    usadas: int = 0
    descartadas_ambiguas: int = 0
    descartadas_anexo: int = 0
    sin_ofertas: int = 0
    cas_con_anexo: set[str] = field(default_factory=set)

    def como_dict(self) -> dict[str, int]:
        return {
            "lineas_ok": self.lineas_ok,
            "lineas_usadas": self.usadas,
            "descartadas_ambiguas": self.descartadas_ambiguas,
            "descartadas_anexo": self.descartadas_anexo,
            "lineas_sin_ofertas": self.sin_ofertas,
            "cas_con_lineas_de_anexo": len(self.cas_con_anexo),
        }


@dataclass(frozen=True)
class LineaCa:
    producto_id: int
    cantidad: float
    cantidad_base: float


def lineas_atribuibles(
    lineas: Iterable[CaLinea], conteo: ConteoUnion | None = None
) -> dict[str, LineaCa]:
    """`codigo_producto` → línea `ok` de UNA CA a la que se pueden atribuir las ofertas."""
    lineas = list(lineas)
    codigos = Counter(ln.codigo_unspsc for ln in lineas if ln.codigo_unspsc and ln.anexo_id is None)
    out: dict[str, LineaCa] = {}
    for ln in lineas:
        if ln.estado != "ok" or ln.producto_id is None:
            continue
        if conteo is not None:
            conteo.lineas_ok += 1
        if ln.anexo_id is not None:
            if conteo is not None:
                conteo.descartadas_anexo += 1
                conteo.cas_con_anexo.add(ln.ca_codigo)
            continue
        if not ln.codigo_unspsc:
            continue  # manual: sin código de la API
        if codigos[ln.codigo_unspsc] > 1:
            if conteo is not None:
                conteo.descartadas_ambiguas += 1
            continue
        if not ln.cantidad or ln.cantidad <= 0 or not ln.cantidad_base or ln.cantidad_base <= 0:
            continue
        out[ln.codigo_unspsc] = LineaCa(ln.producto_id, ln.cantidad, ln.cantidad_base)
    return out


def observaciones_de_cas(
    session: Session, cas: list[tuple[str, str | None, int | None]], conteo: ConteoUnion
) -> list[Observacion]:
    """Observaciones de un lote de CA `(codigo, comuna, region)`."""
    codigos = [c for c, _, _ in cas]
    lineas_por_ca: dict[str, list[CaLinea]] = defaultdict(list)
    for ln in session.execute(select(CaLinea).where(CaLinea.ca_codigo.in_(codigos))).scalars():
        lineas_por_ca[ln.ca_codigo].append(ln)
    precios: dict[tuple[str, str], list[tuple[str, bool | None, float]]] = defaultdict(list)
    for ca_codigo, rut, sel, codigo, precio in session.execute(
        select(
            CaCotizacion.ca_codigo,
            CaCotizacion.rut_proveedor,
            CaCotizacion.seleccionado,
            CaCotizacionProducto.codigo_producto,
            CaCotizacionProducto.precio_unitario,
        )
        .join(CaCotizacion, CaCotizacion.id == CaCotizacionProducto.cotizacion_id)
        .where(CaCotizacion.ca_codigo.in_(codigos))
    ):
        if codigo and precio is not None and precio > 0:
            precios[(ca_codigo, codigo)].append((_rut(rut), sel, float(precio)))
    out: list[Observacion] = []
    for codigo_ca, comuna, region in cas:
        for codigo_prod, la in lineas_atribuibles(lineas_por_ca.get(codigo_ca, ()), conteo).items():
            elegidas = elegir_por_rut(  # (rut, sel, precio); regla F-merc-1c
                precios.get((codigo_ca, codigo_prod), ()), itemgetter(0), itemgetter(1),
                itemgetter(2),
            )  # fmt: skip
            if not elegidas:
                conteo.sin_ofertas += 1
                continue
            conteo.usadas += 1
            factor = la.cantidad / la.cantidad_base  # unidades pedidas por unidad base
            ganadores = [p for _, s, p in elegidas.values() if s is True]
            ganador = min(ganadores) if ganadores else None
            out.append(
                Observacion(
                    ca_codigo=codigo_ca,
                    producto_id=la.producto_id,
                    cantidad_base=la.cantidad_base,
                    comuna=comuna,
                    region=region,
                    ganador=ganador * factor if ganador is not None else None,
                    ofertas=[p * factor for _, _, p in elegidas.values()],
                    monto_ganador=ganador * la.cantidad if ganador is not None else None,
                )
            )
    return out


def lotes_cas_del_mes(
    session: Session, mes: str, regiones: list[int]
) -> Iterator[list[tuple[str, str | None, int | None]]]:
    """CA de `regiones` cerradas en `mes` (hora de Chile), por lotes de `LOTE`."""
    ini, fin = rango_mes_utc(mes)
    ultimo = ""
    while True:
        filas = session.execute(
            select(CompraAgil.codigo, CompraAgil.comuna_inferida, CompraAgil.region)
            .where(
                CompraAgil.region.in_(regiones),
                CompraAgil.fecha_cierre >= ini,
                CompraAgil.fecha_cierre < fin,
                CompraAgil.codigo > ultimo,
            )
            .order_by(CompraAgil.codigo)
            .limit(LOTE)
        ).all()
        if not filas:
            return
        yield [(c, com, r) for c, com, r in filas]
        ultimo = filas[-1][0]


def _mapa_unspsc(session: Session) -> dict[tuple[str, str], int]:
    """(UNSPSC, base) → producto, solo cuando un único producto canónico activo lo tiene."""
    cuenta: dict[tuple[str, str], list[int]] = defaultdict(list)
    for pid, unspsc, base in session.execute(
        select(
            ProductoCanonico.id, ProductoCanonico.unspsc_sugerido, ProductoCanonico.unidad_base
        ).where(ProductoCanonico.activo.is_(True), ProductoCanonico.unspsc_sugerido.isnot(None))
    ):
        cuenta[(unspsc.strip(), base)].append(pid)
    return {k: v[0] for k, v in cuenta.items() if len(v) == 1}


def recalcular_indice(
    session: Session, meses: Iterable[str], regiones: list[int]
) -> dict[str, Any]:
    """Reescribe `precio_indice` de `meses` (borra y reinserta; commit por mes). Idempotente."""
    total: dict[str, Any] = {"meses": 0, "filas": 0, "observaciones": 0, "lineas_oc_usadas": 0,
                             "lineas_oc_sin_producto_unico": 0, "filas_desde_oc": 0}  # fmt: skip
    conteo = ConteoUnion()
    mapa = _mapa_unspsc(session)
    monto_anexo = 0.0
    for mes in meses:
        ganador: dict[tuple[int, str], list[float]] = defaultdict(list)
        ofertas: dict[tuple[int, str], list[float]] = defaultdict(list)
        for lote in lotes_cas_del_mes(session, mes, regiones):
            antes = set(conteo.cas_con_anexo)
            for ob in observaciones_de_cas(session, lote, conteo):
                total["observaciones"] += 1
                for amb in ambitos(ob.comuna, ob.region):
                    if ob.ganador is not None:
                        ganador[(ob.producto_id, amb)].append(ob.ganador)
                    ofertas[(ob.producto_id, amb)].extend(ob.ofertas)
            nuevas = conteo.cas_con_anexo - antes
            if nuevas:
                monto_anexo += _monto_ganador(session, nuevas)
        oc: dict[tuple[int, str], list[float]] = defaultdict(list)
        for unspsc, base, precio, comuna, region in session.execute(
            select(
                OcLineaCa.codigo_producto,
                OcLineaCa.base,
                OcLineaCa.precio_base,
                OcLineaCa.comuna_compra,
                OcLineaCa.region_compra,
            )
            .where(
                OcLineaCa.mes == mes,
                OcLineaCa.region_compra.in_(regiones),
                OcLineaCa.precio_base.isnot(None),
                OcLineaCa.base.isnot(None),
            )
            .execution_options(yield_per=LOTE)
        ):
            pid = mapa.get(((unspsc or "").strip(), base))
            if pid is None or not precio or precio <= 0:
                total["lineas_oc_sin_producto_unico"] += 1
                continue
            total["lineas_oc_usadas"] += 1
            for amb in ambitos(nombre_comuna(comuna), region):
                oc[(pid, amb)].append(float(precio))

        session.execute(delete(PrecioIndice).where(PrecioIndice.mes == mes))
        filas: list[dict[str, Any]] = []
        for clave in set(ganador) | set(ofertas) | set(oc):
            pid, amb = clave
            g = ganador.get(clave, [])
            fuente = "cot" if g else None
            if not g and oc.get(clave):
                g, fuente = oc[clave], "oc"
                total["filas_desde_oc"] += 1
            ng, g25, g50, g75 = _resumen(g)
            no, o25, o50, o75 = _resumen(ofertas.get(clave, []))
            filas.append({"producto_id": pid, "ambito": amb, "mes": mes, "n_ganador": ng,
                          "p25_ganador": g25, "mediana_ganador": g50, "p75_ganador": g75,
                          "n_ofertas": no, "p25_ofertas": o25, "mediana_ofertas": o50,
                          "p75_ofertas": o75, "fuente_ganador": fuente})  # fmt: skip
        for i in range(0, len(filas), LOTE):
            session.execute(insert(PrecioIndice), filas[i : i + LOTE])
        session.commit()
        total["meses"] += 1
        total["filas"] += len(filas)
    total.update(conteo.como_dict())
    total["monto_cas_con_lineas_de_anexo"] = round(monto_anexo)
    _log.info("precio_indice: %s", total)
    return total


def _monto_ganador(session: Session, codigos: Iterable[str]) -> float:
    """Suma del `valor_neto` de las ofertas seleccionadas de esas CA (monto fuera del índice)."""
    filas = session.execute(
        select(CaCotizacion.valor_neto).where(
            CaCotizacion.ca_codigo.in_(list(codigos)),
            CaCotizacion.seleccionado.is_(True),
            CaCotizacion.valor_neto.isnot(None),
        )
    ).scalars()
    return float(sum(v or 0.0 for v in filas))


def meses_con_cotizaciones(session: Session, regiones: list[int]) -> list[str]:
    """Meses (de cierre, hora de Chile) con alguna CA de `regiones` (para `--completo`)."""
    fechas = session.execute(
        select(CompraAgil.fecha_cierre)
        .where(CompraAgil.region.in_(regiones), CompraAgil.fecha_cierre.isnot(None))
        .where(CompraAgil.codigo.in_(select(CaCotizacion.ca_codigo)))
        .execution_options(yield_per=LOTE)
    ).scalars()
    return sorted({m for m in (mes_de(f) for f in fechas) if m})


# ---------------------------------------------------------------------------
# Referencia jerárquica
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReferenciaPrecio:
    """Referencia de precio neto por unidad base para un producto en una comuna y mes.

    `ambito` = de dónde salió (`comuna:…`, `provincia:…`, `region:…`, `pais`) o None si no hay
    datos. `n` = observaciones de precio ganador. `suficiente` = n ≥ 5 en ese ámbito. Los
    percentiles de varios meses se combinan con promedio ponderado por n (aproximación)."""

    producto_id: int
    ambito: str | None
    n: int
    p25_ganador: float | None = None
    mediana_ganador: float | None = None
    p75_ganador: float | None = None
    n_ofertas: int = 0
    p25_ofertas: float | None = None
    mediana_ofertas: float | None = None
    p75_ofertas: float | None = None
    meses: tuple[str, ...] = ()
    suficiente: bool = False

    @property
    def mediana_para_ratio(self) -> float | None:
        """Mediana de las ofertas (comparación pareada); sin ofertas, la del ganador."""
        return self.mediana_ofertas or self.mediana_ganador


def intervalo_80(ref: ReferenciaPrecio) -> tuple[float, float] | None:
    """Intervalo 80 % del precio ganador suponiendo log-normal: σ = ln(p75/p25) / 1,349."""
    p25, med, p75 = ref.p25_ganador, ref.mediana_ganador, ref.p75_ganador
    if not p25 or not med or not p75 or p25 <= 0 or med <= 0 or p75 <= 0:
        return None
    s = max(0.0, math.log(p75 / p25)) / _IQR_SIGMA
    return med * math.exp(-_Z80 * s), med * math.exp(_Z80 * s)


_Fila = tuple[
    int, float | None, float | None, float | None, int, float | None, float | None, float | None
]


def _combinar(filas: list[_Fila]) -> _Fila:
    def _prom(i: int, n_i: int) -> float | None:
        pares: list[tuple[int, float]] = []
        for f in filas:
            n, v = f[n_i], f[i]
            if isinstance(n, int) and n > 0 and isinstance(v, float | int):
                pares.append((n, float(v)))
        tot = sum(n for n, _ in pares)
        return sum(n * v for n, v in pares) / tot if tot else None

    return (
        sum(f[0] for f in filas), _prom(1, 0), _prom(2, 0), _prom(3, 0),
        sum(f[4] for f in filas), _prom(5, 4), _prom(6, 4), _prom(7, 4),
    )  # fmt: skip


class IndiceMemoria:
    """`precio_indice` (y `precio_referencia` para el país) de unos meses, en memoria.

    Lo usan `referencia_precio` (un producto) y la evaluación walk-forward (muchos)."""

    def __init__(
        self,
        filas: Mapping[tuple[int, str, str], _Fila],
        productos: Mapping[int, tuple[str | None, str]],
        pais: Mapping[tuple[str, str], tuple[int, float, float, float]],
    ) -> None:
        self.filas = filas
        self.productos = productos
        self.pais = pais

    @classmethod
    def cargar(
        cls, session: Session, meses: Iterable[str], producto_ids: Iterable[int] | None = None
    ) -> IndiceMemoria:
        meses = sorted(set(meses))
        ids = sorted(set(producto_ids)) if producto_ids is not None else None
        q = select(PrecioIndice).where(PrecioIndice.mes.in_(meses))
        qp = select(
            ProductoCanonico.id, ProductoCanonico.unspsc_sugerido, ProductoCanonico.unidad_base
        )
        if ids is not None:
            q = q.where(PrecioIndice.producto_id.in_(ids))
            qp = qp.where(ProductoCanonico.id.in_(ids))
        filas = {
            (r.producto_id, r.ambito, r.mes): (
                r.n_ganador, r.p25_ganador, r.mediana_ganador, r.p75_ganador,
                r.n_ofertas, r.p25_ofertas, r.mediana_ofertas, r.p75_ofertas,
            )
            for r in session.execute(q.execution_options(yield_per=LOTE)).scalars()
        }  # fmt: skip
        productos = {pid: (u, b) for pid, u, b in session.execute(qp)}
        claves = {f"{u.strip()}|{b}" for u, b in productos.values() if u}
        pais: dict[tuple[str, str], tuple[int, float, float, float]] = {}
        if claves:
            for r in session.execute(
                select(PrecioReferencia).where(
                    PrecioReferencia.mes.in_(meses), PrecioReferencia.clave.in_(sorted(claves))
                )
            ).scalars():
                pais[(r.clave, r.mes)] = (r.n, r.p25, r.mediana, r.p75)
        return cls(filas, productos, pais)

    def referencia(
        self,
        producto_id: int,
        comuna: str | None,
        mes: str,
        *,
        region: int | None = None,
        ventana: int = VENTANA_MESES,
        minimo: int = MIN_N,
    ) -> ReferenciaPrecio:
        """Caída comuna → provincia → región → país con los `ventana` meses ANTERIORES a `mes`."""
        meses = tuple(sumar_meses(mes, -k) for k in range(ventana, 0, -1))
        niveles: list[tuple[str, _Fila | None]] = []
        for amb in ambitos(nombre_comuna(comuna), region):
            filas = [
                self.filas[(producto_id, amb, m)]
                for m in meses
                if (producto_id, amb, m) in self.filas
            ]
            niveles.append((amb, _combinar(filas) if filas else None))
        unspsc, base = self.productos.get(producto_id, (None, ""))
        if unspsc:
            clave = f"{unspsc.strip()}|{base}"
            pf: list[_Fila] = [
                (n, p25, med, p75, 0, None, None, None)
                for (n, p25, med, p75) in (
                    self.pais[(clave, m)] for m in meses if (clave, m) in self.pais
                )
            ]
            niveles.append(("pais", _combinar(pf) if pf else None))
        mejor: tuple[str, _Fila] | None = None
        for amb, f in niveles:
            if f is None or f[0] == 0:
                continue
            if f[0] >= minimo:
                return _ref(producto_id, amb, f, meses, True)
            if mejor is None or f[0] > mejor[1][0]:
                mejor = (amb, f)
        if mejor is None:
            return ReferenciaPrecio(producto_id=producto_id, ambito=None, n=0, meses=meses)
        return _ref(producto_id, mejor[0], mejor[1], meses, False)


def _ref(
    pid: int, amb: str, f: _Fila, meses: tuple[str, ...], suficiente: bool
) -> ReferenciaPrecio:
    return ReferenciaPrecio(
        producto_id=pid, ambito=amb, n=f[0], p25_ganador=f[1], mediana_ganador=f[2],
        p75_ganador=f[3], n_ofertas=f[4], p25_ofertas=f[5], mediana_ofertas=f[6],
        p75_ofertas=f[7], meses=meses, suficiente=suficiente,
    )  # fmt: skip


def referencia_precio(
    session: Session,
    producto_id: int,
    comuna: str | None,
    mes: str,
    *,
    region: int | None = None,
    ventana: int = VENTANA_MESES,
) -> ReferenciaPrecio:
    """Referencia de precio para una CA de `mes` en `comuna` (meses anteriores, sin fuga).

    Sin `region`, la de la comuna si es de Ñuble (16). Nombre distinto de la tabla
    `precio_referencia` (F-merc-2, nacional por UNSPSC), que este nivel usa como «país»."""
    if region is None and provincia_de(comuna):
        region = 16
    meses = [sumar_meses(mes, -k) for k in range(ventana, 0, -1)]
    memoria = IndiceMemoria.cargar(session, meses, [producto_id])
    return memoria.referencia(producto_id, comuna, mes, region=region, ventana=ventana)
