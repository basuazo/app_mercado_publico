"""Datos de «Hoy» (F-dis-datos): «Te necesita», cifras del período, embudo, señales por CA y
«Desde tu última visita».

Datos puros: sin HTML ni `Request`. Todo lo comercial se filtra por `empresa_id` (reglas 17 y 26);
lo común a todas las empresas (líneas por revisar) se acota a las `regiones` que pasa quien llama.

Convención de fechas (regla 6, verificada en `app.core.tiempo`, `parse_fecha_v2` y
`mesa_dia.condiciones_vigente`): `fecha_publicacion`, `fecha_cierre`, `creado_en` y demás columnas
`DateTime` se guardan **naive en UTC** (la API de Compra Ágil habla en hora de Chile y la ingesta lo
convierte con `a_utc_naive`). Los períodos son DÍAS CALENDARIO de Chile (`America/Santiago`):
hoy = el día, semana = lunes a domingo, mes = mes calendario. Cada borde se convierte a UTC naive
con `borde_del_dia_utc_naive` (00:00 de Chile de ese día, con el horario de verano que corresponda)
y se compara en el intervalo semiabierto `[inicio, fin)`.

«Te necesita» se calcula EN VIVO desde el estado: no lee `ca_eventos` (los eventos de sistema son
un registro de notificaciones, únicos por empresa × CA × tipo, que no se resuelven solos).
Consultas en lote, nunca una por CA (regla 12). Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.presentacion import formato_clp, formato_pct
from app.core.tiempo import borde_del_dia_utc_naive
from app.mercado import anexos_pendientes as pend
from app.mercado import equipo as eq
from app.mercado import lote as lt
from app.mercado import requisitos as rq
from app.mercado import ventas as vt
from app.mercado.catalogo import POR_REVISAR, REEMPLAZADA, _ca_vigente, conteo_por_revisar
from app.mercado.impuestos import ids_por_clasificar
from app.mercado.mesa_dia import condiciones_vigente, fin_de_dia, hoy_en_chile
from app.mercado.preferencias import guardar_vista, vista_de
from app.mercado.proveedores import dias_de_precio, dias_vigencia, mejores_costos
from app.models.tables import (
    CaEvento,
    CaLinea,
    CaRequisito,
    CaTrabajo,
    CompraAgil,
    CostoHistorial,
    Empresa,
    EmpresaProducto,
    Lote,
    LoteCa,
    LoteLinea,
    OfertaPropia,
    ProductoCanonico,
    Proveedor,
    ProveedorProducto,
    Usuario,
    Venta,
)

FUENTE = "Fuente: Dirección ChileCompra"

URGENTE = "urgente"
ATENCION = "atencion"

# --- «Te necesita» ----------------------------------------------------------------------------

CIERRES = "cierres"
PRECIOS = "precios"
LINEAS = "lineas"
ANEXOS = "anexos"
VENTAS = "ventas"
TIPOS = (CIERRES, PRECIOS, LINEAS, ANEXOS, VENTAS)
ETIQUETA_TIPO = {
    CIERRES: "Cierres",
    PRECIOS: "Precios",
    LINEAS: "Líneas",
    ANEXOS: "Anexos",
    VENTAS: "Ventas",
}
_ORDEN_TIPO = {t: i for i, t in enumerate(TIPOS)}
HORAS_CIERRE = 24  # el mismo corte que `equipo.cierran_sin_enviar`
LIMITE = 30
MUESTRA_NOMBRES = 3  # nombres que se muestran en una fila agrupada


@dataclass(frozen=True)
class Pendiente:
    """Una fila de «Te necesita»."""

    tipo: str  # cierres | precios | lineas | anexos | ventas
    nivel: str  # urgente | atencion
    senal: str  # «Cierra hoy 15:00», «Precio desactualizado · 23 días»…
    titulo: str
    detalle: str
    accion: str  # verbo: «Terminar oferta», «Pedir precio»…
    url: str
    ca_codigo: str | None = None
    vence: datetime | None = None  # UTC naive; para ordenar


def _s(n: int, singular: str, plural: str | None = None) -> str:
    """«1 línea» / «2 líneas»."""
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _url_ca(codigo: str) -> str:
    return eq.url_de_evento(codigo)


def _primeros(nombres: list[str]) -> str:
    vistos = [n for n in nombres if n][:MUESTRA_NOMBRES]
    resto = len([n for n in nombres if n]) - len(vistos)
    return " · ".join(vistos) + (f" y {resto} más" if resto > 0 else "")


def _cierres(session: Session, empresa_id: int, ahora: datetime) -> list[Pendiente]:
    """CA en curso que cierran en menos de 24 h (criterio de `equipo.cierran_sin_enviar`), con
    qué les falta: lote y productos sin precio, requisitos obligatorios u oferta sin armar."""
    filas = session.execute(
        select(
            CompraAgil.codigo, CompraAgil.nombre, CompraAgil.organismo_nombre,
            CompraAgil.fecha_cierre, CaTrabajo.estado,
        )
        .join(CaTrabajo, CaTrabajo.ca_codigo == CompraAgil.codigo)
        .where(
            CaTrabajo.empresa_id == empresa_id,
            CaTrabajo.estado.in_(eq.ACTIVOS),
            CompraAgil.fecha_cierre > ahora,
            CompraAgil.fecha_cierre <= ahora + timedelta(hours=HORAS_CIERRE),
        )
        .order_by(CompraAgil.fecha_cierre, CompraAgil.codigo)
    ).all()  # fmt: skip
    if not filas:
        return []
    codigos = [f.codigo for f in filas]
    lotes = lt.lotes_de_codigos(session, empresa_id, codigos)
    sin_precio: dict[int, int] = {}
    if lotes:
        sin_precio = {
            int(lid): int(n)
            for lid, n in session.execute(
                select(LoteLinea.lote_id, func.count())
                .where(
                    LoteLinea.lote_id.in_({lo.id for lo in lotes.values()}),
                    LoteLinea.costo_unitario_sugerido.is_(None),
                    LoteLinea.a_comprar_base > 0,
                )
                .group_by(LoteLinea.lote_id)
            )
        }
    requisitos = rq.resumen_por_ca(session, empresa_id, codigos)
    con_oferta = set(
        session.execute(
            select(OfertaPropia.ca_codigo).where(
                OfertaPropia.empresa_id == empresa_id, OfertaPropia.ca_codigo.in_(codigos)
            )
        ).scalars()
    )
    hoy = hoy_en_chile(ahora)
    out: list[Pendiente] = []
    for f in filas:
        lote = lotes.get(f.codigo)
        cumplidos, total = requisitos.get(f.codigo, (0, 0))
        faltan_req = total - cumplidos
        partes: list[str] = []
        if lote is not None:
            k = sin_precio.get(lote.id, 0)
            partes.append(
                f"lote {lote.nombre}" + (f", falta precio de {_s(k, 'producto')}" if k else "")
            )
        if faltan_req:
            partes.append(
                _s(
                    faltan_req,
                    "requisito obligatorio pendiente",
                    "requisitos obligatorios pendientes",
                )
            )
        if f.codigo not in con_oferta:
            partes.append("oferta sin armar")
        if not partes:
            partes.append(eq.ETIQUETA_ESTADO.get(f.estado, f.estado))
        if lote is not None:
            accion, url = "Terminar oferta", f"/lotes/{lote.id}"
        elif f.codigo not in con_oferta:
            accion, url = "Armar oferta", _url_ca(f.codigo)
        elif f.estado == eq.LISTA and not faltan_req:
            accion, url = "Enviar oferta", _url_ca(f.codigo)
        else:
            accion, url = "Terminar oferta", _url_ca(f.codigo)
        senal = eq.cuando_cierra(f.fecha_cierre, ahora)
        out.append(
            Pendiente(
                tipo=CIERRES,
                nivel=URGENTE if hoy_en_chile(f.fecha_cierre) == hoy else ATENCION,
                senal=senal[:1].upper() + senal[1:],
                titulo=f.nombre or f.codigo,
                detalle=" · ".join(
                    p for p in (f.organismo_nombre, f.codigo, ", ".join(partes)) if p
                ),
                accion=accion,
                url=url,
                ca_codigo=f.codigo,
                vence=f.fecha_cierre,
            )
        )
    return out


def _precios_vencidos(session: Session, empresa_id: int, ahora: datetime) -> list[Pendiente]:
    """(a) Costos desactualizados (más días que la vigencia de la empresa) SOLO de productos que
    pide alguna CA de un lote activo o una CA en trabajo vigente: una fila por producto × proveedor
    con el último costo. Los que no pide ninguna CA no entran (quedan en /proveedores)."""
    lote_de: dict[str, tuple[int, str]] = {
        c: (int(lid), nombre)
        for c, lid, nombre in session.execute(
            select(LoteCa.ca_codigo, Lote.id, Lote.nombre)
            .join(Lote, Lote.id == LoteCa.lote_id)
            .where(Lote.empresa_id == empresa_id, Lote.estado.in_(lt.ACTIVOS))
        )
    }
    en_trabajo = set(
        session.execute(
            select(CaTrabajo.ca_codigo)
            .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
            .where(
                CaTrabajo.empresa_id == empresa_id,
                CaTrabajo.estado.in_(eq.ACTIVOS),
                CompraAgil.fecha_cierre > ahora,
            )
        ).scalars()
    )
    codigos = set(lote_de) | en_trabajo
    if not codigos:
        return []
    piden: dict[int, set[str]] = defaultdict(set)
    cierre_de: dict[str, datetime | None] = {}
    for codigo, pid, cierre in session.execute(
        select(CaLinea.ca_codigo, CaLinea.producto_id, CompraAgil.fecha_cierre)
        .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
        .where(
            CaLinea.ca_codigo.in_(codigos),
            CaLinea.producto_id.is_not(None),
            CaLinea.estado.in_(("ok", "dudosa")),
        )
    ):
        piden[int(pid)].add(codigo)
        cierre_de[codigo] = cierre
    if not piden:
        return []
    dias_vig = dias_vigencia(session, empresa_id)
    dia = hoy_en_chile(ahora)
    # El último costo de cada producto × proveedor: si es reciente, ese par no está desactualizado
    # aunque otro formato del mismo proveedor lo esté.
    ultimo: dict[tuple[int, int], tuple[ProveedorProducto, Proveedor, str | None]] = {}
    for pp, prov, nombre in session.execute(
        select(ProveedorProducto, Proveedor, ProductoCanonico.nombre)
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .outerjoin(ProductoCanonico, ProductoCanonico.id == ProveedorProducto.producto_id)
        .where(
            Proveedor.empresa_id == empresa_id,
            Proveedor.activo.is_(True),
            ProveedorProducto.disponible.is_(True),
            ProveedorProducto.producto_id.in_(set(piden)),
        )
    ).all():
        clave = (int(pp.producto_id or 0), prov.id)
        previo = ultimo.get(clave)
        if previo is None or (pp.vigente_desde, pp.id) > (previo[0].vigente_desde, previo[0].id):
            ultimo[clave] = (pp, prov, nombre)
    out: list[Pendiente] = []
    for (pid, _prov_id), (pp, prov, nombre) in sorted(ultimo.items()):
        dias = dias_de_precio(pp.vigente_desde, dia)
        if dias <= dias_vig:
            continue
        cas = piden[pid]
        lotes = {lote_de[c] for c in cas if c in lote_de}
        frase = f"lo {'pide' if len(cas) == 1 else 'piden'} {len(cas)} CA"
        if len(lotes) == 1 and all(c in lote_de for c in cas):
            lid, lnombre = next(iter(lotes))
            frase += f" del lote {lnombre}"
            url = f"/lotes/{lid}"
        else:
            if lotes:
                frase += " (lotes " + ", ".join(sorted(n for _i, n in lotes)) + ")"
            url = f"/proveedores/{prov.id}"
        cierres = [x for x in (cierre_de.get(c) for c in cas) if x is not None]
        out.append(
            Pendiente(
                tipo=PRECIOS,
                nivel=ATENCION,
                senal=f"Precio desactualizado · {dias} días",
                titulo=nombre or pp.producto_texto,
                detalle=(
                    f"{prov.nombre} · último costo neto {formato_clp(pp.precio_neto_actual)} · "
                    f"{frase} · vigencia de la empresa: {dias_vig} días"
                ),
                accion="Pedir precio",
                url=url,
                vence=min(cierres) if cierres else None,
            )
        )
    return out


def _falta_costo(session: Session, empresa_id: int) -> list[Pendiente]:
    """(b) Líneas a comprar sin costo en los lotes que todavía se recalculan (`lote.EDITABLES`):
    una fila por lote. Un lote ofertado ya no se recalcula (su costo quedó fijo al ofertar)."""
    por_lote: dict[int, list[str]] = defaultdict(list)
    lotes: dict[int, tuple[str, datetime | None]] = {}
    for lid, lnombre, limite, pnombre in session.execute(
        select(Lote.id, Lote.nombre, Lote.fecha_limite, ProductoCanonico.nombre)
        .join(LoteLinea, LoteLinea.lote_id == Lote.id)
        .join(ProductoCanonico, ProductoCanonico.id == LoteLinea.producto_id)
        .where(
            Lote.empresa_id == empresa_id,
            Lote.estado.in_(lt.EDITABLES),
            LoteLinea.costo_unitario_sugerido.is_(None),
            LoteLinea.a_comprar_base > 0,
        )
        .order_by(Lote.id, LoteLinea.id)
    ):
        por_lote[int(lid)].append(pnombre)
        lotes[int(lid)] = (lnombre, limite)
    return [
        Pendiente(
            tipo=PRECIOS,
            nivel=ATENCION,
            senal=f"Falta costo · {_s(len(nombres), 'línea')}",
            titulo=_primeros(nombres),
            detalle=f"Lote {lotes[lid][0]} · sin proveedor con precio en el catálogo",
            accion="Agregar costo",
            url=f"/lotes/{lid}",
            vence=lotes[lid][1],
        )
        for lid, nombres in por_lote.items()
    ]


def _lineas(session: Session, regiones: list[int], ahora: datetime) -> list[Pendiente]:
    """Una fila agrupada con las líneas por revisar de CA vigentes (`conteo_por_revisar`)."""
    n = conteo_por_revisar(session, regiones, ahora)["vigentes"]
    if not n:
        return []
    n_anexos = int(
        session.execute(
            select(func.count(func.distinct(CaLinea.anexo_id)))
            .join(CompraAgil, CompraAgil.codigo == CaLinea.ca_codigo)
            .where(
                CompraAgil.region.in_(regiones),
                CaLinea.estado.in_(POR_REVISAR),
                CaLinea.anexo_id.is_not(None),
                _ca_vigente(ahora),
            )
        ).scalar_one()
    )
    return [
        Pendiente(
            tipo=LINEAS,
            nivel=ATENCION,
            senal=f"{_s(n, 'línea')} por revisar",
            titulo=f"Líneas leídas de {_s(n_anexos, 'anexo')}"
            if n_anexos
            else "Líneas de CA vigentes",
            detalle="Confirma producto, cantidad y unidad antes de cotizar",
            accion="Revisar líneas",
            url="/catalogo/revision",
        )
    ]


def _anexos(session: Session, empresa_id: int, ahora: datetime) -> list[Pendiente]:
    """Una fila agrupada con las CA vigentes que esperan su anexo (el criterio de la lista)."""
    filas = pend.filas_pendientes(session, empresa_id, ahora)
    if not filas:
        return []
    return [
        Pendiente(
            tipo=ANEXOS,
            nivel=ATENCION,
            senal=f"{_s(len(filas), 'anexo')} sin leer",
            titulo="CA vigentes que esperan su anexo",
            detalle=_primeros([str(f["nombre"]) for f in filas]),
            accion="Ver anexos",
            url="/catalogo/anexos/pendientes",
            vence=filas[0]["fecha_cierre"],
        )
    ]


def _ventas(session: Session, empresa_id: int, ahora: datetime) -> list[Pendiente]:
    """Las condiciones de `ventas.condiciones_seguimiento` (una sola verdad con las alarmas).

    `req_pendientes` no se repite aquí: toda CA que la cumple (cotizando o lista, cierre < 24 h)
    ya aparece en «cierres» con sus requisitos pendientes en el detalle."""
    conds = [c for c in vt.condiciones_seguimiento(session, empresa_id, ahora)
             if c[0] != eq.EV_REQ_PENDIENTES]  # fmt: skip
    if not conds:
        return []
    ids = {int(d["venta_id"]) for _t, _c, d in conds}
    ventas = {
        v.id: (v, nombre, organismo)
        for v, nombre, organismo in session.execute(
            select(Venta, CompraAgil.nombre, CompraAgil.organismo_nombre)
            .join(CompraAgil, CompraAgil.codigo == Venta.ca_codigo)
            .where(Venta.empresa_id == empresa_id, Venta.id.in_(ids))
        ).all()
    }
    hoy = vt.hoy_de(ahora)
    out: list[Pendiente] = []
    for tipo, codigo, datos in conds:
        fila = ventas.get(int(datos["venta_id"]))
        if fila is None:
            continue
        v, nombre, organismo = fila
        nivel, vence = ATENCION, None
        if tipo == eq.EV_OC_SIN_REGISTRAR:
            senal, accion = f"OC sin registrar · {(ahora - v.creado_en).days} días", "Registrar OC"
        elif tipo == eq.EV_ENTREGA_VENCE:
            f = v.entrega_fecha_comprometida
            assert f is not None
            vence = borde_del_dia_utc_naive(f, fin_de_dia=True)
            accion = "Planificar entrega"
            if f < hoy:
                senal, nivel = f"Entrega atrasada · {_s((hoy - f).days, 'día')}", URGENTE
            elif f == hoy:
                senal, nivel = "Entrega hoy", URGENTE
            elif f == hoy + timedelta(days=1):
                senal = "Entrega mañana"
            else:
                senal = f"Entrega el {f:%d/%m}"
        elif tipo == eq.EV_FACTURA_PENDIENTE:
            entregada = v.entrega_fecha_real or vt.hoy_de(v.actualizado_en)
            senal = f"Sin factura · {_s((hoy - entregada).days, 'día')} desde la entrega"
            accion = "Registrar factura"
        else:  # pago_atrasado
            p = v.pago_esperado_en
            assert p is not None
            vence = borde_del_dia_utc_naive(p, fin_de_dia=True)
            senal, accion = f"Pago atrasado · {_s((hoy - p).days, 'día')}", "Registrar pago"
        out.append(
            Pendiente(
                tipo=VENTAS,
                nivel=nivel,
                senal=senal,
                titulo=nombre or codigo,
                detalle=" · ".join(
                    p for p in (organismo, eq.ETIQUETA_VENTA.get(v.estado, v.estado)) if p
                ),
                accion=accion,
                url=f"/ventas/{v.id}",
                ca_codigo=codigo,
                vence=vence,
            )
        )
    return out


def ordenar(pendientes: list[Pendiente]) -> list[Pendiente]:
    """`urgente` primero, después por `vence` ascendente (los None al final), después por tipo."""
    return sorted(
        pendientes,
        key=lambda p: (
            p.nivel != URGENTE,
            p.vence is None,
            p.vence or datetime.max,
            _ORDEN_TIPO.get(p.tipo, len(TIPOS)),
        ),
    )


def te_necesita(
    session: Session,
    empresa_id: int,
    usuario_id: int,  # noqa: ARG001 - reservado para «solo lo mío» (F-dis-hoy); hoy no filtra
    regiones: list[int],
    ahora: datetime,
    limite: int = LIMITE,
) -> list[Pendiente]:
    """Lo que la empresa tiene que hacer ahora, ordenado por urgencia (máx. `limite`)."""
    todos = (
        _cierres(session, empresa_id, ahora)
        + _precios_vencidos(session, empresa_id, ahora)
        + _falta_costo(session, empresa_id)
        + _lineas(session, regiones, ahora)
        + _anexos(session, empresa_id, ahora)
        + _ventas(session, empresa_id, ahora)
    )
    return ordenar(todos)[:limite]


def conteo_por_tipo(pendientes: list[Pendiente]) -> dict[str, int]:
    """`{tipo: n}` con todos los tipos (0 incluido), para los chips de filtro."""
    out = dict.fromkeys(TIPOS, 0)
    for p in pendientes:
        out[p.tipo] = out.get(p.tipo, 0) + 1
    return out


# --- Cifras del período -----------------------------------------------------------------------

PERIODOS = ("hoy", "semana", "mes")
ETIQUETA_PERIODO = {"hoy": "Hoy", "semana": "Semana", "mes": "Mes"}
TASA_MIN_RESULTADOS = 5
_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
    "octubre", "noviembre", "diciembre",
)  # fmt: skip


@dataclass(frozen=True)
class Cifra:
    clave: str  # en_juego | nuevas | cierran | enviadas
    etiqueta: str
    valor: float  # número crudo; el formato va en la vista
    unidad: str  # clp | n | pct
    nota: str | None = None
    delta: float | None = None  # fracción contra el período anterior


def _dias_del_periodo(periodo: str, d: date) -> tuple[date, date]:
    """Días de Chile `[inicio, fin)` del período que contiene `d`."""
    if periodo == "semana":
        lunes = d - timedelta(days=d.weekday())
        return lunes, lunes + timedelta(days=7)
    if periodo == "mes":
        inicio = d.replace(day=1)
        fin = (
            date(inicio.year + 1, 1, 1)
            if inicio.month == 12
            else inicio.replace(month=inicio.month + 1)
        )
        return inicio, fin
    return d, d + timedelta(days=1)


def rango(periodo: str, ahora: datetime, *, anterior: bool = False) -> tuple[datetime, datetime]:
    """`[inicio, fin)` en UTC naive del período de Chile que contiene `ahora` (o del anterior
    equivalente: ayer, la semana pasada, el mes pasado)."""
    if periodo not in PERIODOS:
        raise ValueError(f"Período inválido: {periodo!r}")
    ini, fin = _dias_del_periodo(periodo, hoy_en_chile(ahora))
    if anterior:
        ini, fin = _dias_del_periodo(periodo, ini - timedelta(days=1))
    return (
        borde_del_dia_utc_naive(ini, fin_de_dia=False),
        borde_del_dia_utc_naive(fin, fin_de_dia=False),
    )


def _delta(actual: int, previo: int) -> float | None:
    return None if previo == 0 else (actual - previo) / previo


def _enviadas_en(session: Session, empresa_id: int, inicio: datetime, fin: datetime) -> set[str]:
    """CA distintas con un evento `estado` → `enviada` en `[inicio, fin)` (el JSON se lee en
    Python: son pocas filas y así vale igual en SQLite y Postgres)."""
    return {
        codigo
        for codigo, datos in session.execute(
            select(CaEvento.ca_codigo, CaEvento.datos).where(
                CaEvento.empresa_id == empresa_id,
                CaEvento.tipo == eq.EV_ESTADO,
                CaEvento.creado_en >= inicio,
                CaEvento.creado_en < fin,
            )
        )
        if isinstance(datos, dict) and datos.get("a") == eq.ENVIADA
    }


def _resultados(session: Session, empresa_id: int, codigos: set[str]) -> dict[str, int]:
    out = {eq.GANADA: 0, eq.PERDIDA: 0}
    if not codigos:
        return out
    for res in session.execute(
        select(CaTrabajo.resultado).where(
            CaTrabajo.empresa_id == empresa_id, CaTrabajo.ca_codigo.in_(codigos)
        )
    ).scalars():
        if res in out:
            out[res] += 1
    return out


def cifras(
    session: Session, empresa_id: int, regiones: list[int], periodo: str, ahora: datetime
) -> list[Cifra]:
    """Las 4 cifras del período (`hoy|semana|mes`, en hora de Chile)."""
    inicio, fin = rango(periodo, ahora)
    ini_prev, fin_prev = rango(periodo, ahora, anterior=True)
    out: list[Cifra] = []

    # 1) En juego (hoy/semana) o adjudicado en el mes.
    if periodo == "mes":
        total = 0.0
        n_oc = 0
        con_margen: list[tuple[float, float]] = []
        for v, oferta_total, margen in session.execute(
            select(Venta, OfertaPropia.total_neto, OfertaPropia.margen_realizado)
            .outerjoin(OfertaPropia, OfertaPropia.id == Venta.oferta_id)
            .where(
                Venta.empresa_id == empresa_id,
                Venta.estado != vt.ANULADA,
                Venta.creado_en >= inicio,
                Venta.creado_en < fin,
            )
        ).all():
            monto = vt.monto_de(v, oferta_total) or 0.0
            total += monto
            n_oc += 1
            if margen is not None and monto:
                con_margen.append((float(margen), monto))
        nota = _s(n_oc, "OC", "OC")
        if con_margen:
            pct = sum(m for m, _ in con_margen) / sum(x for _, x in con_margen)
            nota += f" · margen {formato_pct(pct, 1)}"
        mes = hoy_en_chile(ahora).month
        out.append(
            Cifra("en_juego", f"Adjudicado en {_MESES[mes - 1]}", round(total, 2), "clp", nota)
        )
    else:
        cond = [
            CaTrabajo.empresa_id == empresa_id,
            CaTrabajo.estado.in_((eq.PRIORIZADA, eq.COTIZANDO, eq.LISTA)),
            *condiciones_vigente(regiones, ahora),
        ]
        if periodo == "semana":
            cond.append(CompraAgil.fecha_cierre < fin)
        n, suma = session.execute(
            select(func.count(), func.sum(CompraAgil.monto_disponible_clp))
            .select_from(CompraAgil)
            .join(CaTrabajo, CaTrabajo.ca_codigo == CompraAgil.codigo)
            .where(*cond)
        ).one()
        etiqueta = "En juego en la mesa priorizada" if periodo == "hoy" else "En juego esta semana"
        out.append(
            Cifra(
                "en_juego",
                etiqueta,
                float(suma or 0.0),
                "clp",
                f"{_s(int(n or 0), 'CA', 'CA')} priorizadas",
            )
        )

    # 2) CA nuevas del territorio publicadas en el período, contra el anterior equivalente.
    def _nuevas(a: datetime, b: datetime) -> int:
        if not regiones:
            return 0
        return int(
            session.execute(
                select(func.count())
                .select_from(CompraAgil)
                .where(
                    CompraAgil.region.in_(regiones),
                    CompraAgil.fecha_publicacion >= a,
                    CompraAgil.fecha_publicacion < b,
                )
            ).scalar_one()
        )

    nuevas, previas = _nuevas(inicio, fin), _nuevas(ini_prev, fin_prev)
    if periodo == "mes":
        mes_prev = hoy_en_chile(ini_prev + timedelta(days=1)).month
        contra = f"vs. {_MESES[mes_prev - 1]}"
    else:
        contra = "vs. ayer" if periodo == "hoy" else "vs. semana pasada"
    out.append(Cifra("nuevas", "CA nuevas", nuevas, "n", contra, _delta(nuevas, previas)))

    # 3) CA en curso que cierran en lo que queda del período.
    n_cierran, n_no_lista = session.execute(
        select(
            func.count(),
            func.count().filter(CaTrabajo.estado != eq.LISTA),
        )
        .select_from(CaTrabajo)
        .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
        .where(
            CaTrabajo.empresa_id == empresa_id,
            CaTrabajo.estado.in_(eq.ACTIVOS),
            CompraAgil.fecha_cierre > ahora,
            CompraAgil.fecha_cierre < fin,
        )
    ).one()
    etiqueta_cierran = {
        "hoy": "Cierran hoy", "semana": "Cierran esta semana", "mes": "Cierran este mes",
    }  # fmt: skip
    out.append(
        Cifra(
            "cierran",
            etiqueta_cierran[periodo],
            int(n_cierran or 0),
            "n",
            f"{int(n_no_lista or 0)} sin oferta lista",
        )  # fmt: skip
    )

    # 4) Ofertas enviadas (CA distintas) y su resultado.
    enviadas = _enviadas_en(session, empresa_id, inicio, fin)
    res = _resultados(session, empresa_id, enviadas)
    nota_env = f"{_s(res[eq.GANADA], 'adjudicada')}"
    if periodo == "mes":
        con_resultado = res[eq.GANADA] + res[eq.PERDIDA]
        if con_resultado >= TASA_MIN_RESULTADOS:
            nota_env = f"tasa de adjudicación {formato_pct(res[eq.GANADA] / con_resultado)}"
    out.append(Cifra("enviadas", "Ofertas enviadas", len(enviadas), "n", nota_env))
    return out


# --- Embudo -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Etapa:
    clave: str  # priorizada | cotizando | lista | enviada
    etiqueta: str  # `equipo.ETIQUETA_ESTADO`
    n: int
    monto: float
    nota: str


def embudo(session: Session, empresa_id: int, ahora: datetime) -> list[Etapa]:
    """Priorizadas · Cotizando · Listas para enviar (vigentes) · Enviadas esta semana. El monto es
    la suma de `monto_disponible_clp` de las CA de la etapa."""
    por_estado: dict[str, tuple[int, float]] = {}
    for estado, n, suma in session.execute(
        select(CaTrabajo.estado, func.count(), func.sum(CompraAgil.monto_disponible_clp))
        .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
        .where(
            CaTrabajo.empresa_id == empresa_id,
            CaTrabajo.estado.in_(eq.ACTIVOS),
            CompraAgil.fecha_cierre > ahora,
        )
        .group_by(CaTrabajo.estado)
    ):
        por_estado[estado] = (int(n), float(suma or 0.0))
    n_lotes = int(
        session.execute(
            select(func.count())
            .select_from(Lote)
            .where(Lote.empresa_id == empresa_id, Lote.estado.in_(lt.ACTIVOS))
        ).scalar_one()
    )
    por_pedir = int(
        session.execute(
            select(func.count())
            .select_from(LoteLinea)
            .join(Lote, Lote.id == LoteLinea.lote_id)
            .where(
                Lote.empresa_id == empresa_id,
                Lote.estado.in_(lt.EDITABLES),
                LoteLinea.a_comprar_base > 0,
                LoteLinea.costo_unitario_sugerido.is_(None) | LoteLinea.precio_vencido.is_(True),
            )
        ).scalar_one()
    )
    manana = fin_de_dia(hoy_en_chile(ahora) + timedelta(days=1))
    listas_pronto = int(
        session.execute(
            select(func.count())
            .select_from(CaTrabajo)
            .join(CompraAgil, CompraAgil.codigo == CaTrabajo.ca_codigo)
            .where(
                CaTrabajo.empresa_id == empresa_id,
                CaTrabajo.estado == eq.LISTA,
                CompraAgil.fecha_cierre > ahora,
                CompraAgil.fecha_cierre <= manana,
            )
        ).scalar_one()
    )
    inicio, fin = rango("semana", ahora)
    enviadas = _enviadas_en(session, empresa_id, inicio, fin)
    monto_env = 0.0
    if enviadas:
        monto_env = float(
            session.execute(
                select(func.sum(CompraAgil.monto_disponible_clp)).where(
                    CompraAgil.codigo.in_(enviadas)
                )
            ).scalar_one()
            or 0.0
        )
    res = _resultados(session, empresa_id, enviadas)

    def _etapa(estado: str, nota: str) -> Etapa:
        n, monto = por_estado.get(estado, (0, 0.0))
        return Etapa(estado, eq.ETIQUETA_ESTADO[estado], n, monto, nota)

    return [
        _etapa(
            eq.PRIORIZADA, f"{formato_clp(por_estado.get(eq.PRIORIZADA, (0, 0.0))[1])} en juego"
        ),
        _etapa(
            eq.COTIZANDO,
            f"{_s(n_lotes, 'lote activo', 'lotes activos')} · "
            f"{_s(por_pedir, 'precio por pedir', 'precios por pedir')}",
        ),
        _etapa(eq.LISTA, f"{listas_pronto} cierran mañana o antes"),
        Etapa(
            eq.ENVIADA,
            eq.ETIQUETA_ESTADO[eq.ENVIADA],
            len(enviadas),
            monto_env,
            f"esta semana · {_s(res[eq.GANADA], 'ganada')} · {_s(res[eq.PERDIDA], 'perdida')}",
        ),
    ]


# --- Señales por CA (fila de la Mesa y panel) -------------------------------------------------

CIERRA_HOY = "cierra_hoy"
FALTA_COSTO = "falta_costo"
PRECIO_VENCIDO = "precio_vencido"  # el texto dice «Precio desactualizado»
ANEXO_SIN_LEER = "anexo_sin_leer"
LINEAS_POR_REVISAR = "lineas_por_revisar"
REQUISITOS = "requisitos"
IMPUESTO_POR_CLASIFICAR = "impuesto_por_clasificar"
TIPOS_SENAL = (
    CIERRA_HOY, REQUISITOS, FALTA_COSTO, PRECIO_VENCIDO, IMPUESTO_POR_CLASIFICAR, ANEXO_SIN_LEER,
    LINEAS_POR_REVISAR,
)  # fmt: skip


@dataclass(frozen=True)
class Senal:
    tipo: str
    nivel: str
    texto: str


def senales_por_ca(
    session: Session, empresa_id: int, codigos: list[str], ahora: datetime
) -> dict[str, list[Senal]]:
    """Señales de cada CA, en lote: un número FIJO de consultas sin importar cuántos códigos
    (regla 12). Las de costo usan el mismo criterio que la ficha (`proveedores.mejores_costos`
    = `mejor_costo` por línea `ok` con producto): sin candidato es «falta costo»; si el ganador
    está desactualizado, «Precio desactualizado · N días» con los días del costo más viejo que usa
    la CA (vigencia de la empresa, `empresas.dias_vigencia_precio`)."""
    if not codigos:
        return {}
    codigos = list(dict.fromkeys(codigos))
    cierres: dict[str, datetime | None] = {
        c: f
        for c, f in session.execute(
            select(CompraAgil.codigo, CompraAgil.fecha_cierre).where(CompraAgil.codigo.in_(codigos))
        )
    }
    lineas = session.execute(
        select(CaLinea.ca_codigo, CaLinea.estado, CaLinea.producto_id, CaLinea.cantidad_base).where(
            CaLinea.ca_codigo.in_(codigos), CaLinea.estado != REEMPLAZADA
        )
    ).all()
    pids = {int(p) for _c, e, p, _q in lineas if p is not None and e == "ok"}
    unidad: dict[int, str | None] = (
        {
            int(i): u
            for i, u in session.execute(
                select(ProductoCanonico.id, ProductoCanonico.unidad_base).where(
                    ProductoCanonico.id.in_(pids)
                )
            )
        }
        if pids
        else {}
    )
    pedidos = list(
        dict.fromkeys(
            (int(p), unidad.get(int(p)), q) for _c, e, p, q in lineas if p is not None and e == "ok"
        )
    )
    costos = mejores_costos(session, empresa_id, pedidos, hoy_en_chile(ahora))
    anexos = pend.motivos_de(session, codigos, ahora)
    requisitos: dict[str, int] = {
        c: int(n)
        for c, n in session.execute(
            select(CaRequisito.ca_codigo, func.count())
            .where(
                CaRequisito.empresa_id == empresa_id,
                CaRequisito.ca_codigo.in_(codigos),
                CaRequisito.obligatorio.is_(True),
                CaRequisito.cumplido.is_(False),
            )
            .group_by(CaRequisito.ca_codigo)
        )
    }
    sin_clasificar = ids_por_clasificar(session)

    falta: dict[str, int] = defaultdict(int)
    vencido_dias: dict[str, int] = {}
    por_revisar: dict[str, int] = defaultdict(int)
    impuesto: set[str] = set()
    for c, estado, pid, cant in lineas:
        if estado in POR_REVISAR:
            por_revisar[c] += 1
        if pid is not None and int(pid) in sin_clasificar:
            impuesto.add(c)
        if estado != "ok" or pid is None:
            continue
        mc = costos.get((int(pid), unidad.get(int(pid)), cant))
        if mc is None:
            falta[c] += 1
        elif mc.ganador.vencido:
            vencido_dias[c] = max(vencido_dias.get(c, 0), mc.ganador.dias or 0)

    hoy = hoy_en_chile(ahora)
    out: dict[str, list[Senal]] = {}
    for c in codigos:
        s: list[Senal] = []
        cierre = cierres.get(c)
        cierra_hoy = cierre is not None and cierre > ahora and hoy_en_chile(cierre) == hoy
        if cierra_hoy:
            s.append(Senal(CIERRA_HOY, URGENTE, "Cierra hoy"))
        if requisitos.get(c):
            s.append(
                Senal(REQUISITOS, URGENTE if cierra_hoy else ATENCION,
                      f"Requisitos pendientes · {int(requisitos[c])}")
            )  # fmt: skip
        if falta.get(c):
            s.append(Senal(FALTA_COSTO, ATENCION, f"Falta costo · {_s(falta[c], 'línea')}"))
        if c in vencido_dias:
            s.append(
                Senal(PRECIO_VENCIDO, ATENCION, f"Precio desactualizado · {vencido_dias[c]} días")
            )
        if c in impuesto:
            s.append(Senal(IMPUESTO_POR_CLASIFICAR, ATENCION, "Impuesto por clasificar"))
        if c in anexos:
            s.append(Senal(ANEXO_SIN_LEER, ATENCION, "Anexo sin leer"))
        if por_revisar.get(c):
            s.append(
                Senal(LINEAS_POR_REVISAR, ATENCION, f"{_s(por_revisar[c], 'línea')} por revisar")
            )
        out[c] = s
    return out


# --- Desde tu última visita -------------------------------------------------------------------

PANTALLA = "hoy"
CLAVE_VISTO = "visto_en"
SIN_VISITA = timedelta(hours=24)


@dataclass(frozen=True)
class Novedad:
    texto: str
    n: int
    url: str


def desde_ultima_visita(session: Session, usuario_id: int, ahora: datetime) -> datetime:
    """`visto_en` de la vista «hoy» del usuario (`usuario_vista`, F-dis-1); si no existe o no se
    puede leer, las últimas 24 h."""
    valor = vista_de(session, usuario_id, PANTALLA).get(CLAVE_VISTO)
    if isinstance(valor, str):
        try:
            visto = datetime.fromisoformat(valor)
        except ValueError:
            visto = None
        if visto is not None and visto.tzinfo is None and visto <= ahora:
            return visto
    return ahora - SIN_VISITA


def marcar_visto(session: Session, usuario_id: int, ahora: datetime) -> None:
    """Guarda `visto_en = ahora` sin borrar el resto de la vista «hoy». Sin commit."""
    datos: dict[str, Any] = dict(vista_de(session, usuario_id, PANTALLA))
    datos[CLAVE_VISTO] = ahora.replace(microsecond=0).isoformat()
    guardar_vista(session, usuario_id, PANTALLA, datos)


def novedades(
    session: Session, empresa_id: int, usuario_id: int, desde: datetime, ahora: datetime
) -> list[Novedad]:
    """Lo que pasó en `(desde, ahora]`, sin filas con `n = 0`:

    - CA nuevas del territorio de la empresa (publicadas en la ventana, vigentes) que **calzan**:
      al menos una línea con un producto que la empresa cotiza (costo de un proveedor activo) o
      tiene en su inventario [decisión de esta fase, ver docs/00-estado.md].
    - Ofertas ganadas (`ca_trabajo.resultado_en`).
    - Menciones al usuario.
    - Ofertas enviadas por OTROS miembros del equipo (una fila por persona).
    - Listas de precios de proveedores actualizadas (`costo_historial`, una fila por proveedor).
    """
    out: list[Novedad] = []
    empresa = session.get(Empresa, empresa_id)
    regiones = [int(r) for r in (empresa.regiones_prioritarias or [])] if empresa else []
    if regiones:
        cotiza = (
            select(ProveedorProducto.producto_id)
            .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
            .where(
                Proveedor.empresa_id == empresa_id,
                Proveedor.activo.is_(True),
                ProveedorProducto.producto_id.is_not(None),
            )
            .union(
                select(EmpresaProducto.producto_id).where(
                    EmpresaProducto.empresa_id == empresa_id,
                    EmpresaProducto.activo.is_(True),
                )
            )
        )
        calza = (
            select(CaLinea.id)
            .where(
                CaLinea.ca_codigo == CompraAgil.codigo,
                CaLinea.estado != REEMPLAZADA,
                CaLinea.producto_id.in_(cotiza),
            )
            .exists()
        )
        n = int(
            session.execute(
                select(func.count())
                .select_from(CompraAgil)
                .where(
                    *condiciones_vigente(regiones, ahora),
                    CompraAgil.fecha_publicacion > desde,
                    CompraAgil.fecha_publicacion <= ahora,
                    calza,
                )
            ).scalar_one()
        )
        if n:
            out.append(Novedad(f"{_s(n, 'CA nueva', 'CA nuevas')} que calzan", n, "/mesa"))

    ganadas = (
        session.execute(
            select(CompraAgil.nombre)
            .join(CaTrabajo, CaTrabajo.ca_codigo == CompraAgil.codigo)
            .where(
                CaTrabajo.empresa_id == empresa_id,
                CaTrabajo.resultado == eq.GANADA,
                CaTrabajo.resultado_en > desde,
                CaTrabajo.resultado_en <= ahora,
            )
            .order_by(CaTrabajo.resultado_en.desc())
        )
        .scalars()
        .all()
    )
    if ganadas:
        texto = f"1 ganada · {ganadas[0]}" if len(ganadas) == 1 else f"{len(ganadas)} ganadas"
        out.append(Novedad(texto, len(ganadas), "/ventas"))

    menciones = int(
        session.execute(
            select(func.count())
            .select_from(CaEvento)
            .where(
                CaEvento.empresa_id == empresa_id,
                CaEvento.tipo == eq.EV_MENCION,
                CaEvento.destinatario_id == usuario_id,
                CaEvento.creado_en > desde,
                CaEvento.creado_en <= ahora,
            )
        ).scalar_one()
    )
    if menciones:
        out.append(
            Novedad(f"{_s(menciones, 'mención', 'menciones')} a ti", menciones, "/actividad")
        )

    por_persona: dict[int, set[str]] = defaultdict(set)
    for uid, codigo, datos in session.execute(
        select(CaEvento.usuario_id, CaEvento.ca_codigo, CaEvento.datos).where(
            CaEvento.empresa_id == empresa_id,
            CaEvento.tipo == eq.EV_ESTADO,
            CaEvento.usuario_id.is_not(None),
            CaEvento.usuario_id != usuario_id,
            CaEvento.creado_en > desde,
            CaEvento.creado_en <= ahora,
        )
    ):
        if isinstance(datos, dict) and datos.get("a") == eq.ENVIADA:
            por_persona[int(uid)].add(codigo)
    if por_persona:
        nombres = {
            u.id: eq.nombre_visible(u)
            for u in session.execute(
                select(Usuario).where(Usuario.id.in_(set(por_persona)))
            ).scalars()
        }
        for uid, cas in sorted(por_persona.items(), key=lambda kv: nombres.get(kv[0], "")):
            n = len(cas)
            out.append(
                Novedad(f"{nombres.get(uid, 'Alguien')} envió {_s(n, 'oferta')}", n, "/actividad")
            )

    for prov_id, prov_nombre, n in session.execute(
        select(Proveedor.id, Proveedor.nombre, func.count())
        .select_from(CostoHistorial)
        .join(ProveedorProducto, ProveedorProducto.id == CostoHistorial.proveedor_producto_id)
        .join(Proveedor, Proveedor.id == ProveedorProducto.proveedor_id)
        .where(
            Proveedor.empresa_id == empresa_id,
            CostoHistorial.registrado_en > desde,
            CostoHistorial.registrado_en <= ahora,
        )
        .group_by(Proveedor.id, Proveedor.nombre)
        .order_by(Proveedor.nombre)
    ):
        out.append(
            Novedad(
                f"{prov_nombre} actualizó {_s(int(n), 'precio')}", int(n), f"/proveedores/{prov_id}"
            )
        )
    return out
