"""Ingesta de licitacion_items (UNSPSC) desde datos abiertos de ChileCompra.

Complementa fetch_detalles_pendientes (app/ingest/licitaciones.py): esa función
gasta 1 request de cuota de la API por licitación solo para obtener sus ítems.
Esta ingesta lee el ZIP mensual público (sin ticket, sin cuota — ver
docs/04-datos-abiertos.md) y completa SOLO licitaciones activas que aún no
tienen ítems. No marca detalle_obtenido ni toca los demás campos del detalle
(Descripcion, MontoEstimado, etc.) — eso sigue viniendo de la API.

Del mismo ZIP salen también las ofertas (competencia) y, desde
F-estados-vencidos, el estado de licitaciones que ya salieron del listado de
activas. `CacheZipsDA` evita bajar el mismo mes dos veces en una corrida.
"""

from __future__ import annotations

import tempfile
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime
from pathlib import Path

import httpx
from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.clients.datos_abiertos import (
    ItemDA,
    OfertaDA,
    descargar_zip,
    head_last_modified,
    stream_estados,
    stream_items,
    stream_ofertas,
    url_lic_da,
)
from app.core.db_retry import commit_con_retry
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.models.enums import (
    ESTADOS_TERMINALES,
    EstadoOportunidad,
    codigo_v1_licitacion,
    estado_licitacion_da,
)
from app.models.tables import (
    Licitacion,
    LicitacionItem,
    OfertaCompetencia,
    OportunidadSeguida,
    SyncState,
)

_log = get_logger(__name__)
_FUENTE = "datos_abiertos_lic"

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


class CacheZipsDA:
    """ZIPs lic-da ya descargados en una corrida, para no bajar dos veces el mismo mes.

    El ciclo nocturno crea UNA y la pasa a ítems, estados y competencia
    (F-estados-vencidos). Vive en un directorio temporal que se borra al cerrar:
    el disco de Render es efímero y nada de acá tiene que sobrevivir (regla 10).
    """

    def __init__(self, settings: Settings) -> None:
        self._base_url = settings.datos_abiertos_base_url
        self._tmp = tempfile.TemporaryDirectory(prefix="mp_datos_abiertos_")
        self._rutas: dict[tuple[int, int], Path] = {}
        self.descargados = 0

    def ruta(self, anio: int, mes: int) -> Path:
        """Ruta local del ZIP del mes; lo descarga si esta corrida aún no lo tiene."""
        clave = (anio, mes)
        if clave not in self._rutas:
            destino = Path(self._tmp.name) / f"lic-{anio}-{mes}.zip"
            descargar_zip(url_lic_da(anio, mes, self._base_url), str(destino))
            self.descargados += 1
            self._rutas[clave] = destino
        return self._rutas[clave]

    def cerrar(self) -> None:
        self._tmp.cleanup()

    def __enter__(self) -> CacheZipsDA:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.cerrar()


def _cache_de_corrida(
    zips: CacheZipsDA | None, settings: Settings
) -> AbstractContextManager[CacheZipsDA]:
    """La caché que viene de afuera (sin cerrarla) o una propia de esta llamada."""
    return nullcontext(zips) if zips is not None else CacheZipsDA(settings)


def _mes_actual_chile() -> tuple[int, int]:
    ahora = datetime.now(TZ_CHILE)
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
    state.ultima_ejecucion = ahora_utc()
    if cursor is not None:
        state.cursor = cursor
        state.ultimo_ok = ahora_utc()
    state.notas = notas


def _guardar_resumen_legacy(session: Session, *, notas: str) -> None:
    state = session.get(SyncState, _FUENTE)
    if state is None:
        state = SyncState(fuente=_FUENTE)
        session.add(state)
    state.ultima_ejecucion = ahora_utc()
    state.ultimo_ok = ahora_utc()
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
    zips: CacheZipsDA | None = None,
) -> dict[str, int]:
    """Completa licitacion_items desde lic-da del mes base y meses anteriores.

    `zips`: caché de la corrida nocturna; sin ella, los ZIP viven solo en esta llamada.
    """
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

    with _cache_de_corrida(zips, settings) as cache:
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

            antes = cache.descargados
            zip_path = cache.ruta(anio_mes, mes_mes)
            descargados += cache.descargados - antes

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


def capturar_competencia(
    session: Session, settings: Settings, zips: CacheZipsDA | None = None
) -> dict[str, int]:
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

    with _cache_de_corrida(zips, settings) as cache:
        fallidos: set[tuple[int, int]] = set()

        def _zip_para(anio: int, mes: int) -> Path | None:
            nonlocal descargados
            if (anio, mes) in fallidos:
                return None
            antes = cache.descargados
            try:
                ruta = cache.ruta(anio, mes)
            except (httpx.HTTPError, OSError) as exc:
                url = url_lic_da(anio, mes, settings.datos_abiertos_base_url)
                _log.warning("capturar_competencia: no se pudo descargar %s: %s", url, exc)
                fallidos.add((anio, mes))
                return None
            descargados += cache.descargados - antes
            return ruta

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


# ---------------------------------------------------------------------------
# Estados de licitaciones (F-estados-vencidos)
# ---------------------------------------------------------------------------

# Cuánto avanzó una licitación. lic-da trae el estado del día en que se generó
# el archivo, que puede ir por detrás de lo que ya nos dio la API: solo se
# acepta un estado que AVANZA, nunca uno que retrocede (cerrada → publicada).
_AVANCE_LICITACION: dict[EstadoOportunidad, int] = {
    EstadoOportunidad.PUBLICADA: 0,
    EstadoOportunidad.SUSPENDIDA: 1,
    EstadoOportunidad.CERRADA: 2,
    **{e: 3 for e in ESTADOS_TERMINALES},
}


def _es_avance(actual: str, nuevo: EstadoOportunidad) -> bool:
    # Un código que no entendemos no borra un estado bueno (regla 6: no romper,
    # tampoco pisar); al revés, cualquier estado conocido reemplaza a desconocido.
    if actual == nuevo.value or nuevo is EstadoOportunidad.DESCONOCIDO:
        return False
    try:
        actual_enum = EstadoOportunidad(actual)
    except ValueError:
        return True
    if actual_enum in ESTADOS_TERMINALES:
        return False
    if actual_enum is EstadoOportunidad.DESCONOCIDO:
        return True
    return _AVANCE_LICITACION.get(nuevo, -1) > _AVANCE_LICITACION.get(actual_enum, -1)


def _aplicar_lote_estados(session: Session, lote: list[tuple[str, EstadoOportunidad]]) -> list[str]:
    """Aplica un lote de estados nuevos; devuelve los códigos que cambiaron."""
    nuevos = dict(lote)
    aplicadas: list[str] = []

    def _aplicar() -> None:
        # Se relee todo en cada intento: tras un rollback lo anterior no cuenta.
        aplicadas.clear()
        for lic in session.execute(
            select(Licitacion).where(Licitacion.codigo.in_(list(nuevos)))
        ).scalars():
            nuevo = nuevos[lic.codigo]
            if not _es_avance(lic.estado, nuevo):
                continue
            lic.estado = nuevo.value
            lic.estado_codigo = codigo_v1_licitacion(nuevo)
            lic.actualizado_en = ahora_utc()
            aplicadas.append(lic.codigo)

    if not commit_con_retry(session, _aplicar, contexto="datos_abiertos lote estados"):
        return []
    return list(aplicadas)


def sync_estados_datos_abiertos(
    session: Session,
    settings: Settings,
    zips: CacheZipsDA | None = None,
) -> tuple[dict[str, int], set[str]]:
    """Actualiza el estado de licitaciones propias no terminales desde lic-da (cuota 0).

    Recorre el mes en curso y `DATOS_ABIERTOS_MESES_ATRAS` anteriores, del más
    nuevo al más viejo. Primera fila por código: el CSV repite la licitación por
    ítem × oferta, y en los meses medidos (2026-6..9) un código nunca aparece en
    dos archivos ni con dos estados. Un mes que no se puede bajar o leer se salta:
    los demás siguen valiendo.

    Devuelve los contadores y los códigos que lic-da dejó en estado TERMINAL: solo
    esos salen del fallback por API. Una que lic-da trae como `cerrada` puede venir
    de un mes que ya no se republica, así que la API la sigue mirando.
    """
    vacio = {
        "actualizadas": 0,
        "vistas": 0,
        "desconocidos": 0,
        "meses_escaneados": 0,
        "meses_fallidos": 0,
        "descargados": 0,
    }
    if not settings.datos_abiertos_habilitado:
        return vacio, set()

    objetivo: dict[str, str] = {
        codigo: estado
        for codigo, estado in session.execute(
            select(Licitacion.codigo, Licitacion.estado).where(
                Licitacion.estado.not_in([e.value for e in ESTADOS_TERMINALES])
            )
        ).all()
    }
    if not objetivo:
        return vacio, set()

    anio, mes = _mes_actual_chile()
    meses = _meses_anteriores(anio, mes, max(settings.datos_abiertos_meses_atras, 0) + 1)
    batch_size = settings.ingest_batch_size
    vistas: set[str] = set()
    resueltas: set[str] = set()
    lote: list[tuple[str, EstadoOportunidad]] = []
    contadores = dict(vacio)

    def _aplicar(lote_: list[tuple[str, EstadoOportunidad]]) -> None:
        aplicadas = _aplicar_lote_estados(session, lote_)
        contadores["actualizadas"] += len(aplicadas)
        terminales = {c for c, e in lote_ if e in ESTADOS_TERMINALES}
        resueltas.update(c for c in aplicadas if c in terminales)

    with _cache_de_corrida(zips, settings) as cache:
        for anio_mes, mes_mes in meses:
            antes = cache.descargados
            try:
                ruta = cache.ruta(anio_mes, mes_mes)
                for fila in stream_estados(str(ruta)):
                    codigo = fila.codigo_externo
                    if codigo in vistas or codigo not in objetivo:
                        continue
                    vistas.add(codigo)
                    nuevo = estado_licitacion_da(fila.codigo_estado)
                    if nuevo is EstadoOportunidad.DESCONOCIDO:
                        # Ya logueado por estado_licitacion_da; queda para la API.
                        contadores["desconocidos"] += 1
                    if not _es_avance(objetivo[codigo], nuevo):
                        continue
                    lote.append((codigo, nuevo))
                    if len(lote) >= batch_size:
                        _aplicar(lote)
                        lote = []
            except Exception as exc:
                # Descarga, ZIP o CSV roto: es un mes de una fuente secundaria, no
                # vale la pena perder los otros por él.
                _log.warning("sync_estados_datos_abiertos: %s-%s no se pudo leer: %s", anio_mes, mes_mes, exc)
                contadores["meses_fallidos"] += 1
            else:
                contadores["meses_escaneados"] += 1
            contadores["descargados"] += cache.descargados - antes

        if lote:
            _aplicar(lote)

    contadores["vistas"] = len(vistas)
    _log.info(
        "sync_estados_datos_abiertos: actualizadas=%d vistas=%d desconocidos=%d meses=%d fallidos=%d",
        contadores["actualizadas"],
        contadores["vistas"],
        contadores["desconocidos"],
        contadores["meses_escaneados"],
        contadores["meses_fallidos"],
    )
    return contadores, resueltas
