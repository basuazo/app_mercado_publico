"""Panel lateral de una Compra Ágil en la Mesa (F-dis-datos): la CA sin salir de la lista.

Solo arma datos que ya existen; no evalúa en el GET:
- Cifras: la caché de evaluación (`mesa_dia.evaluaciones_de`, la misma que lee la Mesa). Si la
  empresa guardó su oferta (`ofertas_propias`), manda esa. Sin caché quedan en None con el motivo.
- Líneas: `oferta.armar` (el criterio de costo de la ficha y de `/mesa/ca/{c}/precio`, con el
  proveedor que eligió el lote si la CA está en uno, como `lote.armar_ofertas`). Costos y precios
  en NETO; cada línea trae también su BRUTO (`impuestos.bruto`) para el explicativo.
- Trabajo, lote, venta, requisitos y señales (`hoy.senales_por_ca`).

Ownership (reglas 17 y 26): todo por `empresa_id`; el territorio lo valida la ruta (404).
Sin HTML ni `Request`. Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.mercado import equipo as eq
from app.mercado import impuestos as imp
from app.mercado import lote as lt
from app.mercado import oferta as of
from app.mercado import requisitos as rq
from app.mercado import ventas as vt
from app.mercado.anexos_pendientes import motivos_de
from app.mercado.hoy import FUENTE, Senal, senales_por_ca
from app.mercado.mesa_dia import (
    cierre_chile,
    comuna_en_direccion,
    cuenta_regresiva,
    evaluaciones_de,
)
from app.models.tables import CompraAgil, Empresa, Usuario

SIN_EVALUACION = "sin evaluación"
FALTAN_LINEAS_ANEXO = "faltan las líneas del anexo"
SIN_COSTO = "sin_costo"
VENCIDO = "vencido"


@dataclass(frozen=True)
class CifrasPanel:
    oferta: float | None  # la oferta propia guardada o, si no hay, la sugerida de la caché
    oferta_propia: bool
    pct_disponible: float | None  # fracción del monto disponible
    margen_pct: float | None
    costos_con: int | None  # líneas `ok` con costo…
    costos_de: int | None  # …de cuántas
    p_ganar: float | None
    p_nota: str | None
    motivo: str | None  # por qué falta algo («sin evaluación», «faltan las líneas del anexo»)
    evaluada_en: datetime | None


@dataclass(frozen=True)
class LineaPanel:
    n: int
    texto: str
    estado: str
    producto: str | None
    cantidad: float | None
    unidad: str | None  # formato pedido o unidad base del producto
    proveedor: str | None
    costo_unitario: float | None  # neto, por unidad pedida
    costo_unitario_bruto: float | None
    precio_unitario: float | None  # neto: el de la oferta propia (la caché no guarda por línea)
    precio_unitario_bruto: float | None
    marca: str | None  # sin_costo | vencido | None
    dias_vencido: int | None
    impuesto: str  # etiqueta del impuesto («impuesto por clasificar» incluida) o ""


@dataclass(frozen=True)
class TrabajoPanel:
    estado: str
    etiqueta: str
    responsable: str | None
    version: int  # para mutaciones futuras con control de concurrencia (0 = sin fila)
    lote_id: int | None
    lote_nombre: str | None
    venta_id: int | None
    venta_estado: str | None
    venta_etiqueta: str | None
    requisitos_pendientes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PanelCA:
    codigo: str
    nombre: str
    organismo: str | None
    unidad: str | None
    comuna: str | None  # detectada en la dirección de entrega [I]
    cierre: str
    cuenta_regresiva: str
    monto: float | None
    cifras: CifrasPanel
    lineas: list[LineaPanel]
    espera_anexo: str | None  # motivo si la CA espera su anexo
    trabajo: TrabajoPanel
    senales: list[Senal]
    url_ficha: str
    url_publica: str | None
    url_comprador: str
    url_competidores: str
    url_precio: str
    fuente: str = FUENTE


def _bruto(neto: float | None, impuesto: imp.Resolucion) -> float | None:
    return None if neto is None else round(imp.bruto(neto, impuesto), 2)


def _linea(lo: of.LineaOferta) -> LineaPanel:
    impuesto: imp.Resolucion = imp.POR_CLASIFICAR if lo.por_clasificar else lo.impuesto
    costo = lo.costo_total / lo.cantidad if lo.costo_total is not None and lo.cantidad else None
    marca: str | None = None
    if lo.estado == "ok" and lo.costo_total is None and lo.origen != of.ORIGEN_STOCK:
        marca = SIN_COSTO
    elif lo.vencido_dias is not None:
        marca = VENCIDO
    return LineaPanel(
        n=lo.n,
        texto=lo.texto,
        estado=lo.estado,
        producto=lo.producto,
        cantidad=lo.cantidad,
        unidad=lo.formato or lo.unidad_base,
        proveedor=lo.proveedor,
        costo_unitario=round(costo, 2) if costo is not None else None,
        costo_unitario_bruto=_bruto(costo, impuesto),
        precio_unitario=lo.precio_unitario,
        precio_unitario_bruto=_bruto(lo.precio_unitario, impuesto),
        marca=marca,
        dias_vencido=lo.vencido_dias,
        impuesto=imp.etiqueta(impuesto),
    )


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) else None


def panel(session: Session, empresa_id: int, codigo: str, ahora: datetime) -> PanelCA | None:
    """Los datos del panel lateral, o None si la CA no existe."""
    ca = session.execute(
        select(CompraAgil)
        .options(defer(CompraAgil.raw_json), defer(CompraAgil.descripcion))  # regla 12
        .where(CompraAgil.codigo == codigo)
    ).scalar_one_or_none()
    if ca is None:
        return None
    empresa = session.get(Empresa, empresa_id)
    comunas = list(empresa.comunas_prioritarias or []) if empresa else []

    # Trabajo del equipo, lote y venta.
    t = eq.trabajo_de(session, empresa_id, codigo)
    estado = t.estado if t is not None else eq.NUEVA
    responsable = None
    if t is not None and t.responsable_id is not None:
        responsable = eq.nombre_visible(session.get(Usuario, t.responsable_id))
    lote = lt.lote_activo_de(session, empresa_id, codigo)
    venta = vt.venta_de_ca(session, empresa_id, codigo)
    trabajo = TrabajoPanel(
        estado=estado,
        etiqueta=eq.ETIQUETA_ESTADO.get(estado, estado),
        responsable=responsable,
        version=t.version if t is not None else 0,
        lote_id=lote.id if lote else None,
        lote_nombre=lote.nombre if lote else None,
        venta_id=venta.id if venta else None,
        venta_estado=venta.estado if venta else None,
        venta_etiqueta=eq.ETIQUETA_VENTA.get(venta.estado) if venta else None,
        requisitos_pendientes=rq.pendientes(session, empresa_id, codigo),
    )

    # Líneas con el costo de la ficha (el del lote si está en uno).
    guardada = of.oferta_de(session, empresa_id, codigo)
    costos_lote = lt._costos_del_lote(session, lote) if lote is not None else None
    lineas = [
        _linea(lo)
        for lo in of.armar(session, empresa_id, ca, None, guardada, costos_por_producto=costos_lote)
    ]
    espera_anexo = motivos_de(session, [codigo], ahora).get(codigo)

    # Cifras: la caché manda; la oferta propia guardada, sobre la sugerida.
    cache = evaluaciones_de(session, empresa_id, [codigo]).get(codigo)
    d = cache.datos if cache is not None else {}
    monto = ca.monto_disponible_clp
    costo_total = _num(d.get("costo_total"))
    oferta = float(guardada.total_neto) if guardada is not None else _num(d.get("precio_sugerido"))
    margen: float | None
    if guardada is not None:
        margen = (oferta - costo_total) / oferta if oferta and costo_total is not None else None
    else:
        margen = _num(d.get("margen_pct"))
    p_ganar = _num((d.get("p_ganar") or {}).get("sugerido")) if cache is not None else None
    p_nota: str | None = None
    if cache is not None:
        if p_ganar is None:
            p_nota = "sin modelo de adjudicación activo"
        elif guardada is not None:
            p_nota = "calculada para el precio sugerido, no para tu oferta"
        else:
            p_nota = "para el precio sugerido" + (
                f" · modelo {d['modelo']}" if d.get("modelo") else ""
            )
    motivo: str | None = None
    if cache is None:
        motivo = FALTAN_LINEAS_ANEXO if not lineas and espera_anexo else SIN_EVALUACION
    n_ok = d.get("n_ok")
    n_sin = d.get("n_sin_costo")
    cifras = CifrasPanel(
        oferta=oferta,
        oferta_propia=guardada is not None,
        pct_disponible=oferta / monto if oferta is not None and monto else None,
        margen_pct=margen,
        costos_con=int(n_ok) - int(n_sin or 0) if isinstance(n_ok, int) else None,
        costos_de=int(n_ok) if isinstance(n_ok, int) else None,
        p_ganar=p_ganar,
        p_nota=p_nota,
        motivo=motivo,
        evaluada_en=cache.evaluada_en if cache is not None else None,
    )

    from app.api.query import _url_ficha, mostrar_ficha_oficial  # lazy: solo arma enlaces

    c = quote(codigo, safe="")
    return PanelCA(
        codigo=codigo,
        nombre=ca.nombre or codigo,
        organismo=ca.organismo_nombre,
        unidad=ca.unidad_compra,
        comuna=comuna_en_direccion(ca.direccion_entrega, comunas),
        cierre=cierre_chile(ca.fecha_cierre),
        cuenta_regresiva=cuenta_regresiva(ca.fecha_cierre, ahora),
        monto=monto,
        cifras=cifras,
        lineas=lineas,
        espera_anexo=espera_anexo,
        trabajo=trabajo,
        senales=senales_por_ca(session, empresa_id, [codigo], ahora).get(codigo, []),
        url_ficha=f"/oportunidad/compras_agiles/{c}",
        url_publica=_url_ficha("compras_agiles", codigo)
        if mostrar_ficha_oficial(ca.estado)
        else None,
        url_comprador=f"/mesa/ca/{c}/comprador",
        url_competidores=f"/mesa/ca/{c}/competidores",
        url_precio=f"/mesa/ca/{c}/precio",
    )
