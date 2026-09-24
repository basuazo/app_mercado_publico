"""Un solo "ahora" y una sola conversión de husos para todo el proyecto.

Criterio de almacenamiento (F-fecha-cierre)
-------------------------------------------
Las columnas ``DateTime`` del modelo NO llevan ``timezone=True``, así que en la
base se guarda SIEMPRE **naive en UTC**. Este módulo es el único de ``app/`` que
importa ``ZoneInfo``: un ``ZoneInfo`` en cualquier otro archivo es un error, y
también lo es un ``datetime.now(...)`` que no salga de :func:`ahora_utc`.

Quienes necesitan el DÍA CALENDARIO chileno —el corte de cuota de
``app/clients/base.py``, la ventana 22:00–07:00 (:func:`en_ventana_nocturna`, acá)
y ``app/ingest/datos_abiertos.py``, el digest de ``app/alerts/email.py``—
importan :data:`TZ_CHILE` de acá. Eso es otra cosa que convertir un instante y
está bien: no pasan por :func:`a_utc_naive`.

Qué está verificado y qué se supone
-----------------------------------
- **[V]** Lo que hace el código: leído en ``parse_fecha_v1``, ``parse_fecha_iso``,
  ``_fecha_a_dt`` y el filtro de candidatos de ``app/matching/engine.py``.
- **[I]** A qué huso se refieren los valores SIN offset que manda la API de
  Mercado Público. Aquí se interpretan como hora de **Chile continental**
  (``America/Santiago``), porque es una API del Estado de Chile publicando
  plazos chilenos. Es una SUPOSICIÓN, no un hecho: la verificación contra la
  fuente primaria es el Paso 0 de F-fecha-cierre
  (``scripts/smoke_test.py --fechas``, la corre el humano). Reglas 20 y 23.

Si el Paso 0 muestra que la API manda hora de UTC sin marcarla, el único cambio
necesario es ``_TZ_SIN_OFFSET``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

TZ_CHILE = ZoneInfo("America/Santiago")

# Huso con el que se interpreta un valor de la API que llega SIN offset. [I]
_TZ_SIN_OFFSET = TZ_CHILE


def en_ventana_nocturna(now_fn: Callable[..., datetime] | None = None) -> bool:
    """True si la hora actual en Chile está entre 22:00 y 07:00 (regla 5).

    `now_fn` es inyectable para tests (ej. lambda tz: frozen_datetime).
    """
    ahora = now_fn(TZ_CHILE) if now_fn is not None else datetime.now(TZ_CHILE)
    hora = ahora.hour
    return hora >= 22 or hora < 7


def ahora_utc() -> datetime:
    """El único "ahora" del proyecto: instante actual, naive, en UTC.

    Naive porque así son las columnas de la base; UTC porque es contra eso que
    se comparan los ``fecha_cierre`` ya convertidos.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def a_utc_naive(valor: datetime) -> datetime:
    """Convierte un instante a la representación de la base: naive en UTC.

    - Con offset (``Z``, ``+00:00``, ``-03:00``): se convierte a UTC y se
      descarta el ``tzinfo``.
    - Sin offset: se interpreta como hora de Chile continental **[I]** (ver el
      docstring del módulo) y se convierte a UTC.
    """
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=_TZ_SIN_OFFSET)
    return valor.astimezone(UTC).replace(tzinfo=None)


def a_naive_como_la_api(valor: datetime) -> datetime:
    """Inversa de :func:`a_utc_naive`, para devolverle a la API lo que ella manda.

    Solo la usa ``app/clients/mp_v2.py`` al serializar ``cambio_desde`` y
    ``cambio_hasta``: la v2 habla SIEMPRE en hora de Chile —incluso cuando pone
    una `Z` falsa, ver ``parse_fecha_v2``— y lo guardamos convertido a UTC. Mandarlo de vuelta como UTC naive correría el
    cursor varias horas hacia adelante y la ingesta incremental **perdería
    cambios**. Round-trip simétrico: lo que entró sin offset, sale sin offset.
    """
    if valor.tzinfo is None:
        valor = valor.replace(tzinfo=UTC)
    return valor.astimezone(_TZ_SIN_OFFSET).replace(tzinfo=None)


def borde_del_dia_utc_naive(d: date, *, fin_de_dia: bool) -> datetime:
    """Convierte una fecha SIN hora al instante que le corresponde, en UTC naive.

    Cuando la fuente solo entrega ``ddmmaaaa`` (o un ISO de 10 caracteres) no
    hay hora que conservar, pero sí hay que elegir bien el borde:

    - ``fin_de_dia=True`` → 23:59:59 de ese día **en hora de Chile**. Es lo que
      corresponde a un ``fecha_cierre``: una licitación que cierra "el 24" está
      abierta durante todo el 24. Con medianoche de INICIO la dábamos por
      cerrada un día antes, y peor: leída como UTC, esa medianoche son las
      21:00 del día 23 en Chile, así que dejaba de ser candidata durante todo
      su último día. Ese era el bug de F-fecha-cierre.
    - ``fin_de_dia=False`` → 00:00:00 de ese día en hora de Chile. Es lo que
      corresponde a un ``fecha_publicacion``, donde el borde temprano es el
      conservador.
    """
    momento = time(23, 59, 59) if fin_de_dia else time(0, 0, 0)
    return datetime.combine(d, momento, tzinfo=TZ_CHILE).astimezone(UTC).replace(tzinfo=None)
