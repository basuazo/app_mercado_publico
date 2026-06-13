"""Tipos de retorno de los clientes de la API de Mercado Público."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

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
    """Parsea fechas v1 en formato ddmmaaaa."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    if len(s) != 8:
        return None
    try:
        return date(int(s[4:]), int(s[2:4]), int(s[:2]))
    except (ValueError, TypeError):
        return None


def parse_fecha_iso(s: object) -> datetime | None:
    """Parsea fechas ISO-8601 con tolerancia a formatos parciales."""
    if not s or not isinstance(s, str):
        return None
    s = s.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


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
    fecha_publicacion: date | None
    fecha_cierre: date | None
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
