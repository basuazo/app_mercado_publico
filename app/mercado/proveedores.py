"""Proveedores, productos con precio y costo histórico de una empresa (F-plat-3).

Todo se filtra por `empresa_id` (regla 26). El precio actual vive en
`proveedor_productos`; cada cambio inserta una fila en `costo_historial` en la misma
transacción (append-only: nunca se edita). Sin commit acá: lo hace quien llama.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalogos.canonico import Catalogo, sugerir_vinculo
from app.catalogos.mercado import norm
from app.core.tiempo import TZ_CHILE
from app.models.tables import (
    CostoHistorial,
    Empresa,
    EmpresaProducto,
    Proveedor,
    ProveedorProducto,
    TarifaLogistica,
)

UNIDADES = ("kg", "L", "u")
# `solicitud` (F-plat-lote): la respuesta de un proveedor a una solicitud de precio de un lote.
FUENTES = ("manual", "planilla", "lista_llm", "solicitud")
DIAS_VIGENCIA_DEFAULT = 15  # `empresas.dias_vigencia_precio` (decisión de Boris, 5-nov)
TRAMOS_MAX = 20
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
    tramos: list[dict[str, Any]] | None = None,
) -> tuple[ProveedorProducto, str]:
    """Crea el producto o actualiza su precio. Devuelve (producto,
    'nuevo'|'cambiado'|'historico'|'igual'|'confirmado').

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
    # F-plat-lote: `tramos` None = no tocar; `[]` = borrarlos.
    tramos_ok = normalizar_tramos(tramos) if tramos is not None else None
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
            tramos=tramos_ok,
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
    if tramos is not None:
        pp.tramos = tramos_ok
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
        if fuente == "solicitud" and vigente > pp.vigente_desde:
            # F-plat-lote: el proveedor CONFIRMÓ el mismo precio: deja de estar vencido y queda
            # en el historial (sin esto, un precio sin cambios seguiría «de hace n días»).
            pp.vigente_desde = vigente
            pp.fuente = fuente
            _registrar_costo(session, pp, float(precio_neto), vigente, fuente, usuario_id)
            return pp, "confirmado"
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


def dias_de_precio(vigente_desde: date, hoy: date) -> int:
    """Antigüedad del precio en días (0 = de hoy)."""
    return max((hoy - vigente_desde).days, 0)


def vencido(pp: ProveedorProducto, hoy: date, dias: int) -> bool:
    """F-plat-lote §1.4: `hoy − vigente_desde > dias` (`empresas.dias_vigencia_precio`)."""
    return dias_de_precio(pp.vigente_desde, hoy) > dias


def dias_vigencia(session: Session, empresa_id: int) -> int:
    empresa = session.get(Empresa, empresa_id)
    valor = getattr(empresa, "dias_vigencia_precio", None)
    return int(valor) if valor is not None else DIAS_VIGENCIA_DEFAULT


def normalizar_tramos(valor: object) -> list[dict[str, float]] | None:
    """`[{desde, precio}]` válido (desde > 0 en unidades del formato, precio neto > 0 por
    formato), ordenado por `desde` y sin `desde` repetidos. Vacío → None. ValueError si no."""
    if valor is None or valor == []:
        return None
    if not isinstance(valor, list) or len(valor) > TRAMOS_MAX:
        raise ValueError(f"Tramos: una lista de hasta {TRAMOS_MAX} pares desde/precio")
    out: dict[float, float] = {}
    for t in valor:
        try:
            desde, precio = float(t["desde"]), float(t["precio"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Cada tramo necesita «desde» y «precio» numéricos") from None
        if not (desde > 0 and precio > 0) or desde != desde or precio != precio:
            raise ValueError("En un tramo, «desde» y «precio» deben ser mayores que 0")
        out[round(desde, 3)] = round(precio, 2)
    return [{"desde": d, "precio": p} for d, p in sorted(out.items())]


def tramo_para(
    tramos: list[dict[str, Any]] | None, cantidad_formato: float | None
) -> tuple[float, float] | None:
    """El tramo `(desde, precio)` de mayor `desde` que la cantidad alcanza; None si ninguno."""
    if not tramos or not cantidad_formato or cantidad_formato <= 0:
        return None
    mejor: tuple[float, float] | None = None
    for t in tramos:
        try:
            desde, precio = float(t["desde"]), float(t["precio"])
        except (KeyError, TypeError, ValueError):
            continue  # parseo defensivo: un tramo roto no rompe el costo
        if desde <= cantidad_formato + _TOL and precio > 0 and (mejor is None or desde > mejor[0]):
            mejor = (desde, precio)
    return mejor


@dataclass(frozen=True)
class Candidato:
    proveedor_id: int
    proveedor: str
    producto_id: int  # id de `proveedor_productos` (no del canónico)
    producto_texto: str
    formato_texto: str
    unidad_base: str
    precio_neto: float  # por formato (el del tramo si aplicó)
    precio_por_unidad_base: float
    # F-plat-lote §2.3: antigüedad del precio y tramo por volumen aplicado (su `desde`).
    vencido: bool = False
    dias: int | None = None
    tramo_aplicado: float | None = None
    habitual: bool = False


@dataclass(frozen=True)
class MejorCosto:
    unidad_base: str
    ganador: Candidato
    candidatos: list[Candidato]  # todos: los vigentes primero, del más barato al más caro

    @property
    def vencido(self) -> bool:
        """El ganador es vencido = no queda ningún precio vigente (la UI lo pinta en ámbar)."""
        return self.ganador.vencido


def _habituales(session: Session, empresa_id: int, producto_id: int | None) -> set[int]:
    if producto_id is None:
        return set()
    return {
        int(pid)
        for pid in session.execute(
            select(EmpresaProducto.proveedor_habitual_id).where(
                EmpresaProducto.empresa_id == empresa_id,
                EmpresaProducto.producto_id == producto_id,
                EmpresaProducto.activo.is_(True),
                EmpresaProducto.proveedor_habitual_id.is_not(None),
            )
        ).scalars()
        if pid is not None
    }


def mejor_costo(  # noqa: PLR0913 - una dimensión del costo = un parámetro
    session: Session,
    empresa_id: int,
    producto_id: int | None = None,
    producto_texto: str | None = None,
    unidad_base: str | None = None,
    *,
    cantidad_base: float | None = None,
    permitir_vencidos: bool = False,
    hoy: date | None = None,
) -> MejorCosto | None:
    """Mejor costo VIGENTE por (producto normalizado, unidad base) de la empresa. Única
    definición: la mesa (job), la ficha (F-plat-5), abastecimiento y el lote la llaman.

    Compite `precio / factor_base` (precio por kg, L o u) entre proveedores ACTIVOS con el
    producto `disponible`; sin `factor_base`/`unidad_base` no compite. `producto_id` filtra por la
    columna homónima; `producto_texto`, por `norm`. Si hay más de una unidad base y no se pide una,
    gana la que tiene más candidatos (empate: orden alfabético). None si nadie compite.

    F-plat-lote §2.3. Orden: no vencido antes que vencido (`empresas.dias_vigencia_precio`; con
    `permitir_vencidos` compiten igual), precio (el del tramo que cubre `cantidad_base` convertida
    a unidades del formato, si el producto tiene `tramos`), y el proveedor habitual del inventario
    como desempate. Si solo quedan vencidos, gana el mejor vencido con `vencido=True`.
    """
    if producto_id is None and not producto_texto:
        raise ValueError("producto_id o producto_texto")
    dia = hoy or hoy_chile()
    dias = dias_vigencia(session, empresa_id)
    habituales = _habituales(session, empresa_id, producto_id)
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
        precio = pp.precio_neto_actual
        tramo = tramo_para(pp.tramos, cantidad_base / pp.factor_base if cantidad_base else None)
        if tramo is not None and tramo[1] < precio:
            precio = tramo[1]  # un tramo por volumen solo puede bajar el precio
        else:
            tramo = None
        por_unidad.setdefault(pp.unidad_base, []).append(
            Candidato(
                proveedor_id=prov.id,
                proveedor=prov.nombre,
                producto_id=pp.id,
                producto_texto=pp.producto_texto,
                formato_texto=pp.formato_texto,
                unidad_base=pp.unidad_base,
                precio_neto=precio,
                precio_por_unidad_base=precio / pp.factor_base,
                vencido=vencido(pp, dia, dias),
                dias=dias_de_precio(pp.vigente_desde, dia),
                tramo_aplicado=tramo[0] if tramo else None,
                habitual=prov.id in habituales,
            )
        )
    if not por_unidad:
        return None
    unidad = sorted(por_unidad, key=lambda u: (-len(por_unidad[u]), u))[0]
    ordenados = sorted(
        por_unidad[unidad],
        key=lambda c: (
            c.vencido and not permitir_vencidos,
            round(c.precio_por_unidad_base, 6),
            not c.habitual,
            c.proveedor,
        ),
    )
    return MejorCosto(unidad, ordenados[0], ordenados)


def vencidos_por_proveedor(
    session: Session, empresa_id: int, hoy: date | None = None
) -> dict[int, int]:
    """`{proveedor_id: n precios vencidos}` de los proveedores ACTIVOS de la empresa (productos
    disponibles). Para el filtro «Vencidos» de /proveedores y «n precios por actualizar» de Hoy."""
    corte = (hoy or hoy_chile()) - timedelta(days=dias_vigencia(session, empresa_id))
    out: dict[int, int] = {}
    for pid in session.execute(
        select(ProveedorProducto.proveedor_id)
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(
            Proveedor.empresa_id == empresa_id,
            Proveedor.activo.is_(True),
            ProveedorProducto.disponible.is_(True),
            ProveedorProducto.vigente_desde < corte,
        )
    ).scalars():
        out[pid] = out.get(pid, 0) + 1
    return out


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
