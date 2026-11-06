"""Mi oferta a una Compra Ágil (F-plat-5): armar, simular, guardar y exportar.

- Parte de los precios sugeridos de `evaluar_ca`: el total sugerido se reparte entre las líneas
  según su peso en la referencia (cantidad_base × mediana del ganador) y se pasa a precio unitario
  en la unidad que pide la CA (`ca_lineas.cantidad`).
- Costo por línea: el proveedor que eligió el usuario (uno de los `proveedor_productos` de SU
  empresa, misma unidad base) o, si no eligió, el mejor costo (`adjudicacion.CostosEmpresa`).
  Logística, financiero y margen salen de `adjudicacion.costo_ca` y `adjudicacion.margen`; P(ganar)
  de `adjudicacion.p_ganar`. Nada se define dos veces.
- `ofertas_propias` guarda solo el contenido; el estado del flujo vive en `ca_trabajo` (F-equipo).
  Guardar manda la `version` que vio el usuario (conflicto como `ca_trabajo`).
- No hay API para enviar ofertas [I]: se transcriben a mano en el portal (el XLSX ayuda).
Ownership (regla 26): toda función recibe `empresa_id` y filtra por él. Fuente: Dirección
ChileCompra.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tiempo import ahora_utc
from app.mercado import adjudicacion as adj
from app.mercado import equipo as eq
from app.mercado import oferta_propia as op
from app.mercado.proveedores import Candidato, mejor_costo
from app.models.tables import (
    CaLinea,
    CompraAgil,
    Empresa,
    EmpresaProducto,
    OfertaPropia,
    ProductoCanonico,
    Proveedor,
    ProveedorProducto,
    Usuario,
)

NOTAS_MAX = 2000
PLAZO_MAX_DIAS = 365
PRECIO_MAX = 1e12
FUENTE = "Fuente: Dirección ChileCompra"
ORIGEN_STOCK = "stock"  # F-plat-capas: «Stock propio»; no tiene `proveedor_producto_id`
PREFIJO_STOCK = "stock:"  # valor del <select> de proveedor: `stock:<empresa_producto_id>`


class ErrorOferta(Exception):
    """Entrada inválida (→ 422)."""


class ProveedorAjeno(Exception):
    """`proveedor_producto_id` de otra empresa o inexistente (→ 404, regla 26)."""


@dataclass
class ConflictoOferta(Exception):
    """La oferta cambió desde que el usuario la abrió: no se guardó."""

    quien: str
    cuando: Any

    def __str__(self) -> str:
        return f"Conflicto de versión de la oferta ({self.quien})"


@dataclass(frozen=True)
class Entrada:
    """Lo que manda el formulario por línea. `origen = 'stock'` + `empresa_producto_id` =
    «Stock propio» (en ese caso `proveedor_producto_id` es None)."""

    precio_unitario: float | None
    proveedor_producto_id: int | None
    origen: str | None = None
    empresa_producto_id: int | None = None


@dataclass
class LineaOferta:
    linea_id: int
    n: int
    texto: str
    estado: str
    producto_id: int | None
    producto: str | None
    unidad_base: str | None
    cantidad: float | None
    cantidad_base: float | None
    precio_sugerido_unitario: float | None
    precio_unitario: float | None
    proveedor_producto_id: int | None
    candidatos: list[Candidato] = field(default_factory=list)
    costo_por_base: float | None = None
    costo_total: float | None = None
    proveedor: str | None = None
    # F-plat-capas: el stock propio que cubre la línea (si hay) y si es el origen elegido.
    stock: op.StockPropio | None = None
    origen: str | None = None
    empresa_producto_id: int | None = None

    @property
    def total(self) -> float | None:
        if self.precio_unitario is None or not self.cantidad:
            return None
        return self.precio_unitario * self.cantidad


@dataclass
class Simulacion:
    total: float
    costo_productos: float | None
    logistica: float | None
    financiero: float | None
    costo_total: float | None
    p_ganar: float | None
    margen_esperado: float | None
    margen_pct: float | None
    avisos: list[str]


# ---------------------------------------------------------------------------
# Costos por línea
# ---------------------------------------------------------------------------


def _candidatos(
    session: Session, empresa_id: int, producto_id: int | None, unidad_base: str | None
) -> list[Candidato]:
    if producto_id is None or not unidad_base:
        return []
    mc = mejor_costo(session, empresa_id, producto_id=producto_id, unidad_base=unidad_base)
    return mc.candidatos if mc else []


def validar_proveedor_producto(session: Session, empresa_id: int, pp_id: int) -> ProveedorProducto:
    """El `proveedor_producto` si es de la empresa; si no, `ProveedorAjeno` (→ 404)."""
    pp = session.execute(
        select(ProveedorProducto)
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(ProveedorProducto.id == pp_id, Proveedor.empresa_id == empresa_id)
    ).scalar_one_or_none()
    if pp is None:
        raise ProveedorAjeno(pp_id)
    return pp


def _unidades(session: Session, lineas: list[CaLinea]) -> dict[int, tuple[str, str | None]]:
    ids = {ln.producto_id for ln in lineas if ln.producto_id is not None}
    if not ids:
        return {}
    return {
        pid: (nombre, ub)
        for pid, nombre, ub in session.execute(
            select(
                ProductoCanonico.id, ProductoCanonico.nombre, ProductoCanonico.unidad_base
            ).where(ProductoCanonico.id.in_(ids))
        )
    }


def lineas_de(session: Session, codigo: str) -> list[CaLinea]:
    return list(
        session.execute(
            select(CaLinea).where(CaLinea.ca_codigo == codigo).order_by(CaLinea.n)
        ).scalars()
    )


def validar_empresa_producto(session: Session, empresa_id: int, ep_id: int) -> EmpresaProducto:
    """El producto del inventario si es de la empresa; si no, `ProveedorAjeno` (→ 404)."""
    try:
        return op.obtener(session, empresa_id, ep_id)
    except op.InventarioAjeno:
        raise ProveedorAjeno(ep_id) from None


def _costo_elegido(
    candidatos: list[Candidato], pp_id: int | None
) -> tuple[Candidato | None, int | None]:
    """El candidato elegido (si es uno de los válidos) o el más barato. (candidato, pp_id)."""
    if pp_id is not None:
        for c in candidatos:
            if c.producto_id == pp_id:
                return c, pp_id
    if candidatos:
        return candidatos[0], None
    return None, None


# ---------------------------------------------------------------------------
# Armar la oferta (desde lo guardado o desde la sugerencia)
# ---------------------------------------------------------------------------


def precios_sugeridos_por_linea(ev: adj.EvaluacionCA | None) -> dict[int, float]:
    """n de línea → precio unitario sugerido (en la unidad de la CA). Reparte el total sugerido
    según el peso de cada línea en la referencia; una línea sin referencia queda sin sugerencia."""
    if ev is None or not ev.precio_sugerido:
        return {}
    pesos = {
        e.n: (e.cantidad_base or 0.0) * (e.referencia.mediana_ganador or 0.0)
        for e in ev.lineas
        if e.estado == "ok" and e.referencia is not None and e.referencia.mediana_ganador
    }
    total = sum(pesos.values())
    if total <= 0:
        return {}
    return {n: ev.precio_sugerido * w / total for n, w in pesos.items() if w > 0}


def armar(
    session: Session,
    empresa_id: int,
    ca: CompraAgil,
    ev: adj.EvaluacionCA | None,
    guardada: OfertaPropia | None,
    entradas: Mapping[int, Entrada] | None = None,
) -> list[LineaOferta]:
    """Líneas de la oferta: lo que manda el formulario, si no lo guardado, si no lo sugerido."""
    lineas = lineas_de(session, ca.codigo)
    unidades = _unidades(session, lineas)
    sugeridos = precios_sugeridos_por_linea(ev)
    previas: dict[int, dict[str, Any]] = {}
    if guardada is not None:
        for d in guardada.lineas or []:
            lid = _int(d.get("linea_id"))
            if lid is not None:
                previas[lid] = d
    out: list[LineaOferta] = []
    for ln in lineas:
        nombre, ub = unidades.get(ln.producto_id, (None, None)) if ln.producto_id else (None, None)
        total_sug = sugeridos.get(ln.n)
        unit_sug = total_sug / ln.cantidad if total_sug is not None and ln.cantidad else None
        origen: str | None = None
        if entradas is not None and ln.id in entradas:
            precio, pp_id = entradas[ln.id].precio_unitario, entradas[ln.id].proveedor_producto_id
            origen = entradas[ln.id].origen
        elif ln.id in previas:
            precio = _float(previas[ln.id].get("precio_unitario"))
            pp_id = _int(previas[ln.id].get("proveedor_producto_id"))
            origen = previas[ln.id].get("origen")
        else:
            precio, pp_id = (round(unit_sug, 2) if unit_sug is not None else None), None
        cands = _candidatos(session, empresa_id, ln.producto_id, ub) if ln.estado == "ok" else []
        # F-plat-capas: el stock propio solo se ofrece si CUBRE la línea; si lo elegido ya no
        # alcanza (alguien movió el stock), se vuelve al mejor costo.
        stock = (
            op.stock_propio(session, empresa_id, ln.producto_id, ub, ln.cantidad_base)
            if ln.estado == "ok"
            else None
        )
        usa_stock = origen == ORIGEN_STOCK and stock is not None
        elegido, pp_ok = (None, None) if usa_stock else _costo_elegido(cands, pp_id)
        lo = LineaOferta(
            linea_id=ln.id, n=ln.n, texto=ln.texto_origen, estado=ln.estado,
            producto_id=ln.producto_id, producto=nombre, unidad_base=ub, cantidad=ln.cantidad,
            cantidad_base=ln.cantidad_base, precio_sugerido_unitario=unit_sug,
            precio_unitario=precio, proveedor_producto_id=pp_ok, candidatos=cands,
            stock=stock,
        )  # fmt: skip
        if usa_stock and stock is not None:
            # Regla del costo (`oferta_propia.costo_de_reposicion`): el proveedor habitual del
            # producto o, si no hay, el último `costo_historial` de cualquier proveedor.
            lo.origen = ORIGEN_STOCK
            lo.empresa_producto_id = stock.empresa_producto_id
            lo.proveedor = "Stock propio"
            if stock.costo_por_base is not None:
                lo.costo_por_base = stock.costo_por_base
                if ln.cantidad_base:
                    lo.costo_total = stock.costo_por_base * ln.cantidad_base
        elif elegido is not None:
            lo.costo_por_base = elegido.precio_por_unidad_base
            lo.proveedor = elegido.proveedor
            if ln.cantidad_base:
                lo.costo_total = elegido.precio_por_unidad_base * ln.cantidad_base
        out.append(lo)
    return out


def _float(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return None if v is None or v == "" else int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Simular (sin persistir) y margen realizado
# ---------------------------------------------------------------------------


def costo_de(
    session: Session,
    empresa_id: int,
    ca: CompraAgil,
    lineas: list[LineaOferta],
    tasa: float = adj.COSTO_FINANCIERO_DEFAULT,
) -> tuple[adj.CostoCA | None, list[str]]:
    """Costo de entregar las líneas OFERTADAS (precio > 0). Si alguna no tiene costo, None:
    sin costo no se inventa margen."""
    ofertadas = [lo for lo in lineas if lo.precio_unitario and lo.precio_unitario > 0]
    if not ofertadas:
        return None, ["La oferta no tiene líneas con precio"]
    sin = [lo for lo in ofertadas if lo.costo_total is None]
    if sin:
        n = len(sin)
        return None, [
            f"Falta costo de {n} producto{'s' if n != 1 else ''}: el margen queda sin calcular"
        ]
    kg = sum(lo.cantidad_base or 0.0 for lo in ofertadas if lo.unidad_base == "kg")
    cc = adj.costo_ca(
        sum(lo.costo_total or 0.0 for lo in ofertadas), kg,
        adj.cargar_tarifas(session, empresa_id), ca.comuna_inferida, tasa,
    )  # fmt: skip
    avisos = [] if cc.logistica.tarifa else ["Sin tarifa de logística para la comuna: logística 0"]
    return cc, avisos


def total_de(lineas: list[LineaOferta]) -> float:
    return sum(lo.total or 0.0 for lo in lineas)


def simular(
    session: Session,
    empresa_id: int,
    ca: CompraAgil,
    lineas: list[LineaOferta],
    tasa: float = adj.COSTO_FINANCIERO_DEFAULT,
    activo: adj.ModeloActivo | None = None,
) -> Simulacion:
    """Total, costo, P(ganar) y margen de la oferta tal como está en pantalla. No persiste."""
    total = total_de(lineas)
    cc, avisos = costo_de(session, empresa_id, ca, lineas, tasa)
    p = (
        adj.p_ganar(
            session, ca, None, total, empresa=session.get(Empresa, empresa_id), activo=activo
        )  # fmt: skip
        if total > 0
        else None
    )
    if p is None and total > 0:
        avisos.append("Sin P(ganar): falta modelo, referencia de precio o detalle completo")
    m_esp, m_pct = adj.margen(total, cc.total if cc else None, p)
    return Simulacion(
        total=total, costo_productos=cc.productos if cc else None,
        logistica=cc.logistica.costo if cc else None, financiero=cc.financiero if cc else None,
        costo_total=cc.total if cc else None, p_ganar=p, margen_esperado=m_esp, margen_pct=m_pct,
        avisos=avisos,
    )  # fmt: skip


def margen_realizado(
    session: Session,
    empresa_id: int,
    ca: CompraAgil,
    oferta: OfertaPropia,
    precio: float,
    tasa: float = adj.COSTO_FINANCIERO_DEFAULT,
) -> float | None:
    """Precio adjudicado − costo de las líneas guardadas − logística − financiero (las funciones
    de F-plat-4). None si alguna línea ofertada no tiene costo."""
    lineas = armar(session, empresa_id, ca, None, oferta)
    cc, _ = costo_de(session, empresa_id, ca, lineas, tasa)
    return None if cc is None else precio - cc.total


# ---------------------------------------------------------------------------
# Guardar
# ---------------------------------------------------------------------------


def oferta_de(session: Session, empresa_id: int, codigo: str) -> OfertaPropia | None:
    return session.execute(
        select(OfertaPropia).where(
            OfertaPropia.empresa_id == empresa_id, OfertaPropia.ca_codigo == codigo
        )
    ).scalar_one_or_none()


def validar_entradas(
    session: Session, empresa_id: int, codigo: str, crudas: Mapping[int, tuple[str, str]]
) -> dict[int, Entrada]:
    """`{linea_id: (precio, pp_id)}` del formulario → entradas válidas. Una línea de otra CA se
    ignora; un precio negativo o absurdo es 422; un proveedor de otra empresa, 404."""
    ids = {ln.id for ln in lineas_de(session, codigo)}
    out: dict[int, Entrada] = {}
    for lid, (precio_txt, pp_txt) in crudas.items():
        if lid not in ids:
            continue
        precio = _precio(precio_txt)
        valor = (pp_txt or "").strip()
        if valor.startswith(PREFIJO_STOCK):
            ep_id = _int(valor[len(PREFIJO_STOCK) :])
            if ep_id is None:
                raise ErrorOferta("Origen de costo inválido")
            validar_empresa_producto(session, empresa_id, ep_id)
            out[lid] = Entrada(precio, None, ORIGEN_STOCK, ep_id)
            continue
        pp_id = _int(valor) if valor else None
        if pp_id is not None:
            validar_proveedor_producto(session, empresa_id, pp_id)
        out[lid] = Entrada(precio, pp_id)
    return out


def _precio(texto: str) -> float | None:
    t = (texto or "").strip().replace("$", "").replace(" ", "")
    if not t:
        return None
    # «1.234,5» (Chile) o «1234.5»: si hay coma, el punto es separador de miles.
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        raise ErrorOferta(f"Precio inválido: {texto!r}") from None
    if v < 0 or v > PRECIO_MAX or v != v:
        raise ErrorOferta("El precio debe ser un número positivo")
    return v


def plazo_valido(texto: str) -> int | None:
    t = (texto or "").strip()
    if not t:
        return None
    if not t.isdigit() or int(t) > PLAZO_MAX_DIAS:
        raise ErrorOferta(f"Plazo de entrega inválido (0 a {PLAZO_MAX_DIAS} días)")
    return int(t)


def guardar(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    ca: CompraAgil,
    lineas: list[LineaOferta],
    *,
    plazo: int | None,
    notas: str,
    version: int,
) -> OfertaPropia:
    """Crea o actualiza la oferta de la empresa (versión 0 = el usuario no vio ninguna). Si la CA
    está en `nueva`/`priorizada`, la pasa a `cotizando` (con su evento, F-equipo). No hace commit.
    """
    ahora = ahora_utc()
    o = session.execute(
        select(OfertaPropia)
        .where(OfertaPropia.empresa_id == empresa_id, OfertaPropia.ca_codigo == ca.codigo)
        .with_for_update()
    ).scalar_one_or_none()
    if (o.version if o is not None else 0) != version:
        raise _conflicto(session, o)
    contenido = [
        {
            "linea_id": lo.linea_id,
            "precio_unitario": lo.precio_unitario,
            "proveedor_producto_id": lo.proveedor_producto_id,
            # «Stock propio» (F-plat-capas): F-plat-lote reserva a partir de estos campos.
            **(
                {"origen": ORIGEN_STOCK, "empresa_producto_id": lo.empresa_producto_id}
                if lo.origen == ORIGEN_STOCK
                else {}
            ),
        }  # fmt: skip
        for lo in lineas
    ]
    if o is None:
        o = OfertaPropia(ca_codigo=ca.codigo, empresa_id=empresa_id, version=1)
        session.add(o)
        try:
            session.flush()
        except IntegrityError:
            session.rollback()
            raise _conflicto(session, oferta_de(session, empresa_id, ca.codigo)) from None
    else:
        o.version += 1
    o.lineas = contenido
    o.total_neto = total_de(lineas)
    o.plazo_entrega_dias = plazo
    o.notas = (notas or "").strip()[:NOTAS_MAX] or None
    o.usuario_id = usuario.id
    o.guardada_en = ahora
    t = eq.trabajo_de(session, empresa_id, ca.codigo)
    if t is None or t.estado in (eq.NUEVA, eq.PRIORIZADA):
        eq.cambiar_estado(
            session, empresa_id, usuario, ca.codigo, eq.COTIZANDO, t.version if t else 0
        )
    session.flush()
    return o


def _conflicto(session: Session, o: OfertaPropia | None) -> ConflictoOferta:
    quien = "Alguien del equipo"
    if o is not None and o.usuario_id is not None:
        u = session.get(Usuario, o.usuario_id)
        quien = eq.nombre_visible(u)
    return ConflictoOferta(quien=quien, cuando=o.guardada_en if o else None)


def marcar_enviada(session: Session, empresa_id: int, usuario: Usuario, codigo: str) -> None:
    """`ca_trabajo` → `enviada` (F-equipo, con su evento). Exige empresa con RUT: sin él no se
    puede saber después si ganamos. No hace commit."""
    if eq.rut_empresa(session.get(Empresa, empresa_id)) is None:
        raise ErrorOferta("Para marcarla enviada, tu empresa necesita su RUT")
    t = eq.trabajo_de(session, empresa_id, codigo)
    eq.cambiar_estado(session, empresa_id, usuario, codigo, eq.ENVIADA, t.version if t else 0)


# ---------------------------------------------------------------------------
# Exportar
# ---------------------------------------------------------------------------


def xlsx(ca: CompraAgil, lineas: list[LineaOferta], plazo: int | None) -> bytes:
    """Planilla para transcribir la oferta al portal (líneas, cantidad, precio unitario neto,
    total, plazo) con la fuente."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Oferta"
    ws.append([f"Oferta a la Compra Ágil {ca.codigo}"])
    ws.append([ca.nombre or ""])
    ws.append([])
    ws.append(["N°", "Producto solicitado", "Cantidad", "Precio unitario neto", "Total neto"])
    for lo in lineas:
        ws.append([lo.n, lo.texto, lo.cantidad, lo.precio_unitario, lo.total])
    ws.append([])
    ws.append(["", "Total neto", "", "", total_de(lineas)])
    ws.append(["", "Plazo de entrega (días)", plazo if plazo is not None else "No indicado"])
    ws.append([])
    ws.append([FUENTE])
    ws.column_dimensions["B"].width = 60
    for col in ("C", "D", "E"):
        ws.column_dimensions[col].width = 18
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
