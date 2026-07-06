"""Enums del dominio — EstadoOportunidad unificado y roles."""

from __future__ import annotations

import enum

from app.core.logging import get_logger

_log = get_logger(__name__)


class RolUsuario(enum.StrEnum):
    ADMIN = "admin"
    USUARIO = "usuario"


class EstadoAlerta(enum.StrEnum):
    PENDIENTE = "pendiente"
    ENVIADA = "enviada"
    FALLIDA = "fallida"


class EstadoOportunidad(enum.StrEnum):
    # Estados licitaciones
    PUBLICADA = "publicada"
    CERRADA = "cerrada"
    DESIERTA = "desierta"
    ADJUDICADA = "adjudicada"
    REVOCADA = "revocada"
    SUSPENDIDA = "suspendida"
    # Estados OC / CA
    ENVIADA_PROVEEDOR = "enviada_proveedor"
    EN_PROCESO = "en_proceso"
    ACEPTADA = "aceptada"
    CANCELADA = "cancelada"
    RECEPCION_CONFORME = "recepcion_conforme"
    PENDIENTE_RECEPCION = "pendiente_recepcion"
    RECEPCION_PARCIAL = "recepcion_parcial"
    RECEPCION_CONFORME_INCOMPLETA = "recepcion_conforme_incompleta"
    PROVEEDOR_SELECCIONADO = "proveedor_seleccionado"
    DESCONOCIDO = "desconocido"


# Mapeos de código numérico a enum (licitaciones y OC)
_MAP_LICITACION: dict[int, EstadoOportunidad] = {
    5: EstadoOportunidad.PUBLICADA,
    6: EstadoOportunidad.CERRADA,
    7: EstadoOportunidad.DESIERTA,
    8: EstadoOportunidad.ADJUDICADA,
    18: EstadoOportunidad.REVOCADA,
    19: EstadoOportunidad.SUSPENDIDA,
}

_MAP_OC: dict[int, EstadoOportunidad] = {
    4: EstadoOportunidad.ENVIADA_PROVEEDOR,
    5: EstadoOportunidad.EN_PROCESO,
    6: EstadoOportunidad.ACEPTADA,
    9: EstadoOportunidad.CANCELADA,
    12: EstadoOportunidad.RECEPCION_CONFORME,
    13: EstadoOportunidad.PENDIENTE_RECEPCION,
    14: EstadoOportunidad.RECEPCION_PARCIAL,
    15: EstadoOportunidad.RECEPCION_CONFORME_INCOMPLETA,
}

# CA usa strings directamente
_MAP_CA: dict[str, EstadoOportunidad] = {
    "publicada": EstadoOportunidad.PUBLICADA,
    "cerrada": EstadoOportunidad.CERRADA,
    "desierta": EstadoOportunidad.DESIERTA,
    "cancelada": EstadoOportunidad.CANCELADA,
    "proveedor_seleccionado": EstadoOportunidad.PROVEEDOR_SELECCIONADO,
}

# Estados terminales (candidatos para purga de retención)
ESTADOS_TERMINALES: frozenset[EstadoOportunidad] = frozenset(
    {
        EstadoOportunidad.ADJUDICADA,
        EstadoOportunidad.CANCELADA,
        EstadoOportunidad.DESIERTA,
        EstadoOportunidad.REVOCADA,
    }
)


def estado_licitacion(codigo: object) -> EstadoOportunidad:
    try:
        key = int(str(codigo))
    except (TypeError, ValueError):
        _log.warning("Estado licitacion desconocido: %r", codigo)
        return EstadoOportunidad.DESCONOCIDO
    estado = _MAP_LICITACION.get(key)
    if estado is None:
        _log.warning("Estado licitacion sin mapeo: %d", key)
        return EstadoOportunidad.DESCONOCIDO
    return estado


def estado_oc(codigo: object) -> EstadoOportunidad:
    try:
        key = int(str(codigo))
    except (TypeError, ValueError):
        _log.warning("Estado OC desconocido: %r", codigo)
        return EstadoOportunidad.DESCONOCIDO
    estado = _MAP_OC.get(key)
    if estado is None:
        _log.warning("Estado OC sin mapeo: %d", key)
        return EstadoOportunidad.DESCONOCIDO
    return estado


def estado_ca(valor: object) -> EstadoOportunidad:
    if not isinstance(valor, str):
        _log.warning("Estado CA no es string: %r", valor)
        return EstadoOportunidad.DESCONOCIDO
    estado = _MAP_CA.get(valor.lower().strip())
    if estado is None:
        _log.warning("Estado CA sin mapeo: %r", valor)
        return EstadoOportunidad.DESCONOCIDO
    return estado


class EstadoPlanificacionPAC(enum.StrEnum):
    """Estado de una línea del Plan Anual de Compra — dominio propio (F-plan),
    distinto de EstadoOportunidad: no hay relación licitación/OC/CA aquí."""

    PUBLICADO = "publicado"
    DESCONOCIDO = "desconocido"


# En el spike (docs/07-plan-anual.md §5-bis c) el 100 % de las filas observadas
# vino como "Publicado" — cualquier otro valor futuro se trata como desconocido.
_MAP_PLANIFICACION_PAC: dict[str, EstadoPlanificacionPAC] = {
    "publicado": EstadoPlanificacionPAC.PUBLICADO,
}


def estado_planificacion_pac(valor: object) -> EstadoPlanificacionPAC:
    if not isinstance(valor, str):
        _log.warning("Estado planificación PAC no es string: %r", valor)
        return EstadoPlanificacionPAC.DESCONOCIDO
    estado = _MAP_PLANIFICACION_PAC.get(valor.lower().strip())
    if estado is None:
        _log.warning("Estado planificación PAC sin mapeo: %r", valor)
        return EstadoPlanificacionPAC.DESCONOCIDO
    return estado


# Sector del organismo (F-datos) — dominio propio, ver docs/08-datos-organismos.md §3-bis b.
SECTOR_SIN_CLASIFICACION = "Sin clasificación"
ID_SECTOR_SIN_CLASIFICACION = 8


def normalizar_sector(id_sector: object, sector: object) -> tuple[int, str]:
    """Normaliza (idSector, sector) del bulk `/v1/elastic/organization/all`.

    La fuente siempre trae idSector entero 1-8, pero para idSector==8 el
    campo `sector` viene null (sin etiqueta legible) — y cualquier organismo
    del catálogo que no aparezca en el bulk tampoco tiene sector. En ambos
    casos se devuelve el centinela "Sin clasificación", nunca None/null
    (regla 6: no propagar null a la UI). No se hardcodea el texto de los
    7 sectores nombrados: se usa tal cual viene de la fuente.
    """
    try:
        id_sector_int = int(str(id_sector))
    except (TypeError, ValueError):
        _log.warning("normalizar_sector: idSector inválido: %r", id_sector)
        return ID_SECTOR_SIN_CLASIFICACION, SECTOR_SIN_CLASIFICACION
    if not (1 <= id_sector_int <= 8):
        _log.warning("normalizar_sector: idSector fuera de rango 1-8: %d", id_sector_int)
        return ID_SECTOR_SIN_CLASIFICACION, SECTOR_SIN_CLASIFICACION
    nombre = sector.strip() if isinstance(sector, str) and sector.strip() else SECTOR_SIN_CLASIFICACION
    return id_sector_int, nombre


class ValorFeedback(enum.StrEnum):
    """Feedback explícito del usuario sobre un match (F10 parte 2 / F11).

    Solo se REGISTRA aquí; F11 es quien la consumirá como señal de
    entrenamiento para reponderar el matching — este módulo no reordena nada.
    """

    SIRVE = "sirve"
    DESCARTE = "descarte"
