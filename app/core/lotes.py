"""Inserciones por lote en UNA sentencia (F-plat-6-fix).

`session.execute(insert(Modelo), lista_de_dicts)` es un executemany: con psycopg contra Neon
(us-east) desde Chile, ~50 ms por fila [V, log de `mercado-competencia --completo` del 5-nov:
8.719 filas en 434 s]; un `--completo` de demanda no terminó en 24 min. Acá cada lote es un solo
`INSERT ... VALUES (...), (...), ...`: un viaje por lote.

Tamaño del lote: `filas × columnas` bajo `MAX_PARAMETROS` (Postgres admite 65.535 parámetros por
sentencia; SQLite 32.766 desde 3.32), con tope de `MAX_FILAS` filas para no armar sentencias
gigantes en memoria (regla 12). Se cuentan TODAS las columnas de la tabla, no solo las del dict:
SQLAlchemy renderiza también las que tienen default, así que es una cota segura.

Todas las filas de una llamada deben traer las mismas llaves (lo exige el VALUES multi-fila).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import insert
from sqlalchemy.orm import Session

MAX_PARAMETROS = 30_000
MAX_FILAS = 1_000


def filas_por_lote(n_columnas: int) -> int:
    """Filas por sentencia para una tabla de `n_columnas` columnas."""
    return max(1, min(MAX_FILAS, MAX_PARAMETROS // max(1, n_columnas)))


def lote_de(modelo: Any) -> int:
    """`filas_por_lote` para un modelo ORM (o una `Table`)."""
    tabla = getattr(modelo, "__table__", modelo)
    return filas_por_lote(len(tabla.columns))


def insertar_por_lotes(session: Session, modelo: Any, filas: Sequence[Mapping[str, Any]]) -> int:
    """Inserta `filas` en `modelo` con un `INSERT ... VALUES` multi-fila por lote; nunca
    executemany. No commitea. Devuelve cuántas sentencias ejecutó."""
    if not filas:
        return 0
    lote = lote_de(modelo)
    n = 0
    for i in range(0, len(filas), lote):
        session.execute(insert(modelo).values([dict(f) for f in filas[i : i + lote]]))
        n += 1
    return n
