"""Ingesta de órdenes de compra de Compra Ágil desde datos abiertos (F-merc-2).

Fuente: `oc-da/{año}-{mes}.zip` (Azure Blob público): sin ticket, sin cuota, sin 429. Por
cada mes guarda en `oc_lineas_ca` las líneas `EsCompraAgil = Si`, en `GRUPOS` y con comprador
en las regiones activas (F-plat-3), y en `precio_referencia` los percentiles nacionales por producto.

- Patrón de `datos_abiertos`: HEAD + `Last-Modified` como cursor por mes en `sync_state`
  (`mercado_oc:AAAA-MM`); una descarga fallida no aborta el resto de los meses (R6).
- Streaming (regla 12): el CSV se lee fila a fila; en memoria solo van lotes de 2.000 filas
  y, por mes, las listas de precios por clave para los percentiles.
- Idempotente por mes: borra las líneas del mes y las reinserta como upsert sobre la PK
  `(codigo_oc, id_item)` en UNA transacción. Una OC puede venir en más de un ZIP mensual: la
  fila queda en el mes más reciente.
- Sin dependencias nuevas; el lock es el advisory de siempre (`_locked` en el CLI).

Fuente: Dirección ChileCompra.
"""

from __future__ import annotations

import functools
import tempfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Engine, delete, func, insert, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.catalogos.mercado import grupo_de, moneda_codigo, normalizar, num, rut
from app.catalogos.regiones import region_codigo
from app.clients.datos_abiertos import descargar_zip, head_last_modified, iterar_oc_da, url_oc_da
from app.core.logging import get_logger
from app.core.settings import Settings
from app.ingest.datos_abiertos import _guardar_estado, _leer_cursor, _mes_actual_chile
from app.mercado.territorio import regiones_activas_engine
from app.models.tables import OcLineaCa, PrecioReferencia

_log = get_logger(__name__)

PREFIJO_FUENTE = "mercado_oc:"
_LOTE = 2000
_TANDA_UPSERT = 400
_MIN_PRECIOS = 3  # una clave con menos precios en el mes no da percentiles útiles
_MESES_ATRAS = 2  # mes en curso y 2 anteriores
# Columnas con "item" en el nombre que no son clave de línea (F-merc-1c): `tieneItems` es una
# marca sí/no. En minúsculas; una columna nueva con "item" sigue avisando.
_COLUMNAS_ITEM_CONOCIDAS = frozenset({"tieneitems"})
_FORMATOS_FECHA = (
    "%Y-%m-%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
)

_region = functools.lru_cache(maxsize=512)(region_codigo)


def _fuente(anio: int, mes: int) -> str:
    return f"{PREFIJO_FUENTE}{anio:04d}-{mes:02d}"


def _fecha(texto: str) -> datetime | None:
    t = (texto or "").strip()
    for fmt in _FORMATOS_FECHA:
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def _txt(v: str | None, largo: int | None) -> str | None:
    """Texto recortado a `largo` (columnas String(n)); `None` = sin tope (columnas Text)."""
    v = (v or "").strip()
    if not v:
        return None
    return v if largo is None else v[:largo]


def pct(xs: list[float], q: float) -> float:
    """Percentil por rango más cercano (el mismo `pct` del observatorio)."""
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, round(q * (len(xs) - 1))))]


def parsear_mes(mes: str) -> tuple[int, int]:
    """'AAAA-MM' → (año, mes). ValueError si no es un mes válido."""
    y, m = mes.split("-")
    anio, num_mes = int(y), int(m)
    if not 1 <= num_mes <= 12:
        raise ValueError(mes)
    return anio, num_mes


def meses_a_cargar(desde: str | None, hoy: date) -> list[tuple[int, int]]:
    """Meses a procesar, del más antiguo al más nuevo: `desde` hasta hoy, o el actual y 2 anteriores."""
    fin = hoy.year * 12 + hoy.month - 1
    if desde:
        a, m = parsear_mes(desde)
        ini = min(fin, a * 12 + m - 1)
    else:
        ini = fin - _MESES_ATRAS
    return [(i // 12, i % 12 + 1) for i in range(ini, fin + 1)]


def moneda_de_linea(r: dict[str, str]) -> tuple[str, bool]:
    """(moneda, difiere): `monedaItem` si viene, y `TipoMonedaOC` si no.

    `difiere` = ambas vienen y no dan el mismo código (se cuenta en las notas del mes).
    """
    item = (r.get("monedaItem") or "").strip()
    oc = (r.get("TipoMonedaOC") or "").strip()
    if item:
        codigo = moneda_codigo(item)
        return codigo, bool(oc) and moneda_codigo(oc) != codigo
    return moneda_codigo(oc), False


def _fila(r: dict[str, str], mes: str, id_item: str, gr: str, rc: int | None) -> dict[str, Any]:
    moneda, _ = moneda_de_linea(r)
    nz = normalizar(r) if moneda == "CLP" else None
    rp = _region(r.get("RegionProveedor"))
    espec = (r.get("EspecificacionComprador") or "").strip() or (
        r.get("EspecificacionProveedor") or ""
    ).strip()
    return {
        "codigo_oc": (r.get("Codigo") or "").strip()[:50],  # String(50): nunca más largo en la práctica
        "id_item": id_item,
        "mes": mes,
        "fecha_creacion": _fecha(r.get("FechaCreacion", "")),
        "codigo_estado": _txt(r.get("codigoEstado"), 10),
        "moneda": moneda,
        "rut_unidad": _txt(rut(r.get("RutUnidadCompra")), 20),
        "unidad_compra": _txt(r.get("UnidadCompra"), None),
        "organismo": _txt(r.get("OrganismoPublico"), None),
        "sector": _txt(r.get("sector"), 100),
        "comuna_compra": _txt(r.get("CiudadUnidadCompra"), 100),
        "region_compra": rc,
        "rut_proveedor": _txt(rut(r.get("RutSucursal")), 20),
        "proveedor": _txt(r.get("NombreProveedor") or r.get("CodigoProveedor"), None),
        "comuna_proveedor": _txt(r.get("ComunaProveedor"), 100),
        "region_proveedor": rp,
        "proveedor_local": (rp == rc) if rp is not None and rc is not None else None,
        "codigo_producto": _txt(r.get("codigoProductoONU"), 20),
        "producto_generico": _txt(r.get("NombreroductoGenerico"), None),
        "grupo": gr[:100],
        "especificacion": espec[:500] or None,
        "cantidad": num(r.get("cantidad")),
        "unidad_medida": _txt(r.get("UnidadMedida"), 50),
        "precio_neto": num(r.get("precioNeto")),
        "total_linea_neto": num(r.get("totalLineaNeto")),
        "precio_base": nz[1] if nz else None,
        "base": nz[0].rsplit("|", 1)[1] if nz else None,
    }


def _upsert(session: Session, filas: list[dict[str, Any]]) -> None:
    """Upsert sobre `(codigo_oc, id_item)`. Una fila de un mes POSTERIOR no se pisa (queda en el
    mes más reciente). Postgres en producción; SQLite en los tests."""
    crear = sqlite_insert if session.get_bind().dialect.name == "sqlite" else pg_insert
    pk = {"codigo_oc", "id_item"}
    # Tandas chicas: un INSERT multi-fila de 2.000 × 26 columnas pasa el tope de parámetros de SQLite.
    for i in range(0, len(filas), _TANDA_UPSERT):
        tanda = filas[i : i + _TANDA_UPSERT]
        stmt = crear(OcLineaCa).values(tanda)
        stmt = stmt.on_conflict_do_update(
            index_elements=[OcLineaCa.codigo_oc, OcLineaCa.id_item],
            set_={k: stmt.excluded[k] for k in tanda[0] if k not in pk},
            where=OcLineaCa.mes <= stmt.excluded.mes,
        )
        session.execute(stmt)


def cargar_mes(
    session: Session,
    zip_path: str,
    mes: str,
    regiones: list[int],
    columnas_extra: list[str] | None = None,
) -> dict[str, int]:
    """Carga un mes (AAAA-MM) desde el ZIP local. NO commitea: borra y reinserta en la sesión.

    Dos productos de un mismo recorrido: las líneas del comprador en `regiones` y los percentiles
    nacionales (todas las regiones, solo CLP). Devuelve conteos.
    """
    session.execute(delete(OcLineaCa).where(OcLineaCa.mes == mes))
    session.execute(delete(PrecioReferencia).where(PrecioReferencia.mes == mes))
    precios: dict[str, list[float]] = defaultdict(list)
    vistas: set[tuple[str, str]] = set()
    lote: list[dict[str, Any]] = []
    c = {
        "lineas_leidas": 0,
        "lineas_ca_en_alcance": 0,
        "lineas_guardadas": 0,
        "sin_id_item": 0,
        "repetidas_en_el_mes": 0,
        "movidas_de_otro_mes": 0,
        "conservadas_de_mes_posterior": 0,
        "moneda_distinta": 0,
    }
    conjunto = set(regiones)

    def _vaciar() -> None:
        if not lote:
            return
        llaves = [(f["codigo_oc"], f["id_item"]) for f in lote]
        existentes = session.execute(
            select(OcLineaCa.mes, func.count())
            .where(tuple_(OcLineaCa.codigo_oc, OcLineaCa.id_item).in_(llaves), OcLineaCa.mes != mes)
            .group_by(OcLineaCa.mes)
        ).all()
        c["movidas_de_otro_mes"] += sum(n for m, n in existentes if m < mes)
        c["conservadas_de_mes_posterior"] += sum(n for m, n in existentes if m > mes)
        _upsert(session, lote)
        lote.clear()

    for r in iterar_oc_da(zip_path, columnas_extra):
        c["lineas_leidas"] += 1
        if (r["EsCompraAgil"] or "").strip().lower() != "si":
            continue
        gr = grupo_de(r["codigoProductoONU"], r["NombreroductoGenerico"])
        if not gr:
            continue
        c["lineas_ca_en_alcance"] += 1
        moneda, difiere = moneda_de_linea(r)
        c["moneda_distinta"] += difiere
        if moneda == "CLP":
            nz = normalizar(r)
            if nz:
                precios[nz[0]].append(nz[1])
        rc = _region(r["RegionUnidadCompra"])
        if rc is None or rc not in conjunto:
            continue
        codigo = (r["Codigo"] or "").strip()
        id_item = (r["IDItem"] or "").strip()[:30]
        if not codigo or not id_item:
            c["sin_id_item"] += 1
            continue
        if (codigo, id_item) in vistas:
            c["repetidas_en_el_mes"] += 1
            continue
        vistas.add((codigo, id_item))
        lote.append(_fila(r, mes, id_item, gr, rc))
        c["lineas_guardadas"] += 1
        if len(lote) >= _LOTE:
            _vaciar()
    _vaciar()

    refs = [
        {
            "clave": k,
            "mes": mes,
            "n": len(v),
            "p25": pct(v, 0.25),
            "mediana": pct(v, 0.5),
            "p75": pct(v, 0.75),
        }
        for k, v in precios.items()
        if len(v) >= _MIN_PRECIOS
    ]
    for i in range(0, len(refs), _LOTE):
        session.execute(insert(PrecioReferencia), refs[i : i + _LOTE])
    c["referencias"] = len(refs)
    return c


def sync_mercado_oc(
    settings: Settings, engine: Engine, desde: str | None = None, hoy: date | None = None
) -> dict[str, Any]:
    """Job `mercado-oc`: mes en curso y 2 anteriores (o `desde` AAAA-MM hasta hoy).

    Por mes: HEAD → si `Last-Modified` no cambió, nada; si cambió, descarga a un directorio
    temporal, recarga el mes en una transacción y guarda el cursor. Un mes que falla se
    registra y se sigue con el siguiente (R6). 403/404 del ZIP = no disponible todavía.
    """
    if hoy is None:
        anio_h, mes_h = _mes_actual_chile()
        hoy = date(anio_h, mes_h, 1)
    regiones = regiones_activas_engine(engine, settings)
    resultado: dict[str, Any] = {"regiones": regiones, "meses": {}, "errores": []}
    for anio, mes in meses_a_cargar(desde, hoy):
        etiqueta = f"{anio:04d}-{mes:02d}"
        url = url_oc_da(anio, mes, settings.datos_abiertos_base_url)
        ultimo = head_last_modified(url)
        cursor_nuevo = ultimo.isoformat() if ultimo is not None else None
        fuente = _fuente(anio, mes)
        with Session(engine) as session:
            if cursor_nuevo is not None and cursor_nuevo == _leer_cursor(session, fuente):
                resultado["meses"][etiqueta] = "sin cambios"
                continue
        extra: list[str] = []
        try:
            with tempfile.TemporaryDirectory(prefix="mp_oc_da_") as tmp:
                destino = Path(tmp) / f"oc-{anio}-{mes}.zip"
                descargar_zip(url, str(destino))
                with Session(engine) as session:
                    conteos = cargar_mes(session, str(destino), etiqueta, regiones, extra)
                    _guardar_estado(
                        session,
                        fuente,
                        cursor=cursor_nuevo or datetime.now().isoformat(),
                        notas=f"{etiqueta}: " + " ".join(f"{k}={v}" for k, v in conteos.items()),
                    )
                    session.commit()
        except httpx.HTTPStatusError as exc:
            codigo = exc.response.status_code
            _log.warning("mercado-oc: %s respondió %d; sigo con el resto", etiqueta, codigo)
            resultado["meses"][etiqueta] = f"no disponible ({codigo})"
            if codigo not in (403, 404):
                resultado["errores"].append(f"{etiqueta}: HTTP {codigo}")
            continue
        except (httpx.HTTPError, OSError) as exc:
            _log.warning("mercado-oc: %s falló (%s); sigo con el resto", etiqueta, type(exc).__name__)
            resultado["meses"][etiqueta] = "error de red"
            resultado["errores"].append(f"{etiqueta}: {type(exc).__name__}")
            continue
        resultado["meses"][etiqueta] = conteos
        item_cols = [
            c for c in extra if "item" in c.lower() and c.lower() not in _COLUMNAS_ITEM_CONOCIDAS
        ]
        if item_cols:
            _log.warning(
                "mercado-oc: %s trae columnas de ítem %s: evaluar como PK de línea", etiqueta, item_cols
            )
            resultado.setdefault("columnas_item", item_cols)
    return resultado
