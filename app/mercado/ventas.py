"""Post-adjudicación: ventas, compras a proveedores, entrega, factura, pago y caja (F-plat-seguimiento).

Una `Venta` nace cuando una CA enviada resulta `ganada` (`equipo.generar_alarmas`, idempotente por
empresa × CA) o a mano desde una CA `enviada`. El estado de `ca_trabajo` NO cambia (sigue `enviada`
con `resultado = ganada`): los filtros y las alarmas de F-equipo dependen de eso; la etapa vive en
`ventas.estado` (adjudicada → oc_recibida → comprando → entregada → facturada → pagada | anulada).

- Cada cambio de estado escribe su evento `venta` en la MISMA transacción (campana y /actividad).
- Registrar una compra escribe `costo_historial` (`fuente = compra`, vía `guardar_producto`) y, si el
  producto está en el inventario, un movimiento `compra` (+stock).
- Entregar convierte la reserva del lote en movimientos `entrega` (`oferta_propia.ajustar_stock`):
  libera SU parte de la reserva del lote (`liberar_parcial`) y descuenta el stock; lo comprado
  para la venta entró con `compra` y sale con `entrega`. Las líneas `reemplazada` (anexos-2)
  nunca cuentan. Nunca deja `reservado > stock`: si falta stock se descuenta lo que hay y se avisa.
- `ofertas_propias.margen_realizado` se calcula con el costo real cuando existe.
- Alarmas de sistema (`generar_alarmas_seguimiento`): constantes al inicio, idempotentes.

Ownership (reglas 17 y 26): toda función recibe `empresa_id` y filtra por él; un id ajeno es
`VentaAjena` (→ 404). Los datos de contacto de la entrega son del comprador: dato de negocio, no se
exportan a terceros. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.tiempo import TZ_CHILE, ahora_utc
from app.mercado import equipo as eq
from app.mercado import lote as lt
from app.mercado import oferta as of
from app.mercado import oferta_propia as op
from app.mercado import requisitos as rq
from app.mercado.proveedores import guardar_producto, hoy_chile
from app.models.tables import (
    CaLinea,
    CaRequisito,
    CaTrabajo,
    CompraAgil,
    Empresa,
    EmpresaProducto,
    EmpresaProductoMovimiento,
    Lote,
    OfertaPropia,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
    Usuario,
    Venta,
    VentaCompra,
)

ADJUDICADA = "adjudicada"
OC_RECIBIDA = "oc_recibida"
COMPRANDO = "comprando"
ENTREGADA = "entregada"
FACTURADA = "facturada"
PAGADA = "pagada"
ANULADA = "anulada"
ESTADOS = (ADJUDICADA, OC_RECIBIDA, COMPRANDO, ENTREGADA, FACTURADA, PAGADA, ANULADA)
ETIQUETA_ESTADO = eq.ETIQUETA_VENTA
PENDIENTES_DE_ENTREGA = (ADJUDICADA, OC_RECIBIDA, COMPRANDO)
EN_CURSO = PENDIENTES_DE_ENTREGA + (ENTREGADA, FACTURADA)

# --- Alarmas de sistema (§1.5): todas las constantes en un lugar ---------------------------------
HORAS_REQUISITOS = 24  # CA cotizando|lista con obligatorios sin cumplir y cierre en < 24 h
DIAS_OC_SIN_REGISTRAR = 3  # adjudicada hace más de 3 días y sin OC
DIAS_ENTREGA_VENCE = 2  # entrega comprometida en ≤ 2 días y no entregada
DIAS_FACTURA_PENDIENTE = 5  # entregada hace más de 5 días y sin factura
DIAS_ENTREGAS_SEMANA = 7  # tarjeta de «Hoy»
HORIZONTES_CAJA = (30, 60)

PLAZO_MAX_DIAS = 365
MONTO_MAX = 1e12
TEXTO_MAX = 200
MOTIVO_MAX = 300
NOTAS_MAX = 2000
_TOL = 1e-6
_MILES = re.compile(r"\d{1,3}(\.\d{3})+")


class ErrorVenta(ValueError):
    """Entrada inválida o transición no permitida (→ 422)."""


class VentaAjena(LookupError):
    """Venta (o compra) de otra empresa o inexistente (→ 404, regla 26)."""


@dataclass
class ConflictoVenta(Exception):
    """La venta cambió desde que se abrió: el cambio no se aplicó (→ 409)."""

    quien: str
    cuando: datetime | None

    def __str__(self) -> str:
        return f"Conflicto de versión de la venta ({self.quien})"


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------


def hoy_de(ahora: datetime) -> date:
    """El día en Chile de un instante UTC naive."""
    return ahora.replace(tzinfo=UTC).astimezone(TZ_CHILE).date()


def _texto(valor: str | None, campo: str, maximo: int, *, obligatorio: bool = False) -> str | None:
    t = " ".join((valor or "").split())
    if not t:
        if obligatorio:
            raise ErrorVenta(f"{campo}: es obligatorio")
        return None
    if len(t) > maximo:
        raise ErrorVenta(f"{campo}: máximo {maximo} caracteres")
    return t


def _fecha(valor: str | date | None, campo: str, *, obligatoria: bool = False) -> date | None:
    if isinstance(valor, date):
        return valor
    t = (valor or "").strip()
    if not t:
        if obligatoria:
            raise ErrorVenta(f"{campo}: es obligatoria")
        return None
    try:
        return date.fromisoformat(t)
    except ValueError:
        raise ErrorVenta(f"{campo}: fecha inválida") from None


def _monto(valor: str | float | None, campo: str, *, obligatorio: bool = False) -> float | None:
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        if obligatorio:
            raise ErrorVenta(f"{campo}: es obligatorio")
        return None
    t = str(valor).strip().replace("$", "").replace(" ", "")
    if "," in t:  # «1.234,5» (Chile)
        t = t.replace(".", "").replace(",", ".")
    elif _MILES.fullmatch(t):  # «96.000»: los pesos no llevan decimales, el punto separa miles
        t = t.replace(".", "")
    try:
        v = float(t)
    except ValueError:
        raise ErrorVenta(f"{campo}: número inválido") from None
    if v != v or v <= 0 or v > MONTO_MAX:
        raise ErrorVenta(f"{campo}: debe ser un número positivo")
    return v


def _dias(valor: str | int | None, campo: str) -> int | None:
    if valor is None or str(valor).strip() == "":
        return None
    try:
        n = int(str(valor).strip())
    except ValueError:
        raise ErrorVenta(f"{campo}: debe ser un número de días") from None
    if not 0 <= n <= PLAZO_MAX_DIAS:
        raise ErrorVenta(f"{campo}: de 0 a {PLAZO_MAX_DIAS} días")
    return n


def obtener(
    session: Session, empresa_id: int, venta_id: int, *, para_escribir: bool = False
) -> Venta:
    q = select(Venta).where(Venta.id == venta_id, Venta.empresa_id == empresa_id)
    if para_escribir:
        q = q.with_for_update()
    v = session.execute(q).scalar_one_or_none()
    if v is None:
        raise VentaAjena(venta_id)
    return v


def venta_de_ca(session: Session, empresa_id: int, codigo: str) -> Venta | None:
    return session.execute(
        select(Venta).where(Venta.empresa_id == empresa_id, Venta.ca_codigo == codigo)
    ).scalar_one_or_none()


def ventas_de_codigos(
    session: Session, empresa_id: int, codigos: list[str]
) -> dict[str, Venta]:
    if not codigos:
        return {}
    return {
        v.ca_codigo: v
        for v in session.execute(
            select(Venta).where(Venta.empresa_id == empresa_id, Venta.ca_codigo.in_(codigos))
        ).scalars()
    }


def _bloquear(session: Session, empresa_id: int, venta_id: int, version: int | None) -> Venta:
    v = obtener(session, empresa_id, venta_id, para_escribir=True)
    if version is not None and v.version != version:
        quien = "Alguien del equipo"
        if v.actualizado_por_id is not None:
            quien = eq.nombre_visible(session.get(Usuario, v.actualizado_por_id))
        raise ConflictoVenta(quien=quien, cuando=v.actualizado_en)
    return v


def _tocar(v: Venta, usuario_id: int | None, ahora: datetime | None = None) -> None:
    v.version += 1
    v.actualizado_en = ahora or ahora_utc()
    v.actualizado_por_id = usuario_id


def _evento_estado(session: Session, v: Venta, usuario_id: int | None, ahora: datetime) -> None:
    datos = {"estado": v.estado, "venta_id": v.id}
    if usuario_id is None:
        eq._evento_sistema(session, v.empresa_id, v.ca_codigo, eq.EV_VENTA, datos, ahora)
    else:
        eq._evento(session, v.empresa_id, v.ca_codigo, usuario_id, eq.EV_VENTA, datos, creado_en=ahora)


def _oferta_de(session: Session, v: Venta) -> OfertaPropia | None:
    if v.oferta_id is not None:
        o = session.get(OfertaPropia, v.oferta_id)
        if o is not None and o.empresa_id == v.empresa_id:
            return o
    return of.oferta_de(session, v.empresa_id, v.ca_codigo)


# ---------------------------------------------------------------------------
# Crear
# ---------------------------------------------------------------------------


def crear_desde_ganada(
    session: Session, empresa_id: int, codigo: str, usuario: Usuario | None = None
) -> Venta:
    """La venta de una CA ganada (idempotente por empresa × CA). No hace commit. Con `usuario` es
    la creación a mano; sin él, la del sistema (evento de sistema, una sola vez)."""
    v = venta_de_ca(session, empresa_id, codigo)
    if v is not None:
        return v
    ca = session.get(CompraAgil, codigo)
    empresa = session.get(Empresa, empresa_id)
    oferta = of.oferta_de(session, empresa_id, codigo)
    lote = lt.lote_activo_de(session, empresa_id, codigo)
    ahora = ahora_utc()
    nueva = Venta(
        empresa_id=empresa_id,
        ca_codigo=codigo,
        lote_id=lote.id if lote is not None else None,
        oferta_id=oferta.id if oferta is not None else None,
        estado=ADJUDICADA,
        entrega_direccion=(ca.direccion_entrega or None) if ca is not None else None,
        plazo_pago_dias=(empresa.plazo_pago_comprador_dias if empresa is not None else 30),
        version=1,
        creado_en=ahora,
        actualizado_en=ahora,
        actualizado_por_id=usuario.id if usuario is not None else None,
    )
    try:
        with session.begin_nested():
            session.add(nueva)
    except IntegrityError:  # otra sesión la creó a la vez: es la misma venta
        return venta_de_ca(session, empresa_id, codigo) or nueva
    session.flush()
    _evento_estado(session, nueva, usuario.id if usuario is not None else None, ahora)
    session.flush()
    return nueva


def crear_manual(session: Session, empresa_id: int, usuario: Usuario, codigo: str) -> Venta:
    """«Registrar venta» a mano: solo sobre una CA de la empresa en estado `enviada`."""
    t = eq.trabajo_de(session, empresa_id, codigo)
    existente = venta_de_ca(session, empresa_id, codigo)
    if existente is not None:
        return existente
    if t is None or t.estado != eq.ENVIADA:
        raise ErrorVenta("Solo se puede registrar la venta de una Compra Ágil enviada")
    return crear_desde_ganada(session, empresa_id, codigo, usuario)


# ---------------------------------------------------------------------------
# Etapas
# ---------------------------------------------------------------------------


def _exigir_estado(v: Venta, permitidos: tuple[str, ...], accion: str) -> None:
    if v.estado not in permitidos:
        raise ErrorVenta(
            f"No se puede {accion} una venta en «{ETIQUETA_ESTADO.get(v.estado, v.estado)}»"
        )


def _cambiar(
    session: Session, v: Venta, nuevo: str, usuario: Usuario, ahora: datetime
) -> None:
    anterior = v.estado
    v.estado = nuevo
    _tocar(v, usuario.id, ahora)
    if nuevo != anterior:
        _evento_estado(session, v, usuario.id, ahora)


def _plazo_oferta(session: Session, v: Venta) -> int | None:
    o = _oferta_de(session, v)
    if o is not None and o.plazo_entrega_dias is not None:
        return o.plazo_entrega_dias
    ca = session.get(CompraAgil, v.ca_codigo)
    return ca.plazo_entrega_dias if ca is not None else None


def registrar_oc(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    venta_id: int,
    version: int | None,
    *,
    numero: str,
    fecha: str | date | None,
    monto: str | float | None = None,
) -> Venta:
    """Orden de compra recibida. La fecha comprometida de entrega = fecha de la OC + el plazo de
    la oferta (o el de la CA). Se puede corregir mientras no se entregue."""
    v = _bloquear(session, empresa_id, venta_id, version)
    _exigir_estado(v, PENDIENTES_DE_ENTREGA, "registrar la OC de")
    v.oc_numero = _texto(numero, "N° de OC", 50, obligatorio=True)
    v.oc_fecha = _fecha(fecha, "Fecha de la OC", obligatoria=True)
    v.oc_monto = _monto(monto, "Monto de la OC")
    plazo = _plazo_oferta(session, v)
    v.entrega_fecha_comprometida = (
        v.oc_fecha + timedelta(days=plazo) if plazo is not None and v.oc_fecha else None
    )
    ahora = ahora_utc()
    _cambiar(session, v, OC_RECIBIDA if v.estado == ADJUDICADA else v.estado, usuario, ahora)
    _sincronizar_margen(session, v)
    session.flush()
    return v


def guardar_entrega_datos(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    venta_id: int,
    version: int | None,
    *,
    direccion: str,
    horario: str,
    recibe: str,
    comprometida: str | date | None = None,
    notas: str = "",
) -> Venta:
    """Datos logísticos de la entrega (prellenados con los de la CA). No cambia el estado."""
    v = _bloquear(session, empresa_id, venta_id, version)
    _exigir_estado(v, ESTADOS[:-1], "editar los datos de entrega de")
    v.entrega_direccion = _texto(direccion, "Dirección", 500)
    v.entrega_horario = _texto(horario, "Horario", TEXTO_MAX)
    v.entrega_recibe = _texto(recibe, "Quién recibe", TEXTO_MAX)
    if comprometida not in (None, ""):
        v.entrega_fecha_comprometida = _fecha(comprometida, "Fecha comprometida")
    v.notas = (notas or "").strip()[:NOTAS_MAX] or None
    _tocar(v, usuario.id)
    session.flush()
    return v


# --- Compras -----------------------------------------------------------------------------------


def costo_real_de(session: Session, venta_id: int) -> float | None:
    total = session.execute(
        select(func.sum(VentaCompra.cantidad * VentaCompra.precio_neto)).where(
            VentaCompra.venta_id == venta_id
        )
    ).scalar_one()
    return None if total is None else round(float(total), 2)


def _ingreso(session: Session, v: Venta, o: OfertaPropia | None) -> float | None:
    """Lo que entra por la venta, en orden de confianza: factura, OC, precio real del ganador y
    el total de nuestra oferta."""
    for candidato in (
        v.factura_monto,
        v.oc_monto,
        o.precio_ganador_real if o is not None else None,
        o.total_neto if o is not None else None,
    ):
        if candidato:
            return float(candidato)
    return None


def _sincronizar_margen(session: Session, v: Venta) -> None:
    """`costo_real` de la venta y, con él, `ofertas_propias.margen_realizado` (ingreso − costo real).
    Sin compras registradas no se toca: sigue valiendo el margen estimado de F-plat-5."""
    v.costo_real = costo_real_de(session, v.id)
    if v.costo_real is None:
        return
    o = _oferta_de(session, v)
    ingreso = _ingreso(session, v, o)
    if o is not None and ingreso is not None:
        o.margen_realizado = round(ingreso - v.costo_real, 2)


def tiene_costo_real(session: Session, empresa_id: int, codigo: str) -> bool:
    """¿La venta de esa CA ya tiene compras registradas? (`sombra` no pisa ese margen)."""
    v = venta_de_ca(session, empresa_id, codigo)
    return v is not None and v.costo_real is not None


def registrar_compra(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    venta_id: int,
    version: int | None,
    *,
    proveedor_producto_id: int,
    cantidad: str | float,
    precio_neto: str | float,
    fecha: str | date | None = None,
    pagada: bool = False,
    pagar_en: str | date | None = None,
) -> VentaCompra:
    """Una compra a un proveedor para esta venta. `cantidad` en unidades del formato del proveedor;
    `precio_neto` por formato. Escribe `costo_historial` (fuente `compra`), suma stock si el
    producto está en el inventario y recalcula el costo real y el margen realizado."""
    v = _bloquear(session, empresa_id, venta_id, version)
    _exigir_estado(v, PENDIENTES_DE_ENTREGA, "registrar compras de")
    pp = of.validar_proveedor_producto(session, empresa_id, proveedor_producto_id)  # 404 si ajeno
    prov = session.get(Proveedor, pp.proveedor_id)
    cant = _monto(cantidad, "Cantidad", obligatorio=True)
    precio = _monto(precio_neto, "Precio neto", obligatorio=True)
    assert cant is not None and precio is not None
    dia = _fecha(fecha, "Fecha de la compra") or hoy_chile()
    vence = _fecha(pagar_en, "Fecha de pago al proveedor")
    if vence is None and prov is not None and prov.plazo_pago_dias is not None:
        vence = dia + timedelta(days=prov.plazo_pago_dias)
    c = VentaCompra(
        venta_id=v.id,
        proveedor_id=pp.proveedor_id,
        producto_id=pp.producto_id,
        formato_id=pp.formato_id,
        descripcion=f"{pp.producto_texto} · {pp.formato_texto}".strip(" ·")[:300],
        cantidad=cant,
        precio_neto=precio,
        fecha=dia,
        pagada=bool(pagada),
        pagar_en=None if pagada else vence,
        creado_por_id=usuario.id,
    )
    session.add(c)
    session.flush()
    if prov is not None:
        guardar_producto(
            session, prov, producto_texto=pp.producto_texto, formato_texto=pp.formato_texto,
            precio_neto=precio, vigente_desde=dia, factor_base=pp.factor_base,
            unidad_base=pp.unidad_base, fuente="compra", usuario_id=usuario.id,
        )  # fmt: skip
    if pp.producto_id is not None and pp.formato_id is not None:
        ep = _ep_de(session, empresa_id, pp.producto_id, pp.formato_id)
        if ep is not None:
            op.ajustar_stock(session, empresa_id, ep.id, cant, "compra", f"venta:{v.id}", usuario.id)
    ahora = ahora_utc()
    _cambiar(session, v, COMPRANDO if v.estado in (ADJUDICADA, OC_RECIBIDA) else v.estado, usuario, ahora)
    _sincronizar_margen(session, v)
    session.flush()
    return c


def _ep_de(
    session: Session, empresa_id: int, producto_id: int, formato_id: int
) -> EmpresaProducto | None:
    return session.execute(
        select(EmpresaProducto).where(
            EmpresaProducto.empresa_id == empresa_id,
            EmpresaProducto.producto_id == producto_id,
            EmpresaProducto.formato_id == formato_id,
            EmpresaProducto.activo.is_(True),
        )
    ).scalar_one_or_none()


def marcar_compra_pagada(
    session: Session, empresa_id: int, compra_id: int, pagada: bool
) -> VentaCompra:
    c = session.execute(
        select(VentaCompra)
        .join(Venta, Venta.id == VentaCompra.venta_id)
        .where(VentaCompra.id == compra_id, Venta.empresa_id == empresa_id)
    ).scalar_one_or_none()
    if c is None:
        raise VentaAjena(compra_id)
    c.pagada = pagada
    session.flush()
    return c


# --- Entrega -----------------------------------------------------------------------------------


def _stock_de_la_oferta(session: Session, v: Venta) -> dict[int, float]:
    """`{producto_id: cantidad en unidad base}` de las líneas de la oferta cuyo costo sale del
    stock propio. Una línea `reemplazada` (o que ya no existe) no cuenta."""
    o = _oferta_de(session, v)
    if o is None:
        return {}
    ids = {
        int(d["linea_id"])
        for d in (o.lineas or [])
        if d.get("origen") == of.ORIGEN_STOCK and d.get("linea_id") is not None
    }
    if not ids:
        return {}
    out: dict[int, float] = {}
    for producto_id, cantidad_base in session.execute(
        select(CaLinea.producto_id, CaLinea.cantidad_base).where(
            CaLinea.id.in_(ids),
            CaLinea.ca_codigo == v.ca_codigo,
            CaLinea.estado != "reemplazada",
        )
    ):
        if producto_id is not None and cantidad_base:
            out[int(producto_id)] = out.get(int(producto_id), 0.0) + float(cantidad_base)
    return out


def _sacar(
    session: Session,
    v: Venta,
    ep: EmpresaProducto,
    unidades: float,
    usuario_id: int | None,
) -> float:
    """Entrega `unidades` (del formato) del producto: primero libera su parte de la reserva del lote
    y luego descuenta el stock, sin pasar de lo disponible (`reservado ≤ stock` siempre).
    Devuelve lo descontado."""
    if v.lote_id is not None:
        op.liberar_parcial(session, v.empresa_id, ep.id, unidades, f"{eq.PREFIJO_LOTE}{v.lote_id}", usuario_id)
        session.refresh(ep)
    tomar = round(min(unidades, max(ep.disponible, 0.0)), 3)
    if tomar <= _TOL:
        return 0.0
    op.ajustar_stock(session, v.empresa_id, ep.id, -tomar, "entrega", f"venta:{v.id}", usuario_id)
    return tomar


def _entregar_stock(session: Session, v: Venta, usuario_id: int | None) -> list[str]:
    """Convierte reserva en movimientos `entrega` y devuelve los avisos (faltó stock)."""
    avisos: list[str] = []
    # 1) Lo que la oferta cubrió con stock propio: por producto, repartido entre sus formatos
    #    activos (primero el de más stock), en unidad base.
    for producto_id, base in sorted(_stock_de_la_oferta(session, v).items()):
        filas = list(
            session.execute(
                select(EmpresaProducto, ProductoFormato.factor_base)
                .join(ProductoFormato, ProductoFormato.id == EmpresaProducto.formato_id)
                .where(
                    EmpresaProducto.empresa_id == v.empresa_id,
                    EmpresaProducto.producto_id == producto_id,
                    EmpresaProducto.activo.is_(True),
                )
                .order_by(EmpresaProducto.id)
                .with_for_update(of=EmpresaProducto)
            ).all()
        )
        filas.sort(key=lambda f: -((f[0].stock or 0.0) * float(f[1])))
        restante = base
        for ep, factor in filas:
            if restante <= _TOL:
                break
            f = float(factor)
            hecho = _sacar(session, v, ep, round(restante / f, 3), usuario_id)
            restante -= hecho * f
        if restante > 1e-3:
            nombre = session.execute(
                select(ProductoCanonico.nombre).where(ProductoCanonico.id == producto_id)
            ).scalar_one_or_none()
            avisos.append(
                f"{nombre or 'Producto'}: faltaron {restante:g} (unidad base) en el stock; "
                "se descontó lo que había"
            )
    # 2) Lo que se compró para esta venta entró con `compra`: sale con `entrega`.
    for c in session.execute(select(VentaCompra).where(VentaCompra.venta_id == v.id)).scalars():
        if c.producto_id is None or c.formato_id is None:
            continue
        ep = _ep_de(session, v.empresa_id, c.producto_id, c.formato_id)
        if ep is None:
            continue
        hecho = _sacar(session, v, ep, float(c.cantidad), usuario_id)
        if hecho + _TOL < c.cantidad:
            avisos.append(f"{c.descripcion}: se entregaron {hecho:g} de {c.cantidad:g} del stock")
    return avisos


def registrar_entrega(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    venta_id: int,
    version: int | None,
    *,
    fecha_real: str | date | None = None,
    guia: str = "",
    recibe: str = "",
    horario: str = "",
    direccion: str = "",
) -> tuple[Venta, list[str]]:
    """Marca la venta `entregada`: descuenta el stock (movimientos `entrega`, referencia
    `venta:{id}`) y libera su parte de la reserva del lote. Una sola vez (el estado lo impide)."""
    v = _bloquear(session, empresa_id, venta_id, version)
    _exigir_estado(v, PENDIENTES_DE_ENTREGA, "entregar")
    v.entrega_fecha_real = _fecha(fecha_real, "Fecha de entrega") or hoy_chile()
    v.guia_numero = _texto(guia, "N° de guía", 50)
    if direccion.strip():
        v.entrega_direccion = _texto(direccion, "Dirección", 500)
    if horario.strip():
        v.entrega_horario = _texto(horario, "Horario", TEXTO_MAX)
    if recibe.strip():
        v.entrega_recibe = _texto(recibe, "Quién recibe", TEXTO_MAX)
    avisos = _entregar_stock(session, v, usuario.id)
    _cambiar(session, v, ENTREGADA, usuario, ahora_utc())
    session.flush()
    return v, avisos


# --- Factura y pago ----------------------------------------------------------------------------


def registrar_factura(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    venta_id: int,
    version: int | None,
    *,
    numero: str,
    fecha: str | date | None,
    monto: str | float | None = None,
    plazo_pago_dias: str | int | None = None,
) -> Venta:
    """Factura emitida: `pago_esperado_en` = fecha + plazo de pago (el de la empresa salvo que se
    indique otro para esta venta). Sin monto, el de la OC o el total de la oferta."""
    v = _bloquear(session, empresa_id, venta_id, version)
    _exigir_estado(v, (ENTREGADA, FACTURADA), "facturar")
    v.factura_numero = _texto(numero, "N° de factura", 50, obligatorio=True)
    v.factura_fecha = _fecha(fecha, "Fecha de la factura", obligatoria=True)
    o = _oferta_de(session, v)
    v.factura_monto = _monto(monto, "Monto de la factura") or _ingreso(session, v, o)
    dias = _dias(plazo_pago_dias, "Plazo de pago")
    if dias is not None:
        v.plazo_pago_dias = dias
    assert v.factura_fecha is not None
    v.pago_esperado_en = v.factura_fecha + timedelta(days=v.plazo_pago_dias)
    _cambiar(session, v, FACTURADA, usuario, ahora_utc())
    _sincronizar_margen(session, v)
    session.flush()
    return v


def registrar_pago(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    venta_id: int,
    version: int | None,
    *,
    fecha: str | date | None = None,
) -> Venta:
    v = _bloquear(session, empresa_id, venta_id, version)
    _exigir_estado(v, (FACTURADA,), "registrar el pago de")
    v.pago_recibido_en = _fecha(fecha, "Fecha del pago") or hoy_chile()
    _cambiar(session, v, PAGADA, usuario, ahora_utc())
    session.flush()
    return v


def anular(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    venta_id: int,
    version: int | None,
    motivo: str,
) -> Venta:
    """Anula una venta que aún no se entregó (lo entregado ya movió stock y no se revierte)."""
    v = _bloquear(session, empresa_id, venta_id, version)
    _exigir_estado(v, PENDIENTES_DE_ENTREGA, "anular")
    v.motivo_anulacion = _texto(motivo, "Motivo", MOTIVO_MAX, obligatorio=True)
    _cambiar(session, v, ANULADA, usuario, ahora_utc())
    session.flush()
    return v


# ---------------------------------------------------------------------------
# Vistas
# ---------------------------------------------------------------------------


@dataclass
class FilaVenta:
    venta: Venta
    nombre_ca: str
    comprador: str
    monto: float | None
    atrasada: bool  # entrega comprometida vencida (rojo) o pago esperado vencido


def monto_de(v: Venta, oferta_total: float | None) -> float | None:
    for m in (v.factura_monto, v.oc_monto, oferta_total):
        if m:
            return float(m)
    return None


def listar(
    session: Session, empresa_id: int, estado: str | None = None, hoy: date | None = None
) -> list[FilaVenta]:
    hoy = hoy or hoy_chile()
    q = (
        select(Venta, CompraAgil.nombre, CompraAgil.organismo_nombre, OfertaPropia.total_neto)
        .join(CompraAgil, CompraAgil.codigo == Venta.ca_codigo)
        .outerjoin(OfertaPropia, OfertaPropia.id == Venta.oferta_id)
        .where(Venta.empresa_id == empresa_id)
        .order_by(Venta.creado_en.desc(), Venta.id.desc())
    )
    if estado in ESTADOS:
        q = q.where(Venta.estado == estado)
    out: list[FilaVenta] = []
    for v, nombre, organismo, total in session.execute(q):
        atrasada = (
            v.estado in PENDIENTES_DE_ENTREGA
            and v.entrega_fecha_comprometida is not None
            and v.entrega_fecha_comprometida < hoy
        ) or (
            v.estado == FACTURADA and v.pago_esperado_en is not None and v.pago_esperado_en < hoy
        )
        out.append(FilaVenta(v, nombre or v.ca_codigo, organismo or "", monto_de(v, total), atrasada))
    return out


def conteo_por_estado(session: Session, empresa_id: int) -> dict[str, int]:
    return {
        e: int(n)
        for e, n in session.execute(
            select(Venta.estado, func.count()).where(Venta.empresa_id == empresa_id).group_by(Venta.estado)
        )
    }


@dataclass
class VistaVenta:
    venta: Venta
    nombre_ca: str
    comprador: str
    oferta: OfertaPropia | None
    compras: list[VentaCompra]
    movimientos: list[dict[str, Any]]
    ingreso: float | None
    margen_ofertado: float | None
    margen_realizado: float | None
    siguientes: list[str] = field(default_factory=list)
    lote_nombre: str | None = None


def vista(session: Session, empresa_id: int, venta_id: int) -> VistaVenta:
    v = obtener(session, empresa_id, venta_id)
    ca = session.get(CompraAgil, v.ca_codigo)
    o = _oferta_de(session, v)
    compras = list(
        session.execute(
            select(VentaCompra).where(VentaCompra.venta_id == v.id).order_by(VentaCompra.fecha, VentaCompra.id)
        ).scalars()
    )
    movimientos = [
        {"cuando": m.creado_en, "producto": nombre, "formato": formato, "delta": float(m.delta), "motivo": m.motivo}
        for m, nombre, formato in session.execute(
            select(EmpresaProductoMovimiento, ProductoCanonico.nombre, ProductoFormato.nombre)
            .join(EmpresaProducto, EmpresaProducto.id == EmpresaProductoMovimiento.empresa_producto_id)
            .join(ProductoCanonico, ProductoCanonico.id == EmpresaProducto.producto_id)
            .join(ProductoFormato, ProductoFormato.id == EmpresaProducto.formato_id)
            .where(
                EmpresaProducto.empresa_id == empresa_id,
                EmpresaProductoMovimiento.referencia == f"venta:{v.id}",
            )
            .order_by(EmpresaProductoMovimiento.id)
        )
    ]
    ofertado = None
    if o is not None and ca is not None and o.total_neto:
        ofertado = of.margen_realizado(session, empresa_id, ca, o, o.total_neto)
    ingreso = _ingreso(session, v, o)
    realizado = (
        round(ingreso - v.costo_real, 2) if ingreso is not None and v.costo_real is not None else None
    )
    lote_nombre = None
    if v.lote_id is not None:
        lote_nombre = session.execute(
            select(Lote.nombre).where(Lote.id == v.lote_id, Lote.empresa_id == empresa_id)
        ).scalar_one_or_none()
    return VistaVenta(
        venta=v,
        nombre_ca=(ca.nombre if ca is not None else None) or v.ca_codigo,
        comprador=(ca.organismo_nombre if ca is not None else None) or "",
        oferta=o,
        compras=compras,
        movimientos=movimientos,
        ingreso=ingreso,
        margen_ofertado=ofertado,
        margen_realizado=realizado,
        lote_nombre=lote_nombre,
    )


def productos_de_proveedores(session: Session, empresa_id: int, venta: Venta) -> list[tuple[int, str]]:
    """Opciones del formulario de compra: los productos de proveedores de la empresa, con los de
    las líneas de la oferta primero."""
    o = _oferta_de(session, venta)
    vinculados: set[int] = set()
    if o is not None:
        ids = [int(d["linea_id"]) for d in (o.lineas or []) if d.get("linea_id") is not None]
        if ids:
            vinculados = {
                int(p)
                for p in session.execute(
                    select(CaLinea.producto_id).where(
                        CaLinea.id.in_(ids), CaLinea.estado != "reemplazada", CaLinea.producto_id.is_not(None)
                    )
                ).scalars()
                if p is not None
            }
    filas = list(
        session.execute(
            select(ProveedorProducto, Proveedor.nombre)
            .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
            .where(Proveedor.empresa_id == empresa_id, Proveedor.activo.is_(True))
            .order_by(Proveedor.nombre, ProveedorProducto.producto_texto)
        ).all()
    )
    filas.sort(key=lambda f: 0 if f[0].producto_id in vinculados else 1)
    return [
        (
            pp.id,
            f"{nombre} — {pp.producto_texto}"
            + (f" ({pp.formato_texto})" if pp.formato_texto else "")
            + f" · ${pp.precio_neto_actual:,.0f}".replace(",", "."),
        )
        for pp, nombre in filas
    ]


# ---------------------------------------------------------------------------
# Caja (§2): una tabla y tres números
# ---------------------------------------------------------------------------


@dataclass
class MovimientoCaja:
    fecha: date
    concepto: str
    monto: float
    venta_id: int | None = None
    vencido: bool = False


@dataclass
class Caja:
    por_cobrar: list[MovimientoCaja]  # facturado y no pagado
    por_cobrar_total: float
    comprometido: list[MovimientoCaja]  # compras a proveedores no pagadas
    comprometido_total: float
    sin_facturar: list[MovimientoCaja]  # adjudicadas aún sin facturar (monto de la OC o la oferta)
    sin_facturar_total: float
    neto: dict[int, float] = field(default_factory=dict)  # horizonte (días) → cobros − pagos


def caja(session: Session, empresa_id: int, hoy: date | None = None) -> Caja:
    """Por cobrar al Estado, comprometido con proveedores, adjudicado sin facturar y el neto a 30 y
    60 días. Montos NETOS. El neto del horizonte = cobros esperados con fecha ≤ horizonte
    (facturas por su `pago_esperado_en`; lo no facturado por entrega comprometida + plazo de pago)
    menos las compras impagas con fecha de pago ≤ horizonte; lo ya vencido cuenta de inmediato."""
    hoy = hoy or hoy_chile()
    por_cobrar: list[MovimientoCaja] = []
    sin_facturar: list[MovimientoCaja] = []
    for v, nombre, total in session.execute(
        select(Venta, CompraAgil.nombre, OfertaPropia.total_neto)
        .join(CompraAgil, CompraAgil.codigo == Venta.ca_codigo)
        .outerjoin(OfertaPropia, OfertaPropia.id == Venta.oferta_id)
        .where(Venta.empresa_id == empresa_id, Venta.estado.in_(PENDIENTES_DE_ENTREGA + (ENTREGADA, FACTURADA)))
    ):
        monto = monto_de(v, total) or 0.0
        concepto = f"{nombre or v.ca_codigo}" + (f" · factura {v.factura_numero}" if v.factura_numero else "")
        if v.estado == FACTURADA:
            fecha = v.pago_esperado_en or hoy
            por_cobrar.append(MovimientoCaja(fecha, concepto, monto, v.id, fecha < hoy))
        else:
            base = v.entrega_fecha_comprometida or hoy
            sin_facturar.append(
                MovimientoCaja(base + timedelta(days=v.plazo_pago_dias), concepto, monto, v.id)
            )
    comprometido = [
        MovimientoCaja(
            c.pagar_en or c.fecha,
            f"{c.descripcion or 'Compra'}" + (f" · {prov or ''}" if prov else ""),
            round(c.cantidad * c.precio_neto, 2),
            v.id,
            (c.pagar_en or c.fecha) < hoy,
        )
        for c, v, prov in session.execute(
            select(VentaCompra, Venta, Proveedor.nombre)
            .join(Venta, Venta.id == VentaCompra.venta_id)
            .outerjoin(Proveedor, Proveedor.id == VentaCompra.proveedor_id)
            .where(Venta.empresa_id == empresa_id, Venta.estado != ANULADA, VentaCompra.pagada.is_(False))
        )
    ]
    for lista in (por_cobrar, sin_facturar, comprometido):
        lista.sort(key=lambda m: m.fecha)
    neto: dict[int, float] = {}
    for dias in HORIZONTES_CAJA:
        limite = hoy + timedelta(days=dias)
        cobros = sum(m.monto for m in por_cobrar + sin_facturar if m.fecha <= limite)
        pagos = sum(m.monto for m in comprometido if m.fecha <= limite)
        neto[dias] = round(cobros - pagos, 2)
    return Caja(
        por_cobrar, round(sum(m.monto for m in por_cobrar), 2),
        comprometido, round(sum(m.monto for m in comprometido), 2),
        sin_facturar, round(sum(m.monto for m in sin_facturar), 2), neto,
    )  # fmt: skip


def resumen_hoy(session: Session, empresa_id: int, hoy: date | None = None) -> dict[str, Any]:
    """Tarjeta de «Hoy»: ventas en curso, por cobrar y entregas de la semana."""
    hoy = hoy or hoy_chile()
    en_curso = session.execute(
        select(func.count()).select_from(Venta).where(Venta.empresa_id == empresa_id, Venta.estado.in_(EN_CURSO))
    ).scalar_one()
    por_cobrar = session.execute(
        select(Venta, OfertaPropia.total_neto)
        .outerjoin(OfertaPropia, OfertaPropia.id == Venta.oferta_id)
        .where(Venta.empresa_id == empresa_id, Venta.estado == FACTURADA)
    ).all()
    entregas = session.execute(
        select(func.count())
        .select_from(Venta)
        .where(
            Venta.empresa_id == empresa_id,
            Venta.estado.in_(PENDIENTES_DE_ENTREGA),
            Venta.entrega_fecha_comprometida.is_not(None),
            Venta.entrega_fecha_comprometida <= hoy + timedelta(days=DIAS_ENTREGAS_SEMANA),
        )
    ).scalar_one()
    return {
        "en_curso": int(en_curso or 0),
        "por_cobrar": round(sum(monto_de(v, t) or 0.0 for v, t in por_cobrar), 2),
        "entregas_semana": int(entregas or 0),
    }


# ---------------------------------------------------------------------------
# Alarmas de sistema (§1.5)
# ---------------------------------------------------------------------------


def generar_alarmas_seguimiento(session: Session, empresa_id: int, ahora: datetime) -> dict[str, int]:
    """`req_pendientes`, `oc_sin_registrar`, `entrega_vence`, `factura_pendiente` y `pago_atrasado`
    de la empresa. Idempotentes como las de F-equipo (índice único parcial: un evento por empresa ×
    CA × tipo). No hace commit. Solo columnas livianas (regla 12)."""
    cuenta = dict.fromkeys(eq.TIPOS_SEGUIMIENTO, 0)
    hoy = hoy_de(ahora)

    def _emitir(tipo: str, codigo: str, datos: dict[str, Any]) -> None:
        if eq._evento_sistema(session, empresa_id, codigo, tipo, datos, ahora):
            cuenta[tipo] += 1

    # Requisitos obligatorios sin cumplir y cierre en menos de 24 h.
    for (codigo,) in session.execute(
        select(CaTrabajo.ca_codigo)
        .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
        .where(
            CaTrabajo.empresa_id == empresa_id,
            CaTrabajo.estado.in_((eq.COTIZANDO, eq.LISTA)),
            CompraAgil.fecha_cierre > ahora,
            CompraAgil.fecha_cierre <= ahora + timedelta(hours=HORAS_REQUISITOS),
            select(CaRequisito.id)
            .where(
                CaRequisito.empresa_id == CaTrabajo.empresa_id,
                CaRequisito.ca_codigo == CaTrabajo.ca_codigo,
                CaRequisito.obligatorio.is_(True),
                CaRequisito.cumplido.is_(False),
            )
            .exists(),
        )
    ):
        _emitir(eq.EV_REQ_PENDIENTES, codigo, {"pendientes": len(rq.pendientes(session, empresa_id, codigo))})

    ventas = session.execute(
        select(Venta).where(Venta.empresa_id == empresa_id, Venta.estado.in_(EN_CURSO))
    ).scalars()
    for v in ventas:
        datos = {"venta_id": v.id, "estado": v.estado}
        if v.estado == ADJUDICADA and v.creado_en < ahora - timedelta(days=DIAS_OC_SIN_REGISTRAR):
            _emitir(eq.EV_OC_SIN_REGISTRAR, v.ca_codigo, datos)
        if (
            v.estado in PENDIENTES_DE_ENTREGA
            and v.entrega_fecha_comprometida is not None
            and v.entrega_fecha_comprometida <= hoy + timedelta(days=DIAS_ENTREGA_VENCE)
        ):
            _emitir(eq.EV_ENTREGA_VENCE, v.ca_codigo, datos)
        if v.estado == ENTREGADA:
            entregada = v.entrega_fecha_real or hoy_de(v.actualizado_en)
            if entregada < hoy - timedelta(days=DIAS_FACTURA_PENDIENTE):
                _emitir(eq.EV_FACTURA_PENDIENTE, v.ca_codigo, datos)
        if v.estado == FACTURADA and v.pago_esperado_en is not None and v.pago_esperado_en < hoy:
            _emitir(eq.EV_PAGO_ATRASADO, v.ca_codigo, datos)
    session.flush()
    return cuenta
