"""Enums del dominio — EstadoOportunidad unificado, roles y frecuencias."""

from __future__ import annotations

import enum

from app.core.logging import get_logger

_log = get_logger(__name__)


class RolUsuario(enum.StrEnum):
    ADMIN = "admin"
    USUARIO = "usuario"


class FrecuenciaAlerta(enum.StrEnum):
    INMEDIATA = "inmediata"
    DIGEST = "digest"


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
