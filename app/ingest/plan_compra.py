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
from app.core.retencion import tamano_bd, tamano_tabla
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

# Guarda de espacio (F-plan-busqueda-fix, regla 11 CLAUDE.md — Neon 0,5 GB):
# durante el reemplazo del año completo conviven dos lotes (pico ~58% medido
# en el Paso 0 del 27-sep, ver docs/handoff-2026-09-27.md). Mismo límite que
# /salud (500 MB); no se comparte el literal para no acoplar app/ingest a
# app/api.
_LIMITE_BD_BYTES = 500 * 1024 * 1024
_UMBRAL_ESPACIO = 0.70


@dataclass
class ResultadoPlan:
    estado: str  # "ok" | "sin_plan"
    lineas: list[PlanCompraLinea]
    fetched_at: datetime


def _fresco(fetched_at: datetime, ttl_dias: int, ahora: datetime) -> bool:
    return (ahora - fetched_at) < timedelta(days=ttl_dias)


def _lineas_cacheadas(
    session: Session, codigo_entidad: int, agno: int, lote_id: int | None = None
) -> list[PlanCompraLinea]:
    conds = [
        PlanCompraLinea.codigo_entidad == codigo_entidad,
        PlanCompraLinea.agno == agno,
    ]
    if lote_id is not None:
        # Filtra al lote vigente (F-plan-busqueda-fix): si una carga del año
        # completo murió a medias, la tabla puede tener temporalmente dos
        # lotes del mismo año — sin este filtro, filas duplicadas.
        conds.append(PlanCompraLinea.lote_id == lote_id)
    return list(
        session.execute(select(PlanCompraLinea).where(*conds).order_by(PlanCompraLinea.id)).scalars()
    )


def _fuente_anual_completo(agno: int) -> str:
    return f"plan_compra_anual_{agno}"


def _fuente_lote_vigente(agno: int) -> str:
    return f"plan_compra_anual_lote_{agno}"


def lote_vigente(session: Session, agno: int) -> int | None:
    """`lote_id` de la última carga completa y exitosa del PAC del año (job
    `plan-anual`), o None si nunca terminó una carga con éxito.

    Vive en su PROPIA fila de SyncState (campo `cursor`, texto con el id —
    no dentro de `notas` como log de texto libre) para que las consultas
    puedan filtrar de forma confiable aunque la tabla tenga temporalmente dos
    lotes del mismo año (carga interrumpida a medias). No comparte fila con
    `_fuente_anual_completo`, que usa `cursor` para el `Last-Modified` de
    detección de cambios — son dos señales distintas que solo coinciden
    porque ambas se escriben juntas al terminar bien una carga."""
    state = session.get(SyncState, _fuente_lote_vigente(agno))
    if state is None or state.cursor is None:
        return None
    return int(state.cursor)


def _marcar_lote_vigente(session: Session, agno: int, lote_id: int, ahora: datetime) -> None:
    state = session.get(SyncState, _fuente_lote_vigente(agno))
    if state is None:
        state = SyncState(fuente=_fuente_lote_vigente(agno))
        session.add(state)
    state.cursor = str(lote_id)
    state.ultima_ejecucion = ahora
    state.ultimo_ok = ahora


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
        lineas = _lineas_cacheadas(session, codigo_entidad, agno, lote_vigente(session, agno))
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
    instituciones) si `Last-Modified` cambió y no se cargó hace menos de
    `PLAN_ANUAL_DIAS_MIN_ENTRE_CARGAS` días, y reemplaza las filas de ese año
    sin dejar la tabla a medias.

    Idempotente: inserta las filas nuevas con un `lote_id` fresco (streaming
    por lotes de `_LOTE_INSERT`, regla 12) y solo al final borra, en una sola
    sentencia, las filas viejas de ese año (`lote_id` distinto o NULL —
    incluye cualquier caché on-demand previa de este mismo año). Mientras
    dura la carga conviven dos lotes del mismo año en la tabla (regla 12: no
    se puede tener 300k+ filas nuevas en memoria antes de decidir el corte);
    `lote_vigente()` es la que le dice al resto de la app cuál de los dos leer
    (ver `app/plan_busqueda.py` y `get_plan`), y si la corrida muere a medias
    (excepción, SIGTERM, OOM) el lote nuevo se borra o queda huérfano para que
    lo limpie la corrida siguiente — nunca queda como vigente ni se cuenta dos
    veces.

    Corre en GitHub Actions (workflow `catalogos`), no en Render: sin el techo
    de RAM de 512 MB de la regla 12, pero igual se inserta en lotes para no
    acumular 300k+ objetos ORM en la identity map de una sola sesión.
    """
    if agno is None:
        agno = datetime.now(TZ_CHILE).year

    ahora = ahora_utc()
    fuente = _fuente_anual_completo(agno)

    # Huérfanos de una corrida anterior que murió sin llegar al `except` de
    # más abajo (SIGTERM, OOM, reinicio del host): se limpian ANTES de
    # decidir si esta corrida descarga algo, porque si `Last-Modified` no
    # cambió, la función corta más abajo sin llegar a ningún otro punto de
    # limpieza y el huérfano quedaría para siempre.
    vigente_antes = lote_vigente(session, agno)
    cond_huerfanos: list[Any] = [PlanCompraLinea.agno == agno, PlanCompraLinea.lote_id.is_not(None)]
    if vigente_antes is not None:
        cond_huerfanos.append(PlanCompraLinea.lote_id != vigente_antes)
    huerfanos = session.execute(delete(PlanCompraLinea).where(*cond_huerfanos)).rowcount  # type: ignore[attr-defined]
    if huerfanos:
        session.commit()
        _log.warning(
            "sync_plan_anual_completo: agno=%d huérfanos de una corrida anterior borrados=%d",
            agno,
            huerfanos,
        )

    last_modified = head_pac_completo(agno, base_url=settings.plan_compra_pac_base_url)
    if last_modified is None:
        _log.warning("sync_plan_anual_completo: agno=%d sin archivo publicado (403)", agno)
        return {"actualizado": 0, "filas": 0}

    state = session.get(SyncState, fuente)

    if state is not None and state.ultimo_ok is not None:
        dias_desde_ultima_carga = (ahora - state.ultimo_ok).days
        if dias_desde_ultima_carga < settings.plan_anual_dias_min_entre_cargas:
            _log.info(
                "sync_plan_anual_completo: agno=%d omitido por frecuencia (última carga OK hace %d días)",
                agno,
                dias_desde_ultima_carga,
            )
            return {"actualizado": 0, "filas": 0, "omitido_por_frecuencia": 1}

    if state is not None and state.cursor == last_modified:
        _log.info("sync_plan_anual_completo: agno=%d sin cambios (Last-Modified igual)", agno)
        return {"actualizado": 0, "filas": 0}

    tam_bd = tamano_bd(session)
    tam_tabla = tamano_tabla(session, "plan_compra_lineas")
    if (
        tam_bd is not None
        and tam_tabla is not None
        and (tam_bd + tam_tabla) > _LIMITE_BD_BYTES * _UMBRAL_ESPACIO
    ):
        motivo = (
            f"omitido por espacio: bd={tam_bd} + plan_compra_lineas={tam_tabla} "
            f"bytes > {_UMBRAL_ESPACIO:.0%} de {_LIMITE_BD_BYTES} (regla 11)"
        )
        _log.warning("sync_plan_anual_completo: agno=%d %s", agno, motivo)
        if state is None:
            state = SyncState(fuente=fuente)
            session.add(state)
        state.ultima_ejecucion = ahora
        state.notas = motivo
        session.commit()
        return {"actualizado": 0, "filas": 0, "omitido_por_espacio": 1}

    zip_bytes = descargar_pac_completo(agno, base_url=settings.plan_compra_pac_base_url)
    if zip_bytes is None:
        _log.warning("sync_plan_anual_completo: agno=%d sin archivo publicado (403 en la descarga)", agno)
        return {"actualizado": 0, "filas": 0}

    lineas = parse_pac_csv(zip_bytes)
    nuevo_lote = int(ahora.timestamp() * 1000)

    total = 0
    descartadas = 0
    try:
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
    except Exception:
        # No dejar el lote nuevo a medias (regla del prompt): lo que ya se
        # alcanzó a commitear de este intento se borra, el lote vigente
        # anterior queda intacto y visible, y la excepción sigue su curso
        # para que el job se marque como fallido.
        session.rollback()
        session.execute(
            delete(PlanCompraLinea).where(
                PlanCompraLinea.agno == agno,
                PlanCompraLinea.lote_id == nuevo_lote,
            )
        )
        session.commit()
        raise

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
    state.notas = f"filas={total} descartadas={descartadas}"
    _marcar_lote_vigente(session, agno, nuevo_lote, ahora)
    session.commit()

    _log.info(
        "sync_plan_anual_completo: agno=%d filas=%d descartadas=%d",
        agno,
        total,
        descartadas,
    )
    return {"actualizado": 1, "filas": total, "descartadas": descartadas}
