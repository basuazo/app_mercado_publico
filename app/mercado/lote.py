"""Cotizar varias Compras Ágiles a la vez (F-plat-lote): lote, agregado, solicitudes de precio a
proveedores, ofertas pre-llenadas, reserva de stock y «En trabajo».

- Un **lote** junta 3–5 CA de la empresa. Una CA está en a lo más un lote NO cerrado: lo valida el
  servidor bajo el bloqueo de la fila de la empresa (`FOR UPDATE`), así dos personas que agregan la
  misma CA a dos lotes distintos a la vez no pasan las dos.
- El **agregado** (`lote_lineas`) es derivado: suma en la unidad base las líneas `ok|dudosa` con
  producto de todas las CA, lo redondea hacia arriba al formato de compra, descuenta el stock
  propio y sugiere el proveedor con `proveedores.mejor_costo` (vigente, con tramos). Se recalcula
  entero al agregar/quitar CA, al cambiar un proveedor o al registrar una respuesta; el proveedor
  elegido a mano (`proveedor_fijado`) se respeta.
- Las **solicitudes** (una por proveedor con líneas a comprar) generan el mensaje para pegar en
  WhatsApp o correo y una planilla. Esta fase NO manda nada: la persona lo envía. El mensaje y la
  planilla nunca llevan el código de la CA ni el comprador (no revelar qué se cotiza).
- La respuesta vuelve a `proveedor_productos` por `proveedores.guardar_producto` (fuente
  `solicitud`, vigente hoy, con historial) y el lote se recalcula.

Estados: abierto → solicitado (≥ 1 enviada) → con_precios (todas respondidas o sin respuesta) →
ofertado (reserva el stock) → cerrado (libera). Hacia atrás es libre salvo desde `cerrado`.
`version` como F-equipo. Ownership (reglas 17 y 26): toda función recibe `empresa_id` y un id de
otra empresa es `LoteAjeno` (→ 404). Sin httpx. Sin commit: lo hace quien llama.
"""

from __future__ import annotations

import io
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from jinja2 import Environment
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.mercado import adjudicacion as adj
from app.mercado import equipo as eq
from app.mercado import oferta as of
from app.mercado import oferta_propia as op
from app.mercado.mesa_dia import condiciones_vigente
from app.mercado.proveedores import (
    MejorCosto,
    guardar_producto,
    hoy_chile,
    mejor_costo,
    normalizar_tramos,
)
from app.models.tables import (
    ESTADOS_LOTE,
    CaEvento,
    CaLinea,
    CaTrabajo,
    CompraAgil,
    Empresa,
    Lote,
    LoteCa,
    LoteLinea,
    OfertaPropia,
    ProductoCanonico,
    ProductoFormato,
    Proveedor,
    ProveedorProducto,
    SolicitudLinea,
    SolicitudPrecio,
    Usuario,
)

_log = get_logger(__name__)

ABIERTO, SOLICITADO, CON_PRECIOS, OFERTADO, CERRADO = ESTADOS_LOTE
ACTIVOS = (ABIERTO, SOLICITADO, CON_PRECIOS, OFERTADO)  # «lote abierto» = no cerrado
EDITABLES = (ABIERTO, SOLICITADO, CON_PRECIOS)  # se agregan/quitan CA y se recalcula
ETIQUETA_ESTADO = {
    ABIERTO: "Abierto",
    SOLICITADO: "Precios solicitados",
    CON_PRECIOS: "Con precios",
    OFERTADO: "Ofertado",
    CERRADO: "Cerrado",
}
BORRADOR, ENVIADA, RESPONDIDA, SIN_RESPUESTA = "borrador", "enviada", "respondida", "sin_respuesta"
ETIQUETA_SOLICITUD = {
    BORRADOR: "Borrador",
    ENVIADA: "Enviada",
    RESPONDIDA: "Respondida",
    SIN_RESPUESTA: "Sin respuesta",
}
CANALES = {
    "whatsapp": "WhatsApp",
    "correo": "Correo",
    "llamada": "Llamada",
    "planilla": "Planilla",
    "otro": "Otro",
}
MAX_CAS_LOTE = 30
NOMBRE_MAX = 200
NOTAS_MAX = 2000
PLAZO_MAX_DIAS = 365
PRECIO_MAX = 1e12
RETENCION_DIAS = 730  # 24 meses: lotes `cerrado` (F-plat-7 §1.4)
FUENTE = "Fuente: Dirección ChileCompra"
_TOL = 1e-6

# Prefijos del <select> de proveedor de una línea del agregado.
PREFIJO_PP = "pp:"  # un `proveedor_productos.id` con precio
PREFIJO_PROV = "prov:"  # un proveedor SIN precio de ese producto: se le pedirá


class ErrorLote(ValueError):
    """Entrada o transición inválida (→ 422); el mensaje se muestra."""


class LoteAjeno(LookupError):
    """Lote, solicitud, línea o proveedor de otra empresa, o inexistente (→ 404, regla 26)."""


@dataclass
class CaEnOtroLote(Exception):
    """La CA ya está en otro lote no cerrado de la empresa (→ 409 con el nombre del lote)."""

    codigo: str
    lote_id: int
    lote_nombre: str

    def __str__(self) -> str:
        return f"La CA {self.codigo} ya está en el lote «{self.lote_nombre}»"


@dataclass
class ConflictoLote(Exception):
    """El lote (o la solicitud) cambió desde que el usuario lo abrió: no se aplicó (→ 409)."""

    quien: str
    cuando: datetime | None

    def __str__(self) -> str:
        return f"Conflicto de versión del lote ({self.quien})"


# ---------------------------------------------------------------------------
# Lectura con ownership
# ---------------------------------------------------------------------------


def obtener(
    session: Session, empresa_id: int, lote_id: int, *, para_escribir: bool = False
) -> Lote:
    consulta = select(Lote).where(Lote.id == lote_id, Lote.empresa_id == empresa_id)
    if para_escribir:
        consulta = consulta.with_for_update()
    lote = session.execute(consulta).scalar_one_or_none()
    if lote is None:
        raise LoteAjeno(lote_id)
    return lote


def _bloquear(session: Session, empresa_id: int, lote_id: int, version: int | None) -> Lote:
    """El lote bloqueado; si `version` no es la vigente, `ConflictoLote` (no se aplica nada)."""
    lote = obtener(session, empresa_id, lote_id, para_escribir=True)
    if version is not None and lote.version != version:
        raise ConflictoLote(quien="Alguien del equipo", cuando=lote.actualizado_en)
    return lote


def _tocar(lote: Lote) -> None:
    lote.version += 1
    lote.actualizado_en = ahora_utc()


def _bloquear_empresa(session: Session, empresa_id: int) -> Empresa:
    """Serializa los cambios de membresía de CA entre lotes de la empresa (Postgres: FOR UPDATE de
    la fila de la empresa; dos lotes distintos no se bloquean entre sí, la empresa sí)."""
    empresa = session.execute(
        select(Empresa).where(Empresa.id == empresa_id).with_for_update()
    ).scalar_one_or_none()
    if empresa is None:
        raise LoteAjeno(empresa_id)
    return empresa


def codigos_de(session: Session, lote_id: int) -> list[str]:
    return list(
        session.execute(
            select(LoteCa.ca_codigo).where(LoteCa.lote_id == lote_id).order_by(LoteCa.id)
        ).scalars()
    )


def lote_activo_de(session: Session, empresa_id: int, codigo: str) -> Lote | None:
    """El lote NO cerrado de la empresa que tiene la CA (a lo más uno)."""
    return session.execute(
        select(Lote)
        .join(LoteCa, LoteCa.lote_id == Lote.id)
        .where(Lote.empresa_id == empresa_id, Lote.estado.in_(ACTIVOS), LoteCa.ca_codigo == codigo)
        .limit(1)
    ).scalar_one_or_none()


def lotes_de_codigos(session: Session, empresa_id: int, codigos: Sequence[str]) -> dict[str, Lote]:
    """`{codigo: lote no cerrado}` para una página de la mesa (una consulta)."""
    if not codigos:
        return {}
    return {
        codigo: lt
        for codigo, lt in session.execute(
            select(LoteCa.ca_codigo, Lote)
            .join(Lote, Lote.id == LoteCa.lote_id)
            .where(
                Lote.empresa_id == empresa_id,
                Lote.estado.in_(ACTIVOS),
                LoteCa.ca_codigo.in_(list(codigos)),
            )
        ).all()
    }


def lotes_abiertos(session: Session, empresa_id: int) -> list[Lote]:
    """Los lotes donde aún se pueden agregar CA (para «agregar al lote actual» de la mesa)."""
    return list(
        session.execute(
            select(Lote)
            .where(Lote.empresa_id == empresa_id, Lote.estado.in_(EDITABLES))
            .order_by(Lote.actualizado_en.desc(), Lote.id.desc())
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Crear, agregar y quitar CA
# ---------------------------------------------------------------------------


def _limpiar_codigos(codigos: Sequence[str]) -> list[str]:
    vistos: list[str] = []
    for c in codigos:
        t = (c or "").strip()[:50]
        if t and t not in vistos:
            vistos.append(t)
    if not vistos:
        raise ErrorLote("Elige al menos una Compra Ágil")
    if len(vistos) > MAX_CAS_LOTE:
        raise ErrorLote(f"Un lote admite hasta {MAX_CAS_LOTE} Compras Ágiles")
    return vistos


def _validar_vigentes(
    session: Session, empresa: Empresa, codigos: list[str], ahora: datetime
) -> None:
    """Solo CA vigentes de las regiones de la empresa (otra región = como inexistente)."""
    regiones = [int(r) for r in (empresa.regiones_prioritarias or [])]
    ok = set(
        session.execute(
            select(CompraAgil.codigo).where(
                CompraAgil.codigo.in_(codigos), *condiciones_vigente(regiones, ahora)
            )
        ).scalars()
    )
    malas = [c for c in codigos if c not in ok]
    if malas:
        raise ErrorLote(
            "No están vigentes en tus regiones: "
            + ", ".join(malas[:5])
            + (f" y {len(malas) - 5} más" if len(malas) > 5 else "")
        )


def _agregar(session: Session, lote: Lote, usuario: Usuario, codigos: list[str]) -> int:
    """Agrega las CA (ya validadas) bajo el bloqueo de la empresa. Pasa cada una a `cotizando`
    con su evento (`datos.lote_id`) y guarda el estado previo para devolverla al quitarla."""
    ya = set(codigos_de(session, lote.id))
    n = 0
    for codigo in codigos:
        if codigo in ya:
            continue
        otro = lote_activo_de(session, lote.empresa_id, codigo)
        if otro is not None and otro.id != lote.id:
            raise CaEnOtroLote(codigo, otro.id, otro.nombre)
        t = eq.trabajo_de(session, lote.empresa_id, codigo)
        previo = t.estado if t is not None else eq.NUEVA
        session.add(LoteCa(lote_id=lote.id, ca_codigo=codigo, agregada_por_id=usuario.id,
                           agregada_en=ahora_utc(), estado_previo=previo))  # fmt: skip
        if previo in (eq.NUEVA, eq.PRIORIZADA, eq.DESCARTADA):
            eq.cambiar_estado(
                session, lote.empresa_id, usuario, codigo, eq.COTIZANDO,
                t.version if t is not None else 0, datos_extra={"lote_id": lote.id},
            )  # fmt: skip
        n += 1
    session.flush()
    return n


def nombre_por_defecto(ahora: datetime | None = None) -> str:
    d = (ahora or ahora_utc()).replace(tzinfo=UTC).astimezone(TZ_CHILE)
    return f"Lote del {d:%d-%m-%Y}"


def crear(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    codigos: Sequence[str],
    *,
    nombre: str = "",
    ahora: datetime | None = None,
) -> Lote:
    """Crea el lote con esas CA (vigentes, de las regiones de la empresa, en ningún otro lote
    abierto: si no, `CaEnOtroLote`) y lo recalcula."""
    momento = ahora or ahora_utc()
    lista = _limpiar_codigos(codigos)
    empresa = _bloquear_empresa(session, empresa_id)
    _validar_vigentes(session, empresa, lista, momento)
    lote = Lote(
        empresa_id=empresa_id,
        nombre=(nombre or "").strip()[:NOMBRE_MAX] or nombre_por_defecto(momento),
        estado=ABIERTO, creado_por_id=usuario.id, creado_en=momento, actualizado_en=momento,
        version=1,
    )  # fmt: skip
    session.add(lote)
    session.flush()
    _agregar(session, lote, usuario, lista)
    recalcular(session, lote)
    return lote


def agregar_cas(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    lote_id: int,
    codigos: Sequence[str],
    version: int | None = None,
    *,
    ahora: datetime | None = None,
) -> Lote:
    lista = _limpiar_codigos(codigos)
    empresa = _bloquear_empresa(session, empresa_id)  # siempre empresa → lote (sin deadlock)
    lote = _bloquear(session, empresa_id, lote_id, version)
    if lote.estado not in EDITABLES:
        raise ErrorLote("Este lote ya no admite Compras Ágiles (está ofertado o cerrado)")
    if len(set(codigos_de(session, lote.id)) | set(lista)) > MAX_CAS_LOTE:
        raise ErrorLote(f"Un lote admite hasta {MAX_CAS_LOTE} Compras Ágiles")
    _validar_vigentes(session, empresa, lista, ahora or ahora_utc())
    if _agregar(session, lote, usuario, lista):
        _tocar(lote)
        recalcular(session, lote)
    return lote


def quitar_ca(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    lote_id: int,
    codigo: str,
    version: int | None,
) -> Lote:
    """Quita la CA del lote; si sigue en `cotizando`, vuelve al estado previo (`nueva` o
    `priorizada`) o a `priorizada`."""
    _bloquear_empresa(session, empresa_id)
    lote = _bloquear(session, empresa_id, lote_id, version)
    if lote.estado not in EDITABLES:
        raise ErrorLote("No se quitan CA de un lote ofertado o cerrado")
    lc = session.execute(
        select(LoteCa).where(LoteCa.lote_id == lote.id, LoteCa.ca_codigo == codigo)
    ).scalar_one_or_none()
    if lc is None:
        raise LoteAjeno(codigo)
    previo = lc.estado_previo
    session.delete(lc)
    t = eq.trabajo_de(session, empresa_id, codigo)
    if t is not None and t.estado == eq.COTIZANDO and previo != eq.COTIZANDO:
        destino = previo if previo in (eq.NUEVA, eq.PRIORIZADA) else eq.PRIORIZADA
        eq.cambiar_estado(session, empresa_id, usuario, codigo, destino, t.version,
                          datos_extra={"lote_id": lote.id})  # fmt: skip
    session.flush()
    _tocar(lote)
    recalcular(session, lote)
    return lote


# ---------------------------------------------------------------------------
# Agregado
# ---------------------------------------------------------------------------


def _formatos_de_compra(
    session: Session, empresa_id: int, producto_ids: set[int]
) -> dict[int, ProductoFormato]:
    """Por producto: el formato más usado entre los productos de proveedores (activos) de la
    empresa; si ninguno lo tiene, el formato base (factor 1) o el de menor factor."""
    if not producto_ids:
        return {}
    usos: dict[int, dict[int, int]] = {}
    for pid, fid, n in session.execute(
        select(ProveedorProducto.producto_id, ProveedorProducto.formato_id, func.count())
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(
            Proveedor.empresa_id == empresa_id,
            Proveedor.activo.is_(True),
            ProveedorProducto.producto_id.in_(producto_ids),
            ProveedorProducto.formato_id.is_not(None),
        )
        .group_by(ProveedorProducto.producto_id, ProveedorProducto.formato_id)
    ):
        usos.setdefault(int(pid), {})[int(fid)] = int(n)
    formatos: dict[int, list[ProductoFormato]] = {}
    for f in session.execute(
        select(ProductoFormato)
        .where(ProductoFormato.producto_id.in_(producto_ids))
        .order_by(ProductoFormato.id)
    ).scalars():
        formatos.setdefault(f.producto_id, []).append(f)
    out: dict[int, ProductoFormato] = {}
    for pid, fs in formatos.items():
        por_id = {f.id: f for f in fs}
        conteo = {fid: n for fid, n in usos.get(pid, {}).items() if fid in por_id}
        if conteo:
            fid = sorted(conteo, key=lambda k: (-conteo[k], k))[0]
            out[pid] = por_id[fid]
            continue
        base = [f for f in fs if abs(f.factor_base - 1.0) < _TOL]
        out[pid] = base[0] if base else min(fs, key=lambda f: (f.factor_base, f.id))
    return out


def a_formato(cantidad_base: float, factor: float) -> float:
    """Unidad base → formato de compra, hacia arriba (no se compra media caja)."""
    if cantidad_base <= _TOL or factor <= 0:
        return 0.0
    return float(math.ceil(cantidad_base / factor - 1e-9))


def _elegir_costo(
    mc: MejorCosto | None, ll: LoteLinea
) -> tuple[int | None, int | None, float | None, bool, int | None]:
    """(proveedor_id, proveedor_producto_id, costo/base, vencido, días) según lo fijado o el
    mejor costo vigente."""
    if ll.proveedor_fijado:
        if ll.proveedor_producto_id is not None and mc is not None:
            for c in mc.candidatos:
                if c.producto_id == ll.proveedor_producto_id:
                    return (
                        c.proveedor_id,
                        c.producto_id,
                        c.precio_por_unidad_base,
                        c.vencido,
                        c.dias,
                    )
        # Fijado a un proveedor sin precio comparable (o ya sin ese producto): se le pide.
        return ll.proveedor_sugerido_id, None, None, False, None
    if mc is None:
        return None, None, None, False, None
    g = mc.ganador
    return g.proveedor_id, g.producto_id, g.precio_por_unidad_base, g.vencido, g.dias


def recalcular(session: Session, lote: Lote, *, hoy: date | None = None) -> None:
    """Reescribe el agregado (upsert por producto × formato de compra), sincroniza las
    solicitudes en borrador y avisa si quedan precios vencidos. Un lote ofertado o cerrado no se
    recalcula (su reserva y su `fecha_limite` quedan fijas)."""
    if lote.estado not in EDITABLES:
        return
    codigos = codigos_de(session, lote.id)
    lote.fecha_limite = (
        session.execute(
            select(func.min(CompraAgil.fecha_cierre)).where(CompraAgil.codigo.in_(codigos))
        ).scalar_one_or_none()
        if codigos
        else None
    )
    lineas = (
        list(
            session.execute(
                select(CaLinea)
                .where(
                    CaLinea.ca_codigo.in_(codigos),
                    CaLinea.estado.in_(("ok", "dudosa")),
                    CaLinea.producto_id.is_not(None),
                )
                .order_by(CaLinea.ca_codigo, CaLinea.n)
            ).scalars()
        )
        if codigos
        else []
    )
    por_producto: dict[int, list[CaLinea]] = {}
    for ln in lineas:
        if ln.cantidad_base and ln.cantidad_base > 0 and ln.producto_id is not None:
            por_producto.setdefault(ln.producto_id, []).append(ln)
    pids = set(por_producto)
    productos = (
        {
            p.id: p
            for p in session.execute(
                select(ProductoCanonico).where(ProductoCanonico.id.in_(pids))
            ).scalars()
        }
        if pids
        else {}
    )
    formatos = _formatos_de_compra(session, lote.empresa_id, pids)
    disponible = op.disponible_base(session, lote.empresa_id, pids)
    existentes = {
        (ll.producto_id, ll.formato_id): ll
        for ll in session.execute(select(LoteLinea).where(LoteLinea.lote_id == lote.id)).scalars()
    }
    vistos: set[tuple[int, int]] = set()
    for pid, lns in sorted(por_producto.items()):
        prod, fmt = productos.get(pid), formatos.get(pid)
        if prod is None or fmt is None:
            continue
        cantidad = round(sum(ln.cantidad_base or 0.0 for ln in lns), 6)
        en_stock = round(min(disponible.get(pid, 0.0), cantidad), 6)
        a_comprar = round(max(0.0, cantidad - en_stock), 6)
        clave = (pid, fmt.id)
        vistos.add(clave)
        ll = existentes.get(clave)
        if ll is None:
            ll = LoteLinea(lote_id=lote.id, producto_id=pid, formato_id=fmt.id,
                           proveedor_fijado=False)  # fmt: skip
            session.add(ll)
        ll.cantidad_base = cantidad
        ll.cantidad_formato = a_formato(cantidad, fmt.factor_base)
        ll.en_stock_base = en_stock
        ll.a_comprar_base = a_comprar
        ll.detalle = [
            {"ca": ln.ca_codigo, "n": ln.n, "linea_id": ln.id, "cantidad_base": ln.cantidad_base}
            for ln in lns
        ]
        mc = mejor_costo(
            session, lote.empresa_id, producto_id=pid, unidad_base=prod.unidad_base,
            cantidad_base=a_comprar or cantidad, hoy=hoy,
        )  # fmt: skip
        prov_id, pp_id, costo, vencido, dias = _elegir_costo(mc, ll)
        ll.proveedor_sugerido_id = prov_id
        ll.proveedor_producto_id = pp_id
        ll.costo_unitario_sugerido = round(costo, 6) if costo is not None else None
        ll.precio_vencido = bool(vencido)
        ll.dias_precio = dias
    for clave, ll in existentes.items():
        if clave not in vistos:
            session.execute(delete(SolicitudLinea).where(SolicitudLinea.lote_linea_id == ll.id))
            session.delete(ll)
    session.flush()
    _sincronizar_solicitudes(session, lote)
    _avisar_vencidos(session, lote)


def recalcular_por_cas(session: Session, codigos: Sequence[str]) -> int:
    """Recalcula los lotes editables (de cualquier empresa) que tienen alguna de estas CA. Lo
    llama `catalogo.recalcular_detalle_lineas` cuando cambian líneas. Devuelve cuántos."""
    if not codigos:
        return 0
    lotes = list(
        session.execute(
            select(Lote)
            .join(LoteCa, LoteCa.lote_id == Lote.id)
            .where(LoteCa.ca_codigo.in_(list(codigos)), Lote.estado.in_(EDITABLES))
            .distinct()
        ).scalars()
    )
    for lote in lotes:
        recalcular(session, lote)
    return len(lotes)


def recalcular_lote(session: Session, empresa_id: int, lote_id: int) -> Lote:
    """«Recalcular» a pedido (p. ej. después de corregir líneas de una CA en Demanda MP)."""
    lote = _bloquear(session, empresa_id, lote_id, None)
    if lote.estado not in EDITABLES:
        raise ErrorLote("Un lote ofertado o cerrado no se recalcula")
    recalcular(session, lote)
    return lote


def lineas_de(session: Session, lote_id: int) -> list[LoteLinea]:
    return list(
        session.execute(
            select(LoteLinea).where(LoteLinea.lote_id == lote_id).order_by(LoteLinea.id)
        ).scalars()
    )


def _avisar_vencidos(session: Session, lote: Lote) -> None:
    """Evento de sistema `precio_vencido` UNA vez por lote (campana y «Hoy»), si alguna línea a
    comprar solo tiene precio vencido. Idempotente por el índice único parcial."""
    n = sum(1 for ll in lineas_de(session, lote.id) if ll.precio_vencido and ll.a_comprar_base > 0)
    if n == 0:
        return
    codigo = f"{eq.PREFIJO_LOTE}{lote.id}"
    ya = session.execute(
        select(CaEvento.id).where(
            CaEvento.empresa_id == lote.empresa_id, CaEvento.ca_codigo == codigo,
            CaEvento.tipo == eq.EV_PRECIO_VENCIDO, CaEvento.usuario_id.is_(None),
        )
    ).first()  # fmt: skip
    if ya is not None:
        return
    session.add(
        CaEvento(
            empresa_id=lote.empresa_id, ca_codigo=codigo, usuario_id=None,
            tipo=eq.EV_PRECIO_VENCIDO,
            datos={"lote_id": lote.id, "nombre": lote.nombre, "n": n}, creado_en=ahora_utc(),
        )
    )  # fmt: skip
    session.flush()


def cambiar_proveedor(
    session: Session, empresa_id: int, lote_id: int, linea_id: int, valor: str
) -> Lote:
    """`valor`: '' = automático (mejor costo vigente); `pp:<id>` = un precio de un proveedor de
    la empresa para ESE producto; `prov:<id>` = un proveedor de la empresa sin precio (se le
    pedirá). Otro id o de otra empresa → `LoteAjeno` (404)."""
    lote = _bloquear(session, empresa_id, lote_id, None)
    if lote.estado not in EDITABLES:
        raise ErrorLote("El lote ya está ofertado o cerrado")
    ll = session.execute(
        select(LoteLinea).where(LoteLinea.id == linea_id, LoteLinea.lote_id == lote.id)
    ).scalar_one_or_none()
    if ll is None:
        raise LoteAjeno(linea_id)
    v = (valor or "").strip()
    if not v:
        ll.proveedor_fijado = False
    elif v.startswith(PREFIJO_PP):
        pp_id = _entero(v[len(PREFIJO_PP) :])
        try:
            pp = of.validar_proveedor_producto(session, empresa_id, pp_id) if pp_id else None
        except of.ProveedorAjeno:
            pp = None
        if pp is None or pp.producto_id != ll.producto_id:
            raise LoteAjeno(v)
        ll.proveedor_fijado, ll.proveedor_producto_id = True, pp.id
        ll.proveedor_sugerido_id = pp.proveedor_id
    elif v.startswith(PREFIJO_PROV):
        prov_id = _entero(v[len(PREFIJO_PROV) :])
        prov = session.execute(
            select(Proveedor).where(
                Proveedor.id == (prov_id or 0), Proveedor.empresa_id == empresa_id
            )  # fmt: skip
        ).scalar_one_or_none()
        if prov is None:
            raise LoteAjeno(v)
        ll.proveedor_fijado, ll.proveedor_producto_id = True, None
        ll.proveedor_sugerido_id = prov.id
    else:
        raise ErrorLote("Proveedor inválido")
    session.flush()
    _tocar(lote)
    recalcular(session, lote)
    return lote


def _entero(texto: str) -> int | None:
    try:
        return int(texto)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Solicitudes de precio
# ---------------------------------------------------------------------------

_ENTORNO_TEXTO = Environment(
    autoescape=False, keep_trailing_newline=True
)  # texto plano  # noqa: S701
_PLANTILLA_MENSAJE = _ENTORNO_TEXTO.from_string(
    "Hola{% if contacto %} {{ contacto }}{% endif %}:\n"
    "\n"
    "Le escribimos de {{ empresa }}. Necesitamos cotizar los siguientes productos:\n"
    "\n"
    "{% for l in lineas %}- {{ l.producto }} · {{ l.formato }} · {{ l.cantidad }}\n{% endfor %}"
    "\n"
    "¿Nos puede enviar{% if fecha %}, antes del {{ fecha }},{% endif %} el precio neto (sin IVA) "
    "por formato, la disponibilidad y el plazo de entrega de cada uno? Si tiene precio por "
    "volumen, indíquenos desde qué cantidad aplica.\n"
    "\n"
    "Gracias.\n"
)


def _fecha_respuesta(lote: Lote) -> str | None:
    """`fecha_limite − 1 día`, en hora de Chile (dd-mm-aaaa)."""
    if lote.fecha_limite is None:
        return None
    d = lote.fecha_limite.replace(tzinfo=UTC).astimezone(TZ_CHILE) - timedelta(days=1)
    return f"{d:%d-%m-%Y}"


def _formato_cantidad(v: float) -> str:
    return f"{int(v)}" if abs(v - round(v)) < _TOL else f"{v:.3f}".rstrip("0").rstrip(",.")


@dataclass(frozen=True)
class FilaSolicitud:
    solicitud_linea: SolicitudLinea
    lote_linea: LoteLinea
    producto: str
    formato: str
    unidad_base: str

    @property
    def tramos_txt(self) -> str:
        """«10:950; 50:900» (el mismo formato que se tipea y que trae la planilla)."""
        return "; ".join(
            f"{float(t['desde']):g}:{float(t['precio']):g}"
            for t in (self.solicitud_linea.tramos or [])
            if isinstance(t, dict) and "desde" in t and "precio" in t
        )


def filas_de(session: Session, solicitud: SolicitudPrecio) -> list[FilaSolicitud]:
    out: list[FilaSolicitud] = []
    for sl, ll, pnom, ub, fnom in session.execute(
        select(
            SolicitudLinea,
            LoteLinea,
            ProductoCanonico.nombre,
            ProductoCanonico.unidad_base,
            ProductoFormato.nombre,
        )  # fmt: skip
        .join(LoteLinea, LoteLinea.id == SolicitudLinea.lote_linea_id)
        .join(ProductoCanonico, ProductoCanonico.id == LoteLinea.producto_id)
        .join(ProductoFormato, ProductoFormato.id == LoteLinea.formato_id)
        .where(SolicitudLinea.solicitud_id == solicitud.id)
        .order_by(ProductoCanonico.nombre, SolicitudLinea.id)
    ).all():
        out.append(FilaSolicitud(sl, ll, pnom, fnom, ub))
    return out


def generar_texto(session: Session, solicitud: SolicitudPrecio, lote: Lote) -> str:
    """El mensaje listo para pegar: SIN código de CA ni comprador (§3)."""
    empresa = session.get(Empresa, lote.empresa_id)
    prov = session.get(Proveedor, solicitud.proveedor_id)
    lineas = [
        {
            "producto": f.producto,
            "formato": f.formato,
            "cantidad": _formato_cantidad(f.solicitud_linea.cantidad_formato),
        }  # fmt: skip
        for f in filas_de(session, solicitud)
    ]
    return _PLANTILLA_MENSAJE.render(
        contacto=(prov.contacto_nombre or "").strip() if prov else "",
        empresa=empresa.nombre if empresa else "nuestra empresa",
        lineas=lineas,
        fecha=_fecha_respuesta(lote),
    )


def _sincronizar_solicitudes(session: Session, lote: Lote) -> None:
    """Una solicitud por proveedor con líneas a comprar. Solo se tocan las `borrador` (lo ya
    enviado queda como se envió); un borrador sin líneas se borra."""
    asignadas: dict[int, list[LoteLinea]] = {}
    for ll in lineas_de(session, lote.id):
        if ll.a_comprar_base > _TOL and ll.proveedor_sugerido_id is not None:
            asignadas.setdefault(ll.proveedor_sugerido_id, []).append(ll)
    factores = (
        {
            f.id: f.factor_base
            for f in session.execute(
                select(ProductoFormato).where(
                    ProductoFormato.id.in_(
                        {ll.formato_id for lls in asignadas.values() for ll in lls}
                    )
                )
            ).scalars()
        }
        if asignadas
        else {}
    )
    existentes = {
        s.proveedor_id: s
        for s in session.execute(
            select(SolicitudPrecio).where(SolicitudPrecio.lote_id == lote.id)
        ).scalars()
    }
    for prov_id, vieja in existentes.items():
        if vieja.estado == BORRADOR and prov_id not in asignadas:
            session.execute(delete(SolicitudLinea).where(SolicitudLinea.solicitud_id == vieja.id))
            session.delete(vieja)
    session.flush()
    for prov_id, lls in asignadas.items():
        s = existentes.get(prov_id)
        if s is not None and s.estado != BORRADOR:
            continue  # lo enviado queda como se envió
        if s is None:
            s = SolicitudPrecio(lote_id=lote.id, proveedor_id=prov_id, estado=BORRADOR, version=1)
            session.add(s)
            session.flush()
        deseadas = {ll.id: a_formato(ll.a_comprar_base, factores.get(ll.formato_id, 1.0))
                    for ll in lls}  # fmt: skip
        actuales = {
            sl.lote_linea_id: sl
            for sl in session.execute(
                select(SolicitudLinea).where(SolicitudLinea.solicitud_id == s.id)
            ).scalars()
        }
        for llid, sobra in actuales.items():
            if llid not in deseadas:
                session.delete(sobra)
        for llid, cant in deseadas.items():
            actual = actuales.get(llid)
            if actual is None:
                session.add(SolicitudLinea(solicitud_id=s.id, lote_linea_id=llid,
                                           cantidad_formato=cant))  # fmt: skip
            else:
                actual.cantidad_formato = cant
        session.flush()
        s.texto_generado = generar_texto(session, s, lote)
    session.flush()


def solicitud_de(
    session: Session, empresa_id: int, solicitud_id: int, *, para_escribir: bool = False
) -> tuple[SolicitudPrecio, Lote]:
    consulta = (
        select(SolicitudPrecio, Lote)
        .join(Lote, Lote.id == SolicitudPrecio.lote_id)
        .where(SolicitudPrecio.id == solicitud_id, Lote.empresa_id == empresa_id)
    )
    if para_escribir:
        consulta = consulta.with_for_update(of=SolicitudPrecio)
    fila = session.execute(consulta).first()
    if fila is None:
        raise LoteAjeno(solicitud_id)
    return fila[0], fila[1]


def _solicitud_version(
    session: Session, empresa_id: int, solicitud_id: int, version: int | None
) -> tuple[SolicitudPrecio, Lote]:
    """Bloquea primero el lote y después la solicitud (mismo orden que las operaciones del lote:
    sin deadlock) y verifica la `version` de la solicitud."""
    _, lote = solicitud_de(session, empresa_id, solicitud_id)
    lote = _bloquear(session, empresa_id, lote.id, None)
    s, _ = solicitud_de(session, empresa_id, solicitud_id, para_escribir=True)
    if version is not None and s.version != version:
        u = session.get(Usuario, s.enviada_por_id) if s.enviada_por_id else None
        raise ConflictoLote(quien=eq.nombre_visible(u) if u else "Alguien del equipo",
                            cuando=s.respondida_en or s.enviada_en)  # fmt: skip
    return s, lote


def regenerar_texto(session: Session, empresa_id: int, solicitud_id: int) -> SolicitudPrecio:
    s, lote = solicitud_de(session, empresa_id, solicitud_id, para_escribir=True)
    s.texto_generado = generar_texto(session, s, lote)
    session.flush()
    return s


def _estado_automatico(session: Session, lote: Lote) -> None:
    """abierto → solicitado con ≥ 1 enviada; → con_precios cuando todas las ENVIADAS respondieron
    o quedaron sin respuesta (un borrador nuevo no lo retrasa: aún no se pidió). No toca un lote
    con_precios, ofertado ni cerrado."""
    if lote.estado not in (ABIERTO, SOLICITADO):
        return
    estados = [
        e
        for e in session.execute(
            select(SolicitudPrecio.estado).where(SolicitudPrecio.lote_id == lote.id)
        ).scalars()
        if e != BORRADOR
    ]
    if not estados:
        return
    nuevo = CON_PRECIOS if all(e in (RESPONDIDA, SIN_RESPUESTA) for e in estados) else SOLICITADO
    if nuevo != lote.estado:
        lote.estado = nuevo
        _tocar(lote)


def marcar_enviada(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    solicitud_id: int,
    canal: str,
    version: int | None,
) -> SolicitudPrecio:
    if canal not in CANALES:
        raise ErrorLote("Elige por dónde la enviaste")
    s, lote = _solicitud_version(session, empresa_id, solicitud_id, version)
    if lote.estado == CERRADO:
        raise ErrorLote("El lote está cerrado")
    s.estado, s.canal = ENVIADA, canal
    s.enviada_en, s.enviada_por_id = ahora_utc(), usuario.id
    s.version += 1
    session.flush()
    _estado_automatico(session, lote)
    session.flush()
    return s


def marcar_sin_respuesta(
    session: Session, empresa_id: int, solicitud_id: int, version: int | None
) -> SolicitudPrecio:
    s, lote = _solicitud_version(session, empresa_id, solicitud_id, version)
    if lote.estado == CERRADO:
        raise ErrorLote("El lote está cerrado")
    s.estado = SIN_RESPUESTA
    s.version += 1
    session.flush()
    _estado_automatico(session, lote)
    session.flush()
    return s


@dataclass(frozen=True)
class Respuesta:
    """Lo que respondió el proveedor por línea (precio neto por formato de compra)."""

    precio: float | None
    tramos: list[dict[str, float]] | None = None
    disponible: bool | None = None
    plazo_dias: int | None = None


def validar_respuesta(
    precio: Any, tramos_txt: str = "", disponible: Any = None, plazo: Any = None
) -> Respuesta:
    """Del formulario o la planilla a una `Respuesta` válida (ErrorLote si no). `tramos_txt`:
    «desde:precio» separados por `;` o salto de línea (p. ej. «10:950; 50:900»)."""
    p = _precio(precio)
    tramos: list[dict[str, float]] | None = None
    t = (tramos_txt or "").replace("\n", ";").strip()
    if t:
        pares: list[dict[str, Any]] = []
        for trozo in [x for x in t.split(";") if x.strip()]:
            desde, _, pr = trozo.partition(":")
            if not pr:
                raise ErrorLote(f"Tramo inválido: {trozo.strip()!r} (usa desde:precio)")
            pares.append({"desde": _precio(desde), "precio": _precio(pr)})
        try:
            tramos = normalizar_tramos(pares)
        except ValueError as exc:
            raise ErrorLote(str(exc)) from None
    disp = disponible if isinstance(disponible, bool) or disponible is None else None
    pl = None
    if plazo not in (None, ""):
        try:
            pl = int(float(str(plazo).replace(",", ".")))
        except ValueError:
            raise ErrorLote(f"Plazo inválido: {plazo!r}") from None
        if not 0 <= pl <= PLAZO_MAX_DIAS:
            raise ErrorLote(f"Plazo: de 0 a {PLAZO_MAX_DIAS} días")
    return Respuesta(p, tramos, disp, pl)


def _precio(valor: Any) -> float | None:
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return None
    leido: float | None
    if isinstance(valor, int | float) and not isinstance(valor, bool):
        leido = float(valor)
    else:
        from app.mercado.importar import convertir_numero

        try:
            # La lectura de las planillas: «1.900» = 1900, «1.234,5», «$ 1.300», «12,5».
            leido = convertir_numero(str(valor))
        except ValueError:
            raise ErrorLote(f"Precio inválido: {valor!r}") from None
    if leido is None:
        return None
    v = leido
    if not math.isfinite(v) or v < 0 or v > PRECIO_MAX:
        raise ErrorLote("El precio debe ser un número positivo")
    return v or None


def _pp_de(
    session: Session, proveedor_id: int, producto_id: int, formato_id: int
) -> ProveedorProducto | None:
    return session.execute(
        select(ProveedorProducto)
        .where(
            ProveedorProducto.proveedor_id == proveedor_id,
            ProveedorProducto.producto_id == producto_id,
            ProveedorProducto.formato_id == formato_id,
        )
        .order_by(ProveedorProducto.id)
        .limit(1)
    ).scalar_one_or_none()


def registrar_respuesta(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    solicitud_id: int,
    respuestas: Mapping[int, Respuesta],
    version: int | None,
    *,
    hoy: date | None = None,
) -> int:
    """Guarda la respuesta (por `solicitud_lineas.id`) y la lleva a `proveedor_productos` con
    `guardar_producto` (fuente `solicitud`, vigente hoy, historial en la misma transacción): un
    precio igual se CONFIRMA (deja de estar vencido). Recalcula el lote. Devuelve cuántas líneas
    traían precio. Una línea de otra solicitud se ignora."""
    s, lote = _solicitud_version(session, empresa_id, solicitud_id, version)
    if lote.estado == CERRADO:
        raise ErrorLote("El lote está cerrado")
    dia = hoy or hoy_chile()
    prov = session.get(Proveedor, s.proveedor_id)
    if prov is None or prov.empresa_id != empresa_id:
        raise LoteAjeno(s.proveedor_id)
    ahora = ahora_utc()
    con_precio = 0
    for fila in filas_de(session, s):
        r = respuestas.get(fila.solicitud_linea.id)
        if r is None:
            continue
        sl, ll = fila.solicitud_linea, fila.lote_linea
        sl.precio_respuesta, sl.tramos = r.precio, r.tramos
        sl.disponible, sl.plazo_dias, sl.respondida_en = r.disponible, r.plazo_dias, ahora
        fmt = session.get(ProductoFormato, ll.formato_id)
        pp = _pp_de(session, prov.id, ll.producto_id, ll.formato_id)
        if r.precio is not None and fmt is not None:
            guardar_producto(
                session, prov,
                producto_texto=pp.producto_texto if pp else fila.producto,
                formato_texto=pp.formato_texto if pp else fila.formato,
                precio_neto=r.precio, vigente_desde=dia, factor_base=fmt.factor_base,
                unidad_base=fila.unidad_base, disponible=r.disponible, fuente="solicitud",
                usuario_id=usuario.id, producto_id=ll.producto_id, formato_id=ll.formato_id,
                tramos=r.tramos or [],  # la respuesta define los tramos: sin tramos, se borran
            )  # fmt: skip
            con_precio += 1
        elif r.disponible is False and pp is not None:
            pp.disponible = False
    s.estado, s.respondida_en = RESPONDIDA, ahora
    s.version += 1
    session.flush()
    recalcular(session, lote, hoy=dia)
    _estado_automatico(session, lote)
    session.flush()
    return con_precio


# --- Planilla para el proveedor (ida y vuelta) -------------------------------------------------

COLUMNAS_XLSX = ["id", "producto", "formato", "cantidad", "precio_neto", "disponible",
                 "plazo_dias", "tramos"]  # fmt: skip


def xlsx_solicitud(session: Session, empresa_id: int, solicitud_id: int) -> bytes:
    """Una hoja `precios` con las líneas y columnas vacías para que el proveedor la devuelva
    llena (sin código de CA ni comprador). `id` identifica la línea al subirla de vuelta."""
    from openpyxl import Workbook

    s, lote = solicitud_de(session, empresa_id, solicitud_id)
    empresa = session.get(Empresa, empresa_id)
    wb = Workbook()
    ws = wb.active
    ws.title = "precios"
    ws.append(["id", "producto", "formato", "cantidad", "precio_neto (sin IVA, por formato)",
               "disponible (si/no)", "plazo_dias", "tramos (desde:precio; …)"])  # fmt: skip
    for f in filas_de(session, s):
        sl = f.solicitud_linea
        ws.append([sl.id, f.producto, f.formato, sl.cantidad_formato, sl.precio_respuesta,
                   None if sl.disponible is None else ("si" if sl.disponible else "no"),
                   sl.plazo_dias,
                   "; ".join(f"{t['desde']:g}:{t['precio']:g}" for t in (sl.tramos or []))])  # fmt: skip
    info = wb.create_sheet("instrucciones")
    info.append([f"Solicitud de precios de {empresa.nombre if empresa else ''}"])
    fecha = _fecha_respuesta(lote)
    info.append([f"Responder antes del {fecha}" if fecha else "Responder lo antes posible"])
    info.append(["Completa precio_neto (sin IVA, por formato), disponible (si/no), plazo_dias y, "
                 "si tienes precio por volumen, tramos «desde:precio» separados por punto y coma."])  # fmt: skip
    info.append(["No cambies la columna id."])
    ws.column_dimensions["B"].width = 40
    ws.column_dimensions["C"].width = 22
    for col in ("E", "F", "G", "H"):
        ws.column_dimensions[col].width = 20
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def leer_xlsx_respuesta(
    session: Session, empresa_id: int, solicitud_id: int, data: bytes
) -> tuple[dict[int, Respuesta], list[str]]:
    """La planilla devuelta → respuestas por `solicitud_lineas.id` + errores por fila (una fila
    mala no aborta las demás). Reutiliza la lectura de `importar` (tamaño, filas, números)."""
    from app.mercado import importar

    s, _ = solicitud_de(session, empresa_id, solicitud_id)
    if len(data) > importar.MAX_BYTES:
        raise ErrorLote(f"El archivo supera {importar.MAX_BYTES // 1024} KB")
    try:
        hojas = importar._filas_xlsx(data)
    except importar.PlanillaInvalida as exc:
        raise ErrorLote(str(exc)) from None
    if "precios" not in hojas:
        raise ErrorLote("Falta la hoja 'precios'")
    validas = {f.solicitud_linea.id for f in filas_de(session, s)}
    out: dict[int, Respuesta] = {}
    errores: list[str] = []
    for n, fila in importar._como_dicts(hojas["precios"]):
        try:
            sl_id = importar._entero(fila.get("id"))
            if sl_id is None or sl_id not in validas:
                errores.append(f"Fila {n}: id {fila.get('id')!r} no es de esta solicitud")
                continue
            r = validar_respuesta(
                importar._num(fila.get("precio_neto")),
                importar._txt(fila.get("tramos")) or "",
                importar._si_no(fila.get("disponible")),
                importar._entero(fila.get("plazo_dias")),
            )
        except (ValueError, ErrorLote) as exc:
            errores.append(f"Fila {n}: {exc}")
            continue
        if r.precio is None and r.disponible is None and r.plazo_dias is None and not r.tramos:
            continue  # fila sin responder
        out[sl_id] = r
    return out, errores


# ---------------------------------------------------------------------------
# Ofertas, estados, reserva y cierre
# ---------------------------------------------------------------------------


@dataclass
class ResultadoArmado:
    armadas: int = 0
    sin_oferta: list[str] = field(default_factory=list)  # CA con líneas sin precio o sin producto


def _costos_del_lote(session: Session, lote: Lote) -> dict[int, of.Entrada]:
    """Por producto, el origen de costo que eligió el lote: stock propio si lo cubre entero, si
    no el precio del proveedor elegido."""
    out: dict[int, of.Entrada] = {}
    for ll in lineas_de(session, lote.id):
        if ll.a_comprar_base <= _TOL and ll.en_stock_base > _TOL:
            out[ll.producto_id] = of.Entrada(None, None, of.ORIGEN_STOCK, None)
        elif ll.proveedor_producto_id is not None:
            out[ll.producto_id] = of.Entrada(None, ll.proveedor_producto_id)
    return out


def armar_ofertas(
    session: Session, empresa_id: int, usuario: Usuario, lote_id: int, version: int | None
) -> ResultadoArmado:
    """Pre-llena y guarda la oferta de cada CA del lote (`oferta.armar` + `oferta.guardar`) con el
    proveedor y costo elegidos en el lote. Precio: el sugerido por F-plat-4 si existe; si no,
    `costo × (1 + empresas.margen_objetivo)`. La persona ajusta cada CA en su ficha."""
    lote = _bloquear(session, empresa_id, lote_id, version)
    if lote.estado not in EDITABLES:
        raise ErrorLote("Las ofertas se arman antes de marcar el lote ofertado")
    empresa = session.get(Empresa, empresa_id)
    margen = float(getattr(empresa, "margen_objetivo", 0.15) or 0.0)
    costos = _costos_del_lote(session, lote)
    res = ResultadoArmado()
    for codigo in codigos_de(session, lote.id):
        ca = session.get(CompraAgil, codigo)
        if ca is None:
            continue
        ev = adj.evaluar_ca(session, codigo, empresa_id)
        guardada = of.oferta_de(session, empresa_id, codigo)
        lineas = of.armar(session, empresa_id, ca, ev, guardada, costos_por_producto=costos)
        for lo in lineas:
            if lo.precio_unitario is None and lo.costo_total is not None and lo.cantidad:
                lo.precio_unitario = round(lo.costo_total * (1 + margen) / lo.cantidad, 2)
        if any(lo.precio_unitario is None for lo in lineas):
            res.sin_oferta.append(codigo)
        try:
            of.guardar(
                session, empresa_id, usuario, ca, lineas,
                plazo=guardada.plazo_entrega_dias if guardada else ca.plazo_entrega_dias,
                notas=(guardada.notas if guardada else "") or "",
                version=guardada.version if guardada else 0,
            )  # fmt: skip
        except (of.ConflictoOferta, eq.ConflictoVersion) as exc:
            # Alguien guardó esa oferta (o movió la CA) a la vez: `oferta.guardar` pudo revertir
            # la sesión, así que no se sigue con media operación. Todo o nada.
            raise ConflictoLote(quien=getattr(exc, "quien", "Alguien del equipo"),
                                cuando=getattr(exc, "cuando", None)) from None  # fmt: skip
        res.armadas += 1
    _tocar(lote)
    session.flush()
    return res


def _pendientes_para_ofertar(session: Session, lote: Lote) -> list[str]:
    codigos = codigos_de(session, lote.id)
    listos = (
        set(
            session.execute(
                select(CaTrabajo.ca_codigo).where(
                    CaTrabajo.empresa_id == lote.empresa_id,
                    CaTrabajo.ca_codigo.in_(codigos),
                    CaTrabajo.estado.in_((eq.LISTA, eq.ENVIADA)),
                )
            ).scalars()
        )
        if codigos
        else set()
    )
    return [c for c in codigos if c not in listos]


def referencia(lote: Lote) -> str:
    return f"{eq.PREFIJO_LOTE}{lote.id}"


def reservar(session: Session, lote: Lote, usuario_id: int | None) -> list[str]:
    """Reserva en el inventario lo que cubre el stock (`en_stock_base` de cada línea). Nunca más
    que lo disponible: si no alcanza, avisa y reserva lo que hay."""
    avisos: list[str] = []
    nombres: dict[int, str] = {
        int(pid): str(nombre)
        for pid, nombre in session.execute(
            select(ProductoCanonico.id, ProductoCanonico.nombre)
            .join(LoteLinea, LoteLinea.producto_id == ProductoCanonico.id)
            .where(LoteLinea.lote_id == lote.id)
        ).all()
    }
    for ll in lineas_de(session, lote.id):
        if ll.en_stock_base <= _TOL:
            continue
        hecho = op.reservar(session, lote.empresa_id, ll.producto_id, ll.en_stock_base,
                            referencia(lote), usuario_id)  # fmt: skip
        if hecho + _TOL < ll.en_stock_base:
            avisos.append(f"{nombres.get(ll.producto_id, 'producto')}: se reservó {hecho:g} de "
                          f"{ll.en_stock_base:g} (el stock cambió)")  # fmt: skip
    return avisos


def cambiar_estado(
    session: Session,
    empresa_id: int,
    usuario: Usuario,
    lote_id: int,
    nuevo: str,
    version: int | None,
) -> list[str]:
    """Transiciones a mano. `ofertado` exige que todas las CA estén `lista|enviada` y reserva el
    stock; `cerrado` (o volver atrás desde `ofertado`) libera. Desde `cerrado` no se sale.
    Devuelve avisos (p. ej. reserva parcial)."""
    if nuevo not in ESTADOS_LOTE:
        raise ErrorLote("Estado desconocido")
    lote = _bloquear(session, empresa_id, lote_id, version)
    if lote.estado == CERRADO:
        raise ErrorLote("Un lote cerrado no se reabre")
    if nuevo == lote.estado:
        return []
    avisos: list[str] = []
    if nuevo == OFERTADO:
        pendientes = _pendientes_para_ofertar(session, lote)
        if pendientes:
            raise ErrorLote(
                f"Faltan {len(pendientes)} CA por dejar «Lista para enviar» o «Enviada»: "
                + ", ".join(pendientes[:5])
            )
        recalcular(session, lote)  # última foto del stock antes de fijarla
        avisos = reservar(session, lote, usuario.id)
    elif lote.estado == OFERTADO or nuevo == CERRADO:
        op.liberar(session, empresa_id, referencia(lote), usuario.id)
    lote.estado = nuevo
    _tocar(lote)
    session.flush()
    if nuevo in EDITABLES:
        recalcular(session, lote)
    return avisos


# ---------------------------------------------------------------------------
# Vistas
# ---------------------------------------------------------------------------


@dataclass
class OpcionProveedor:
    valor: str
    texto: str
    vencido: bool = False


@dataclass
class VistaLinea:
    ll: LoteLinea
    producto: str
    formato: str
    factor: float
    unidad_base: str
    proveedor: str | None
    opciones: list[OpcionProveedor]
    valor_elegido: str

    @property
    def subtotal(self) -> float | None:
        c = self.ll.costo_unitario_sugerido
        return None if c is None else round(c * self.ll.a_comprar_base, 2)

    @property
    def a_comprar_formato(self) -> float:
        return a_formato(self.ll.a_comprar_base, self.factor)


@dataclass
class VistaCa:
    codigo: str
    nombre: str
    comprador: str
    cierre: datetime | None
    detalle: str
    estado: str
    n_lineas: int
    sin_producto: int
    oferta_total: float | None
    resultado: str | None


@dataclass
class VistaSolicitud:
    s: SolicitudPrecio
    proveedor: Proveedor
    filas: list[FilaSolicitud]


@dataclass
class VistaLote:
    lote: Lote
    cas: list[VistaCa]
    lineas: list[VistaLinea]
    solicitudes: list[VistaSolicitud]
    sin_producto_lineas: int
    sin_producto_cas: int
    sin_cantidad: int
    resumen: dict[str, Any]
    otros_lotes: list[Lote] = field(default_factory=list)

    @property
    def vencidas(self) -> int:
        return sum(1 for v in self.lineas if v.ll.precio_vencido and v.ll.a_comprar_base > _TOL)


def _opciones(
    session: Session, empresa_id: int, ll: LoteLinea, unidad_base: str, provs: list[Proveedor]
) -> tuple[list[OpcionProveedor], str]:
    mc = mejor_costo(session, empresa_id, producto_id=ll.producto_id, unidad_base=unidad_base,
                     cantidad_base=ll.a_comprar_base or ll.cantidad_base)  # fmt: skip
    opciones: list[OpcionProveedor] = []
    con_precio: set[int] = set()
    for c in mc.candidatos if mc else []:
        con_precio.add(c.proveedor_id)
        extra = f" · precio de hace {c.dias} días" if c.vencido else ""
        tramo = f" · tramo desde {c.tramo_aplicado:g}" if c.tramo_aplicado else ""
        opciones.append(OpcionProveedor(
            f"{PREFIJO_PP}{c.producto_id}",
            f"{c.proveedor} · {c.formato_texto or c.producto_texto} · "
            f"${c.precio_por_unidad_base:,.0f}/{c.unidad_base}{tramo}{extra}".replace(",", "."),
            c.vencido,
        ))  # fmt: skip
    for p in provs:
        if p.id not in con_precio:
            opciones.append(
                OpcionProveedor(f"{PREFIJO_PROV}{p.id}", f"{p.nombre} · sin precio: pedir")
            )
    if not ll.proveedor_fijado:
        elegido = ""
    elif ll.proveedor_producto_id is not None:
        elegido = f"{PREFIJO_PP}{ll.proveedor_producto_id}"
    else:
        elegido = f"{PREFIJO_PROV}{ll.proveedor_sugerido_id}"
    return opciones, elegido


def vista(session: Session, empresa_id: int, lote_id: int) -> VistaLote:
    lote = obtener(session, empresa_id, lote_id)
    codigos = codigos_de(session, lote.id)
    cas_rows = (
        {
            c.codigo: c
            for c in session.execute(
                select(CompraAgil).where(CompraAgil.codigo.in_(codigos))
            ).scalars()
        }
        if codigos
        else {}
    )
    trabajo = (
        {
            t.ca_codigo: t
            for t in session.execute(
                select(CaTrabajo).where(
                    CaTrabajo.empresa_id == empresa_id, CaTrabajo.ca_codigo.in_(codigos)
                )  # fmt: skip
            ).scalars()
        }
        if codigos
        else {}
    )
    ofertas = (
        {
            o.ca_codigo: o
            for o in session.execute(
                select(OfertaPropia).where(
                    OfertaPropia.empresa_id == empresa_id, OfertaPropia.ca_codigo.in_(codigos)
                )  # fmt: skip
            ).scalars()
        }
        if codigos
        else {}
    )
    conteo: dict[str, tuple[int, int]] = {}
    for codigo, total, sin in (
        session.execute(
            select(
                CaLinea.ca_codigo,
                func.count(),
                func.count().filter(CaLinea.producto_id.is_(None)),
            )
            .where(CaLinea.ca_codigo.in_(codigos))
            .group_by(CaLinea.ca_codigo)
        )
        if codigos
        else []
    ):
        conteo[codigo] = (int(total), int(sin))
    cas: list[VistaCa] = []
    for codigo in codigos:
        c = cas_rows.get(codigo)
        t = trabajo.get(codigo)
        o = ofertas.get(codigo)
        total, sin = conteo.get(codigo, (0, 0))
        cas.append(VistaCa(
            codigo=codigo, nombre=c.nombre if c and c.nombre else codigo,
            comprador=(c.organismo_nombre or "Organismo no informado") if c else "",
            cierre=c.fecha_cierre if c else None,
            detalle=c.detalle_lineas if c else "sin_detalle",
            estado=t.estado if t else eq.NUEVA, n_lineas=total, sin_producto=sin,
            oferta_total=o.total_neto if o else None, resultado=t.resultado if t else None,
        ))  # fmt: skip
    sin_cantidad = int(
        session.execute(
            select(func.count()).select_from(CaLinea).where(
                CaLinea.ca_codigo.in_(codigos), CaLinea.estado.in_(("ok", "dudosa")),
                CaLinea.producto_id.is_not(None),
                (CaLinea.cantidad_base.is_(None)) | (CaLinea.cantidad_base <= 0),
            )
        ).scalar_one()
    ) if codigos else 0  # fmt: skip
    provs = list(
        session.execute(
            select(Proveedor)
            .where(Proveedor.empresa_id == empresa_id, Proveedor.activo.is_(True))
            .order_by(Proveedor.nombre)
        ).scalars()
    )
    nombres_prov = {p.id: p.nombre for p in provs}
    lineas: list[VistaLinea] = []
    for ll, pnom, ub, fnom, factor in session.execute(
        select(
            LoteLinea,
            ProductoCanonico.nombre,
            ProductoCanonico.unidad_base,
            ProductoFormato.nombre,
            ProductoFormato.factor_base,
        )  # fmt: skip
        .join(ProductoCanonico, ProductoCanonico.id == LoteLinea.producto_id)
        .join(ProductoFormato, ProductoFormato.id == LoteLinea.formato_id)
        .where(LoteLinea.lote_id == lote.id)
        .order_by(ProductoCanonico.nombre)
    ).all():
        opciones, elegido = _opciones(session, empresa_id, ll, ub, provs)
        prov_nombre = nombres_prov.get(ll.proveedor_sugerido_id or 0)
        if prov_nombre is None and ll.proveedor_sugerido_id is not None:
            p = session.get(Proveedor, ll.proveedor_sugerido_id)
            prov_nombre = p.nombre if p else None
        lineas.append(VistaLinea(ll, pnom, fnom, float(factor), ub, prov_nombre, opciones, elegido))
    solicitudes: list[VistaSolicitud] = []
    for s, p in session.execute(
        select(SolicitudPrecio, Proveedor)
        .join(Proveedor, Proveedor.id == SolicitudPrecio.proveedor_id)
        .where(SolicitudPrecio.lote_id == lote.id)
        .order_by(Proveedor.nombre)
    ).all():
        solicitudes.append(VistaSolicitud(s, p, filas_de(session, s)))
    sin_lineas = sum(c.sin_producto for c in cas)
    sin_cas = sum(1 for c in cas if c.sin_producto)
    return VistaLote(
        lote=lote, cas=cas, lineas=lineas, solicitudes=solicitudes,
        sin_producto_lineas=sin_lineas, sin_producto_cas=sin_cas, sin_cantidad=sin_cantidad,
        resumen=resumen(session, empresa_id, lote, cas, lineas),
    )  # fmt: skip


def resumen(
    session: Session,
    empresa_id: int,
    lote: Lote,
    cas: list[VistaCa],
    lineas: list[VistaLinea],
) -> dict[str, Any]:
    """Total a vender (ofertas guardadas), total a comprar (agregado), margen simple del lote y,
    al cerrar, ganadas y su monto."""
    total_vender = sum(c.oferta_total or 0.0 for c in cas)
    total_comprar = sum(v.subtotal or 0.0 for v in lineas)
    falta_costo = sum(1 for v in lineas if v.ll.a_comprar_base > _TOL and v.subtotal is None)
    ganadas = [c for c in cas if c.resultado == eq.GANADA]
    return {
        "total_vender": total_vender,
        "total_comprar": total_comprar,
        "margen_simple": (total_vender - total_comprar)
        if total_vender and not falta_costo
        else None,
        "falta_costo": falta_costo,
        "sin_oferta": sum(1 for c in cas if c.oferta_total is None),
        "ganadas": len(ganadas),
        "monto_ganado": sum(c.oferta_total or 0.0 for c in ganadas),
        "con_resultado": sum(1 for c in cas if c.resultado),
    }


# ---------------------------------------------------------------------------
# «En trabajo» y «Hoy»
# ---------------------------------------------------------------------------


@dataclass
class EnTrabajo:
    lotes: list[tuple[Lote, list[tuple[str, str, str]]]]  # (lote, [(codigo, nombre, estado)])
    sin_lote: list[dict[str, Any]]
    cierran_24h: list[dict[str, Any]]


def en_trabajo(session: Session, empresa_id: int, ahora: datetime) -> EnTrabajo:
    lotes = list(
        session.execute(
            select(Lote)
            .where(Lote.empresa_id == empresa_id, Lote.estado.in_(ACTIVOS))
            .order_by(Lote.fecha_limite.is_(None), Lote.fecha_limite, Lote.id)
        ).scalars()
    )
    por_lote: dict[int, list[tuple[str, str, str]]] = {lt.id: [] for lt in lotes}
    en_lote: set[str] = set()
    if lotes:
        for lid, codigo, nombre, estado in session.execute(
            select(LoteCa.lote_id, LoteCa.ca_codigo, CompraAgil.nombre, CaTrabajo.estado)
            .join(CompraAgil, CompraAgil.codigo == LoteCa.ca_codigo)
            .outerjoin(
                CaTrabajo,
                (CaTrabajo.ca_codigo == LoteCa.ca_codigo) & (CaTrabajo.empresa_id == empresa_id),
            )  # fmt: skip
            .where(LoteCa.lote_id.in_(list(por_lote)))
            .order_by(LoteCa.id)
        ).all():
            por_lote[lid].append((codigo, nombre or codigo, estado or eq.NUEVA))
            en_lote.add(codigo)
    sin_lote: list[dict[str, Any]] = []
    cierran: list[dict[str, Any]] = []
    for t, ca in session.execute(
        select(CaTrabajo, CompraAgil)
        .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
        .where(CaTrabajo.empresa_id == empresa_id, CaTrabajo.estado.in_(eq.ACTIVOS))
        .order_by(CompraAgil.fecha_cierre, CompraAgil.codigo)
    ).all():
        fila = {"codigo": ca.codigo, "nombre": ca.nombre or ca.codigo, "estado": t.estado,
                "cierre": ca.fecha_cierre, "en_lote": ca.codigo in en_lote}  # fmt: skip
        if ca.fecha_cierre is not None and ahora < ca.fecha_cierre <= ahora + timedelta(hours=24):
            cierran.append(fila)
        if ca.codigo not in en_lote and (ca.fecha_cierre is None or ca.fecha_cierre > ahora):
            sin_lote.append(fila)
    return EnTrabajo([(lt, por_lote[lt.id]) for lt in lotes], sin_lote, cierran)


def resumen_hoy(session: Session, empresa_id: int) -> dict[str, Any]:
    """Tarjeta de «Hoy»: lotes no cerrados, sus CA y el cierre más próximo."""
    n_lotes, cierre = session.execute(
        select(func.count(), func.min(Lote.fecha_limite)).where(
            Lote.empresa_id == empresa_id, Lote.estado.in_(ACTIVOS)
        )
    ).one()
    n_ca = session.execute(
        select(func.count())
        .select_from(LoteCa)
        .join(Lote, Lote.id == LoteCa.lote_id)
        .where(Lote.empresa_id == empresa_id, Lote.estado.in_(ACTIVOS))
    ).scalar_one()
    return {"lotes": int(n_lotes or 0), "cas": int(n_ca or 0), "cierre": cierre}


def purgar_cerrados(session: Session, ahora: datetime, dias: int = RETENCION_DIAS) -> int:
    """Retención (F-plat-7 §1.4): borra los lotes `cerrado` sin cambios hace más de `dias` con sus
    hijas (explícito: SQLite no aplica el CASCADE sin PRAGMA). Devuelve cuántos lotes borró."""
    corte = ahora - timedelta(days=dias)
    ids = list(
        session.execute(
            select(Lote.id).where(Lote.estado == CERRADO, Lote.actualizado_en < corte)
        ).scalars()
    )
    if not ids:
        return 0
    sols = select(SolicitudPrecio.id).where(SolicitudPrecio.lote_id.in_(ids))
    session.execute(delete(SolicitudLinea).where(SolicitudLinea.solicitud_id.in_(sols)))
    session.execute(delete(SolicitudPrecio).where(SolicitudPrecio.lote_id.in_(ids)))
    session.execute(delete(LoteLinea).where(LoteLinea.lote_id.in_(ids)))
    session.execute(delete(LoteCa).where(LoteCa.lote_id.in_(ids)))
    session.execute(delete(Lote).where(Lote.id.in_(ids)))
    return len(ids)
