"""Proveedores, productos con precio y costo histórico de una empresa (F-plat-3).

Todo se filtra por `empresa_id` (regla 26). El precio actual vive en
`proveedor_productos`; cada cambio inserta una fila en `costo_historial` en la misma
transacción (append-only: nunca se edita). Sin commit acá: lo hace quien llama.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalogos.canonico import Catalogo, sugerir_vinculo
from app.catalogos.mercado import norm
from app.core.tiempo import TZ_CHILE
from app.models.tables import (
    CostoHistorial,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
)

UNIDADES = ("kg", "L", "u")
FUENTES = ("manual", "planilla", "lista_llm")
PROVINCIAS_NUBLE = ("Diguillín", "Itata", "Punilla")
_ALIAS_UNIDAD = {
    "kg": "kg", "kilo": "kg", "kilos": "kg", "kilogramo": "kg", "kilogramos": "kg",
    "l": "L", "lt": "L", "lts": "L", "litro": "L", "litros": "L",
    "u": "u", "un": "u", "und": "u", "unidad": "u", "unidades": "u",
}  # fmt: skip
_TOL = 1e-9


def hoy_chile() -> date:
    return datetime.now(TZ_CHILE).date()


def normalizar_unidad(valor: str | None) -> str | None:
    """'Kg', 'litros', 'un' → 'kg' | 'L' | 'u'; vacío o desconocido → None."""
    return _ALIAS_UNIDAD.get(norm(valor))


def normalizar_rut(valor: str | None) -> str | None:
    """'76.123.456-7' / '761234567' → '76123456-7'; vacío → None; inválido → ValueError."""
    t = (valor or "").replace(".", "").replace(" ", "").upper()
    if not t:
        return None
    if "-" not in t and len(t) >= 2:
        t = f"{t[:-1]}-{t[-1]}"
    cuerpo, _, dv = t.partition("-")
    if not cuerpo.isdigit() or len(dv) != 1 or not (dv.isdigit() or dv == "K"):
        raise ValueError(f"RUT inválido: {valor!r}")
    return t


def _texto(v: str | None, largo: int) -> str | None:
    t = (v or "").strip()
    return t[:largo] if t else None


# ---------------------------------------------------------------------------
# Productos y costo histórico
# ---------------------------------------------------------------------------


def buscar_producto(
    session: Session, proveedor_id: int, producto_texto: str, formato_texto: str
) -> ProveedorProducto | None:
    """Por (producto, formato) normalizados dentro del proveedor."""
    clave = (norm(producto_texto), norm(formato_texto))
    filas = session.execute(
        select(ProveedorProducto).where(ProveedorProducto.proveedor_id == proveedor_id)
    ).scalars()
    for pp in filas:
        if (norm(pp.producto_texto), norm(pp.formato_texto)) == clave:
            return pp
    return None


def _registrar_costo(
    session: Session,
    pp: ProveedorProducto,
    precio: float,
    vigente_desde: date,
    fuente: str,
    usuario_id: int | None,
) -> None:
    session.add(
        CostoHistorial(
            proveedor_producto_id=pp.id,
            precio_neto=precio,
            vigente_desde=vigente_desde,
            fuente=fuente,
            usuario_id=usuario_id,
        )
    )


def _vinculo_explicito(
    session: Session, producto_id: int, formato_id: int | None, unidad_base: str | None
) -> None:
    """Valida un vínculo al canónico elegido por una persona (ValueError si no calza)."""
    from app.mercado.catalogo import _formato_valido, _producto_activo

    producto = _producto_activo(session, producto_id)  # CatalogoError ⊂ ValueError
    _formato_valido(session, producto.id, formato_id)
    if unidad_base is not None and unidad_base != producto.unidad_base:
        raise ValueError(
            f"La unidad base del proveedor ({unidad_base}) no coincide con la del producto "
            f"del catálogo ({producto.unidad_base})"
        )


def _autovincular(
    session: Session, pp: ProveedorProducto, catalogo: Catalogo, unspsc: str | None
) -> bool:
    """Vincula `pp` al canónico si la sugerencia es única y de confianza alta (F-plat-capas
    §1.3). Nunca pisa un vínculo existente. Devuelve si vinculó."""
    if pp.producto_id is not None:
        return False
    s = sugerir_vinculo(catalogo, pp.producto_texto, pp.formato_texto, unspsc)
    if s is None:
        return False
    producto_id, formato_id = s
    ub = catalogo.productos[producto_id].unidad_base
    if pp.unidad_base is not None and pp.unidad_base != ub:
        return False  # kg contra L: no competirían igual; queda para vincular a mano
    pp.producto_id, pp.formato_id = producto_id, formato_id
    return True


def guardar_producto(
    session: Session,
    proveedor: Proveedor,
    *,
    producto_texto: str,
    formato_texto: str = "",
    precio_neto: float,
    vigente_desde: date | None = None,
    factor_base: float | None = None,
    unidad_base: str | None = None,
    codigo_unspsc: str | None = None,
    disponible: bool | None = None,
    fuente: str = "manual",
    usuario_id: int | None = None,
    producto_id: int | None = None,
    formato_id: int | None = None,
    catalogo: Catalogo | None = None,
) -> tuple[ProveedorProducto, str]:
    """Crea el producto o actualiza su precio. Devuelve (producto, 'nuevo'|'cambiado'|'historico'|'igual').

    Nuevo → primera fila de historial. Precio distinto → otra fila (misma transacción).
    Precio igual → sin fila nueva (idempotente). Fecha anterior a la vigente → solo historial. Los demás datos se actualizan siempre.

    Vínculo al catálogo canónico (F-plat-capas §1.3): `producto_id`/`formato_id` explícitos se
    validan y se guardan (el formato debe ser del producto). Sin ellos, y si viene `catalogo`,
    se vincula solo cuando la sugerencia es única y de confianza alta; si no, queda NULL
    («sin vincular»). Un vínculo existente nunca se pisa solo.
    """
    if fuente not in FUENTES:
        raise ValueError(f"fuente inválida: {fuente!r}")
    producto_texto = (producto_texto or "").strip()[:300]
    formato_texto = (formato_texto or "").strip()[:200]
    if not producto_texto:
        raise ValueError("El producto es obligatorio")
    if not precio_neto or precio_neto <= 0:
        raise ValueError("El precio neto debe ser mayor que 0")
    if unidad_base is not None and unidad_base not in UNIDADES:
        raise ValueError(f"unidad_base debe ser una de {', '.join(UNIDADES)}")
    if factor_base is not None and factor_base <= 0:
        raise ValueError("factor_base debe ser mayor que 0")
    if (factor_base is None) != (unidad_base is None):
        raise ValueError("factor_base y unidad_base van juntos o ninguno")
    if producto_id is not None:
        _vinculo_explicito(session, producto_id, formato_id, unidad_base)
    elif formato_id is not None:
        raise ValueError("formato_id requiere producto_id")
    vigente = vigente_desde or hoy_chile()

    pp = buscar_producto(session, proveedor.id, producto_texto, formato_texto)
    if pp is None:
        pp = ProveedorProducto(
            proveedor_id=proveedor.id,
            producto_texto=producto_texto,
            formato_texto=formato_texto,
            factor_base=factor_base,
            unidad_base=unidad_base,
            codigo_unspsc=_texto(codigo_unspsc, 20),
            precio_neto_actual=float(precio_neto),
            vigente_desde=vigente,
            fuente=fuente,
            disponible=True if disponible is None else disponible,
        )
        if producto_id is not None:
            pp.producto_id, pp.formato_id = producto_id, formato_id
        elif catalogo is not None:
            _autovincular(session, pp, catalogo, codigo_unspsc)
        session.add(pp)
        session.flush()
        _registrar_costo(session, pp, float(precio_neto), vigente, fuente, usuario_id)
        return pp, "nuevo"

    pp.factor_base = factor_base
    pp.unidad_base = unidad_base
    if producto_id is not None:
        pp.producto_id, pp.formato_id = producto_id, formato_id
    elif catalogo is not None:
        _autovincular(session, pp, catalogo, codigo_unspsc)
    if codigo_unspsc is not None:
        pp.codigo_unspsc = _texto(codigo_unspsc, 20)
    if disponible is not None:
        pp.disponible = disponible
    if vigente < pp.vigente_desde:
        # Lista vieja: queda en el historial pero no retrocede el precio actual.
        existe = session.execute(
            select(CostoHistorial.id).where(
                CostoHistorial.proveedor_producto_id == pp.id,
                CostoHistorial.vigente_desde == vigente,
                CostoHistorial.precio_neto == float(precio_neto),
            )
        ).first()
        if existe is not None:
            return pp, "igual"
        _registrar_costo(session, pp, float(precio_neto), vigente, fuente, usuario_id)
        return pp, "historico"
    if abs(pp.precio_neto_actual - float(precio_neto)) <= _TOL:
        return pp, "igual"
    pp.precio_neto_actual = float(precio_neto)
    pp.vigente_desde = vigente
    pp.fuente = fuente
    _registrar_costo(session, pp, float(precio_neto), vigente, fuente, usuario_id)
    return pp, "cambiado"


@dataclass(frozen=True)
class Variacion:
    """Precio actual y su variación frente al anterior y al de hace `dias` días."""

    actual: float
    anterior: float | None
    pct_vs_anterior: float | None
    hace_90: float | None
    pct_vs_90: float | None


def _pct(actual: float, ref: float | None) -> float | None:
    if ref is None or ref == 0:
        return None
    return round((actual - ref) / ref * 100, 1)


def variacion(
    historial: list[CostoHistorial], actual: float, hoy: date | None = None, dias: int = 90
) -> Variacion:
    """`historial` = filas del producto (cualquier orden). La última es el precio actual."""
    filas = sorted(historial, key=lambda h: (h.vigente_desde, h.id))
    anterior = filas[-2].precio_neto if len(filas) >= 2 else None
    corte = (hoy or hoy_chile()) - timedelta(days=dias)
    antiguas = [h for h in filas if h.vigente_desde <= corte]
    ref90 = antiguas[-1].precio_neto if antiguas else None
    return Variacion(actual, anterior, _pct(actual, anterior), ref90, _pct(actual, ref90))


def variaciones_de(
    session: Session, productos: list[ProveedorProducto], hoy: date | None = None
) -> dict[int, Variacion]:
    """Variación de cada producto con UNA consulta de historial."""
    if not productos:
        return {}
    ids = [p.id for p in productos]
    por_pp: dict[int, list[CostoHistorial]] = {i: [] for i in ids}
    for h in session.execute(
        select(CostoHistorial).where(CostoHistorial.proveedor_producto_id.in_(ids))
    ).scalars():
        por_pp[h.proveedor_producto_id].append(h)
    return {p.id: variacion(por_pp[p.id], p.precio_neto_actual, hoy) for p in productos}


# ---------------------------------------------------------------------------
# Mejor costo hoy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidato:
    proveedor_id: int
    proveedor: str
    producto_id: int
    producto_texto: str
    formato_texto: str
    unidad_base: str
    precio_neto: float
    precio_por_unidad_base: float


@dataclass(frozen=True)
class MejorCosto:
    unidad_base: str
    ganador: Candidato
    candidatos: list[Candidato]  # todos, del más barato al más caro


def mejor_costo(
    session: Session,
    empresa_id: int,
    producto_id: int | None = None,
    producto_texto: str | None = None,
    unidad_base: str | None = None,
) -> MejorCosto | None:
    """Mejor costo hoy por (producto normalizado, unidad base) de la empresa.

    Compite `precio_neto_actual / factor_base` (precio por kg, L o u) entre proveedores
    ACTIVOS con el producto `disponible`; sin `factor_base`/`unidad_base` no compite.
    `producto_id` filtra por la columna homónima (la llena F-plat-1); `producto_texto`, por
    `norm`. Si hay más de una unidad base y no se pide una, gana la que tiene más candidatos
    (empate: orden alfabético). None si nadie compite.
    """
    if producto_id is None and not producto_texto:
        raise ValueError("producto_id o producto_texto")
    consulta = (
        select(ProveedorProducto, Proveedor)
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(
            Proveedor.empresa_id == empresa_id,
            Proveedor.activo.is_(True),
            ProveedorProducto.disponible.is_(True),
            ProveedorProducto.factor_base.is_not(None),
            ProveedorProducto.unidad_base.is_not(None),
        )
    )
    if producto_id is not None:
        consulta = consulta.where(ProveedorProducto.producto_id == producto_id)
    clave = norm(producto_texto) if producto_id is None else None
    por_unidad: dict[str, list[Candidato]] = {}
    for pp, prov in session.execute(consulta).all():
        if clave is not None and norm(pp.producto_texto) != clave:
            continue
        if not pp.factor_base or pp.factor_base <= 0 or pp.unidad_base is None:
            continue
        if unidad_base is not None and pp.unidad_base != unidad_base:
            continue
        por_unidad.setdefault(pp.unidad_base, []).append(
            Candidato(
                proveedor_id=prov.id,
                proveedor=prov.nombre,
                producto_id=pp.id,
                producto_texto=pp.producto_texto,
                formato_texto=pp.formato_texto,
                unidad_base=pp.unidad_base,
                precio_neto=pp.precio_neto_actual,
                precio_por_unidad_base=pp.precio_neto_actual / pp.factor_base,
            )
        )
    if not por_unidad:
        return None
    unidad = sorted(por_unidad, key=lambda u: (-len(por_unidad[u]), u))[0]
    ordenados = sorted(por_unidad[unidad], key=lambda c: c.precio_por_unidad_base)
    return MejorCosto(unidad, ordenados[0], ordenados)


# ---------------------------------------------------------------------------
# Logística
# ---------------------------------------------------------------------------


def asegurar_tarifas_iniciales(session: Session, empresa_id: int) -> int:
    """Las 3 provincias de Ñuble con valores 0 para que Boris las complete. Idempotente."""
    existentes = {
        (t.ambito, norm(t.nombre))
        for t in session.execute(
            select(TarifaLogistica).where(TarifaLogistica.empresa_id == empresa_id)
        ).scalars()
    }
    nuevas = 0
    for nombre in PROVINCIAS_NUBLE:
        if ("provincia", norm(nombre)) not in existentes:
            session.add(
                TarifaLogistica(
                    empresa_id=empresa_id,
                    ambito="provincia",
                    nombre=nombre,
                    costo_fijo_clp=0,
                    costo_por_kg_clp=0,
                )
            )
            nuevas += 1
    return nuevas
