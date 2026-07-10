"""Ingesta de licitacion_items (UNSPSC) desde datos abiertos de ChileCompra.

Complementa fetch_detalles_pendientes (app/ingest/licitaciones.py): esa función
gasta 1 request de cuota de la API por licitación solo para obtener sus ítems.
Esta ingesta lee el ZIP mensual público (sin ticket, sin cuota — ver
docs/04-datos-abiertos.md) y completa SOLO licitaciones activas que aún no
tienen ítems. No marca detalle_obtenido ni toca los demás campos del detalle
(Descripcion, MontoEstimado, etc.) — eso sigue viniendo de la API.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.clients.datos_abiertos import (
    ItemDA,
    OfertaDA,
    descargar_zip,
    head_last_modified,
    stream_items,
    stream_ofertas,
    url_lic_da,
)
from app.core.db_retry import commit_con_retry
from app.core.logging import get_logger
from app.core.settings import Settings
from app.models.enums import EstadoOportunidad
from app.models.tables import (
    Licitacion,
    LicitacionItem,
    OfertaCompetencia,
    OportunidadSeguida,
    SyncState,
)

_log = get_logger(__name__)
_FUENTE = "datos_abiertos_lic"
_TZ_CHILE = ZoneInfo("America/Santiago")

_VACIO: dict[str, int] = {
    "licitaciones_tocadas": 0,
    "items_insertados": 0,
    "no_unspsc": 0,
    "descargado": 0,
    "meses_escaneados": 0,
}

_VACIO_COMPETENCIA: dict[str, int] = {
    "licitaciones_tocadas": 0,
    "ofertas_insertadas": 0,
    "sin_encontrar": 0,
    "descargados": 0,
}

_MESES_FALLBACK = 4


def _ahora() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _mes_actual_chile() -> tuple[int, int]:
    ahora = datetime.now(_TZ_CHILE)
    return ahora.year, ahora.month


def _fuente_mes(anio: int, mes: int) -> str:
    return f"{_FUENTE}:{anio}-{mes}"


def _leer_cursor(session: Session, fuente: str = _FUENTE, *, legacy_fallback: bool = False) -> str | None:
    state = session.get(SyncState, fuente)
    if state is None and legacy_fallback:
        state = session.get(SyncState, _FUENTE)
    return state.cursor if state else None


def _guardar_estado(session: Session, fuente: str = _FUENTE, *, cursor: str | None, notas: str) -> None:
    state = session.get(SyncState, fuente)
    if state is None:
        state = SyncState(fuente=fuente)
        session.add(state)
    state.ultima_ejecucion = _ahora()
    if cursor is not None:
        state.cursor = cursor
        state.ultimo_ok = _ahora()
    state.notas = notas


def _guardar_resumen_legacy(session: Session, *, notas: str) -> None:
    state = session.get(SyncState, _FUENTE)
    if state is None:
        state = SyncState(fuente=_FUENTE)
        session.add(state)
    state.ultima_ejecucion = _ahora()
    state.ultimo_ok = _ahora()
    state.notas = notas


def _codigos_objetivo(session: Session) -> set[str]:
    """Licitaciones activas (publicada) que todavía no tienen ítems."""
    tiene_items = exists().where(LicitacionItem.licitacion_codigo == Licitacion.codigo)
    rows = session.execute(
        select(Licitacion.codigo)
        .where(Licitacion.estado == EstadoOportunidad.PUBLICADA.value)
        .where(~tiene_items)
    ).scalars()
    return set(rows)


def _es_unspsc_estandar(codigo_producto: str) -> bool:
    return len(codigo_producto) == 8 and codigo_producto.isdigit()


def _insertar_lote(session: Session, lote: list[ItemDA], contexto: str) -> bool:
    def _aplicar() -> None:
        for it in lote:
            session.add(
                LicitacionItem(
                    licitacion_codigo=it.codigo_externo,
                    codigo_producto=it.codigo_producto,
                    nombre=it.nombre,
                    cantidad=it.cantidad,
                    unidad=it.unidad,
                )
            )

    return commit_con_retry(session, _aplicar, contexto=contexto)


def _sync_items_datos_abiertos_legacy(
    session: Session,
    settings: Settings,
    anio: int | None = None,
    mes: int | None = None,
) -> dict[str, int]:
    """Completa licitacion_items de licitaciones activas desde el ZIP mensual.

    Cursor por Last-Modified del blob (regla: nunca re-descargar 14+ MB sin
    necesidad). El archivo del mes vigente se reescribe periódicamente (ver
    spike), así que comparar Last-Modified sí detecta cambios reales.

    Nota de diseño: si el archivo no cambió, se omite por completo aunque
    hayan aparecido licitaciones objetivo nuevas desde la última corrida —
    es el comportamiento pedido (evitar descargas innecesarias); la próxima
    vez que el blob se actualice (a más tardar al día siguiente, según lo
    observado) esas licitaciones quedan cubiertas.
    """
    if not settings.datos_abiertos_habilitado:
        return dict(_VACIO)

    if anio is None or mes is None:
        anio_def, mes_def = _mes_actual_chile()
        anio = anio if anio is not None else anio_def
        mes = mes if mes is not None else mes_def

    url = url_lic_da(anio, mes, settings.datos_abiertos_base_url)
    last_modified = head_last_modified(url)
    cursor_nuevo = last_modified.isoformat() if last_modified is not None else None

    if cursor_nuevo is not None and cursor_nuevo == _leer_cursor(session):
        _log.info("sync_items_datos_abiertos: sin cambios (Last-Modified=%s) — omitido", cursor_nuevo)
        return dict(_VACIO)

    objetivo = _codigos_objetivo(session)
    if not objetivo:
        _guardar_estado(session, cursor=cursor_nuevo, notas="sin licitaciones objetivo")
        session.commit()
        return dict(_VACIO)

    licitaciones_tocadas: set[str] = set()
    items_insertados = 0
    no_unspsc = 0
    vistos: set[tuple[str, str]] = set()
    lote: list[ItemDA] = []
    batch_size = settings.ingest_batch_size

    with tempfile.TemporaryDirectory(prefix="mp_datos_abiertos_") as tmp_dir:
        zip_path = Path(tmp_dir) / f"lic-{anio}-{mes}.zip"
        descargar_zip(url, str(zip_path))

        for item in stream_items(str(zip_path)):
            if item.codigo_externo not in objetivo:
                continue
            clave = (item.codigo_externo, item.codigo_item)
            if clave in vistos:
                continue
            vistos.add(clave)

            if not _es_unspsc_estandar(item.codigo_producto):
                no_unspsc += 1

            lote.append(item)
            if len(lote) >= batch_size:
                if _insertar_lote(session, lote, "datos_abiertos lote items"):
                    licitaciones_tocadas.update(i.codigo_externo for i in lote)
                    items_insertados += len(lote)
                lote = []

        if lote and _insertar_lote(session, lote, "datos_abiertos lote items"):
            licitaciones_tocadas.update(i.codigo_externo for i in lote)
            items_insertados += len(lote)

    notas = f"licitaciones={len(licitaciones_tocadas)} items={items_insertados} no_unspsc={no_unspsc}"
    _guardar_estado(session, cursor=cursor_nuevo, notas=notas)
    session.commit()

    _log.info(
        "sync_items_datos_abiertos: licitaciones=%d items=%d no_unspsc=%d",
        len(licitaciones_tocadas),
        items_insertados,
        no_unspsc,
    )
    return {
        "licitaciones_tocadas": len(licitaciones_tocadas),
        "items_insertados": items_insertados,
        "no_unspsc": no_unspsc,
        "descargado": 1,
    }


def sync_items_datos_abiertos(
    session: Session,
    settings: Settings,
    anio: int | None = None,
    mes: int | None = None,
) -> dict[str, int]:
    """Completa licitacion_items desde lic-da del mes base y meses anteriores."""
    if not settings.datos_abiertos_habilitado:
        return dict(_VACIO)

    if anio is None or mes is None:
        anio_def, mes_def = _mes_actual_chile()
        anio = anio if anio is not None else anio_def
        mes = mes if mes is not None else mes_def

    objetivo = _codigos_objetivo(session)
    if not objetivo:
        _guardar_resumen_legacy(session, notas="sin licitaciones objetivo; meses_escaneados=0")
        session.commit()
        return dict(_VACIO)

    licitaciones_tocadas: set[str] = set()
    items_insertados = 0
    no_unspsc = 0
    batch_size = settings.ingest_batch_size
    descargados = 0
    meses_escaneados = 0
    meses_visitados: list[str] = []
    meses = _meses_anteriores(anio, mes, max(settings.datos_abiertos_meses_atras, 0) + 1)

    with tempfile.TemporaryDirectory(prefix="mp_datos_abiertos_") as tmp_dir:
        for idx, (anio_mes, mes_mes) in enumerate(meses):
            if not objetivo:
                break

            etiqueta_mes = f"{anio_mes}-{mes_mes}"
            meses_visitados.append(etiqueta_mes)
            fuente_mes = _fuente_mes(anio_mes, mes_mes)
            url = url_lic_da(anio_mes, mes_mes, settings.datos_abiertos_base_url)
            last_modified = head_last_modified(url)
            cursor_nuevo = last_modified.isoformat() if last_modified is not None else None
            cursor_actual = _leer_cursor(session, fuente_mes, legacy_fallback=idx == 0)

            if cursor_nuevo is not None and cursor_nuevo == cursor_actual:
                nota_mes = f"{etiqueta_mes}: sin cambios"
                _guardar_estado(session, fuente_mes, cursor=cursor_nuevo, notas=nota_mes)
                session.commit()
                continue

            meses_escaneados += 1
            tocadas_mes: set[str] = set()
            items_mes = 0
            no_unspsc_mes = 0
            vistos: set[tuple[str, str]] = set()
            lote: list[ItemDA] = []

            zip_path = Path(tmp_dir) / f"lic-{anio_mes}-{mes_mes}.zip"
            descargar_zip(url, str(zip_path))
            descargados += 1

            for item in stream_items(str(zip_path)):
                if item.codigo_externo not in objetivo:
                    continue
                clave = (item.codigo_externo, item.codigo_item)
                if clave in vistos:
                    continue
                vistos.add(clave)

                if not _es_unspsc_estandar(item.codigo_producto):
                    no_unspsc += 1
                    no_unspsc_mes += 1

                lote.append(item)
                if len(lote) >= batch_size:
                    if _insertar_lote(session, lote, "datos_abiertos lote items"):
                        codigos_lote = {i.codigo_externo for i in lote}
                        tocadas_mes.update(codigos_lote)
                        licitaciones_tocadas.update(codigos_lote)
                        items_insertados += len(lote)
                        items_mes += len(lote)
                    lote = []

            if lote and _insertar_lote(session, lote, "datos_abiertos lote items"):
                codigos_lote = {i.codigo_externo for i in lote}
                tocadas_mes.update(codigos_lote)
                licitaciones_tocadas.update(codigos_lote)
                items_insertados += len(lote)
                items_mes += len(lote)

            objetivo.difference_update(tocadas_mes)
            nota_mes = (
                f"{etiqueta_mes}: licitaciones={len(tocadas_mes)} "
                f"items={items_mes} no_unspsc={no_unspsc_mes}"
            )
            _guardar_estado(session, fuente_mes, cursor=cursor_nuevo, notas=nota_mes)
            session.commit()

    notas = (
        f"meses={','.join(meses_visitados)} meses_escaneados={meses_escaneados} "
        f"descargados={descargados} licitaciones={len(licitaciones_tocadas)} "
        f"items={items_insertados} no_unspsc={no_unspsc}"
    )
    _guardar_resumen_legacy(session, notas=notas)
    session.commit()

    _log.info(
        "sync_items_datos_abiertos: licitaciones=%d items=%d no_unspsc=%d descargados=%d",
        len(licitaciones_tocadas),
        items_insertados,
        no_unspsc,
        descargados,
    )
    return {
        "licitaciones_tocadas": len(licitaciones_tocadas),
        "items_insertados": items_insertados,
        "no_unspsc": no_unspsc,
        "descargado": descargados,
        "meses_escaneados": meses_escaneados,
    }


# ---------------------------------------------------------------------------
# Análisis de competencia (F-competencia)
# ---------------------------------------------------------------------------


def _licitaciones_competencia_objetivo(session: Session) -> list[tuple[str, datetime | None]]:
    """Licitaciones SEGUIDAS (no archivadas), adjudicadas, sin ofertas aún capturadas."""
    tiene_ofertas = exists().where(OfertaCompetencia.licitacion_codigo == Licitacion.codigo)
    es_seguida = exists().where(
        OportunidadSeguida.fuente == "licitaciones",
        OportunidadSeguida.codigo_oportunidad == Licitacion.codigo,
        OportunidadSeguida.archivada.is_(False),
    )
    rows = session.execute(
        select(Licitacion.codigo, Licitacion.fecha_publicacion)
        .where(Licitacion.estado == EstadoOportunidad.ADJUDICADA.value)
        .where(es_seguida)
        .where(~tiene_ofertas)
    ).all()
    return [(codigo, fecha_pub) for codigo, fecha_pub in rows]


def _meses_anteriores(anio: int, mes: int, n: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    a, m = anio, mes
    for _ in range(n):
        out.append((a, m))
        m -= 1
        if m == 0:
            m = 12
            a -= 1
    return out


def _candidatos_mes(
    fecha_publicacion: datetime | None, mes_actual: tuple[int, int]
) -> list[tuple[int, int]]:
    """Meses a intentar, en orden: el de `fecha_publicacion` (si existe) primero,
    luego el actual y los ~3 anteriores (regla de fallback — ver docs/05-competencia.md
    §0: fecha_publicacion suele venir NULL para licitaciones adjudicadas)."""
    candidatos: list[tuple[int, int]] = []
    if fecha_publicacion is not None:
        candidatos.append((fecha_publicacion.year, fecha_publicacion.month))
    for am in _meses_anteriores(mes_actual[0], mes_actual[1], _MESES_FALLBACK):
        if am not in candidatos:
            candidatos.append(am)
    return candidatos


def _insertar_lote_ofertas(session: Session, lote: list[OfertaDA], contexto: str) -> bool:
    def _aplicar() -> None:
        for o in lote:
            session.add(
                OfertaCompetencia(
                    licitacion_codigo=o.codigo_externo,
                    codigo_item=o.codigo_item,
                    rut_proveedor=o.rut_proveedor,
                    nombre_proveedor=o.nombre_proveedor,
                    monto_unitario=o.monto_unitario,
                    monto_linea_adjudicada=o.monto_linea_adjudicada,
                    cantidad=o.cantidad,
                    seleccionada=o.seleccionada,
                )
            )

    return commit_con_retry(session, _aplicar, contexto=contexto)


def capturar_competencia(session: Session, settings: Settings) -> dict[str, int]:
    """Captura las ofertas (lic-da) de licitaciones SEGUIDAS y adjudicadas, sin cuota de API.

    Idempotente: una licitación que ya tiene OfertaCompetencia no se vuelve a
    procesar. Como fecha_publicacion suele venir NULL (ver docs/05-competencia.md
    §0), se escanean hasta `_MESES_FALLBACK` meses recientes de lic-da hasta
    encontrar el CodigoExterno; si no aparece en ninguno, se deja para la
    siguiente corrida (no es un error — el archivo del mes puede no estar
    publicado aún).
    """
    if not settings.datos_abiertos_habilitado:
        return dict(_VACIO_COMPETENCIA)

    objetivo = _licitaciones_competencia_objetivo(session)
    if not objetivo:
        return dict(_VACIO_COMPETENCIA)

    mes_actual = _mes_actual_chile()
    batch_size = settings.ingest_batch_size

    licitaciones_tocadas: set[str] = set()
    ofertas_insertadas = 0
    sin_encontrar = 0
    descargados = 0

    with tempfile.TemporaryDirectory(prefix="mp_competencia_") as tmp_dir:
        cache: dict[tuple[int, int], Path | None] = {}

        def _zip_para(anio: int, mes: int) -> Path | None:
            nonlocal descargados
            clave = (anio, mes)
            if clave in cache:
                return cache[clave]
            url = url_lic_da(anio, mes, settings.datos_abiertos_base_url)
            destino = Path(tmp_dir) / f"comp-{anio}-{mes}.zip"
            try:
                descargar_zip(url, str(destino))
                descargados += 1
                cache[clave] = destino
            except (httpx.HTTPError, OSError) as exc:
                _log.warning("capturar_competencia: no se pudo descargar %s: %s", url, exc)
                cache[clave] = None
            return cache[clave]

        for codigo, fecha_publicacion in objetivo:
            encontrado = False
            for anio, mes in _candidatos_mes(fecha_publicacion, mes_actual):
                zip_path = _zip_para(anio, mes)
                if zip_path is None:
                    continue

                vistos: set[tuple[str, str]] = set()
                lote: list[OfertaDA] = []
                hubo_filas = False
                for oferta in stream_ofertas(str(zip_path), codigo):
                    hubo_filas = True
                    clave = (oferta.codigo_item, oferta.rut_proveedor)
                    if clave in vistos:
                        continue
                    vistos.add(clave)
                    lote.append(oferta)
                    if len(lote) >= batch_size:
                        if _insertar_lote_ofertas(session, lote, "competencia lote ofertas"):
                            ofertas_insertadas += len(lote)
                        lote = []
                if lote and _insertar_lote_ofertas(session, lote, "competencia lote ofertas"):
                    ofertas_insertadas += len(lote)

                if hubo_filas:
                    licitaciones_tocadas.add(codigo)
                    encontrado = True
                    break

            if not encontrado:
                sin_encontrar += 1
                _log.warning(
                    "capturar_competencia: %s no encontrada en los últimos %d meses de lic-da",
                    codigo,
                    _MESES_FALLBACK,
                )

    _log.info(
        "capturar_competencia: licitaciones=%d ofertas=%d sin_encontrar=%d descargados=%d",
        len(licitaciones_tocadas),
        ofertas_insertadas,
        sin_encontrar,
        descargados,
    )
    return {
        "licitaciones_tocadas": len(licitaciones_tocadas),
        "ofertas_insertadas": ofertas_insertadas,
        "sin_encontrar": sin_encontrar,
        "descargados": descargados,
    }
