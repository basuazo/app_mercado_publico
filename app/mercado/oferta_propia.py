"""Capa «Inventario» del catálogo en tres capas (F-plat-capas): qué tiene la empresa para
responder una Compra Ágil.

Las tres capas comparten la llave `producto_canonico × producto_formato`: (a) esta tabla por
empresa (`empresa_productos`), (b) `proveedor_productos` (por empresa vía proveedor) y (c) la
demanda (`ca_lineas`, común). Acá no hay httpx ni red. Todo se filtra por `empresa_id`
(reglas 17 y 26): un id de otra empresa es `InventarioAjeno` (→ 404). Sin commit: lo hace
quien llama.

Stock en unidades del FORMATO (`stock`, `reservado`); `cobertura_*` lo convierte a la unidad
base del producto con `producto_formato.factor_base`. No es inventario contable: cada cambio
de stock deja un movimiento append-only (`empresa_producto_movimientos`), que F-plat-lote y
F-plat-seguimiento usan para reservar y descontar al entregar.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.tiempo import ahora_utc
from app.mercado.catalogo import CatalogoError, _formato_valido, _producto_activo
from app.models.tables import (
    CaLinea,
    CostoHistorial,
    EmpresaProducto,
    EmpresaProductoMovimiento,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
)

MOTIVOS_AJUSTE = ("ajuste", "compra", "entrega")
ETIQUETA_MOTIVO = {
    "ajuste": "Ajuste",
    "compra": "Compra (entra)",
    "entrega": "Entrega (sale)",
    "reserva": "Reserva",
    "liberacion": "Liberación",
}
PLAZO_MAX_DIAS = 365
CANTIDAD_MAX = 1e9
NOTAS_MAX = 2000
LOTE_CODIGOS = 400
_LIKE_ESCAPE = "\\"
_TOL = 1e-6


class ErrorInventario(ValueError):
    """Entrada inválida (→ 422): el mensaje se muestra al usuario."""


class InventarioAjeno(LookupError):
    """Producto del inventario o proveedor de otra empresa, o inexistente (→ 404, regla 26)."""


class _Sin:
    """«No viene»: deja el campo como está (a diferencia de None, que lo borra)."""

    def __repr__(self) -> str:  # pragma: no cover
        return "SIN"


SIN = _Sin()


@dataclass(frozen=True)
class VistaInventario:
    ep: EmpresaProducto
    producto: ProductoCanonico
    formato: ProductoFormato
    proveedor: Proveedor | None

    @property
    def bajo_minimo(self) -> bool:
        m = self.ep.stock_minimo
        return m is not None and self.ep.disponible < m


# ---------------------------------------------------------------------------
# Lectura
# ---------------------------------------------------------------------------


def _escapar_like(texto: str) -> str:
    return (
        texto.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2).replace("%", r"\%").replace("_", r"\_")
    )


def listar(
    session: Session, empresa_id: int, *, solo_activos: bool = True, texto: str = ""
) -> list[VistaInventario]:
    """Inventario de la empresa, por nombre de producto. `texto` busca (ILIKE, escapado) en el
    producto, el formato y la marca."""
    consulta = (
        select(EmpresaProducto, ProductoCanonico, ProductoFormato, Proveedor)
        .join(ProductoCanonico, ProductoCanonico.id == EmpresaProducto.producto_id)
        .join(ProductoFormato, ProductoFormato.id == EmpresaProducto.formato_id)
        .outerjoin(Proveedor, Proveedor.id == EmpresaProducto.proveedor_habitual_id)
        .where(EmpresaProducto.empresa_id == empresa_id)
        .order_by(ProductoCanonico.nombre, ProductoFormato.factor_base, EmpresaProducto.id)
    )
    if solo_activos:
        consulta = consulta.where(EmpresaProducto.activo.is_(True))
    t = (texto or "").strip()[:100]
    if t:
        patron = f"%{_escapar_like(t)}%"
        consulta = consulta.where(
            or_(
                ProductoCanonico.nombre.ilike(patron, escape=_LIKE_ESCAPE),
                ProductoFormato.nombre.ilike(patron, escape=_LIKE_ESCAPE),
                EmpresaProducto.marca.ilike(patron, escape=_LIKE_ESCAPE),
            )
        )
    return [VistaInventario(*fila) for fila in session.execute(consulta).all()]


def vista_de(session: Session, empresa_id: int, ep_id: int) -> VistaInventario:
    fila = session.execute(
        select(EmpresaProducto, ProductoCanonico, ProductoFormato, Proveedor)
        .join(ProductoCanonico, ProductoCanonico.id == EmpresaProducto.producto_id)
        .join(ProductoFormato, ProductoFormato.id == EmpresaProducto.formato_id)
        .outerjoin(Proveedor, Proveedor.id == EmpresaProducto.proveedor_habitual_id)
        .where(EmpresaProducto.id == ep_id, EmpresaProducto.empresa_id == empresa_id)
    ).first()
    if fila is None:
        raise InventarioAjeno(ep_id)
    return VistaInventario(*fila)


def obtener(
    session: Session, empresa_id: int, ep_id: int, *, para_escribir: bool = False
) -> EmpresaProducto:
    """El producto del inventario si es de la empresa; si no, `InventarioAjeno` (→ 404)."""
    consulta = select(EmpresaProducto).where(
        EmpresaProducto.id == ep_id, EmpresaProducto.empresa_id == empresa_id
    )
    if para_escribir:
        consulta = consulta.with_for_update()
    ep = session.execute(consulta).scalar_one_or_none()
    if ep is None:
        raise InventarioAjeno(ep_id)
    return ep


def movimientos(
    session: Session, empresa_id: int, ep_id: int, limite: int = 20
) -> list[EmpresaProductoMovimiento]:
    obtener(session, empresa_id, ep_id)
    return list(
        session.execute(
            select(EmpresaProductoMovimiento)
            .where(EmpresaProductoMovimiento.empresa_producto_id == ep_id)
            .order_by(EmpresaProductoMovimiento.creado_en.desc(), EmpresaProductoMovimiento.id.desc())
            .limit(limite)
        ).scalars()
    )


def resumen(session: Session, empresa_id: int) -> tuple[int, int]:
    """`(productos activos, bajo mínimo)` para la tarjeta de «Hoy»."""
    n, bajo = session.execute(
        select(
            func.count(EmpresaProducto.id),
            func.count(EmpresaProducto.id).filter(
                and_(
                    EmpresaProducto.stock_minimo.is_not(None),
                    EmpresaProducto.stock - EmpresaProducto.reservado
                    < EmpresaProducto.stock_minimo,
                )
            ),
        ).where(EmpresaProducto.empresa_id == empresa_id, EmpresaProducto.activo.is_(True))
    ).one()
    return int(n), int(bajo)


def tiene_inventario(session: Session, empresa_id: int) -> bool:
    return (
        session.execute(
            select(EmpresaProducto.id)
            .where(EmpresaProducto.empresa_id == empresa_id, EmpresaProducto.activo.is_(True))
            .limit(1)
        ).first()
        is not None
    )


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------


def _numero(valor: Any, nombre: str, *, minimo: float = 0.0) -> float:
    try:
        v = float(valor)
    except (TypeError, ValueError):
        raise ErrorInventario(f"{nombre}: no es un número") from None
    if not math.isfinite(v) or v < minimo or v > CANTIDAD_MAX:
        raise ErrorInventario(f"{nombre}: debe estar entre {minimo:g} y {CANTIDAD_MAX:g}")
    return round(v, 3)


def _proveedor_de_empresa(session: Session, empresa_id: int, proveedor_id: int) -> int:
    ok = session.execute(
        select(Proveedor.id).where(Proveedor.id == proveedor_id, Proveedor.empresa_id == empresa_id)
    ).first()
    if ok is None:
        raise InventarioAjeno(proveedor_id)
    return proveedor_id


def _validar_producto_formato(session: Session, producto_id: int, formato_id: int | None) -> int:
    """Producto activo y formato suyo; sin formato → el formato base (factor 1)."""
    try:
        producto = _producto_activo(session, producto_id)
        if formato_id is None:
            base = session.execute(
                select(ProductoFormato.id)
                .where(
                    ProductoFormato.producto_id == producto.id,
                    ProductoFormato.factor_base == 1.0,
                )
                .order_by(ProductoFormato.id)
                .limit(1)
            ).scalar_one_or_none()
            if base is None:
                raise ErrorInventario("El producto no tiene un formato base: elige un formato")
            return int(base)
        _formato_valido(session, producto.id, formato_id)
    except CatalogoError as exc:
        raise ErrorInventario(str(exc)) from None
    return formato_id


def _mover(
    session: Session,
    ep: EmpresaProducto,
    delta: float,
    motivo: str,
    referencia: str | None,
    usuario_id: int | None,
) -> None:
    ep.stock = round((ep.stock or 0.0) + delta, 3)
    session.add(
        EmpresaProductoMovimiento(
            empresa_producto_id=ep.id,
            delta=delta,
            motivo=motivo,
            referencia=(referencia or "").strip()[:100] or None,
            usuario_id=usuario_id,
        )
    )


def _poner_campos(
    session: Session,
    empresa_id: int,
    ep: EmpresaProducto,
    *,
    stock_minimo: float | None | _Sin,
    plazo: int | None | _Sin,
    proveedor_habitual_id: int | None | _Sin,
    marca: str | None | _Sin,
    notas: str | None | _Sin,
    activo: bool | None,
) -> bool:
    """Aplica solo lo que viene (SIN = no tocar). Devuelve si algo cambió."""
    cambios: dict[str, Any] = {}
    if not isinstance(stock_minimo, _Sin):
        cambios["stock_minimo"] = (
            None if stock_minimo is None else _numero(stock_minimo, "Stock mínimo")
        )
    if not isinstance(plazo, _Sin):
        if plazo is not None and not 0 <= plazo <= PLAZO_MAX_DIAS:
            raise ErrorInventario(f"Plazo de reposición: de 0 a {PLAZO_MAX_DIAS} días")
        cambios["plazo_reposicion_dias"] = plazo
    if not isinstance(proveedor_habitual_id, _Sin):
        cambios["proveedor_habitual_id"] = (
            None
            if proveedor_habitual_id is None
            else _proveedor_de_empresa(session, empresa_id, proveedor_habitual_id)
        )
    if not isinstance(marca, _Sin):
        cambios["marca"] = (marca or "").strip()[:100] or None
    if not isinstance(notas, _Sin):
        cambios["notas"] = (notas or "").strip()[:NOTAS_MAX] or None
    if activo is not None:
        cambios["activo"] = activo
    cambio = False
    for campo, valor in cambios.items():
        if getattr(ep, campo) != valor:
            setattr(ep, campo, valor)
            cambio = True
    return cambio


def guardar(
    session: Session,
    empresa_id: int,
    usuario_id: int | None,
    *,
    producto_id: int,
    formato_id: int | None,
    stock: float | None = None,
    stock_minimo: float | None | _Sin = SIN,
    plazo: int | None | _Sin = SIN,
    proveedor_habitual_id: int | None | _Sin = SIN,
    marca: str | None | _Sin = SIN,
    notas: str | None | _Sin = SIN,
    activo: bool | None = None,
    referencia: str = "alta",
) -> tuple[EmpresaProducto, str]:
    """Crea o actualiza (upsert por empresa × producto × formato). Devuelve (fila,
    'nuevo'|'actualizado'|'igual'). `stock` es el stock FINAL que se quiere: si difiere del
    actual, escribe un movimiento `ajuste` por la diferencia (misma transacción). Los campos en
    `SIN` no se tocan; `None` los borra. Un producto desactivado se reactiva al guardarlo.
    """
    formato_id = _validar_producto_formato(session, producto_id, formato_id)
    ep = session.execute(
        select(EmpresaProducto)
        .where(
            EmpresaProducto.empresa_id == empresa_id,
            EmpresaProducto.producto_id == producto_id,
            EmpresaProducto.formato_id == formato_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    nuevo = ep is None
    if ep is None:
        ep = EmpresaProducto(
            empresa_id=empresa_id,
            producto_id=producto_id,
            formato_id=formato_id,
            stock=0.0,
            reservado=0.0,
            activo=True,
        )
        session.add(ep)
        session.flush()
    cambio = nuevo
    if not nuevo and not ep.activo and activo is None:
        ep.activo = True  # volver a cargarlo lo reactiva
        cambio = True
    cambio = (
        _poner_campos(
            session,
            empresa_id,
            ep,
            stock_minimo=stock_minimo,
            plazo=plazo,
            proveedor_habitual_id=proveedor_habitual_id,
            marca=marca,
            notas=notas,
            activo=activo,
        )
        or cambio
    )
    if stock is not None:
        objetivo = _numero(stock, "Stock")
        delta = round(objetivo - (ep.stock or 0.0), 3)
        if abs(delta) > _TOL:
            _mover(session, ep, delta, "ajuste", referencia, usuario_id)
            cambio = True
    if cambio:
        ep.actualizado_por_id = usuario_id
        ep.actualizado_en = ahora_utc()
    session.flush()
    return ep, ("nuevo" if nuevo else "actualizado" if cambio else "igual")


def actualizar(
    session: Session,
    empresa_id: int,
    ep_id: int,
    usuario_id: int | None,
    *,
    stock_minimo: float | None,
    plazo: int | None,
    proveedor_habitual_id: int | None,
    marca: str | None,
    notas: str | None,
    activo: bool,
) -> EmpresaProducto:
    """Edición en línea: todos los campos vienen del formulario (vacío = borrar). No toca el
    stock: eso es `ajustar_stock`."""
    ep = obtener(session, empresa_id, ep_id, para_escribir=True)
    if _poner_campos(
        session,
        empresa_id,
        ep,
        stock_minimo=stock_minimo,
        plazo=plazo,
        proveedor_habitual_id=proveedor_habitual_id,
        marca=marca,
        notas=notas,
        activo=activo,
    ):
        ep.actualizado_por_id = usuario_id
        ep.actualizado_en = ahora_utc()
    session.flush()
    return ep


def desactivar(session: Session, empresa_id: int, ep_id: int, usuario_id: int | None) -> None:
    ep = obtener(session, empresa_id, ep_id, para_escribir=True)
    if ep.activo:
        ep.activo = False
        ep.actualizado_por_id = usuario_id
        ep.actualizado_en = ahora_utc()
    session.flush()


def ajustar_stock(
    session: Session,
    empresa_id: int,
    ep_id: int,
    delta: float,
    motivo: str,
    referencia: str | None,
    usuario_id: int | None,
) -> EmpresaProducto:
    """Suma `delta` (con signo, en unidades del formato) al stock y escribe el movimiento en la
    MISMA transacción. El stock nunca queda negativo (ErrorInventario, sin escribir nada).

    Motivos: `ajuste` (cualquier signo), `compra` (entra: > 0) y `entrega` (sale: < 0).
    `reserva` y `liberacion` son de F-plat-lote y mueven `reservado`, no `stock`; que
    `reservado ≤ stock` se valida al reservar, no acá."""
    if motivo not in MOTIVOS_AJUSTE:
        raise ErrorInventario("Motivo inválido")
    d = _numero(delta, "Cantidad", minimo=-CANTIDAD_MAX)
    if abs(d) < _TOL:
        raise ErrorInventario("La cantidad no puede ser 0")
    if (motivo == "compra" and d < 0) or (motivo == "entrega" and d > 0):
        raise ErrorInventario(
            "Una compra suma stock (cantidad positiva) y una entrega lo resta (negativa)"
        )
    ep = obtener(session, empresa_id, ep_id, para_escribir=True)
    if (ep.stock or 0.0) + d < -_TOL:
        raise ErrorInventario(f"El stock no puede quedar negativo (hay {ep.stock:g})")
    _mover(session, ep, d, motivo, referencia, usuario_id)
    ep.actualizado_por_id = usuario_id
    ep.actualizado_en = ahora_utc()
    session.flush()
    return ep


# ---------------------------------------------------------------------------
# Cobertura de una Compra Ágil
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cobertura:
    """Lo que la empresa tiene de UN producto, en la unidad base, frente a UNA línea de CA."""

    disponible: float  # unidad base: Σ (stock − reservado) × factor de cada formato activo
    en_stock: bool  # cubre la cantidad de la línea (sin cantidad: hay algo)
    plazo_dias: int | None  # el menor plazo de reposición declarado
    proveedor_habitual: str | None
    faltante: float = 0.0  # unidad base; 0 si cubre
    empresa_producto_id: int | None = None  # el formato con más disponible (el que se ofrece)


@dataclass(frozen=True)
class ResumenCA:
    """Para la tarjeta de la mesa: «Tengo 3 de 5 líneas en stock · falta: arroz 20 kg»."""

    total: int  # líneas ok|dudosa con producto
    en_stock: int
    faltan: list[str] = field(default_factory=list)

    @property
    def texto_faltan(self) -> str:
        visibles = self.faltan[:3]
        resto = len(self.faltan) - len(visibles)
        return ", ".join(visibles) + (f" y {resto} más" if resto > 0 else "")


def _entradas(
    session: Session, empresa_id: int, producto_ids: Iterable[int]
) -> dict[int, list[tuple[EmpresaProducto, float, str | None]]]:
    ids = sorted(set(producto_ids))
    out: dict[int, list[tuple[EmpresaProducto, float, str | None]]] = {}
    if not ids:
        return out
    for ep, factor, habitual in session.execute(
        select(EmpresaProducto, ProductoFormato.factor_base, Proveedor.nombre)
        .join(ProductoFormato, ProductoFormato.id == EmpresaProducto.formato_id)
        .outerjoin(Proveedor, Proveedor.id == EmpresaProducto.proveedor_habitual_id)
        .where(
            EmpresaProducto.empresa_id == empresa_id,
            EmpresaProducto.activo.is_(True),
            EmpresaProducto.producto_id.in_(ids),
        )
        .order_by(EmpresaProducto.id)
    ):
        out.setdefault(ep.producto_id, []).append((ep, float(factor), habitual))
    return out


def _cobertura(
    entradas: Sequence[tuple[EmpresaProducto, float, str | None]], cantidad_base: float | None
) -> Cobertura:
    disponible = sum(max(ep.disponible, 0.0) * factor for ep, factor, _ in entradas)
    disponible = round(disponible, 6)
    if cantidad_base:
        en_stock = disponible + _TOL >= cantidad_base
        faltante = 0.0 if en_stock else round(cantidad_base - disponible, 6)
    else:
        en_stock, faltante = disponible > _TOL, 0.0
    plazos = [ep.plazo_reposicion_dias for ep, _, _ in entradas if ep.plazo_reposicion_dias is not None]
    habitual = next((h for _, _, h in entradas if h), None)
    mejor = max(entradas, key=lambda e: max(e[0].disponible, 0.0) * e[1])[0]
    return Cobertura(
        disponible=disponible,
        en_stock=en_stock,
        plazo_dias=min(plazos) if plazos else None,
        proveedor_habitual=habitual,
        faltante=faltante,
        empresa_producto_id=mejor.id,
    )


def _lineas_cubribles(session: Session, codigos: Sequence[str]) -> list[CaLinea]:
    return list(
        session.execute(
            select(CaLinea)
            .where(
                CaLinea.ca_codigo.in_(list(codigos)),
                CaLinea.estado.in_(("ok", "dudosa")),
                CaLinea.producto_id.is_not(None),
            )
            .order_by(CaLinea.ca_codigo, CaLinea.n)
        ).scalars()
    )


def cobertura_de_ca(session: Session, empresa_id: int, codigo: str) -> dict[int, Cobertura]:
    """`{linea_id: Cobertura}` de las líneas `ok|dudosa` de la CA cuyo producto la empresa
    tiene (cualquier formato: se suma en la unidad base con `factor_base`). Cada línea se mide
    sola: dos líneas del mismo producto no se descuentan entre sí."""
    lineas = _lineas_cubribles(session, [codigo])
    por_producto = _entradas(session, empresa_id, (ln.producto_id for ln in lineas if ln.producto_id))
    return {
        ln.id: _cobertura(por_producto[ln.producto_id], ln.cantidad_base)
        for ln in lineas
        if ln.producto_id in por_producto
    }


def resumen_por_ca(
    session: Session, empresa_id: int, codigos: Sequence[str]
) -> dict[str, ResumenCA]:
    """Resumen de cobertura de muchas CA con 2 consultas por lote (nunca una por tarjeta).
    Las CA sin líneas `ok|dudosa` con producto no aparecen."""
    acumulado: dict[str, list[CaLinea]] = {}
    for i in range(0, len(codigos), LOTE_CODIGOS):
        for ln in _lineas_cubribles(session, codigos[i : i + LOTE_CODIGOS]):
            acumulado.setdefault(ln.ca_codigo, []).append(ln)
    todas = [ln for ls in acumulado.values() for ln in ls]
    por_producto = _entradas(session, empresa_id, (ln.producto_id for ln in todas if ln.producto_id))
    nombres: dict[int, tuple[str, str]] = {
        pid: (nombre, ub)
        for pid, nombre, ub in session.execute(
            select(
                ProductoCanonico.id, ProductoCanonico.nombre, ProductoCanonico.unidad_base
            ).where(ProductoCanonico.id.in_({ln.producto_id for ln in todas if ln.producto_id}))
        )
    } if todas else {}
    out: dict[str, ResumenCA] = {}
    for codigo, lineas in acumulado.items():
        cubiertas = 0
        faltan: list[str] = []
        for ln in lineas:
            assert ln.producto_id is not None
            entradas = por_producto.get(ln.producto_id)
            c = _cobertura(entradas, ln.cantidad_base) if entradas else None
            if c is not None and c.en_stock:
                cubiertas += 1
                continue
            nombre, ub = nombres.get(ln.producto_id, ("producto", ""))
            falta = c.faltante if c is not None else (ln.cantidad_base or 0.0)
            faltan.append(f"{nombre} {falta:g} {ub}".strip() if falta else nombre)
        out[codigo] = ResumenCA(total=len(lineas), en_stock=cubiertas, faltan=faltan)
    return out


def filtrar_con_stock(codigos: list[str], resumenes: dict[str, ResumenCA]) -> list[str]:
    """Filtro «Con stock» de la mesa: al menos una línea cubierta; conserva el orden."""
    return [c for c in codigos if c in resumenes and resumenes[c].en_stock > 0]


# ---------------------------------------------------------------------------
# «Stock propio» como origen de costo en «mi oferta»
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StockPropio:
    empresa_producto_id: int
    disponible: float  # unidad base
    costo_por_base: float | None
    proveedor: str | None  # de dónde sale ese costo (habitual o último con precio)


def costo_de_reposicion(
    session: Session, empresa_id: int, producto_id: int, unidad_base: str, habitual_id: int | None
) -> tuple[float, str] | None:
    """Costo por unidad base del stock propio. Regla (F-plat-capas §3):
    1. el `proveedor_habitual` del producto, con su precio actual por unidad base;
    2. si no hay (o no tiene precio comparable), el último `costo_historial` de CUALQUIER
       proveedor de la empresa para ese producto (más reciente por `vigente_desde`).
    Solo cuentan precios con `factor_base` y la misma unidad base. None si no hay ninguno."""
    if habitual_id is not None:
        pp = session.execute(
            select(ProveedorProducto, Proveedor.nombre)
            .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
            .where(
                Proveedor.id == habitual_id,
                Proveedor.empresa_id == empresa_id,
                ProveedorProducto.producto_id == producto_id,
                ProveedorProducto.unidad_base == unidad_base,
                ProveedorProducto.factor_base.is_not(None),
                ProveedorProducto.factor_base > 0,
            )
            .order_by(ProveedorProducto.vigente_desde.desc(), ProveedorProducto.id.desc())
            .limit(1)
        ).first()
        if pp is not None and pp[0].factor_base:
            return pp[0].precio_neto_actual / pp[0].factor_base, pp[1]
    fila = session.execute(
        select(CostoHistorial.precio_neto, ProveedorProducto.factor_base, Proveedor.nombre)
        .join(ProveedorProducto, ProveedorProducto.id == CostoHistorial.proveedor_producto_id)
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(
            Proveedor.empresa_id == empresa_id,
            ProveedorProducto.producto_id == producto_id,
            ProveedorProducto.unidad_base == unidad_base,
            ProveedorProducto.factor_base.is_not(None),
            ProveedorProducto.factor_base > 0,
        )
        .order_by(CostoHistorial.vigente_desde.desc(), CostoHistorial.id.desc())
        .limit(1)
    ).first()
    if fila is None:
        return None
    return float(fila[0]) / float(fila[1]), str(fila[2])


def stock_propio(
    session: Session,
    empresa_id: int,
    producto_id: int | None,
    unidad_base: str | None,
    cantidad_base: float | None,
) -> StockPropio | None:
    """El stock propio que CUBRE la línea (cantidad en unidad base), con su costo; None si no
    hay producto, no cubre o no hay inventario. Con `cantidad_base` desconocida basta con
    tener disponible."""
    if producto_id is None or not unidad_base:
        return None
    entradas = _entradas(session, empresa_id, [producto_id]).get(producto_id)
    if not entradas:
        return None
    c = _cobertura(entradas, cantidad_base)
    if not c.en_stock or c.empresa_producto_id is None:
        return None
    habitual_id = next(
        (ep.proveedor_habitual_id for ep, _, _ in entradas if ep.proveedor_habitual_id), None
    )
    costo = costo_de_reposicion(session, empresa_id, producto_id, unidad_base, habitual_id)
    return StockPropio(
        empresa_producto_id=c.empresa_producto_id,
        disponible=c.disponible,
        costo_por_base=costo[0] if costo else None,
        proveedor=costo[1] if costo else None,
    )


# ---------------------------------------------------------------------------
# Fusión de productos del catálogo (la llama `catalogo.fusionar_productos`)
# ---------------------------------------------------------------------------


def fusionar_inventario(
    session: Session, origen_id: int, destino_id: int, mapa_formatos: dict[int, int]
) -> int:
    """Mueve el inventario del producto `origen` al `destino` (todas las empresas). Si la
    empresa ya tenía el destino en ese formato, suma stock y reservado, conserva los datos del
    destino y pasa los movimientos a esa fila; si no, la fila se reasigna. Devuelve cuántas
    filas se tocaron."""
    tocadas = 0
    for ep in list(
        session.execute(
            select(EmpresaProducto).where(EmpresaProducto.producto_id == origen_id)
        ).scalars()
    ):
        nuevo_formato = mapa_formatos.get(ep.formato_id, ep.formato_id)
        existente = session.execute(
            select(EmpresaProducto).where(
                EmpresaProducto.empresa_id == ep.empresa_id,
                EmpresaProducto.producto_id == destino_id,
                EmpresaProducto.formato_id == nuevo_formato,
            )
        ).scalar_one_or_none()
        if existente is None:
            ep.producto_id, ep.formato_id = destino_id, nuevo_formato
        else:
            existente.stock = round((existente.stock or 0.0) + (ep.stock or 0.0), 3)
            existente.reservado = round((existente.reservado or 0.0) + (ep.reservado or 0.0), 3)
            existente.activo = existente.activo or ep.activo
            for m in session.execute(
                select(EmpresaProductoMovimiento).where(
                    EmpresaProductoMovimiento.empresa_producto_id == ep.id
                )
            ).scalars():
                m.empresa_producto_id = existente.id
            session.flush()
            session.delete(ep)
        tocadas += 1
    session.flush()
    return tocadas
