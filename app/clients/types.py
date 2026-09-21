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


@dataclass
class CompraAgilDetalle(CompraAgilBasica):
    descripcion: str = ""
    productos: list[CompraAgilItem] = field(default_factory=list)
    id_orden_compra: str | None = None
    estado_convocatoria: int | None = None


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
