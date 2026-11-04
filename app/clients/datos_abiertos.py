"""Cliente de datos abiertos de ChileCompra (ZIP/CSV de licitaciones, sin ticket).

Capa anti-corrupción separada de app/clients/mp_v1.py: formato y reglas de parseo
completamente distintos (Azure Blob público, ZIP, CSV separado por ';', encoding
Latin-1, sin ticket ni cuota de API — ver docs/04-datos-abiertos.md). Nada de lo
que vive aquí debe filtrarse a app/ingest, que solo conoce ItemDA.
"""

from __future__ import annotations

import csv
import io
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.core.logging import get_logger

_log = get_logger(__name__)

_BASE_URL_DEFAULT = "https://transparenciachc.blob.core.windows.net"
_ENCODING = "latin-1"
_DELIMITER = ";"


def url_lic_da(anio: int, mes: int, base_url: str = _BASE_URL_DEFAULT) -> str:
    """URL del ZIP mensual de licitaciones (datos abiertos). `mes` SIN cero a la izquierda."""
    return f"{base_url.rstrip('/')}/lic-da/{anio}-{mes}.zip"


def url_oc_da(anio: int, mes: int, base_url: str = _BASE_URL_DEFAULT) -> str:
    """URL del ZIP mensual de órdenes de compra (datos abiertos). `mes` SIN cero a la izquierda."""
    return f"{base_url.rstrip('/')}/oc-da/{anio}-{mes}.zip"


def head_last_modified(url: str, *, timeout: float = 30.0) -> datetime | None:
    """HEAD al blob; devuelve Last-Modified (UTC, sin tzinfo) o None si no está disponible.

    Sin ticket: no hay cuota ni rate limiter de la API que aplicar aquí.
    """
    try:
        resp = httpx.head(url, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        _log.warning("head_last_modified: error de red en %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        _log.warning("head_last_modified: %s respondió %d", url, resp.status_code)
        return None
    raw = resp.headers.get("last-modified")
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def descargar_zip(url: str, destino: str, *, timeout: float = 120.0) -> None:
    """Descarga el ZIP a `destino` en streaming — nunca carga el archivo completo en memoria."""
    with httpx.stream("GET", url, timeout=timeout, follow_redirects=True) as resp:
        resp.raise_for_status()
        with open(destino, "wb") as f:
            for chunk in resp.iter_bytes():
                f.write(chunk)


@dataclass
class ItemDA:
    """Un ítem de licitación tal como viene en el CSV de datos abiertos."""

    codigo_externo: str
    codigo_item: str
    codigo_producto: str
    nombre: str
    unidad: str
    cantidad: float | None


def _parse_cantidad(v: str | None) -> float | None:
    if not v:
        return None
    v = v.strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    try:
        return float(v.replace(",", "."))
    except ValueError:
        return None


def _parse_monto(v: str | None) -> float | None:
    """Parseo defensivo de columnas monetarias de lic-da (regla 6).

    ~97 % vienen como entero plano, ~2 % en notación científica ("5e+07") y
    ~1 % mezclan coma decimal con notación científica ("9,9e+07" = 99.000.000)
    — ver docs/05-competencia.md §5. Inválido -> None, nunca rompe la captura.
    """
    if not v:
        return None
    v = v.strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        pass
    try:
        return float(v.replace(",", "."))
    except ValueError:
        return None


@dataclass
class OfertaDA:
    """Una oferta (proveedor × ítem) de una licitación, tal como viene en lic-da."""

    codigo_externo: str
    codigo_item: str
    rut_proveedor: str
    nombre_proveedor: str
    monto_unitario: float | None
    monto_linea_adjudicada: float | None
    cantidad: float | None
    seleccionada: bool


def stream_ofertas(zip_path: str, codigo_externo: str) -> Iterator[OfertaDA]:
    """Stream de ofertas de UNA licitación (`codigo_externo`) desde el CSV del ZIP.

    Mismo patrón RAM-safe que stream_items, filtrando por CodigoExterno antes
    de yieldear — el archivo completo nunca se carga en memoria. Acceso a
    columnas por nombre exacto (ver docs/05-competencia.md §1): `Codigoitem`,
    `RutProveedor`, `NombreProveedor`, `MontoUnitarioOferta`, `MontoLineaAdjudica`,
    `CantidadAdjudicada`, `"Oferta seleccionada"`.
    """
    with zipfile.ZipFile(zip_path) as zf:
        nombres_csv = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not nombres_csv:
            _log.warning("stream_ofertas: %s no contiene ningún CSV", zip_path)
            return
        with zf.open(nombres_csv[0]) as raw:
            texto = io.TextIOWrapper(raw, encoding=_ENCODING, newline="")
            reader = csv.DictReader(texto, delimiter=_DELIMITER)
            for fila in reader:
                if (fila.get("CodigoExterno") or "").strip() != codigo_externo:
                    continue
                yield OfertaDA(
                    codigo_externo=codigo_externo,
                    codigo_item=(fila.get("Codigoitem") or "").strip(),
                    rut_proveedor=(fila.get("RutProveedor") or "").strip(),
                    nombre_proveedor=(fila.get("NombreProveedor") or "").strip(),
                    monto_unitario=_parse_monto(fila.get("MontoUnitarioOferta")),
                    monto_linea_adjudicada=_parse_monto(fila.get("MontoLineaAdjudica")),
                    cantidad=_parse_cantidad(fila.get("CantidadAdjudicada")),
                    seleccionada=(fila.get("Oferta seleccionada") or "").strip() == "Seleccionada",
                )


@dataclass
class EstadoDA:
    """Estado de una licitación tal como viene en lic-da (código crudo, sin mapear)."""

    codigo_externo: str
    codigo_estado: str


def stream_estados(zip_path: str) -> Iterator[EstadoDA]:
    """Stream de (CodigoExterno, CodigoEstado) desde el CSV del ZIP (RAM-safe).

    Una fila por fila del CSV: la licitación se repite por ítem × oferta, así
    que deduplicar por código es cosa del caller. `CodigoEstado` sale crudo: sus
    códigos NO son los de la API v1 (ver `estado_licitacion_da`).
    """
    with zipfile.ZipFile(zip_path) as zf:
        nombres_csv = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not nombres_csv:
            _log.warning("stream_estados: %s no contiene ningún CSV", zip_path)
            return
        with zf.open(nombres_csv[0]) as raw:
            texto = io.TextIOWrapper(raw, encoding=_ENCODING, newline="")
            reader = csv.DictReader(texto, delimiter=_DELIMITER)
            for fila in reader:
                codigo_externo = (fila.get("CodigoExterno") or "").strip()
                if not codigo_externo:
                    continue
                yield EstadoDA(
                    codigo_externo=codigo_externo,
                    codigo_estado=(fila.get("CodigoEstado") or "").strip(),
                )


def stream_items(zip_path: str) -> Iterator[ItemDA]:
    """Stream de ítems desde el CSV de licitaciones dentro del ZIP (RAM-safe).

    Acceso a columnas por NOMBRE (csv.DictReader), no por índice — el orden de
    columnas del dataset no está garantizado. Parseo defensivo (regla 6):
    codigo_producto que no sea UNSPSC estándar (8 díg, ej. el pseudo-código de
    9 díg de "CONSULTORIA") se devuelve tal cual, sin truncar ni inventar —
    el caller decide qué hacer con eso.
    """
    with zipfile.ZipFile(zip_path) as zf:
        nombres_csv = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not nombres_csv:
            _log.warning("stream_items: %s no contiene ningún CSV", zip_path)
            return
        with zf.open(nombres_csv[0]) as raw:
            texto = io.TextIOWrapper(raw, encoding=_ENCODING, newline="")
            reader = csv.DictReader(texto, delimiter=_DELIMITER)
            for fila in reader:
                codigo_externo = (fila.get("CodigoExterno") or "").strip()
                if not codigo_externo:
                    continue
                yield ItemDA(
                    codigo_externo=codigo_externo,
                    codigo_item=(fila.get("Codigoitem") or "").strip(),
                    codigo_producto=(fila.get("CodigoProductoONU") or "").strip(),
                    nombre=(fila.get("Nombre producto genrico") or "").strip(),
                    unidad=(fila.get("UnidadMedida") or "").strip(),
                    cantidad=_parse_cantidad(fila.get("Cantidad")),
                )


# Columnas de oc-da que usa la plataforma (mismas que `COLS_OC` del observatorio). El RUT del
# proveedor es `RutSucursal` [V 31-oct]; los nombres tienen erratas oficiales ("NombreroductoGenerico").
COLS_OC = (
    "Codigo", "CodigoAbreviadoTipoOC", "EsCompraAgil", "FechaCreacion", "codigoEstado",
    "CodigoOrganismoPublico", "OrganismoPublico", "sector", "RutUnidadCompra", "UnidadCompra",
    "CiudadUnidadCompra", "RegionUnidadCompra", "RutSucursal", "CodigoProveedor", "NombreProveedor",
    "ComunaProveedor", "RegionProveedor", "codigoProductoONU", "NombreroductoGenerico",
    "EspecificacionComprador", "EspecificacionProveedor", "cantidad", "UnidadMedida",
    "precioNeto", "totalLineaNeto", "TipoMonedaOC", "IDItem", "monedaItem",
)  # fmt: skip


def iterar_oc_da(zip_path: str, columnas_extra: list[str] | None = None) -> Iterator[dict[str, str]]:
    """Stream de líneas de oc-da como dict {columna: texto} con solo `COLS_OC` (RAM-safe).

    Nunca carga el mes en memoria: el csv lee del ZIP fila a fila (campos multilínea incluidos).
    Una columna que falta queda en "" (parseo defensivo, regla 6) y se avisa una vez.
    `columnas_extra` (lista que se llena) recibe los nombres de columna del archivo que NO están
    en `COLS_OC`: sirve para detectar un id de ítem (F-merc-2 §4).
    """
    csv.field_size_limit(1 << 30)
    with zipfile.ZipFile(zip_path) as zf:
        nombres_csv = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not nombres_csv:
            _log.warning("iterar_oc_da: %s no contiene ningún CSV", zip_path)
            return
        with zf.open(nombres_csv[0]) as raw:
            texto = io.TextIOWrapper(raw, encoding=_ENCODING, newline="")
            reader = csv.reader(texto, delimiter=_DELIMITER)
            header = next(reader, None)
            if header is None:
                return
            pos = {h.strip(): i for i, h in enumerate(header)}
            faltan = [c for c in COLS_OC if c not in pos]
            if faltan:
                _log.warning("iterar_oc_da: columnas faltantes (quedan vacías): %s", faltan)
            if columnas_extra is not None:
                columnas_extra.extend(h.strip() for h in header if h.strip() not in COLS_OC)
            indices = [(c, pos.get(c)) for c in COLS_OC]
            for fila in reader:
                n = len(fila)
                yield {c: (fila[i] if i is not None and i < n else "") for c, i in indices}
