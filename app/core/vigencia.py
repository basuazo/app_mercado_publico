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
from typing import Any

from sqlalchemy import and_, func, or_

from app.models.enums import FamiliaEstado, familia_de_estado, valores_de_estado_en
from app.models.tables import CompraAgil, Licitacion

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


def cierre_vencido(
    estado: object,
    fecha_cierre: datetime | None,
    fuente: str,
    ahora: datetime,
    fecha_publicacion: datetime | None = None,
) -> bool:
    """El estado sigue abierto ("publicada") pero `es_vigente` dice que ya no se puede
    postular: cierre pasado, o CA sin cierre publicada hace más de
    `CA_SIN_CIERRE_VIGENCIA_DIAS` días. La tarjeta y la ficha lo muestran como
    "Publicada · cierre vencido" y no como "Abierta" (F-ficha-modal).

    Sin fecha con la que juzgar (licitación sin cierre, o CA sin cierre ni
    publicación) no se afirma nada: False (regla 6)."""
    if familia_de_estado(estado) != FamiliaEstado.ABIERTA:
        return False
    if fecha_cierre is None and not (fuente == "compras_agiles" and fecha_publicacion is not None):
        return False
    return not es_vigente(estado, fecha_cierre, fuente, ahora, fecha_publicacion)


def fecha_vencimiento(op: Licitacion | CompraAgil, fuente: str) -> datetime | None:
    """Cuándo dejó de poder postularse la oportunidad (F-registro). Una sola regla:

    - con `fecha_cierre`: `fecha_cierre` (aunque el estado haya dejado de ser
      Abierta antes: una CA cancelada con cierre futuro aparece como vencida recién
      cuando ese cierre pasa);
    - CA sin cierre: `fecha_publicacion` + `CA_SIN_CIERRE_VIGENCIA_DIAS`, el momento
      en que `es_vigente` dejó de darla por vigente;
    - sin ninguna de las dos (licitación sin cierre, o CA sin cierre ni publicación):
      None. NO se usa `actualizado_en`: tiene `onupdate` y la ingesta lo reescribe en
      cada refresco, así que el "vencimiento" se correría a hoy indefinidamente. Sin
      fecha confiable no se puede fechar el vencimiento: esas filas no entran a
      "Vencidas recientes"; si están guardadas siguen visibles en "Cerradas".
    """
    if op.fecha_cierre is not None:
        return op.fecha_cierre
    if fuente == "compras_agiles" and op.fecha_publicacion is not None:
        return op.fecha_publicacion + timedelta(days=CA_SIN_CIERRE_VIGENCIA_DIAS)
    return None


def condicion_vencida_en_ventana(fuente: str, desde: datetime, hasta: datetime) -> Any:
    """`desde < fecha_vencimiento <= hasta` en SQL, la misma regla que `fecha_vencimiento`.

    Las fechas se calculan en Python (para una CA sin cierre, la publicación se
    compara con la ventana corrida 7 días): así no hay aritmética de fechas en la
    base y sirve igual en SQLite. Sin fecha confiable (ver `fecha_vencimiento`) la
    fila no entra a la ventana. tests/test_registro.py
    (`test_fecha_vencimiento_python_coincide_con_la_ventana_sql`) la compara con
    `fecha_vencimiento` sobre una matriz de casos."""

    def en(col: Any, a: datetime, b: datetime) -> Any:
        return and_(col > a, col <= b)

    if fuente == "licitaciones":
        return and_(Licitacion.fecha_cierre.is_not(None), en(Licitacion.fecha_cierre, desde, hasta))
    corrimiento = timedelta(days=CA_SIN_CIERRE_VIGENCIA_DIAS)
    return or_(
        and_(CompraAgil.fecha_cierre.is_not(None), en(CompraAgil.fecha_cierre, desde, hasta)),
        and_(
            CompraAgil.fecha_cierre.is_(None),
            CompraAgil.fecha_publicacion.is_not(None),
            en(CompraAgil.fecha_publicacion, desde - corrimiento, hasta - corrimiento),
        ),
    )


def condicion_lic_vigente(ahora: datetime) -> Any:
    """`es_vigente` para licitaciones, escrita en SQL (F-ajustes): cierre no nulo y
    futuro, y familia ABIERTA o DESCONOCIDO. Mismo patrón que `condicion_ca_vigente`;
    tests/test_ajustes_pg.py compara ambas con `es_vigente`."""
    estado = func.lower(func.trim(Licitacion.estado))
    no_validos = valores_de_estado_en(
        [f for f in FamiliaEstado if f not in _FAMILIAS_CON_CIERRE_FUTURO]
    )
    return and_(
        Licitacion.fecha_cierre.is_not(None),
        Licitacion.fecha_cierre > ahora,
        estado.not_in(sorted(no_validos)),
    )


def condicion_ca_vigente(ahora: datetime) -> Any:
    """`es_vigente` para Compra Ágil, escrita en SQL (F-ca-explorar).

    El explorador pagina en la base (regla 12: nunca el universo en Python), así
    que necesita la MISMA decisión como cláusula. Cualquier cambio a `es_vigente`
    se replica acá; tests/test_ca_explorar.py compara ambas sobre una matriz de casos.
    El estado se normaliza como en `familia_de_estado` (minúsculas, sin espacios);
    un valor sin mapear cuenta como DESCONOCIDO, o sea, "no está en las demás familias".
    """
    estado = func.lower(func.trim(CompraAgil.estado))
    con_cierre_futuro_no_valido = valores_de_estado_en(
        [f for f in FamiliaEstado if f not in _FAMILIAS_CON_CIERRE_FUTURO]
    )
    abiertas = valores_de_estado_en([FamiliaEstado.ABIERTA])
    return or_(
        and_(
            CompraAgil.fecha_cierre.is_not(None),
            CompraAgil.fecha_cierre > ahora,
            estado.not_in(sorted(con_cierre_futuro_no_valido)),
        ),
        and_(
            CompraAgil.fecha_cierre.is_(None),
            estado.in_(sorted(abiertas)),
            CompraAgil.fecha_publicacion.is_not(None),
            CompraAgil.fecha_publicacion >= ahora - timedelta(days=CA_SIN_CIERRE_VIGENCIA_DIAS),
        ),
    )
