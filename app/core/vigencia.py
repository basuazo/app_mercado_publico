"""Una sola definición de "vigente" para todo el proyecto (F-vigencia).

Se decide por FECHA antes que por estado: el estado puede venir atrasado
aunque F-estados-vencidos lo mejore (`familia_de_estado` sigue siendo la
fuente del badge visual, pero no de esta decisión).

Regla (decisión de Boris, 26-sep):
- familia ABIERTA o DESCONOCIDO **y** `fecha_cierre > ahora`, **o**
- Compra Ágil con `fecha_cierre` NULL, familia ABIERTA **y** `fecha_publicacion`
  de hace `CA_SIN_CIERRE_VIGENCIA_DIAS` días o menos. [V, Paso 0 de F-ca-rubro]
  8.567 de las CA abiertas (58 %) no tienen `fecha_cierre`, y el 90 % de las CA
  cierra antes de ~5 días desde su publicación (p50 47 h, p90 119 h). Sin este
  tope quedarían "vigentes" para siempre. CA sin cierre y sin
  `fecha_publicacion` -> no vigente.
- Todo lo demás (cierre pasado, cerrada, terminales, suspendida) -> no vigente.

`fecha_cierre`/`fecha_publicacion` son naive en UTC (criterio de
almacenamiento de `app/core/tiempo.py`); `ahora` sale de `ahora_utc()`.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models.enums import FamiliaEstado, familia_de_estado

# [V, Paso 0 de F-ca-rubro, 26-sep-2026]: ver docstring del módulo.
CA_SIN_CIERRE_VIGENCIA_DIAS = 7

_FAMILIAS_CON_CIERRE_FUTURO = (FamiliaEstado.ABIERTA, FamiliaEstado.DESCONOCIDO)


def es_vigente(
    estado: object,
    fecha_cierre: datetime | None,
    fuente: str,
    ahora: datetime,
    fecha_publicacion: datetime | None = None,
) -> bool:
    """True si la oportunidad todavía se puede postular hoy. Ver el docstring
    del módulo para la regla completa."""
    familia = familia_de_estado(estado)
    if fecha_cierre is not None:
        return familia in _FAMILIAS_CON_CIERRE_FUTURO and fecha_cierre > ahora
    if fuente != "compras_agiles" or familia != FamiliaEstado.ABIERTA:
        return False
    if fecha_publicacion is None:
        return False
    return fecha_publicacion >= ahora - timedelta(days=CA_SIN_CIERRE_VIGENCIA_DIAS)
