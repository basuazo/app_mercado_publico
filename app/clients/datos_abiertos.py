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
