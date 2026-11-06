"""Probabilidad de adjudicación y evaluación de una CA (F-plat-4).

- Dataset: una fila por OFERTA (regla de elección F-merc-1c: una por RUT; el ganador con su
  oferta seleccionada, cada competidor con la más barata) en CA cerradas con ganador de las
  regiones activas. Objetivo `gano`. Ofertas con `seleccionado` nulo en una CA con ganador no
  entran (la API no lo dijo: no es "perdió"), pero sí cuentan para la mediana y el n.º de oferentes.
- Logística con L2 por Newton/IRLS en Python puro (decisión de Boris, 4-nov): filas dispersas
  (las categóricas son 0/1 sin estandarizar), sistema chico por eliminación gaussiana. La web solo
  evalúa coeficientes guardados (producto punto + sigmoide).
- Línea base obligatoria («ranking simple»): p = σ(logit(1/n) + b·log_ratio), un solo
  coeficiente. El logístico solo se activa si supera a la base en Brier (`evaluacion.py`).

**Punto de extensión (F-plat-comp):** las variables son un registro (`VARIABLES`), no columnas
fijas. Una fase nueva agrega un extractor con `registrar_variable(...)` y entrena con
`entrenar_logistico(filas, variables=(*VARIABLES_BASE, "competencia"))`. El extractor recibe un
`ContextoOferta` (con `extra` para lo que necesite) y devuelve `{columna: valor}`; las columnas con
`:` son categóricas (0/1, con mínimo de CA), las demás continuas (se estandarizan).

Sin plazo ofrecido: no se guarda [V] (mejora anotada). Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import logging
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from operator import itemgetter
from typing import Any, NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalogos.mercado import FAMILIAS_ALIMENTOS, G_ALIM, grupo_de, norm
from app.catalogos.territorio_nuble import REGION_NUBLE, comuna_canonica, provincia_de
from app.mercado.comprador import clave_unidad  # única definición (F-mesa-v0)
from app.mercado.eleccion import elegir_por_rut  # única regla F-merc-1c (F-plat-comp)
from app.mercado.indice import (
    LOTE,
    IndiceMemoria,
    ReferenciaPrecio,
    _rut,
    intervalo_80,
    mes_actual,
    mes_de,
    rango_mes_utc,
    sumar_meses,
)
from app.mercado.proveedores import mejor_costo
from app.models.tables import (
    CaCotizacion,
    CaLinea,
    CaProducto,
    CompraAgil,
    Empresa,
    ModeloAdjudicacion,
    OcLineaCa,
    ProductoCanonico,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
)

_log = logging.getLogger(__name__)

MESES_ENTRENAMIENTO = 18
MIN_CA_CATEGORIA = 20  # efecto fijo solo con ≥ 20 CA; el resto cae en "otra" (todo 0)
MAX_CATEGORIAS = 40  # por prefijo, las más frecuentes (tiempo del ajuste en Python puro)
N_OFERTAS_DEFAULT = 3.0
COSTO_FINANCIERO_DEFAULT = 0.015  # mensual; 30 días = 1 mes
MARGEN_GANABLE = 0.08
P_GANABLE = 0.5
_P_MIN, _P_MAX = 0.01, 0.99


# ---------------------------------------------------------------------------
# Logística con L2 (Newton/IRLS, Python puro)
# ---------------------------------------------------------------------------


def sigmoide(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def logit(p: float) -> float:
    p = min(_P_MAX, max(_P_MIN, p))
    return math.log(p / (1 - p))


def _resolver(a: list[list[float]], b: list[float]) -> list[float]:
    """Eliminación gaussiana con pivoteo parcial (a y b se copian)."""
    n = len(b)
    m = [fila[:] + [b[i]] for i, fila in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[piv][c]) < 1e-12:
            raise ArithmeticError("sistema singular")
        m[c], m[piv] = m[piv], m[c]
        for r in range(c + 1, n):
            f = m[r][c] / m[c][c]
            if f:
                fila_r, fila_c = m[r], m[c]
                for j in range(c, n + 1):
                    fila_r[j] -= f * fila_c[j]
    x = [0.0] * n
    for c in range(n - 1, -1, -1):
        x[c] = (m[c][n] - sum(m[c][j] * x[j] for j in range(c + 1, n))) / m[c][c]
    return x


Filas = Sequence[tuple[Sequence[int], Sequence[float]]]


def _objetivo(x: Filas, y: Sequence[float], beta: list[float], off: Sequence[float] | None,
              l2: float, libres: frozenset[int]) -> float:  # fmt: skip
    """Log-verosimilitud penalizada (a maximizar)."""
    ll = 0.0
    for r, (idx, val) in enumerate(x):
        eta = sum(beta[i] * v for i, v in zip(idx, val, strict=True)) + (off[r] if off else 0.0)
        # log σ(η) y log(1-σ(η)) estables
        ll += y[r] * eta - (
            eta + math.log1p(math.exp(-eta)) if eta > 0 else math.log1p(math.exp(eta))
        )
    return ll - 0.5 * l2 * sum(b * b for i, b in enumerate(beta) if i not in libres)


def ajustar_logistica(
    x: Filas,
    y: Sequence[float],
    k: int,
    *,
    l2: float = 1.0,
    offset: Sequence[float] | None = None,
    libres: frozenset[int] = frozenset({0}),
    max_iter: int = 50,
    tol: float = 1e-7,
) -> tuple[list[float], int, bool]:
    """Coeficientes por Newton con búsqueda de paso. `x[r]` = (índices ascendentes, valores).

    `libres` = columnas sin penalizar (el intercepto). Devuelve (beta, iteraciones, convergió)."""
    beta = [0.0] * k
    obj = _objetivo(x, y, beta, offset, l2, libres)
    for it in range(1, max_iter + 1):
        g = [0.0] * k
        h = [[0.0] * k for _ in range(k)]
        for r, (idx, val) in enumerate(x):
            eta = sum(beta[i] * v for i, v in zip(idx, val, strict=True)) + (
                offset[r] if offset else 0.0
            )
            p = sigmoide(eta)
            resid, w = y[r] - p, p * (1.0 - p)
            n = len(idx)
            for a in range(n):
                ia, va = idx[a], val[a]
                g[ia] += resid * va
                wva, ha = w * va, h[ia]
                for b in range(a, n):
                    ha[idx[b]] += wva * val[b]
        for i in range(k):
            if i not in libres:
                g[i] -= l2 * beta[i]
                h[i][i] += l2
            else:
                h[i][i] += 1e-9
            for j in range(i + 1, k):
                h[j][i] = h[i][j]
        delta = _resolver(h, g)
        paso = 1.0
        for _ in range(30):
            nuevo = [b + paso * d for b, d in zip(beta, delta, strict=True)]
            obj_n = _objetivo(x, y, nuevo, offset, l2, libres)
            if obj_n >= obj - 1e-12:
                break
            paso /= 2
        beta, obj = nuevo, obj_n
        if max((abs(d) * paso for d in delta), default=0.0) < tol:
            return beta, it, True
    return beta, max_iter, False


# ---------------------------------------------------------------------------
# Variables (registro extensible)
# ---------------------------------------------------------------------------


@dataclass
class ContextoOferta:
    """Lo que se sabe de una oferta y su CA. Para una CA nueva, `n_ofertas` y `log_ratio` son
    estimaciones (mediana histórica del comprador × familia; índice de precios)."""

    ca_codigo: str
    mes: str
    region: int | None
    familia: str
    unidad: str  # unidad compradora: "<organismo_rut>|<unidad normalizada>"
    n_ofertas: float
    log_ratio: float
    es_local: bool | None = None
    es_emt: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Extractor = Callable[[ContextoOferta], dict[str, float]]


def _cat(prefijo: str, valor: bool | None) -> dict[str, float]:
    if valor is None:
        return {f"{prefijo}:desconocido": 1.0}
    return {f"{prefijo}:si": 1.0} if valor else {}


VARIABLES: dict[str, Extractor] = {
    "precio": lambda c: {"log_ratio": c.log_ratio},
    "n_ofertas": lambda c: {
        "log_n": math.log(max(c.n_ofertas, 1.0)),
        **({"un_oferente:si": 1.0} if c.n_ofertas <= 1 else {}),
    },
    "local": lambda c: _cat("local", c.es_local),
    "emt": lambda c: _cat("emt", c.es_emt),
    "familia": lambda c: {f"familia:{c.familia}": 1.0},
    "unidad": lambda c: {f"unidad:{c.unidad}": 1.0},
}
VARIABLES_BASE: tuple[str, ...] = tuple(VARIABLES)


def registrar_variable(nombre: str, extractor: Extractor) -> None:
    """Agrega un grupo de variables al registro (F-plat-comp). No cambia `VARIABLES_BASE`: entra
    al modelo solo si se pide en `entrenar_logistico(..., variables=...)`."""
    VARIABLES[nombre] = extractor


def extraer(c: ContextoOferta, variables: Iterable[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for nombre in variables:
        ext = VARIABLES.get(nombre)
        if ext is None:
            _log.warning("variable %r no registrada: se ignora", nombre)
            continue
        out.update(ext(c))
    return out


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------


@dataclass
class FilaOferta:
    contexto: ContextoOferta
    gano: bool
    valor_neto: float


@dataclass
class ModeloLogistico:
    columnas: list[str]  # columnas[0] = intercepto
    beta: list[float]
    medias: dict[str, float]
    desv: dict[str, float]
    variables: list[str]
    l2: float
    iteraciones: int = 0
    convergio: bool = True
    tipo: str = "logistico"

    def p(self, c: ContextoOferta) -> float:
        pos = {col: i for i, col in enumerate(self.columnas)}
        eta = self.beta[0]
        for col, v in extraer(c, self.variables).items():
            i = pos.get(col)
            if i is None:
                continue
            if col in self.medias:
                v = (v - self.medias[col]) / self.desv[col]
            eta += self.beta[i] * v
        return sigmoide(eta)

    def a_json(self) -> dict[str, Any]:
        return {"tipo": self.tipo, "columnas": self.columnas, "beta": self.beta,
                "medias": self.medias, "desv": self.desv, "variables": self.variables,
                "l2": self.l2, "iteraciones": self.iteraciones, "convergio": self.convergio}  # fmt: skip


@dataclass
class ModeloBase:
    """Ranking simple: el más barato gana; p = σ(logit(1/n) + b·log_ratio)."""

    b: float
    tipo: str = "base"

    def p(self, c: ContextoOferta) -> float:
        if c.n_ofertas <= 1:
            return _P_MAX
        return sigmoide(logit(1.0 / c.n_ofertas) + self.b * c.log_ratio)

    def a_json(self) -> dict[str, Any]:
        return {"tipo": self.tipo, "b": self.b}


Modelo = ModeloLogistico | ModeloBase


def modelo_desde_json(d: Mapping[str, Any]) -> Modelo:
    if d.get("tipo") == "base":
        return ModeloBase(b=float(d["b"]))
    return ModeloLogistico(
        columnas=list(d["columnas"]), beta=[float(b) for b in d["beta"]],
        medias=dict(d["medias"]), desv=dict(d["desv"]), variables=list(d["variables"]),
        l2=float(d.get("l2", 1.0)), iteraciones=int(d.get("iteraciones", 0)),
        convergio=bool(d.get("convergio", True)),
    )  # fmt: skip


def entrenar_logistico(
    filas: Sequence[FilaOferta], *, variables: Sequence[str] = VARIABLES_BASE, l2: float = 1.0
) -> ModeloLogistico:
    if not filas:
        raise ValueError("sin filas para entrenar")
    feats = [extraer(f.contexto, variables) for f in filas]
    cas_por_col: dict[str, set[str]] = defaultdict(set)
    continuas: set[str] = set()
    for f, d in zip(filas, feats, strict=True):
        for col in d:
            if ":" in col:
                cas_por_col[col].add(f.contexto.ca_codigo)
            else:
                continuas.add(col)
    por_prefijo: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for col, cas in cas_por_col.items():
        if len(cas) >= MIN_CA_CATEGORIA:
            por_prefijo[col.split(":", 1)[0]].append((len(cas), col))
    categoricas = sorted(
        col
        for lista in por_prefijo.values()
        for _, col in sorted(lista, reverse=True)[:MAX_CATEGORIAS]
    )
    columnas = ["(intercepto)", *sorted(continuas), *categoricas]
    pos = {c: i for i, c in enumerate(columnas)}
    medias: dict[str, float] = {}
    desv: dict[str, float] = {}
    for col in continuas:
        xs = [d.get(col, 0.0) for d in feats]
        m = sum(xs) / len(xs)
        s = math.sqrt(sum((v - m) ** 2 for v in xs) / len(xs))
        medias[col], desv[col] = m, (s if s > 1e-12 else 1.0)
    x: list[tuple[list[int], list[float]]] = []
    for d in feats:
        pares = [(0, 1.0)]
        for col, v in d.items():
            i = pos.get(col)
            if i is None:
                continue
            pares.append((i, (v - medias[col]) / desv[col] if col in medias else v))
        pares.sort()
        x.append(([i for i, _ in pares], [v for _, v in pares]))
    y = [1.0 if f.gano else 0.0 for f in filas]
    beta, it, ok = ajustar_logistica(x, y, len(columnas), l2=l2)
    if not ok:
        _log.warning("logística: no convergió en %d iteraciones", it)
    return ModeloLogistico(columnas, beta, medias, desv, list(variables), l2, it, ok)


def entrenar_base(filas: Sequence[FilaOferta]) -> ModeloBase:
    utiles = [f for f in filas if f.contexto.n_ofertas > 1]
    if not utiles:
        return ModeloBase(b=0.0)
    x = [([0], [f.contexto.log_ratio]) for f in utiles]
    off = [logit(1.0 / f.contexto.n_ofertas) for f in utiles]
    y = [1.0 if f.gano else 0.0 for f in utiles]
    beta, _, _ = ajustar_logistica(x, y, 1, l2=1e-6, offset=off, libres=frozenset())
    return ModeloBase(b=beta[0])


def brier(pares: Iterable[tuple[float, bool]]) -> tuple[float | None, int]:
    lista = list(pares)
    if not lista:
        return None, 0
    return sum((p - (1.0 if y else 0.0)) ** 2 for p, y in lista) / len(lista), len(lista)


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------


def familia_de_codigo(unspsc: str | None, nombre: str | None = "") -> str:
    """Familia de un producto: la de alimentos (4 dígitos) o el grupo intermediable."""
    u = (unspsc or "").strip()
    g = grupo_de(u, nombre)
    if g in G_ALIM:
        return FAMILIAS_ALIMENTOS.get(u[:4], f"Otros alimentos ({u[:4]})")
    return g or "otra"


def familia_de_ca(productos: Iterable[tuple[str | None, str | None]]) -> str:
    cuenta = Counter(familia_de_codigo(c, n) for c, n in productos)
    if not cuenta:
        return "otra"
    return sorted(cuenta.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def mapa_regiones_proveedor(session: Session) -> dict[str, int]:
    """RUT de proveedor → región más frecuente en `oc_lineas_ca` (proveedores que ya vendieron)."""
    cuenta: dict[str, Counter[int]] = defaultdict(Counter)
    for rut, region in session.execute(
        select(OcLineaCa.rut_proveedor, OcLineaCa.region_proveedor)
        .where(OcLineaCa.rut_proveedor.isnot(None), OcLineaCa.region_proveedor.isnot(None))
        .execution_options(yield_per=LOTE)
    ):
        cuenta[_rut(rut)][int(region)] += 1
    return {r: sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] for r, c in cuenta.items()}


@dataclass
class CaDataset:
    codigo: str
    mes: str
    region: int | None
    comuna: str | None
    familia: str
    unidad: str
    n_ofertas: int
    filas: list[FilaOferta]
    sin_dato: int  # ofertas con `seleccionado` nulo (fuera del entrenamiento)
    monto_ganador: float
    # Líneas (producto, cantidad_base) si la CA está completa (todas `ok` y todos sus productos
    # son alimentos, cubiertos por líneas); si no, None: sin estimación ex ante.
    lineas: list[tuple[int, float]] | None


class _LineaLeve(NamedTuple):
    estado: str
    producto_id: int | None
    cantidad_base: float | None


def lineas_completas(
    detalle_lineas: str,
    productos: Iterable[tuple[str | None, str | None]],
    lineas: Iterable[CaLinea | _LineaLeve],
) -> list[tuple[int, float]] | None:
    productos = list(productos)
    if detalle_lineas != "completo" or not productos:
        return None
    if any(grupo_de((c or "").strip(), n) not in G_ALIM for c, n in productos):
        return None
    out = [
        (ln.producto_id, float(ln.cantidad_base))
        for ln in lineas
        if ln.estado == "ok"
        and ln.producto_id is not None
        and ln.cantidad_base
        and ln.cantidad_base > 0
    ]
    return out or None


def cargar_dataset(
    session: Session,
    regiones: list[int],
    desde_mes: str,
    hasta_mes: str,
    regiones_proveedor: Mapping[str, int] | None = None,
) -> list[CaDataset]:
    """CA de `regiones` cerradas entre `desde_mes` y `hasta_mes` (incluidos) con oferta
    seleccionada. Por lotes de `LOTE` CA (regla 12)."""
    if regiones_proveedor is None:
        regiones_proveedor = mapa_regiones_proveedor(session)
    ini, _ = rango_mes_utc(desde_mes)
    _, fin = rango_mes_utc(hasta_mes)
    out: list[CaDataset] = []
    ultimo = ""
    while True:
        cas = session.execute(
            select(
                CompraAgil.codigo, CompraAgil.fecha_cierre, CompraAgil.region,
                CompraAgil.comuna_inferida, CompraAgil.organismo_rut, CompraAgil.unidad_compra,
                CompraAgil.detalle_lineas,
            )
            .where(
                CompraAgil.region.in_(regiones), CompraAgil.fecha_cierre >= ini,
                CompraAgil.fecha_cierre < fin, CompraAgil.codigo > ultimo,
            )
            .order_by(CompraAgil.codigo)
            .limit(LOTE)
        ).all()  # fmt: skip
        if not cas:
            break
        ultimo = cas[-1][0]
        codigos = [c[0] for c in cas]
        ofertas: dict[str, list[tuple[str, bool | None, float, bool | None]]] = defaultdict(list)
        for ca_codigo, rut, sel, valor, emt in session.execute(
            select(
                CaCotizacion.ca_codigo, CaCotizacion.rut_proveedor, CaCotizacion.seleccionado,
                CaCotizacion.valor_neto, CaCotizacion.es_emt,
            ).where(CaCotizacion.ca_codigo.in_(codigos))
        ):  # fmt: skip
            if valor is not None and valor > 0:
                ofertas[ca_codigo].append((_rut(rut), sel, float(valor), emt))
        productos: dict[str, list[tuple[str | None, str | None]]] = defaultdict(list)
        for ca_codigo, cod, nombre in session.execute(
            select(CaProducto.ca_codigo, CaProducto.codigo_producto, CaProducto.nombre).where(
                CaProducto.ca_codigo.in_(codigos)
            )
        ):
            productos[ca_codigo].append((cod, nombre))
        # Tuplas, no objetos ORM: 18+ meses de líneas no se acumulan en la sesión (regla 12).
        lineas: dict[str, list[_LineaLeve]] = defaultdict(list)
        for ca_codigo, estado, pid, cb in session.execute(
            select(
                CaLinea.ca_codigo, CaLinea.estado, CaLinea.producto_id, CaLinea.cantidad_base
            ).where(CaLinea.ca_codigo.in_(codigos))
        ):
            lineas[ca_codigo].append(_LineaLeve(estado, pid, cb))
        for codigo, cierre, region, comuna, org_rut, unidad, detalle in cas:
            item = _ca_dataset(
                codigo, mes_de(cierre) or "", region, comuna, clave_unidad(org_rut, unidad),
                familia_de_ca(productos.get(codigo, ())), ofertas.get(codigo, []),
                regiones_proveedor,
                lineas_completas(detalle, productos.get(codigo, ()), lineas.get(codigo, ())),
            )  # fmt: skip
            if item is not None:
                out.append(item)
    return out


def _ca_dataset(
    codigo: str,
    mes: str,
    region: int | None,
    comuna: str | None,
    unidad: str,
    familia: str,
    ofertas: list[tuple[str, bool | None, float, bool | None]],
    regiones_proveedor: Mapping[str, int],
    lineas: list[tuple[int, float]] | None,
) -> CaDataset | None:
    # Regla de elección F-merc-1c con la oferta completa: la seleccionada; si no, la más barata.
    elegida = elegir_por_rut(ofertas, itemgetter(0), itemgetter(1), itemgetter(2))
    if not any(o[1] is True for o in elegida.values()):
        return None  # sin ganador conocido
    med = statistics.median(o[2] for o in elegida.values())
    n = len(elegida)
    filas: list[FilaOferta] = []
    sin_dato = 0
    monto = 0.0
    for r, sel, v, emt in elegida.values():
        if sel is True:
            monto += v
        elif sel is None:
            sin_dato += 1  # la API no dijo si perdió: fuera del entrenamiento
            continue
        reg = regiones_proveedor.get(r)
        es_local = None if reg is None or region is None else reg == region
        ctx = ContextoOferta(
            ca_codigo=codigo, mes=mes, region=region, familia=familia, unidad=unidad,
            n_ofertas=float(n), log_ratio=math.log(v / med), es_local=es_local, es_emt=emt,
        )  # fmt: skip
        filas.append(FilaOferta(ctx, sel is True, v))
    return CaDataset(
        codigo, mes, region, comuna, familia, unidad, n, filas, sin_dato, monto, lineas
    )


@dataclass
class TablaN:
    """N.º de oferentes esperado: mediana histórica de la unidad compradora × familia (≥ 3 CA),
    si no la de la familia, si no 3."""

    por_clave: dict[str, float]
    por_familia: dict[str, float]

    def esperado(self, unidad: str, familia: str) -> float:
        return (
            self.por_clave.get(f"{unidad}#{familia}")
            or self.por_familia.get(familia)
            or N_OFERTAS_DEFAULT
        )

    def a_json(self) -> dict[str, Any]:
        return {"por_clave": self.por_clave, "por_familia": self.por_familia}

    @classmethod
    def desde_json(cls, d: Mapping[str, Any] | None) -> TablaN:
        d = d or {}
        return cls(dict(d.get("por_clave", {})), dict(d.get("por_familia", {})))


def tabla_n(cas: Iterable[CaDataset]) -> TablaN:
    por_clave: dict[str, list[int]] = defaultdict(list)
    por_familia: dict[str, list[int]] = defaultdict(list)
    for c in cas:
        por_clave[f"{c.unidad}#{c.familia}"].append(c.n_ofertas)
        por_familia[c.familia].append(c.n_ofertas)
    return TablaN(
        {k: float(statistics.median(v)) for k, v in por_clave.items() if len(v) >= 3},
        {k: float(statistics.median(v)) for k, v in por_familia.items() if len(v) >= 3},
    )


# ---------------------------------------------------------------------------
# Precio esperado de una CA (desde el índice) y curva de P(ganar)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreciosCa:
    """Totales de la CA a partir de las referencias por línea (cantidad_base × precio base)."""

    mediana_estimada: float  # mediana de las ofertas: base del log_ratio de una CA nueva
    agresivo: float  # Σ p25 del ganador
    mediana_ganador: float
    comodo: float  # Σ mediana de las ofertas
    intervalo_80: tuple[float, float] | None  # Σ de los intervalos por línea (conservador)
    grilla: tuple[float, ...]  # precios candidatos (cuantiles 10–90 % del ganador, log-normal)


def precios_de_lineas(lineas: Sequence[tuple[float, ReferenciaPrecio]]) -> PreciosCa | None:
    """None si alguna línea no tiene referencia con mediana del ganador."""
    if not lineas or any(not r.mediana_ganador or not r.mediana_para_ratio for _, r in lineas):
        return None
    med_of = sum(cb * (r.mediana_para_ratio or 0.0) for cb, r in lineas)
    med_g = sum(cb * (r.mediana_ganador or 0.0) for cb, r in lineas)
    agresivo = sum(cb * (r.p25_ganador or r.mediana_ganador or 0.0) for cb, r in lineas)
    intervalos = [(cb, intervalo_80(r)) for cb, r in lineas]
    intervalo = (
        (sum(cb * i[0] for cb, i in intervalos if i), sum(cb * i[1] for cb, i in intervalos if i))
        if all(i for _, i in intervalos)
        else None
    )
    grilla: list[float] = []
    for z in (-1.2816, -0.8416, -0.5244, -0.2533, 0.0, 0.2533, 0.5244, 0.8416, 1.2816):
        tot = 0.0
        for cb, r in lineas:
            med = r.mediana_ganador or 0.0
            s = (
                math.log(r.p75_ganador / r.p25_ganador) / 1.349
                if r.p25_ganador
                and r.p75_ganador
                and r.p25_ganador > 0
                and r.p75_ganador >= r.p25_ganador
                else 0.0
            )
            tot += cb * med * math.exp(z * s)
        grilla.append(tot)
    return PreciosCa(med_of, agresivo, med_g, med_of, intervalo, tuple(sorted(set(grilla))))


@dataclass(frozen=True)
class PuntoCurva:
    nombre: str  # agresivo | sugerido | comodo
    precio: float
    p: float


def _p_en(modelo: Modelo, base: ContextoOferta, precio: float, mediana: float) -> float:
    c = ContextoOferta(**{**base.__dict__, "log_ratio": math.log(precio / mediana)})
    return modelo.p(c)


def curva(
    modelo: Modelo, base: ContextoOferta, precios: PreciosCa, costo_total: float | None = None
) -> list[PuntoCurva]:
    """Agresivo (p25 ganador), sugerido (el de la grilla que maximiza margen × p; sin costo, la
    mediana del ganador) y cómodo (mediana de las ofertas). Ordenada por precio y monótona: si el
    modelo diera p creciente en precio, se recorta (min acumulado) y se avisa en el log."""
    med = precios.mediana_estimada
    if costo_total is not None:
        sugerido = max(
            precios.grilla, key=lambda pr: (pr - costo_total) * _p_en(modelo, base, pr, med)
        )
    else:
        sugerido = precios.mediana_ganador
    puntos = sorted(
        [("agresivo", precios.agresivo), ("sugerido", sugerido), ("comodo", precios.comodo)],
        key=lambda t: t[1],
    )
    out: list[PuntoCurva] = []
    tope = 1.0
    for nombre, precio in puntos:
        p = _p_en(modelo, base, precio, med)
        if p > tope + 1e-12:
            _log.warning("curva_p_ganar: p no decreciente en %s; se recorta", nombre)
            p = tope
        tope = p
        out.append(PuntoCurva(nombre, precio, p))
    return out


# ---------------------------------------------------------------------------
# Modelo activo, p_ganar y curva para una CA nueva
# ---------------------------------------------------------------------------


@dataclass
class ModeloActivo:
    modelo: Modelo
    tabla_n: TablaN
    fila_id: int
    datos_hasta_mes: str


def cargar_modelo_activo(session: Session) -> ModeloActivo | None:
    fila = session.execute(
        select(ModeloAdjudicacion)
        .where(ModeloAdjudicacion.activo.is_(True))
        .order_by(ModeloAdjudicacion.entrenado_en.desc(), ModeloAdjudicacion.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if fila is None:
        return None
    co = fila.coeficientes or {}
    return ModeloActivo(
        modelo_desde_json(co["modelo"]), TablaN.desde_json(co.get("n_esperado")), fila.id,
        fila.datos_hasta_mes,
    )  # fmt: skip


def _mes_de_ca(ca: CompraAgil) -> str:
    return mes_de(ca.fecha_publicacion) or mes_de(ca.fecha_cierre) or mes_actual()


def _productos_de(session: Session, ca_codigo: str) -> list[tuple[str | None, str | None]]:
    return [
        (c, n)
        for c, n in session.execute(
            select(CaProducto.codigo_producto, CaProducto.nombre).where(
                CaProducto.ca_codigo == ca_codigo
            )
        )
    ]


def region_empresa(session: Session, empresa: Empresa | None) -> int | None:
    """Región de la empresa: por su comuna base (Ñuble) o por su RUT en `oc_lineas_ca`."""
    if empresa is None:
        return None
    if provincia_de(empresa.comuna_base):
        return REGION_NUBLE
    if empresa.rut:
        fila = session.execute(
            select(OcLineaCa.region_proveedor)
            .where(
                OcLineaCa.rut_proveedor == _rut(empresa.rut), OcLineaCa.region_proveedor.isnot(None)
            )
            .limit(1)
        ).scalar_one_or_none()
        return int(fila) if fila is not None else None
    return None


def _contexto_ca(
    ca: CompraAgil,
    productos: Iterable[tuple[str | None, str | None]],
    tabla: TablaN,
    es_local: bool | None,
) -> ContextoOferta:
    familia = familia_de_ca(productos)
    unidad = clave_unidad(ca.organismo_rut, ca.unidad_compra)
    return ContextoOferta(
        ca_codigo=ca.codigo, mes=_mes_de_ca(ca), region=ca.region, familia=familia, unidad=unidad,
        n_ofertas=tabla.esperado(unidad, familia), log_ratio=0.0, es_local=es_local, es_emt=None,
    )  # fmt: skip


def _referencias(
    session: Session, ca: CompraAgil, lineas: Sequence[tuple[int, float]], mes: str
) -> list[tuple[float, ReferenciaPrecio]]:
    memoria = IndiceMemoria.cargar(
        session, [sumar_meses(mes, -k) for k in (3, 2, 1)], {pid for pid, _ in lineas}
    )
    return [
        (cb, memoria.referencia(pid, ca.comuna_inferida, mes, region=ca.region))
        for pid, cb in lineas
    ]


def _lineas_ca(session: Session, ca: CompraAgil) -> list[CaLinea]:
    return list(
        session.execute(
            select(CaLinea)
            # `reemplazada`: la línea de la API que reemplazó el anexo (F-plat-anexos-2).
            .where(CaLinea.ca_codigo == ca.codigo, CaLinea.estado != "reemplazada")
            .order_by(CaLinea.n)
        ).scalars()
    )


def p_ganar(
    session: Session,
    ca: CompraAgil,
    lineas: Sequence[CaLinea] | None,
    precio_total: float,
    *,
    empresa: Empresa | None = None,
    activo: ModeloActivo | None = None,
) -> float | None:
    """P(ganar) de una oferta de `precio_total` neto. None si no hay modelo activo, si la CA no
    está completa (no se puede estimar la mediana de las ofertas) o si falta alguna referencia."""
    activo = activo or cargar_modelo_activo(session)
    if activo is None or precio_total <= 0:
        return None
    productos = _productos_de(session, ca.codigo)
    lineas = list(lineas) if lineas is not None else _lineas_ca(session, ca)
    completas = lineas_completas(ca.detalle_lineas, productos, lineas)
    if not completas:
        return None
    base = _contexto_ca(ca, productos, activo.tabla_n, _es_local(session, ca, empresa))
    precios = precios_de_lineas(_referencias(session, ca, completas, base.mes))
    if precios is None:
        return None
    return _p_en(activo.modelo, base, precio_total, precios.mediana_estimada)


def _es_local(session: Session, ca: CompraAgil, empresa: Empresa | None) -> bool | None:
    reg = region_empresa(session, empresa)
    return None if reg is None or ca.region is None else reg == ca.region


def curva_p_ganar(
    session: Session,
    ca: CompraAgil,
    lineas: Sequence[CaLinea] | None = None,
    *,
    empresa: Empresa | None = None,
    costo_total: float | None = None,
    activo: ModeloActivo | None = None,
) -> list[PuntoCurva]:
    """[(agresivo|sugerido|comodo, precio, p)] en orden de precio; vacía si no se puede estimar."""
    activo = activo or cargar_modelo_activo(session)
    if activo is None:
        return []
    productos = _productos_de(session, ca.codigo)
    lineas = list(lineas) if lineas is not None else _lineas_ca(session, ca)
    completas = lineas_completas(ca.detalle_lineas, productos, lineas)
    if not completas:
        return []
    base = _contexto_ca(ca, productos, activo.tabla_n, _es_local(session, ca, empresa))
    precios = precios_de_lineas(_referencias(session, ca, completas, base.mes))
    if precios is None:
        return []
    return curva(activo.modelo, base, precios, costo_total)


# ---------------------------------------------------------------------------
# Costos de la empresa, logística y financiero
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostoLinea:
    precio_por_unidad_base: float
    proveedor: str
    producto_texto: str
    formato_texto: str
    fuente: str  # manual | planilla | lista_llm | solicitud (auditar el margen: Revisión 5-nov §6)
    vigente_desde: date
    # F-plat-lote §1.4: el mejor costo solo quedó con precios vencidos («precio de hace n días»).
    vencido: bool = False
    dias: int | None = None


class CostosEmpresa:
    """Mejor costo hoy por producto canónico de UNA empresa (regla 26), con caché. Con la
    cantidad de la línea (F-plat-lote §2.3: el tramo por volumen depende de ella)."""

    def __init__(self, session: Session, empresa_id: int) -> None:
        self.session = session
        self.empresa_id = empresa_id
        self._cache: dict[tuple[int, str, float | None], CostoLinea | None] = {}

    def tiene_costos(self) -> bool:
        return (
            self.session.execute(
                select(ProveedorProducto.id)
                .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
                .where(
                    Proveedor.empresa_id == self.empresa_id,
                    ProveedorProducto.producto_id.isnot(None),
                )
                .limit(1)
            ).first()
            is not None
        )

    def costo(
        self, producto_id: int, unidad_base: str, cantidad_base: float | None = None
    ) -> CostoLinea | None:
        clave = (producto_id, unidad_base, cantidad_base)
        if clave not in self._cache:
            mc = mejor_costo(
                self.session, self.empresa_id, producto_id=producto_id, unidad_base=unidad_base,
                cantidad_base=cantidad_base,
            )  # fmt: skip
            if mc is None:
                self._cache[clave] = None
            else:
                g = mc.ganador
                pp = self.session.get(ProveedorProducto, g.producto_id)
                self._cache[clave] = CostoLinea(
                    g.precio_por_unidad_base, g.proveedor, g.producto_texto, g.formato_texto,
                    pp.fuente if pp else "", pp.vigente_desde if pp else date.min,
                    vencido=g.vencido, dias=g.dias,
                )  # fmt: skip
        return self._cache[clave]


@dataclass(frozen=True)
class Logistica:
    costo: float
    kg: float
    tarifa: str | None  # "comuna:Chillán" | "provincia:Itata" | None (sin tarifa: 0)
    dias: int | None


Tarifas = Mapping[tuple[str, str], TarifaLogistica]


def cargar_tarifas(session: Session, empresa_id: int) -> dict[tuple[str, str], TarifaLogistica]:
    """Tarifas de UNA empresa por (ámbito, nombre normalizado)."""
    return {
        (t.ambito, norm(t.nombre)): t
        for t in session.execute(
            select(TarifaLogistica).where(TarifaLogistica.empresa_id == empresa_id)
        ).scalars()
    }


def logistica(tarifas: Tarifas, comuna: str | None, kg: float) -> Logistica:
    """Tarifa de la comuna de entrega; si no hay, la de su provincia; si no, 0 (sin tarifa).

    Las provincias de las tarifas son las claves de `PROVINCIAS_NUBLE` (mismo mapa de comunas)."""
    canon = comuna_canonica(comuna) or comuna
    candidatas = [("comuna", canon)]
    prov = provincia_de(comuna)
    if prov:
        candidatas.append(("provincia", prov))
    for ambito, nombre in candidatas:
        t = tarifas.get((ambito, norm(nombre)))
        if t is not None:
            return Logistica(
                t.costo_fijo_clp + t.costo_por_kg_clp * kg, kg, f"{ambito}:{t.nombre}", t.dias
            )
    return Logistica(0.0, kg, None, None)


@dataclass(frozen=True)
class CostoCA:
    productos: float
    logistica: Logistica
    financiero: float
    total: float


def costo_ca(
    costo_productos: float,
    kg: float,
    tarifas: Tarifas,
    comuna: str | None,
    costo_financiero_mensual: float = COSTO_FINANCIERO_DEFAULT,
) -> CostoCA:
    """Costo total de entregar una CA: productos + logística (tarifa de la comuna o su provincia)
    + financiero (30 días × tasa mensual sobre productos + logística). Única definición: la usan
    `evaluar_ca`, la simulación de mi oferta y el margen realizado (F-plat-5)."""
    log = logistica(tarifas, comuna, kg)
    financiero = (costo_productos + log.costo) * costo_financiero_mensual
    return CostoCA(costo_productos, log, financiero, costo_productos + log.costo + financiero)


def margen(
    precio: float, costo_total: float | None, p: float | None
) -> tuple[float | None, float | None]:
    """(margen esperado = (precio − costo) × p, margen % = (precio − costo) / precio). Sin costo
    o sin p → None, nunca 0. Única definición (F-plat-4; F-plat-5 la reutiliza al simular)."""
    if costo_total is None or precio <= 0:
        return None, None
    pct = (precio - costo_total) / precio
    return ((precio - costo_total) * p if p is not None else None), pct


# ---------------------------------------------------------------------------
# evaluar_ca
# ---------------------------------------------------------------------------


@dataclass
class LineaEvaluada:
    n: int
    texto: str
    estado: str
    producto_id: int | None
    producto: str | None
    unidad_base: str | None
    cantidad_base: float | None
    referencia: ReferenciaPrecio | None
    intervalo_80: tuple[float, float] | None
    costo: CostoLinea | None
    costo_total: float | None


@dataclass
class EvaluacionCA:
    ca_codigo: str
    empresa_id: int
    comuna: str | None
    comuna_fuente: str | None
    mes_referencia: str
    lineas: list[LineaEvaluada]
    logistica: Logistica | None
    costo_productos: float | None
    costo_financiero: float | None
    costo_total: float | None
    precio_sugerido: float | None
    intervalo_80: tuple[float, float] | None
    curva: list[PuntoCurva]
    p_ganar: dict[str, float]  # agresivo | sugerido | comodo
    margen_esperado: float | None  # (precio sugerido − costo total) × p(sugerido), CLP
    margen_pct: float | None  # (precio sugerido − costo total) / precio sugerido
    cobertura: float | None  # % del monto estimado con línea ok y con costo
    completa: bool
    modelo: str | None  # logistico | base | None (sin modelo activo)
    avisos: list[str]


def evaluar_ca(
    session: Session,
    ca_codigo: str,
    empresa_id: int,
    *,
    costo_financiero_mensual: float = COSTO_FINANCIERO_DEFAULT,
    activo: ModeloActivo | None = None,
) -> EvaluacionCA | None:
    """Referencia, costo, logística y margen de una CA para UNA empresa (regla 26).

    Por línea `ok`: referencia jerárquica, intervalo 80 %, mejor costo de la empresa (con su
    fuente y vigencia). Por CA: logística (tarifa de la comuna de entrega; kg = Σ cantidad_base de
    las líneas en kg; si no, solo el fijo), financiero (30 días × tasa mensual), precio sugerido,
    P(ganar) en los tres puntos, margen esperado y cobertura. Sin costo → margen None, nunca 0."""
    ca = session.get(CompraAgil, ca_codigo)
    if ca is None:
        return None
    activo = activo if activo is not None else cargar_modelo_activo(session)
    empresa = session.get(Empresa, empresa_id)
    mes = _mes_de_ca(ca)
    lineas = _lineas_ca(session, ca)
    productos = _productos_de(session, ca.codigo)
    ids = {ln.producto_id for ln in lineas if ln.producto_id is not None}
    unidades = {
        pid: (nombre, ub)
        for pid, nombre, ub in session.execute(
            select(
                ProductoCanonico.id, ProductoCanonico.nombre, ProductoCanonico.unidad_base
            ).where(ProductoCanonico.id.in_(ids))
        )
    }
    memoria = IndiceMemoria.cargar(session, [sumar_meses(mes, -k) for k in (3, 2, 1)], ids)
    costos = CostosEmpresa(session, empresa_id)
    avisos: list[str] = []
    evaluadas: list[LineaEvaluada] = []
    for ln in lineas:
        ref = costo = None
        costo_tot = None
        nombre, ub = unidades.get(ln.producto_id, (None, None)) if ln.producto_id else (None, None)
        if ln.estado == "ok" and ln.producto_id is not None:
            ref = memoria.referencia(ln.producto_id, ca.comuna_inferida, mes, region=ca.region)
            if ub:
                costo = costos.costo(ln.producto_id, ub, ln.cantidad_base)
            if costo is not None and ln.cantidad_base:
                costo_tot = costo.precio_por_unidad_base * ln.cantidad_base
        evaluadas.append(
            LineaEvaluada(
                n=ln.n, texto=ln.texto_origen, estado=ln.estado, producto_id=ln.producto_id,
                producto=nombre, unidad_base=ub, cantidad_base=ln.cantidad_base, referencia=ref,
                intervalo_80=intervalo_80(ref) if ref else None, costo=costo, costo_total=costo_tot,
            )
        )  # fmt: skip

    completas = lineas_completas(ca.detalle_lineas, productos, lineas)
    if completas is None:
        avisos.append("CA incompleta: hay líneas sin revisar o productos fuera del catálogo")
    con_ref = [
        (e.cantidad_base or 0.0, e.referencia)
        for e in evaluadas
        if e.estado == "ok" and e.referencia is not None and e.cantidad_base
    ]
    precios = precios_de_lineas(con_ref) if completas else None
    if completas and precios is None:
        avisos.append("Falta la referencia de precio de alguna línea")

    # Cobertura: monto estimado (mediana del ganador) de las líneas ok con costo / total estimado.
    monto = {
        id(e): (e.cantidad_base or 0.0) * (e.referencia.mediana_ganador or 0.0)
        for e in evaluadas
        if e.referencia is not None and e.referencia.mediana_ganador and e.cantidad_base
    }
    total_est = sum(monto.values())
    cubierto = sum(monto.get(id(e), 0.0) for e in evaluadas if e.costo is not None)
    cobertura = cubierto / total_est if total_est else None

    oks = [e for e in evaluadas if e.estado == "ok"]
    todas_con_costo = (
        bool(oks) and completas is not None and all(e.costo_total is not None for e in oks)
    )
    log = costo_prod = financiero = costo_total = None
    if todas_con_costo:
        kg = sum(e.cantidad_base or 0.0 for e in oks if e.unidad_base == "kg")
        cc = costo_ca(
            sum(e.costo_total or 0.0 for e in oks), kg, cargar_tarifas(session, empresa_id),
            ca.comuna_inferida, costo_financiero_mensual,
        )  # fmt: skip
        log, costo_prod, financiero, costo_total = cc.logistica, cc.productos, cc.financiero, cc.total
        if log.tarifa is None:
            avisos.append("Sin tarifa de logística para la comuna ni su provincia: logística 0")
    elif any(e.costo is not None for e in oks):
        avisos.append("Hay líneas sin costo: el margen queda sin calcular")
    else:
        avisos.append("Sin costos de la empresa para estas líneas")

    puntos: list[PuntoCurva] = []
    if activo is not None and precios is not None:
        base = _contexto_ca(ca, productos, activo.tabla_n, _es_local(session, ca, empresa))
        puntos = curva(activo.modelo, base, precios, costo_total)
    elif activo is None:
        avisos.append("Sin modelo de adjudicación entrenado")
    por_nombre = {p.nombre: p for p in puntos}
    sug = por_nombre.get("sugerido")
    precio_sugerido = sug.precio if sug else (precios.mediana_ganador if precios else None)
    m_esperado, margen_pct = (
        margen(sug.precio, costo_total, sug.p) if sug is not None else (None, None)
    )
    return EvaluacionCA(
        ca_codigo=ca.codigo, empresa_id=empresa_id, comuna=ca.comuna_inferida,
        comuna_fuente=ca.comuna_fuente, mes_referencia=mes, lineas=evaluadas, logistica=log,
        costo_productos=costo_prod, costo_financiero=financiero, costo_total=costo_total,
        precio_sugerido=precio_sugerido, intervalo_80=precios.intervalo_80 if precios else None,
        curva=puntos, p_ganar={p.nombre: p.p for p in puntos}, margen_esperado=m_esperado,
        margen_pct=margen_pct, cobertura=cobertura, completa=completas is not None,
        modelo=activo.modelo.tipo if activo else None, avisos=avisos,
    )  # fmt: skip
