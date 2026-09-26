"""Servicio on-demand del Plan Anual de Compra (PAC) — F-plan.

A diferencia del resto de app/ingest (sincronizaciones programadas), esto se
invoca directamente desde la ruta HTML cuando el usuario consulta una
institución/año: descarga y cachea solo lo que se pide (ver
docs/07-plan-anual.md §2 y §6). TTL en vez de comparar Last-Modified vía HEAD
(opción mencionada en el spike): el PAC se regenera ~mensualmente (§5-bis g),
así que un TTL de ~30 días es equivalente en la práctica y evita un round-trip
extra en cada consulta.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, or_, select
from sqlalchemy.orm import Session

from app.clients.plan_compra import (
    descargar_pac,
    descargar_pac_completo,
    head_pac_completo,
    listar_instituciones,
    listar_organismos_sector,
    parse_pac_csv,
)
from app.core.logging import get_logger
from app.core.settings import Settings
from app.core.tiempo import TZ_CHILE, ahora_utc
from app.models.enums import (
    ID_SECTOR_SIN_CLASIFICACION,
    SECTOR_SIN_CLASIFICACION,
    estado_planificacion_pac,
    normalizar_sector,
)
from app.models.tables import InstitucionPAC, PlanCompraLinea, PlanCompraSync, SyncState

_log = get_logger(__name__)

_FUENTE_INSTITUCIONES = "plan_compra_instituciones"
_FUENTE_SECTORES = "plan_compra_sectores"
# Tamaño de lote de INSERT del job plan-anual (F-plan-busqueda): acota cuántas
# filas viven en un solo INSERT/commit — 300k filas de una sola vez sería un
# solo commit gigante; en chunks el progreso queda parcialmente visible y una
# corrida interrumpida no pierde todo el trabajo (aunque sí requiere reintento
# completo, ver sync_plan_anual_completo).
_LOTE_INSERT = 5000


@dataclass
class ResultadoPlan:
    estado: str  # "ok" | "sin_plan"
    lineas: list[PlanCompraLinea]
    fetched_at: datetime


def _fresco(fetched_at: datetime, ttl_dias: int, ahora: datetime) -> bool:
    return (ahora - fetched_at) < timedelta(days=ttl_dias)


def _lineas_cacheadas(session: Session, codigo_entidad: int, agno: int) -> list[PlanCompraLinea]:
    return list(
        session.execute(
            select(PlanCompraLinea)
            .where(
                PlanCompraLinea.codigo_entidad == codigo_entidad,
                PlanCompraLinea.agno == agno,
            )
            .order_by(PlanCompraLinea.id)
        ).scalars()
    )


def _fuente_anual_completo(agno: int) -> str:
    return f"plan_compra_anual_{agno}"


def anio_completo_cargado(session: Session, agno: int) -> bool:
    """True si el job `plan-anual` ya cargó el año completo (todas las
    instituciones) al menos una vez — desde ahí, get_plan de ese año lee
    directo de la tabla, sin descarga on-demand (ver docs/prompt-F-plan-busqueda.md)."""
    state = session.get(SyncState, _fuente_anual_completo(agno))
    return state is not None and state.ultimo_ok is not None


def get_plan(
    session: Session,
    settings: Settings,
    codigo_entidad: int,
    agno: int,
) -> ResultadoPlan:
    """Sirve el PAC de una institución/año desde caché si está fresco; si no,
    descarga, parsea y upserta de forma idempotente (borra+inserta ese par).

    403 del cliente (sin plan publicado) se cachea también con TTL como
    estado='sin_plan' para no re-pegar a la fuente en cada consulta repetida.

    Si el año ya tiene el PAC completo cargado (job `plan-anual`), lee
    directamente de `plan_compra_lineas` sin llamar a la red: esa carga es
    autoritativa y más fresca que cualquier caché on-demand previo.
    """
    if anio_completo_cargado(session, agno):
        state = session.get(SyncState, _fuente_anual_completo(agno))
        assert state is not None and state.ultimo_ok is not None  # anio_completo_cargado ya lo garantiza
        lineas = _lineas_cacheadas(session, codigo_entidad, agno)
        return ResultadoPlan(
            estado="ok" if lineas else "sin_plan",
            lineas=lineas,
            fetched_at=state.ultimo_ok,
        )

    ahora = ahora_utc()
    sync = session.get(PlanCompraSync, (codigo_entidad, agno))
    if sync is not None and _fresco(sync.fetched_at, settings.plan_compra_ttl_dias, ahora):
        if sync.estado == "ok":
            return ResultadoPlan(
                estado="ok",
                lineas=_lineas_cacheadas(session, codigo_entidad, agno),
                fetched_at=sync.fetched_at,
            )
        return ResultadoPlan(estado="sin_plan", lineas=[], fetched_at=sync.fetched_at)

    zip_bytes = descargar_pac(
        codigo_entidad, agno, base_url=settings.plan_compra_pac_base_url
    )

    if zip_bytes is None:
        _registrar_sync(session, codigo_entidad, agno, estado="sin_plan", n_filas=0, fetched_at=ahora)
        session.commit()
        return ResultadoPlan(estado="sin_plan", lineas=[], fetched_at=ahora)

    lineas_da = parse_pac_csv(zip_bytes)

    session.execute(
        delete(PlanCompraLinea).where(
            PlanCompraLinea.codigo_entidad == codigo_entidad,
            PlanCompraLinea.agno == agno,
        )
    )
    nuevas: list[PlanCompraLinea] = []
    for linea in lineas_da:
        fila = PlanCompraLinea(
            codigo_entidad=codigo_entidad,
            agno=agno,
            institucion_nombre=linea.institucion_nombre,
            codigo_producto=linea.codigo_producto,
            descripcion_producto=linea.descripcion_producto,
            cantidad_estimada=linea.cantidad_estimada,
            monto_unitario_clp=linea.monto_unitario_clp,
            monto_estimado_clp=linea.monto_estimado_clp,
            mes_estimado=linea.mes_estimado,
            trimestre_estimado=linea.trimestre_estimado,
            estado_planificacion=estado_planificacion_pac(linea.estado_planificacion).value,
        )
        session.add(fila)
        nuevas.append(fila)

    _registrar_sync(session, codigo_entidad, agno, estado="ok", n_filas=len(nuevas), fetched_at=ahora)
    session.commit()
    _log.info(
        "get_plan: codigo_entidad=%d agno=%d filas=%d (descargado)",
        codigo_entidad,
        agno,
        len(nuevas),
    )
    return ResultadoPlan(estado="ok", lineas=nuevas, fetched_at=ahora)


def _registrar_sync(
    session: Session,
    codigo_entidad: int,
    agno: int,
    *,
    estado: str,
    n_filas: int,
    fetched_at: datetime,
) -> None:
    sync = session.get(PlanCompraSync, (codigo_entidad, agno))
    if sync is None:
        sync = PlanCompraSync(codigo_entidad=codigo_entidad, agno=agno)
        session.add(sync)
    sync.estado = estado
    sync.n_filas = n_filas
    sync.fetched_at = fetched_at


def sync_instituciones_pac(session: Session, settings: Settings) -> int:
    """Refresca el catálogo de instituciones si el caché está vencido (TTL largo,
    reutiliza sync_state — el catálogo cambia con tan poca frecuencia que no
    necesita su propia tabla de control). Devuelve cuántas se cachearon (0 si
    no hubo refresh)."""
    ahora = ahora_utc()
    state = session.get(SyncState, _FUENTE_INSTITUCIONES)
    if state is not None and state.ultimo_ok is not None and _fresco(
        state.ultimo_ok, settings.plan_compra_ttl_dias, ahora
    ):
        return 0

    instituciones = listar_instituciones(kpi_url=settings.plan_compra_kpi_url)

    session.execute(delete(InstitucionPAC))
    for inst in instituciones:
        session.add(
            InstitucionPAC(
                codigo_entidad=inst.codigo_entidad,
                razon_social=inst.razon_social,
                rut=inst.rut,
            )
        )

    if state is None:
        state = SyncState(fuente=_FUENTE_INSTITUCIONES)
        session.add(state)
    state.ultima_ejecucion = ahora
    state.ultimo_ok = ahora
    state.notas = f"instituciones={len(instituciones)}"
    session.commit()

    _log.info("sync_instituciones_pac: instituciones=%d", len(instituciones))
    return len(instituciones)


def sync_sectores_organismos(session: Session, settings: Settings) -> int:
    """Puebla `InstitucionPAC.sector`/`id_sector` desde el bulk de datos
    abiertos (ver docs/08-datos-organismos.md §3-bis a). TTL largo, mismo
    patrón de `sync_state` que `sync_instituciones_pac`.

    Upsert idempotente por `codigo_entidad` (UPDATE en sitio, no
    delete+insert): re-ejecutar no duplica nada. Los organismos del catálogo
    que NO aparezcan en el bulk quedan con el centinela "Sin clasificación"
    (regla 6 — nunca NULL sin manejar).

    `sync_instituciones_pac` reemplaza el catálogo completo (delete+insert)
    cuando SU propio TTL vence, lo que deja `sector`/`id_sector` en NULL para
    las filas nuevas aunque el TTL de este servicio siga fresco — por eso se
    fuerza un refresh si hay alguna fila sin clasificar, sin esperar al
    vencimiento del TTL propio. Debe llamarse junto a/después de
    `sync_instituciones_pac`.
    """
    ahora = ahora_utc()
    state = session.get(SyncState, _FUENTE_SECTORES)
    hay_sin_clasificar = (
        session.execute(select(InstitucionPAC.codigo_entidad).where(InstitucionPAC.id_sector.is_(None)).limit(1)).first()
        is not None
    )
    if (
        state is not None
        and state.ultimo_ok is not None
        and _fresco(state.ultimo_ok, settings.plan_compra_ttl_dias, ahora)
        and not hay_sin_clasificar
    ):
        return 0

    organismos = listar_organismos_sector(bulk_url=settings.plan_compra_sectores_bulk_url)
    por_entcode = {o.entcode: o for o in organismos}

    filas = session.execute(select(InstitucionPAC)).scalars().all()
    actualizadas = 0
    for fila in filas:
        encontrado = por_entcode.get(fila.codigo_entidad)
        if encontrado is not None:
            id_sector, sector = normalizar_sector(encontrado.id_sector, encontrado.sector)
        else:
            id_sector, sector = ID_SECTOR_SIN_CLASIFICACION, SECTOR_SIN_CLASIFICACION
        fila.id_sector = id_sector
        fila.sector = sector
        actualizadas += 1

    if state is None:
        state = SyncState(fuente=_FUENTE_SECTORES)
        session.add(state)
    state.ultima_ejecucion = ahora
    state.ultimo_ok = ahora
    state.notas = f"organismos_bulk={len(organismos)} actualizadas={actualizadas}"
    session.commit()

    _log.info(
        "sync_sectores_organismos: bulk=%d actualizadas=%d",
        len(organismos),
        actualizadas,
    )
    return actualizadas


# ---------------------------------------------------------------------------
# Ingesta del año completo (F-plan-busqueda) — job `plan-anual`, sin cuota
# ---------------------------------------------------------------------------


def _fila_pac_dict(linea: Any, agno: int, lote_id: int, creado_en: datetime) -> dict[str, Any] | None:
    """Convierte una LineaPAC del cliente en el dict de INSERT.

    `codigo_entidad` sale de `rut_institucion` (la errata oficial: es el
    codigoEntidad, no un RUT, ver docs/07-plan-anual.md §5-bis c). Fila
    descartada con log si no es un entero válido — no debe romper la carga del
    resto del archivo (regla 6, parseo defensivo)."""
    try:
        codigo_entidad = int(linea.rut_institucion)
    except (TypeError, ValueError):
        _log.warning(
            "sync_plan_anual_completo: fila descartada, rut_institucion no es un entero: %r",
            linea.rut_institucion,
        )
        return None
    return {
        "codigo_entidad": codigo_entidad,
        "agno": agno,
        "institucion_nombre": linea.institucion_nombre,
        "codigo_producto": linea.codigo_producto,
        "descripcion_producto": linea.descripcion_producto,
        "cantidad_estimada": linea.cantidad_estimada,
        "monto_unitario_clp": linea.monto_unitario_clp,
        "monto_estimado_clp": linea.monto_estimado_clp,
        "mes_estimado": linea.mes_estimado,
        "trimestre_estimado": linea.trimestre_estimado,
        "estado_planificacion": estado_planificacion_pac(linea.estado_planificacion).value,
        "creado_en": creado_en,
        "lote_id": lote_id,
    }


def sync_plan_anual_completo(
    session: Session,
    settings: Settings,
    agno: int | None = None,
) -> dict[str, int]:
    """Job `plan-anual`: descarga el ZIP completo del año (todas las
    instituciones) si `Last-Modified` cambió, y reemplaza las filas de ese año
    sin dejar la tabla a medias.

    Idempotente: inserta las filas nuevas con un `lote_id` fresco (streaming
    por lotes de `_LOTE_INSERT`, regla 12) y solo al final borra, en una sola
    sentencia, las filas viejas de ese año (`lote_id` distinto o NULL —
    incluye cualquier caché on-demand previa de este mismo año). El corte lo
    da un índice (agno, codigo_entidad); no hay ventana sin datos porque las
    filas nuevas ya están insertadas antes del borrado.

    Corre en GitHub Actions (workflow `catalogos`), no en Render: sin el techo
    de RAM de 512 MB de la regla 12, pero igual se inserta en lotes para no
    acumular 300k+ objetos ORM en la identity map de una sola sesión.
    """
    if agno is None:
        agno = datetime.now(TZ_CHILE).year

    fuente = _fuente_anual_completo(agno)
    last_modified = head_pac_completo(agno, base_url=settings.plan_compra_pac_base_url)
    if last_modified is None:
        _log.warning("sync_plan_anual_completo: agno=%d sin archivo publicado (403)", agno)
        return {"actualizado": 0, "filas": 0}

    state = session.get(SyncState, fuente)
    if state is not None and state.cursor == last_modified:
        _log.info("sync_plan_anual_completo: agno=%d sin cambios (Last-Modified igual)", agno)
        return {"actualizado": 0, "filas": 0}

    zip_bytes = descargar_pac_completo(agno, base_url=settings.plan_compra_pac_base_url)
    if zip_bytes is None:
        _log.warning("sync_plan_anual_completo: agno=%d sin archivo publicado (403 en la descarga)", agno)
        return {"actualizado": 0, "filas": 0}

    lineas = parse_pac_csv(zip_bytes)
    ahora = ahora_utc()
    nuevo_lote = int(ahora.timestamp() * 1000)

    total = 0
    descartadas = 0
    buffer: list[dict[str, Any]] = []
    for linea in lineas:
        fila = _fila_pac_dict(linea, agno, nuevo_lote, ahora)
        if fila is None:
            descartadas += 1
            continue
        buffer.append(fila)
        if len(buffer) >= _LOTE_INSERT:
            session.execute(insert(PlanCompraLinea), buffer)
            session.commit()
            total += len(buffer)
            buffer = []
    if buffer:
        session.execute(insert(PlanCompraLinea), buffer)
        session.commit()
        total += len(buffer)

    session.execute(
        delete(PlanCompraLinea).where(
            PlanCompraLinea.agno == agno,
            or_(PlanCompraLinea.lote_id.is_(None), PlanCompraLinea.lote_id != nuevo_lote),
        )
    )

    if state is None:
        state = SyncState(fuente=fuente)
        session.add(state)
    state.cursor = last_modified
    state.ultima_ejecucion = ahora
    state.ultimo_ok = ahora
    state.notas = f"filas={total} descartadas={descartadas} lote_id={nuevo_lote}"
    session.commit()

    _log.info(
        "sync_plan_anual_completo: agno=%d filas=%d descartadas=%d",
        agno,
        total,
        descartadas,
    )
    return {"actualizado": 1, "filas": total, "descartadas": descartadas}
