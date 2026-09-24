"""Ingesta de Compras Ágiles desde la API v2 de Mercado Público."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.clients.base import MPRateLimitError
from app.clients.mp_v2 import MercadoPublicoV2Client
from app.clients.types import CompraAgilBasica, CompraAgilDetalle, RespuestaListadoV2
from app.core.db_retry import commit_con_retry
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import ahora_utc
from app.models.enums import EstadoOportunidad, estado_ca
from app.models.tables import CaProducto, CompraAgil, SyncState

_log = get_logger(__name__)

_FUENTE = "compra_agil"
# Estados que se ingestan. Se mandan también como filtro a la API (ver _filtros_listado):
# /v2/compra-agil responde 500 si la request sale sin NINGÚN filtro real, y con cursor
# NULL (arranque en frío) cambio_desde es None — así que el filtro de estado es lo que
# garantiza que la request nunca salga "pelada" (docs/09-compra-agil-500.md).
# El filtro local de estado se mantiene como defensa adicional por si la API cambia qué
# acepta en el parámetro `estado`.
_ESTADOS_VALIDOS = {"publicada", "cerrada", "proveedor_seleccionado"}

# --- Ventanas (F-ca-ventana) ------------------------------------------------
# Solapamiento con la ventana anterior, para no perder cambios en el borde.
_SOLAPAMIENTO = timedelta(minutes=5)
# La ventana nunca llega al presente: lo recién cambiado puede no estar indexado.
_MARGEN_PRESENTE = timedelta(minutes=10)
# [V] 22-sep-2026: la API informa total_resultados=10000 exacto (200 × 50) y no
# deja pasar de ahí; una ventana que llega al tope no se puede recorrer entera.
_TOPE_RESULTADOS = 10_000
# Una ventana que llega al tope se parte a la mitad, pero nunca por debajo de esto.
_VENTANA_MINIMA = timedelta(minutes=10)


class CompraAgilIngestaError(RuntimeError):
    """La corrida de CA no puede seguir sin arriesgarse a saltar datos."""


def _filtros_listado(cambio_desde: datetime | None) -> list[str]:
    """Devuelve los estados a filtrar en la API y valida que la request no salga "pelada".

    Garantiza que listar_compra_agil SIEMPRE reciba al menos un filtro real (estado,
    y cambio_desde si hay cursor) — nunca solo paginación, que es la combinación que
    dispara el 500 documentado en docs/09-compra-agil-500.md.
    """
    estados = sorted(_ESTADOS_VALIDOS)
    if not estados and cambio_desde is None:
        raise RuntimeError(
            "sync_incremental CA: la request de listado saldría sin filtro real "
            "(ver docs/09-compra-agil-500.md) — abortando antes de llamar a la API"
        )
    return estados


def upsert_ca_basica(session: Session, item: CompraAgilBasica) -> tuple[CompraAgil, bool]:
    """Upsert básico de Compra Ágil. Devuelve (objeto, es_nueva)."""
    existing = session.get(CompraAgil, item.codigo)
    es_nueva = existing is None

    if existing is None:
        ca = CompraAgil(codigo=item.codigo, creado_en=ahora_utc())
        session.add(ca)
    else:
        ca = existing

    ca.nombre = item.nombre
    ca.estado = estado_ca(item.estado).value
    ca.fecha_publicacion = item.fecha_publicacion
    ca.fecha_cierre = item.fecha_cierre
    ca.fecha_ultimo_cambio = item.fecha_ultimo_cambio
    ca.monto_disponible_clp = item.monto_clp
    ca.region = item.region
    ca.organismo_nombre = item.organismo_nombre
    ca.organismo_rut = item.organismo_rut
    ca.total_ofertas = item.total_ofertas
    ca.actualizado_en = ahora_utc()
    return ca, es_nueva


# Largo de ca_productos.descripcion (String(1000) en app/models/tables.py).
_LARGO_DESCRIPCION_PRODUCTO = 1000


def _completar_desde_detalle(ca: CompraAgil, det: CompraAgilDetalle) -> None:
    """Actualiza los campos básicos de una CA YA EXISTENTE con lo que trae el detalle.

    El detalle no trae todo lo que trae el listado —sin `montos`, por ejemplo— y
    lo que falta llega como None. Un None (o vacío, o DESCONOCIDO) no pisa lo que
    ya hay: mismo criterio que `upsert_basica` de licitaciones con las fechas.
    `total_ofertas` no distingue "falta" de 0, así que se queda con el mayor.
    """
    if det.nombre:
        ca.nombre = det.nombre
    estado = estado_ca(det.estado)
    if estado is not EstadoOportunidad.DESCONOCIDO:
        ca.estado = estado.value
    if det.fecha_publicacion is not None:
        ca.fecha_publicacion = det.fecha_publicacion
    if det.fecha_cierre is not None:
        ca.fecha_cierre = det.fecha_cierre
    if det.fecha_ultimo_cambio is not None:
        ca.fecha_ultimo_cambio = det.fecha_ultimo_cambio
    if det.monto_clp is not None:
        ca.monto_disponible_clp = det.monto_clp
    if det.region is not None:
        ca.region = det.region
    if det.organismo_nombre is not None:
        ca.organismo_nombre = det.organismo_nombre
    if det.organismo_rut is not None:
        ca.organismo_rut = det.organismo_rut
    ca.total_ofertas = max(ca.total_ofertas or 0, det.total_ofertas)


def upsert_ca_detalle(session: Session, det: CompraAgilDetalle) -> None:
    """Actualiza una CA con datos de detalle y reemplaza sus productos.

    Si la CA no existe se crea como desde el listado; si existe, el detalle no
    pisa con None lo que ya había (ver :func:`_completar_desde_detalle`).
    """
    existente = session.get(CompraAgil, det.codigo)
    if existente is None:
        ca, _ = upsert_ca_basica(session, det)
    else:
        ca = existente
        _completar_desde_detalle(ca, det)
    ca.descripcion = det.descripcion
    if det.id_orden_compra is not None:
        ca.id_orden_compra = det.id_orden_compra
    if det.estado_convocatoria is not None:
        ca.estado_convocatoria = det.estado_convocatoria
    ca.actualizado_en = ahora_utc()

    for prod in ca.productos:
        session.delete(prod)
    session.flush()

    for p in det.productos:
        session.add(
            CaProducto(
                ca_codigo=ca.codigo,
                codigo_producto=p.codigo_producto,
                nombre=p.nombre,
                descripcion=p.descripcion[:_LARGO_DESCRIPCION_PRODUCTO],
                cantidad=p.cantidad,
                unidad=p.unidad,
            )
        )


def _leer_cursor(session: Session) -> datetime | None:
    """Lee el cursor de sync_state como datetime UTC, None si no existe."""
    state = session.get(SyncState, _FUENTE)
    if state is None or not state.cursor:
        return None
    try:
        return datetime.fromisoformat(state.cursor).replace(tzinfo=UTC)
    except ValueError:
        _log.warning("Cursor de %s inválido: %r — empezando desde cero", _FUENTE, state.cursor)
        return None


def _guardar_cursor(session: Session, nuevo_cursor_dt: datetime, ok: bool) -> None:
    state = session.get(SyncState, _FUENTE)
    if state is None:
        state = SyncState(fuente=_FUENTE)
        session.add(state)
    ahora = ahora_utc()
    state.ultima_ejecucion = ahora
    if ok:
        state.cursor = nuevo_cursor_dt.replace(tzinfo=None).isoformat()
        state.ultimo_ok = ahora


def _registrar_intento(session: Session, *, ok: bool) -> None:
    """Deja constancia de la corrida en sync_state sin tocar el cursor."""
    session.rollback()
    state = session.get(SyncState, _FUENTE)
    if state is None:
        state = SyncState(fuente=_FUENTE)
        session.add(state)
    ahora = ahora_utc()
    state.ultima_ejecucion = ahora
    if ok:
        state.ultimo_ok = ahora
    try:
        session.commit()
    except Exception:
        session.rollback()


@dataclass
class _Totales:
    nuevas: int = 0
    actualizadas: int = 0
    descartadas: int = 0

    def como_dict(self) -> dict[str, Any]:
        return {
            "nuevas": self.nuevas,
            "actualizadas": self.actualizadas,
            "descartadas": self.descartadas,
        }


def _procesar_pagina(
    session: Session, resp: RespuestaListadoV2, totales: _Totales, contexto: str
) -> datetime | None:
    """Upsert de una página con filtro local de estado y commit con reintento.

    Devuelve el mayor fecha_ultimo_cambio de la página. Si el commit no se pudo
    confirmar, lanza CompraAgilIngestaError: una página que no quedó en la base
    NUNCA se salta para seguir con la siguiente (antes se logueaba y se seguía,
    y el cursor avanzaba igual por encima del hueco).
    """
    nuevas = actualizadas = descartadas = 0
    maximo: datetime | None = None

    def _aplicar() -> None:
        nonlocal nuevas, actualizadas, descartadas, maximo
        nuevas = actualizadas = descartadas = 0
        maximo = None
        for ca in resp.items:
            if ca.estado not in _ESTADOS_VALIDOS:
                descartadas += 1
                continue
            _, es_nueva = upsert_ca_basica(session, ca)
            if es_nueva:
                nuevas += 1
            else:
                actualizadas += 1
            if ca.fecha_ultimo_cambio is not None and (
                maximo is None or ca.fecha_ultimo_cambio > maximo
            ):
                maximo = ca.fecha_ultimo_cambio

    # Commit por página con reintento → el progreso persiste incluso ante una
    # desconexión transitoria o ante un 429 en la página siguiente.
    if not commit_con_retry(session, _aplicar, contexto=contexto):
        _log.error(
            "sync_incremental CA: %s no quedó confirmada tras los reintentos — "
            "corrida cortada sin avanzar el cursor",
            contexto,
        )
        raise CompraAgilIngestaError(
            f"{contexto}: el commit falló tras los reintentos; el cursor no avanzó"
        )
    totales.nuevas += nuevas
    totales.actualizadas += actualizadas
    totales.descartadas += descartadas
    return maximo


# ---------------------------------------------------------------------------
# API pública
# ---------------------------------------------------------------------------


def sync_incremental(
    session: Session,
    v2_client: MercadoPublicoV2Client,
    settings: Settings,
) -> dict[str, Any]:
    """Sincronización incremental de Compras Ágiles.

    - Sin cursor (arranque en frío): una pasada sin cambio_desde, igual que
      antes de F-ca-ventana; el cursor queda en el mayor fecha_ultimo_cambio.
    - Con cursor: recorre el atraso en ventanas acotadas de
      ``ca_ventana_horas`` (ver :func:`_sync_en_ventanas`).

    Ante cualquier error el cursor queda en la última ventana completa, se
    registra el intento en sync_state y se re-lanza.
    """
    cursor = _leer_cursor(session)
    try:
        if cursor is None:
            resultado = _sync_arranque_en_frio(session, v2_client)
        else:
            resultado = _sync_en_ventanas(session, v2_client, settings, cursor.replace(tzinfo=None))
    except MPRateLimitError:
        _log.warning(
            "429 en CA incremental — progreso parcial guardado, cursor en la última ventana completa"
        )
        _registrar_intento(session, ok=False)
        raise
    except Exception:
        _log.error("Error en sync_incremental CA", exc_info=True)
        _registrar_intento(session, ok=False)
        raise

    # Como texto: un dict como único argumento, logging lo toma como mapping de
    # argumentos con nombre, y el _SecretFilter lo rompe al reconstruir args.
    _log.info("sync_incremental CA: %s", ", ".join(f"{k}={v}" for k, v in resultado.items()))
    return resultado


def _sync_arranque_en_frio(session: Session, v2_client: MercadoPublicoV2Client) -> dict[str, Any]:
    """Primera corrida: sin cambio_desde, con filtro de estado, páginas de 50."""
    estados = _filtros_listado(None)
    totales = _Totales()
    nuevo_cursor: datetime | None = None
    pagina = 1
    while True:
        resp = v2_client.listar_compra_agil(
            cambio_desde=None,
            estados=estados,
            tamano_pagina=50,
            numero_pagina=pagina,
        )
        maximo = _procesar_pagina(session, resp, totales, contexto=f"CA pág {pagina}")
        if maximo is not None and (nuevo_cursor is None or maximo > nuevo_cursor):
            nuevo_cursor = maximo
        if pagina >= resp.paginacion.total_paginas:
            break
        pagina += 1

    # Cursor avanza SOLO en éxito total.
    if nuevo_cursor is not None:
        _guardar_cursor(session, nuevo_cursor, ok=True)
        session.commit()
    return totales.como_dict()


def _sync_en_ventanas(
    session: Session,
    v2_client: MercadoPublicoV2Client,
    settings: Settings,
    cursor: datetime,
) -> dict[str, Any]:
    """Recorre el atraso ventana a ventana y avanza el cursor al cerrar cada una.

    Por ventana: desde = cursor − 5 min; hasta = min(desde + ancho, ahora − 10 min).
    Siempre se manda cambio_hasta. Se paginan TODAS las páginas de la ventana y,
    al completarla, el cursor pasa a `hasta` y se commitea. La corrida termina OK
    al alcanzar el presente o, antes de abrir una ventana, al llegar al tope de
    requests; lo que falte lo recorre la corrida siguiente.

    `requests_usadas` cuenta las llamadas de listado de esta corrida; los
    reintentos internos del cliente (504, 429/10500) no se ven desde acá.
    """
    estados = _filtros_listado(cursor)
    ancho = timedelta(hours=settings.ca_ventana_horas)
    tamano = settings.ca_tamano_pagina
    totales = _Totales()
    ventanas = partidas = requests = 0

    def listar(desde: datetime, hasta: datetime, pagina: int) -> RespuestaListadoV2:
        nonlocal requests
        requests += 1
        return v2_client.listar_compra_agil(
            cambio_desde=desde,
            cambio_hasta=hasta,
            estados=estados,
            tamano_pagina=tamano,
            numero_pagina=pagina,
        )

    while True:
        desde = cursor - _SOLAPAMIENTO
        hasta = min(desde + ancho, ahora_utc() - _MARGEN_PRESENTE)
        # `hasta <= cursor`, no `hasta <= desde`: por el solapamiento, al llegar al
        # presente desde queda 5 min antes que hasta y la corrida volvería a pedir
        # la misma ventana sin avanzar hasta agotar el tope de requests.
        if hasta <= cursor:
            break
        if requests >= settings.ca_max_requests_por_corrida:
            _log.info(
                "sync_incremental CA: tope de %d requests por corrida alcanzado; "
                "el resto lo recorre la corrida siguiente",
                settings.ca_max_requests_por_corrida,
            )
            break

        # Página 1; si la ventana llega al tope de resultados, se parte a la mitad.
        resp = listar(desde, hasta, 1)
        while resp.paginacion.total_resultados >= _TOPE_RESULTADOS:
            mitad = (hasta - desde) / 2
            if mitad < _VENTANA_MINIMA:
                raise CompraAgilIngestaError(
                    f"CA: la ventana {desde.isoformat()} → {hasta.isoformat()} sigue en el "
                    f"tope de {_TOPE_RESULTADOS} resultados y no se puede partir por debajo "
                    f"de {_VENTANA_MINIMA}; cursor sin avanzar"
                )
            _log.warning(
                "CA: ventana %s → %s en el tope de %d resultados; se parte a la mitad",
                desde.isoformat(),
                hasta.isoformat(),
                _TOPE_RESULTADOS,
            )
            hasta = desde + mitad
            partidas += 1
            resp = listar(desde, hasta, 1)

        total_informado = resp.paginacion.total_resultados
        codigos: set[str] = set()
        pagina = 1
        while True:
            codigos.update(ca.codigo for ca in resp.items)
            _procesar_pagina(
                session, resp, totales, contexto=f"CA ventana {desde.isoformat()} pág {pagina}"
            )
            if pagina >= resp.paginacion.total_paginas:
                break
            pagina += 1
            resp = listar(desde, hasta, pagina)

        if len(codigos) < total_informado:
            # Si un ítem cambia mientras se pagina, sale de la ventana y corre las
            # páginas: alguno puede quedar sin verse. Solo se avisa. [I]
            _log.warning(
                "CA: ventana %s → %s vio %d códigos únicos y la página 1 informó %d; "
                "posible salto por cambios durante la paginación",
                desde.isoformat(),
                hasta.isoformat(),
                len(codigos),
                total_informado,
            )

        cursor = hasta
        _guardar_cursor(session, cursor, ok=True)
        session.commit()
        ventanas += 1

    _registrar_intento(session, ok=True)
    return {
        **totales.como_dict(),
        "ventanas_completadas": ventanas,
        "requests_usadas": requests,
        "cursor_final": cursor.isoformat(),
        "atraso_horas": round((ahora_utc() - cursor).total_seconds() / 3600, 2),
        "ventanas_partidas": partidas,
    }


def fetch_detalle(
    session: Session,
    v2_client: MercadoPublicoV2Client,
    codigo: str,
) -> bool:
    """Descarga y persiste el detalle de una CA por código. Retorna True si OK."""
    try:
        det = v2_client.detalle_compra_agil(codigo)
        upsert_ca_detalle(session, det)
        session.commit()
        return True
    except Exception as exc:
        _log.warning("Error al pedir detalle CA %s: %s", codigo, exc)
        session.rollback()
        return False
