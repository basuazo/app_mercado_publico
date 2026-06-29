"""Cliente del Plan Anual de Compra (PAC) — datos abiertos, sin ticket.

Capa anti-corrupción separada de app/clients/datos_abiertos.py: aunque ambas
fuentes son datos abiertos sin cuota (ver docs/07-plan-anual.md §6), el PAC
vive en un host distinto (`pac-files.da.mercadopublico.cl`) con reglas de
parseo propias (UTF-8 con BOM, sin Latin-1; sin quoting; reconstrucción de
registros multilínea por ausencia de comillas, no por comillas con salto
embebido como lic-da). Nada de lo que vive aquí debe filtrarse a app/ingest
ni a app/api con tipos crudos — los dataclasses de abajo son el contrato.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

import httpx

from app.core.logging import get_logger

_log = get_logger(__name__)

_BASE_URL_PAC_DEFAULT = "https://pac-files.da.mercadopublico.cl"
_KPI_URL_DEFAULT = "https://mserv-datos-abiertos.chilecompra.cl/v1/kpi/instituciones"
_ENCODING = "utf-8-sig"

# Máximo de líneas físicas por registro lógico antes de descartar como ambiguo.
# El spike observó hasta 8 (ver docs/07-plan-anual.md §5-bis b); se deja margen.
_MAX_LINEAS_LOGICAS = 14
_N_CAMPOS_COLA = 6


def url_pac(agno: int, codigo_entidad: int, base_url: str = _BASE_URL_PAC_DEFAULT) -> str:
    """URL del ZIP del PAC filtrado por institución/año (datos abiertos, sin ticket)."""
    return f"{base_url.rstrip('/')}/{agno}/pacorganismos_{agno}_{codigo_entidad}.zip"


def descargar_pac(
    codigo_entidad: int,
    agno: int,
    *,
    base_url: str = _BASE_URL_PAC_DEFAULT,
    timeout: float = 60.0,
) -> bytes | None:
    """Descarga el ZIP del PAC de una institución/año. None si no hay plan publicado.

    403 (AccessDenied de S3) es el comportamiento documentado para institución/año
    sin archivo (ver docs/07-plan-anual.md §5-bis f) — se traduce a None limpio, sin
    excepción. Cualquier otro código de error sí se propaga (no es un caso esperado).
    """
    url = url_pac(agno, codigo_entidad, base_url)
    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    if resp.status_code == 403:
        return None
    resp.raise_for_status()
    return resp.content


@dataclass
class InstitucionPAC:
    """Una institución del catálogo de datos abiertos (alimenta el autocomplete)."""

    codigo_entidad: int
    razon_social: str
    rut: str


def listar_instituciones(
    *,
    kpi_url: str = _KPI_URL_DEFAULT,
    timeout: float = 30.0,
) -> list[InstitucionPAC]:
    """Catálogo completo de instituciones (sin auth, sin cuota — ver §5-bis d).

    `codigoEntidad` de este catálogo es directamente `codigo_organismo` del
    modelo (confirmado en el spike, no requiere mapeo).
    """
    resp = httpx.get(kpi_url, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, list):
        _log.warning("listar_instituciones: payload inesperado: %r", type(data))
        return []

    out: list[InstitucionPAC] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            codigo_entidad = int(item["codigoEntidad"])
        except (KeyError, TypeError, ValueError):
            # %r con un dict como único arg dispara el caso especial de logging
            # (LogRecord trata un Mapping solitario como el dict de %-formato, no
            # como un arg posicional) — se pasa repr(item) ya como str para evitarlo.
            _log.warning("listar_instituciones: institución sin codigoEntidad válido: %s", repr(item))
            continue
        out.append(
            InstitucionPAC(
                codigo_entidad=codigo_entidad,
                razon_social=str(item.get("razonSocial") or "").strip(),
                rut=str(item.get("rut") or "").strip(),
            )
        )
    return out


@dataclass
class LineaPAC:
    """Una línea del PAC tal como viene en el CSV filtrado por institución/año."""

    institucion_nombre: str
    rut_institucion: str  # ERRATA OFICIAL: es codigoEntidad, no un RUT (ver §5-bis c)
    codigo_producto: str  # identificador secuencial interno, NO UNSPSC
    descripcion_producto: str
    cantidad_estimada: float | None
    monto_unitario_clp: float | None
    monto_estimado_clp: float | None
    mes_estimado: int | None
    trimestre_estimado: int | None
    estado_planificacion: str


def _parse_float(v: str | None) -> float | None:
    if not v:
        return None
    v = v.strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _parse_int(v: str | None) -> int | None:
    if not v:
        return None
    v = v.strip()
    if not v.isdigit():
        return None
    return int(v)


def _cola_es_plausible(cola: list[str]) -> bool:
    """Valida que los 6 campos finales tengan forma numérica/enum plausible.

    Heurística de reconstrucción (regla 6, parseo defensivo): sin esto, una
    descripción con ';' embebido podría cortar el registro en el lugar
    equivocado. No basta con contar 9 ';' (ver docs/07-plan-anual.md §5-bis b).
    """
    if len(cola) != _N_CAMPOS_COLA:
        return False
    cantidad, monto_unitario, monto_estimado, mes, trimestre, estado = cola
    if _parse_float(cantidad) is None:
        return False
    if _parse_float(monto_unitario) is None:
        return False
    if _parse_float(monto_estimado) is None:
        return False
    if _parse_int(mes) is None:
        return False
    if _parse_int(trimestre) is None:
        return False
    return bool(estado.strip())


def parse_pac_csv(zip_bytes: bytes) -> list[LineaPAC]:
    """Parsea el CSV (único, dentro del ZIP) del PAC filtrado por institución/año.

    Encoding UTF-8 con BOM, separador ';', SIN quoting, LF puro (ver §5-bis b).
    Reconstruye registros lógicos partidos en varias líneas físicas por saltos
    de línea reales (sin comillas) embebidos en `descripcion_producto`: acumula
    líneas hasta que los últimos 6 campos (cantidad..estado) tengan forma
    plausible, usando los primeros 3 campos como cabeza y los últimos 6 como
    cola — todo lo del medio (pueda o no contener ';') es la descripción.
    Un registro que no logra completar una cola plausible dentro del máximo de
    líneas se descarta con log, sin romper el resto del archivo (regla 6).
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        nombres_csv = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not nombres_csv:
            _log.warning("parse_pac_csv: el ZIP no contiene ningún CSV")
            return []
        with zf.open(nombres_csv[0]) as raw:
            texto = raw.read().decode(_ENCODING)

    lineas_fisicas = texto.split("\n")
    if lineas_fisicas and lineas_fisicas[0].strip().lower().startswith("institucion_nombre"):
        lineas_fisicas = lineas_fisicas[1:]

    resultado: list[LineaPAC] = []
    buffer: list[str] = []

    def _vaciar_buffer_ambiguo() -> None:
        if buffer:
            _log.warning(
                "parse_pac_csv: registro descartado por ambiguo tras %d línea(s): %r",
                len(buffer),
                buffer[0][:80],
            )

    for linea_fisica in lineas_fisicas:
        if not buffer and linea_fisica.strip() == "":
            continue  # línea vacía suelta (ej. trailing newline final)

        buffer.append(linea_fisica)
        if len(buffer) > _MAX_LINEAS_LOGICAS:
            _vaciar_buffer_ambiguo()
            buffer = []
            continue

        acumulado = "\n".join(buffer)
        partes = acumulado.split(";")
        if len(partes) < 10:
            continue  # aún no hay suficientes campos: sigue acumulando

        cola = partes[-_N_CAMPOS_COLA:]
        if not _cola_es_plausible(cola):
            continue  # el corte cayó dentro de la descripción: sigue acumulando

        cabeza = partes[:3]
        descripcion = ";".join(partes[3:-_N_CAMPOS_COLA])
        resultado.append(
            LineaPAC(
                institucion_nombre=cabeza[0].strip(),
                rut_institucion=cabeza[1].strip(),
                codigo_producto=cabeza[2].strip(),
                descripcion_producto=descripcion,
                cantidad_estimada=_parse_float(cola[0]),
                monto_unitario_clp=_parse_float(cola[1]),
                monto_estimado_clp=_parse_float(cola[2]),
                mes_estimado=_parse_int(cola[3]),
                trimestre_estimado=_parse_int(cola[4]),
                estado_planificacion=cola[5].strip(),
            )
        )
        buffer = []

    _vaciar_buffer_ambiguo()  # último buffer sin cerrar (si quedó alguno)
    return resultado
