"""Impuestos específicos (ILA y otros) en el catálogo: neto sin impuestos, bruto con todos
(F-plat-impuestos, decisión de Boris del 6-nov).

- `precio_neto` es SIN IVA y SIN impuestos específicos. Margen, precio sugerido, P(ganar) y la
  competencia se calculan siempre sobre el neto. El bruto es lo que se paga y se cobra: caja,
  solicitudes a proveedores y cotización formal.
- El impuesto de un producto se resuelve en el CATÁLOGO: el código del producto canónico y, si no
  tiene, la regla de su familia (`familia_impuesto`). Una familia `clasificar` deja a sus productos
  sin código «impuesto por clasificar» (`POR_CLASIFICAR`): una señal, nunca se supone una tasa. Un
  producto de proveedor sin vínculo al catálogo usa la PISTA de la planilla
  (`proveedor_productos.impuesto_codigo`); con vínculo manda el catálogo.
- Las tasas viven en `impuesto_especifico`, nunca en el código. La aplicable es la vigente en la
  fecha del costo (la fila de mayor `vigente_desde` que no la supera; si la fecha es anterior a
  todas, la más antigua conocida).
- `recuperable = true` (débito y crédito como el IVA): el impuesto va al bruto y no al neto:
  `bruto = neto × (1 + IVA + tasa)` (los dos sobre el mismo neto). Con `false` el impuesto es un
  costo: ya está DENTRO del neto (`neto = bruto ÷ (1 + IVA)`) y la cotización no lo cobra aparte.
- La constante IVA es una sola: `app.mercado.cotizacion.IVA`.

Sin HTML. Sin commit: lo hace quien llama.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalogos.mercado import norm
from app.models.tables import (
    FamiliaImpuesto,
    ImpuestoEspecifico,
    ProductoCanonico,
    Proveedor,
    ProveedorProducto,
)

MODOS = ("aplica", "clasificar")
SIN_IMPUESTO = "ninguno"  # código explícito de «este producto no lleva» (útil en `clasificar`)
TOLERANCIA_CUADRE = 0.01  # ±1 %: precio_neto y precio_bruto de una misma fila
ETIQUETA_POR_CLASIFICAR = "impuesto por clasificar"


class ErrorImpuesto(ValueError):
    """Entrada inválida (→ 422 o mensaje en la pantalla)."""


class _PorClasificar:
    """Singleton: el producto lleva un impuesto, pero su tasa aún no se clasificó."""

    _inst: _PorClasificar | None = None

    def __new__(cls) -> _PorClasificar:
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __repr__(self) -> str:
        return "POR_CLASIFICAR"

    def __bool__(self) -> bool:  # ninguna tasa que sumar
        return False


POR_CLASIFICAR = _PorClasificar()


@dataclass(frozen=True)
class Impuesto:
    codigo: str
    nombre: str
    tasa: float
    recuperable: bool
    vigente_desde: date

    @property
    def etiqueta(self) -> str:
        return f"{self.nombre} {self.tasa * 100:g} %"

    def a_dict(self) -> dict[str, Any]:
        """Lo que guarda una línea de la oferta (las líneas guardan su impuesto)."""
        return {
            "codigo": self.codigo,
            "nombre": self.nombre,
            "tasa": self.tasa,
            "recuperable": self.recuperable,
            "vigente_desde": self.vigente_desde.isoformat(),
        }

    @staticmethod
    def de_dict(d: Any) -> Impuesto | None:
        """Parseo defensivo de lo guardado: algo roto = sin impuesto (nunca rompe la oferta)."""
        if not isinstance(d, dict):
            return None
        try:
            return Impuesto(
                codigo=str(d["codigo"]),
                nombre=str(d.get("nombre") or d["codigo"]),
                tasa=float(d["tasa"]),
                recuperable=bool(d.get("recuperable", True)),
                vigente_desde=date.fromisoformat(str(d.get("vigente_desde") or "2000-01-01")),
            )
        except (KeyError, TypeError, ValueError):
            return None


Resolucion = Impuesto | _PorClasificar | None


def _iva() -> float:
    from app.mercado.cotizacion import IVA  # lazy: cotizacion importa oferta, que importa esto

    return IVA


# ---------------------------------------------------------------------------
# Tabla en memoria (una consulta por pantalla o importación)
# ---------------------------------------------------------------------------


@dataclass
class Tabla:
    """Las tasas por código (ordenadas por vigencia) y las reglas por familia normalizada."""

    tasas: dict[str, list[Impuesto]] = field(default_factory=dict)
    familias: dict[str, tuple[str, str]] = field(
        default_factory=dict
    )  # norm(familia) → (código, modo)

    @property
    def codigos(self) -> list[str]:
        return sorted(self.tasas)

    def existe(self, codigo: str | None) -> bool:
        return codigo == SIN_IMPUESTO or (codigo is not None and codigo in self.tasas)

    def tasa(self, codigo: str | None, fecha: date) -> Impuesto | None:
        """La fila de `codigo` vigente en `fecha`. Código desconocido o `ninguno` → None."""
        filas = self.tasas.get(codigo or "")
        if not filas:
            return None
        vigente = [i for i in filas if i.vigente_desde <= fecha]
        return vigente[-1] if vigente else filas[0]

    def regla(self, familia: str | None) -> tuple[str, str] | None:
        return self.familias.get(norm(familia)) if familia else None

    def de_producto(
        self, impuesto_codigo: str | None, familia: str | None, fecha: date
    ) -> Resolucion:
        """Producto canónico → su código; si no tiene, la regla de su familia; si no, nada."""
        if impuesto_codigo:
            return self.tasa(impuesto_codigo, fecha)
        r = self.regla(familia)
        if r is None:
            return None
        codigo, modo = r
        if modo == "clasificar":
            return POR_CLASIFICAR
        return self.tasa(codigo, fecha)

    def codigo_resuelto(self, impuesto_codigo: str | None, familia: str | None) -> str | None:
        """El código que el catálogo asigna (sin mirar fechas). `clasificar` sin código → None."""
        if impuesto_codigo:
            return impuesto_codigo
        r = self.regla(familia)
        if r is None:
            return SIN_IMPUESTO
        return None if r[1] == "clasificar" else r[0]


def cargar(session: Session) -> Tabla:
    t = Tabla()
    for fila in session.execute(
        select(ImpuestoEspecifico).order_by(
            ImpuestoEspecifico.codigo, ImpuestoEspecifico.vigente_desde
        )
    ).scalars():
        t.tasas.setdefault(fila.codigo, []).append(
            Impuesto(
                fila.codigo,
                fila.nombre,
                float(fila.tasa),
                bool(fila.recuperable),
                fila.vigente_desde,
            )  # fmt: skip
        )
    for regla in session.execute(select(FamiliaImpuesto)).scalars():
        t.familias[norm(regla.familia)] = (regla.impuesto_codigo, regla.modo)
    return t


# ---------------------------------------------------------------------------
# Resolución
# ---------------------------------------------------------------------------


def datos_productos(
    session: Session, ids: Iterable[int]
) -> dict[int, tuple[str | None, str | None]]:
    ids = {i for i in ids if i is not None}
    if not ids:
        return {}
    return {
        int(pid): (codigo, familia)
        for pid, codigo, familia in session.execute(
            select(
                ProductoCanonico.id, ProductoCanonico.impuesto_codigo, ProductoCanonico.familia
            ).where(ProductoCanonico.id.in_(ids))
        )
    }


def impuesto_de(
    session: Session,
    pp_o_producto: ProveedorProducto | ProductoCanonico | int | None,
    fecha: date,
    tabla: Tabla | None = None,
) -> Resolucion:
    """El impuesto de un producto de proveedor, de un producto canónico (objeto o id) en `fecha`.

    Producto de proveedor CON vínculo → el del catálogo; SIN vínculo → su pista de la planilla."""
    t = tabla or cargar(session)
    if pp_o_producto is None:
        return None
    if isinstance(pp_o_producto, ProductoCanonico):
        return t.de_producto(pp_o_producto.impuesto_codigo, pp_o_producto.familia, fecha)
    if isinstance(pp_o_producto, ProveedorProducto):
        if pp_o_producto.producto_id is None:
            return t.tasa(pp_o_producto.impuesto_codigo, fecha)
        pp_o_producto = pp_o_producto.producto_id
    datos = datos_productos(session, [int(pp_o_producto)]).get(int(pp_o_producto))
    if datos is None:
        return None
    return t.de_producto(datos[0], datos[1], fecha)


def de_productos(
    session: Session, producto_ids: Iterable[int], fecha: date, tabla: Tabla | None = None
) -> dict[int, Resolucion]:
    """Lo mismo para varios productos canónicos con UNA consulta."""
    t = tabla or cargar(session)
    return {
        pid: t.de_producto(cod, fam, fecha)
        for pid, (cod, fam) in datos_productos(session, producto_ids).items()
    }


def de_proveedor_productos(
    session: Session, pps: Sequence[ProveedorProducto], tabla: Tabla | None = None
) -> dict[int, Resolucion]:
    """`{pp.id: impuesto}` en la fecha de cada costo (`vigente_desde`), con UNA consulta."""
    t = tabla or cargar(session)
    canon = datos_productos(session, [pp.producto_id for pp in pps if pp.producto_id is not None])
    out: dict[int, Resolucion] = {}
    for pp in pps:
        if pp.producto_id is None:
            out[pp.id] = t.tasa(pp.impuesto_codigo, pp.vigente_desde)
        else:
            cod, fam = canon.get(pp.producto_id, (None, None))
            out[pp.id] = t.de_producto(cod, fam, pp.vigente_desde)
    return out


def pista_contradice(
    tabla: Tabla, pista: str | None, impuesto_codigo: str | None, familia: str | None
) -> bool:
    """«El catálogo dice otra cosa»: hay vínculo y pista, y el catálogo resuelve otro código. Una
    familia `clasificar` sin código no contradice (el catálogo todavía no dice nada)."""
    if not pista:
        return False
    catalogo = tabla.codigo_resuelto(impuesto_codigo, familia)
    return catalogo is not None and catalogo != pista


# ---------------------------------------------------------------------------
# Neto ↔ bruto
# ---------------------------------------------------------------------------


def factor_bruto(impuesto: Resolucion) -> float:
    """1 + IVA + la tasa del impuesto si es recuperable. Por clasificar o no recuperable: solo IVA."""
    extra = impuesto.tasa if isinstance(impuesto, Impuesto) and impuesto.recuperable else 0.0
    return 1.0 + _iva() + extra


def bruto(neto: float, impuesto: Resolucion) -> float:
    """`neto × (1 + IVA + tasa)`: IVA e impuesto específico sobre el MISMO neto."""
    return neto * factor_bruto(impuesto)


def neto_desde_bruto(precio_bruto: float, impuesto: Resolucion) -> float:
    """Inversa de `bruto`, a centavos (así `round(bruto(neto_desde_bruto(b))) == round(b)`).
    Con `recuperable = false` el neto incluye el impuesto (`bruto ÷ (1 + IVA)`)."""
    return round(precio_bruto / factor_bruto(impuesto), 2)


def tramos_a_neto(
    tramos: list[dict[str, float]] | None, impuesto: Resolucion
) -> list[dict[str, float]] | None:
    if not tramos:
        return tramos
    return [
        {"desde": t["desde"], "precio": neto_desde_bruto(t["precio"], impuesto)} for t in tramos
    ]


def cuadran(precio_neto: float, precio_bruto: float, impuesto: Resolucion) -> bool:
    """¿El neto informado y el calculado desde el bruto difieren a lo más ±1 %?"""
    calculado = neto_desde_bruto(precio_bruto, impuesto)
    return calculado > 0 and abs(precio_neto - calculado) / calculado <= TOLERANCIA_CUADRE


@dataclass(frozen=True)
class FilaImpuestoTotal:
    codigo: str
    etiqueta: str
    monto: int


@dataclass(frozen=True)
class Totales:
    neto: int
    iva: int
    especificos: list[FilaImpuestoTotal]  # solo los recuperables: los otros ya están en el neto
    bruto: int


def totales(lineas: Iterable[tuple[float | None, Impuesto | None]]) -> Totales:
    """(total neto de la línea, su impuesto) → neto, IVA, una fila por impuesto y bruto, en pesos
    enteros: se redondea cada total, no cada línea."""
    neto = 0.0
    por_codigo: dict[str, tuple[str, float]] = {}
    for total, imp in lineas:
        if not total:
            continue
        neto += total
        if isinstance(imp, Impuesto) and imp.recuperable and imp.tasa > 0:
            etiqueta, acum = por_codigo.get(imp.codigo, (imp.etiqueta, 0.0))
            por_codigo[imp.codigo] = (etiqueta, acum + total * imp.tasa)
    neto_r = round(neto)
    iva = round(neto_r * _iva())
    especificos = [FilaImpuestoTotal(c, et, round(m)) for c, (et, m) in sorted(por_codigo.items())]
    return Totales(neto_r, iva, especificos, neto_r + iva + sum(e.monto for e in especificos))


# ---------------------------------------------------------------------------
# Edición del catálogo (solo admin: lo valida la ruta)
# ---------------------------------------------------------------------------


def _validar_codigo(tabla: Tabla, codigo: str | None, *, permitir_ninguno: bool) -> str:
    c = (codigo or "").strip()
    if not c:
        raise ErrorImpuesto("Elige un impuesto")
    if c == SIN_IMPUESTO and permitir_ninguno:
        return c
    if c not in tabla.tasas:
        raise ErrorImpuesto(f"El impuesto {c!r} no está cargado (ver Impuestos del catálogo)")
    return c


def guardar_regla_familia(
    session: Session, familia: str, codigo: str, modo: str, usuario_id: int | None
) -> FamiliaImpuesto:
    fam = " ".join((familia or "").split())[:100]
    if not fam:
        raise ErrorImpuesto("La familia es obligatoria")
    if modo not in MODOS:
        raise ErrorImpuesto("Modo: aplica o clasificar")
    c = _validar_codigo(cargar(session), codigo, permitir_ninguno=False)
    regla = next(
        (
            r
            for r in session.execute(select(FamiliaImpuesto)).scalars()
            if norm(r.familia) == norm(fam)
        ),
        None,
    )
    if regla is None:
        regla = FamiliaImpuesto(familia=fam)
        session.add(regla)
    regla.impuesto_codigo, regla.modo, regla.actualizado_por = c, modo, usuario_id
    session.flush()
    return regla


def quitar_regla_familia(session: Session, familia: str) -> bool:
    for r in session.execute(select(FamiliaImpuesto)).scalars():
        if norm(r.familia) == norm(familia):
            session.delete(r)
            session.flush()
            return True
    return False


def asignar_a_productos(session: Session, producto_ids: Sequence[int], codigo: str | None) -> int:
    """Código para varios productos canónicos (vacío = vuelve a la regla de la familia). Devuelve
    cuántos cambiaron."""
    c = (
        None
        if not (codigo or "").strip()
        else _validar_codigo(cargar(session), codigo, permitir_ninguno=True)
    )
    ids = sorted({int(i) for i in producto_ids})
    if not ids:
        return 0
    n = 0
    for p in session.execute(
        select(ProductoCanonico).where(ProductoCanonico.id.in_(ids))
    ).scalars():
        if p.impuesto_codigo != c:
            p.impuesto_codigo = c
            n += 1
    session.flush()
    return n


def ids_por_clasificar(session: Session, tabla: Tabla | None = None) -> set[int]:
    """Productos canónicos ACTIVOS de una familia `clasificar` sin código propio."""
    t = tabla or cargar(session)
    clasificar = {f for f, (_c, modo) in t.familias.items() if modo == "clasificar"}
    if not clasificar:
        return set()
    return {
        int(pid)
        for pid, familia in session.execute(
            select(ProductoCanonico.id, ProductoCanonico.familia).where(
                ProductoCanonico.activo.is_(True),
                ProductoCanonico.impuesto_codigo.is_(None),
                ProductoCanonico.familia.is_not(None),
            )
        )
        if norm(familia) in clasificar
    }


def por_clasificar(session: Session, tabla: Tabla | None = None) -> list[ProductoCanonico]:
    ids = ids_por_clasificar(session, tabla)
    if not ids:
        return []
    return list(
        session.execute(
            select(ProductoCanonico)
            .where(ProductoCanonico.id.in_(ids))
            .order_by(ProductoCanonico.familia, ProductoCanonico.nombre)
        ).scalars()
    )


def etiqueta(impuesto: Resolucion) -> str:
    if impuesto is POR_CLASIFICAR:
        return ETIQUETA_POR_CLASIFICAR
    if isinstance(impuesto, Impuesto):
        return impuesto.etiqueta + ("" if impuesto.recuperable else " (en el costo)")
    return ""


def por_clasificar_por_proveedor(session: Session, empresa_id: int) -> dict[int, int]:
    """`{proveedor_id: n productos «impuesto por clasificar»}` de la empresa (regla 26): los
    vinculados a un canónico por clasificar. Para el filtro de Catálogo Proveedores."""
    ids = ids_por_clasificar(session)
    if not ids:
        return {}
    out: dict[int, int] = {}
    for pid in session.execute(
        select(ProveedorProducto.proveedor_id)
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(Proveedor.empresa_id == empresa_id, ProveedorProducto.producto_id.in_(ids))
    ).scalars():
        out[pid] = out.get(pid, 0) + 1
    return out
