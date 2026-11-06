"""Tipos de retorno de los clientes de la API de Mercado Público."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from app.core.logging import get_logger
from app.core.tiempo import a_utc_naive, borde_del_dia_utc_naive

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Helpers de parsing defensivo
# ---------------------------------------------------------------------------


def parse_binario(v: object) -> bool | None:
    """Parsea campos binarios de v1 que vienen como 0/1/2/'NO'/null/bool."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v != 0
    s = str(v).strip().upper()
    if s in ("0", "NO", "FALSE", "N"):
        return False
    if s in ("1", "2", "SI", "YES", "TRUE", "S"):
        return True
    return None


def parse_fecha_v1(s: object) -> date | None:
    """Parsea fechas v1: ddmmaaaa (8 dígitos) o ISO-8601 (algunos listados, p.ej. activas).

    El listado de licitaciones activas trae FechaCierre/FechaPublicacion en
    ISO-8601 en vez del ddmmaaaa habitual del resto de v1 (regla 6: parseo
    siempre defensivo). Devuelve None solo si ningún formato calza.
    """
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if len(s) == 8 and s.isdigit():
        try:
            return date(int(s[4:]), int(s[2:4]), int(s[:2]))
        except ValueError:
            return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def parse_fecha_v1_dt(s: object, *, fin_de_dia: bool = False) -> datetime | None:
    """Igual que :func:`parse_fecha_v1`, pero conservando la HORA que manda la fuente.

    El listado de licitaciones activas trae ``FechaCierre``/``FechaPublicacion``
    en ISO-8601, así que ahí sí hay hora; el resto de v1 manda ``ddmmaaaa``, que
    no la tiene. Esta función cubre los dos casos y devuelve siempre **naive en
    UTC**, que es como se guarda en la base (ver ``app/core/tiempo.py``):

    - ISO con hora: se conserva la hora. Si trae offset se convierte a UTC; si
      no lo trae se interpreta como hora de Chile **[I]**.
    - Solo fecha (``ddmmaaaa`` o ISO de 10 caracteres): no hay hora que
      conservar, así que se usa el borde del día que corresponda —
      ``fin_de_dia=True`` para ``fecha_cierre``, ``False`` para
      ``fecha_publicacion`` (ver :func:`app.core.tiempo.borde_del_dia_utc_naive`).

    Regla 6: cualquier valor que no calce devuelve ``None`` y queda logueado,
    nunca rompe la ingesta.
    """
    if not s or not isinstance(s, str):
        return None
    txt = s.strip()

    if len(txt) == 8 and txt.isdigit():
        try:
            d = date(int(txt[4:]), int(txt[2:4]), int(txt[:2]))
        except ValueError:
            _log.warning("Fecha v1 ddmmaaaa inválida: %r", txt)
            return None
        return borde_del_dia_utc_naive(d, fin_de_dia=fin_de_dia)

    solo_fecha = "T" not in txt and " " not in txt and len(txt) <= 10
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        try:
            d = date.fromisoformat(txt[:10])
        except ValueError:
            _log.warning("Fecha v1 no reconocida: %r", txt)
            return None
        return borde_del_dia_utc_naive(d, fin_de_dia=fin_de_dia)

    if solo_fecha:
        return borde_del_dia_utc_naive(dt.date(), fin_de_dia=fin_de_dia)
    return a_utc_naive(dt)


def parse_fecha_iso(s: object, *, fin_de_dia: bool = False) -> datetime | None:
    """Parsea fechas ISO-8601 de v2 con tolerancia a formatos parciales.

    Devuelve **naive en UTC** (ver ``app/core/tiempo.py``). ``datetime.fromisoformat``
    en Python 3.11+ entiende tanto la ``Z`` como los offsets explícitos, así que
    el offset **se conserva** en vez de descartarse; los formatos antiguos
    quedan solo como caída. Si el valor trae solo la fecha, ``fin_de_dia``
    elige el borde del día igual que en :func:`parse_fecha_v1_dt`.
    """
    if not s or not isinstance(s, str):
        return None
    txt = s.strip()
    solo_fecha = "T" not in txt and " " not in txt and len(txt) <= 10

    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        sin_z, en_utc = (txt[:-1], True) if txt.endswith("Z") else (txt, False)
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(sin_z, fmt)
            except ValueError:
                continue
            if en_utc:
                dt = dt.replace(tzinfo=UTC)
            break
        else:
            _log.warning("Fecha ISO no reconocida: %r", txt)
            return None

    if solo_fecha:
        return borde_del_dia_utc_naive(dt.date(), fin_de_dia=fin_de_dia)
    return a_utc_naive(dt)


def parse_fecha_v2(s: object, *, fin_de_dia: bool = False) -> datetime | None:
    """Parsea una fecha de la API v2 (Compra Ágil): SIEMPRE es hora de Chile.

    La `Z` final que traen ``fecha_ultimo_cambio`` y los ``*_llamado`` es falsa
    **[V]** (22-sep-2026): el piso entre nuestro ``actualizado_en`` (UTC real) y
    ``fecha_ultimo_cambio`` pasó de 4 h a 3 h justo con el cambio de horario de
    Chile del 6-sep, y ``fecha_cierre`` '2026-09-23 13:00' viene junto a
    ``fecha_cierre_primer_llamado`` '2026-09-23T13:00:00Z' para el mismo
    instante. Se descarta la `Z` y se delega en la regla "sin offset = Chile"
    de :func:`parse_fecha_iso`, que queda como está.
    """
    if isinstance(s, str):
        txt = s.strip()
        if txt[-1:] in ("Z", "z"):
            s = txt[:-1]
    return parse_fecha_iso(s, fin_de_dia=fin_de_dia)


def parse_float(v: object) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def parse_int(v: object) -> int | None:
    if v is None:
        return None
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# API v1 — Licitaciones
# ---------------------------------------------------------------------------


@dataclass
class ItemLicitacion:
    codigo_producto: str
    nombre: str
    cantidad: float | None
    unidad: str


@dataclass
class LicitacionBasica:
    codigo: str
    nombre: str
    estado: int | None
    # Instantes naive en UTC, ya convertidos por parse_fecha_v1_dt (F-fecha-cierre).
    fecha_publicacion: datetime | None
    fecha_cierre: datetime | None
    tipo: str | None
    codigo_organismo: str | None
    # Bajo `Comprador` en v1 [V, sonda claves-lic]. El listado de activas no los
    # trae: llegan solo con el detalle (F-datos-1).
    organismo_nombre: str | None = None
    region: int | None = None


@dataclass
class LicitacionDetalle(LicitacionBasica):
    descripcion: str = ""
    moneda: str = ""
    monto_estimado: float | None = None
    tipo_monto: int | None = None
    items: list[ItemLicitacion] = field(default_factory=list)
    informada: bool | None = None
    contrato: bool | None = None
    obras: bool | None = None


# ---------------------------------------------------------------------------
# API v1 — Órdenes de Compra
# ---------------------------------------------------------------------------


@dataclass
class OrdenCompraBasica:
    codigo: str
    nombre: str
    estado: int | None
    tipo: int | None
    fecha_creacion: date | None
    codigo_organismo: str | None
    monto: float | None
    moneda: str | None


# ---------------------------------------------------------------------------
# API v1 — Proveedores / Compradores
# ---------------------------------------------------------------------------


@dataclass
class Proveedor:
    rut: str
    nombre: str
    codigo: str | None


@dataclass
class Comprador:
    codigo: str
    nombre: str
    rut: str | None


# ---------------------------------------------------------------------------
# API v2 — Compra Ágil
# ---------------------------------------------------------------------------


@dataclass
class CompraAgilItem:
    codigo_producto: str
    nombre: str
    cantidad: float | None
    unidad: str
    # Texto libre del comprador por producto (F-detalles-match); en la muestra
    # real llegó a 373 caracteres. Default para no romper a quien no lo pase.
    descripcion: str = ""


@dataclass
class CompraAgilBasica:
    codigo: str
    nombre: str
    estado: str
    fecha_publicacion: datetime | None
    fecha_cierre: datetime | None
    fecha_ultimo_cambio: datetime | None
    monto_clp: float | None
    region: int | None
    organismo_nombre: str | None
    organismo_rut: str | None
    total_ofertas: int
    # `institucion.unidad_compra`: el detalle la trae y el listado, si viene (F-merc-0b-fix2).
    unidad_compra: str | None = None


@dataclass
class CotizacionCAProducto:
    """Un producto de una oferta (`productos_cotizados[]`, guía CA v2 §5–§7) [I]."""

    codigo_producto: str | None = None
    nombre_producto: str | None = None
    descripcion: str | None = None
    cantidad: float | None = None
    # Neto, según la guía.
    precio_unitario: float | None = None
    monto_total_producto: float | None = None


@dataclass
class CotizacionCA:
    """Una oferta de `proveedores_cotizando[]` (F-merc-0b).

    Claves verificadas en la sonda del 31-oct [V]: el ganador va en
    `proveedor_seleccionado` al nivel de la oferta y el estado en `estado`; la
    `seleccion.*` de la guía no existe. `seleccionado=None` cuando no viene ninguna
    de las dos: no es lo mismo que `False`, y el cliente no infiere ganadores.
    """

    rut_proveedor: str
    razon_social: str | None = None
    es_emt: bool | None = None
    codigo_empresa: str | None = None
    codigo_sucursal: str | None = None
    id_cotizacion: str | None = None
    id_oc: str | None = None
    estado_id: int | None = None
    estado_glosa: str | None = None
    estado_por_comprador: str | None = None
    activo: bool | None = None
    justificacion_inadmisibilidad: str | None = None
    seleccionado: bool | None = None
    motivo_seleccion: str | None = None
    criterio_seleccion: str | None = None
    fecha_creacion: datetime | None = None
    fecha_vigencia: datetime | None = None
    valor_neto: float | None = None
    total_impuesto: float | None = None
    monto_despacho: float | None = None
    monto_total: float | None = None
    nombre_impuesto: str | None = None
    porcentaje_impuesto: float | None = None
    descripcion: str | None = None
    productos: list[CotizacionCAProducto] = field(default_factory=list)


@dataclass
class CompraAgilDetalle(CompraAgilBasica):
    descripcion: str = ""
    productos: list[CompraAgilItem] = field(default_factory=list)
    id_orden_compra: str | None = None
    estado_convocatoria: int | None = None
    # F-merc-0b: aditivos, con default; los consumidores de antes no los ven.
    cotizaciones: list[CotizacionCA] = field(default_factory=list)
    direccion_entrega: str | None = None
    plazo_entrega_dias: int | None = None
    tipo_presupuesto: str | None = None
    moneda: str | None = None
    fecha_cierre_segundo_llamado: datetime | None = None
    motivo_desierta: str | None = None
    motivo_cancelacion: str | None = None
    n_documentos: int | None = None
    # F-plat-2: `documentos[].{id, nombre}` (guía v3.0 [V en la guía]); la descarga está por
    # verificar (`smoke_test.py --documentos`). Solo los ids legibles.
    documentos: list[DocumentoCA] = field(default_factory=list)


@dataclass
class DocumentoCA:
    """Un anexo listado en el detalle de una CA (F-plat-2). No se persiste."""

    id: str
    nombre: str = ""


# ---------------------------------------------------------------------------
# OpenRouter (F-plat-2): extracción de líneas de un anexo
# ---------------------------------------------------------------------------


@dataclass
class LineaExtraida:
    """Una fila que el modelo leyó en un anexo. Todo opcional salvo el texto (regla 6).

    `cantidad` para anexos de CA; `precio` (neto, por formato) para listas de proveedor."""

    texto: str
    cantidad: float | None = None
    unidad: str | None = None
    formato: str | None = None
    observacion: str | None = None
    precio: float | None = None


@dataclass
class ExtraccionAnexo:
    lineas: list[LineaExtraida] = field(default_factory=list)
    modelo: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    costo_usd: float = 0.0
    desde_cache: bool = False
    # El costo salió de la tarifa configurada, no de `usage.cost` de OpenRouter.
    estimado: bool = False
    # La respuesta no parseó ni tras el reintento: `lineas` queda vacía (no es "anexo vacío").
    invalida: bool = False
    # Respondió `llm_modelo_respaldo` porque el principal falló (F-plat-anexos-2 §2.6).
    respaldo: bool = False


@dataclass
class PaginacionV2:
    total_paginas: int
    total_resultados: int
    numero_pagina: int
    tamano_pagina: int


@dataclass
class RespuestaListadoV2:
    items: list[CompraAgilBasica]
    paginacion: PaginacionV2
