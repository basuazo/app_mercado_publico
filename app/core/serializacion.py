"""Conversión de los dataclasses de detalle a un dict que se puede guardar en JSONB.

``dataclasses.asdict`` deja los ``datetime`` tal cual, y el driver de Postgres
revienta en el ``commit`` con "Object of type datetime is not JSON serializable"
(F-raw-json). Acá se convierte cada tipo de forma explícita, en vez de un
``json.dumps(default=str)`` que taparía cualquier tipo nuevo sin avisar.

Vive en ``app/core`` porque no conoce ni httpx ni SQLAlchemy ni formatos de la
API: recibe cualquier dataclass y devuelve tipos nativos de JSON. Así lo usa el
orchestrator sin saber qué campos trae cada detalle, y ``app/clients`` sigue sin
saber cómo se persisten sus tipos.

Las fechas se guardan como las tiene el dataclass (naive en UTC, ver
``app/core/tiempo.py``): sin agregar zona ni convertir.
"""

from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

from app.core.logging import get_logger

_log = get_logger(__name__)


def a_json(valor: Any) -> Any:
    """Devuelve `valor` convertido, recursivamente, a tipos nativos de JSON.

    - ``None``, ``bool``, ``int``, ``str``: tal cual.
    - ``float`` finito: tal cual; ``NaN``/``inf`` → ``None`` (JSONB los rechaza).
    - ``datetime``, ``date``, ``time`` → ``isoformat()``.
    - ``Decimal`` → ``str`` (no se pierde precisión).
    - ``Enum`` → su ``.value``, también convertido.
    - dataclass → dict de sus campos; ``dict`` → dict con claves ``str``;
      ``list``/``tuple``/``set`` → lista.
    - Cualquier otro tipo → ``str()`` y log DEBUG (regla 6: nunca romper).
    """
    if valor is None or isinstance(valor, (bool, int, str)):
        return valor
    if isinstance(valor, float):
        if math.isfinite(valor):
            return valor
        _log.debug("a_json: float no finito %r → None", valor)
        return None
    # datetime antes que date: datetime es subclase de date.
    if isinstance(valor, (datetime, date, time)):
        return valor.isoformat()
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, Enum):
        return a_json(valor.value)
    if dataclasses.is_dataclass(valor) and not isinstance(valor, type):
        return {f.name: a_json(getattr(valor, f.name)) for f in dataclasses.fields(valor)}
    if isinstance(valor, dict):
        return {str(k): a_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple, set, frozenset)):
        return [a_json(v) for v in valor]
    _log.debug("a_json: tipo no previsto %s → str()", type(valor).__name__)
    return str(valor)


def dataclass_a_json(obj: Any) -> dict[str, Any]:
    """Convierte un dataclass (instancia) en un dict listo para una columna JSONB."""
    if not dataclasses.is_dataclass(obj) or isinstance(obj, type):
        raise TypeError(f"Se esperaba una instancia de dataclass, llegó {type(obj).__name__}")
    resultado: dict[str, Any] = a_json(obj)
    return resultado
