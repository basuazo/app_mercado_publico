"""Vocabulario típico de cada rubro UNSPSC (F-ca-explorar).

El rubro de una Compra Ágil solo se conoce con su detalle (productos), y el
detalle llega para una fracción mínima de las CA. Lo único que tienen todas es el
nombre. Este módulo aprende, desde `licitacion_items` (114 mil filas con UNSPSC y
nombre), qué palabras suelen aparecer en el nombre de lo que se compra en cada
familia, y las deja en `rubro_vocabulario` para que el explorador marque las CA
como "posible" cuando su nombre calza.

- Unidad: la FAMILIA (4 dígitos); un segmento es la unión de sus familias.
- Lexemas: los de Postgres (`unnest(to_tsvector('spanish', inmutable_unaccent(...)))`),
  los mismos con que se arma `compras_agiles.tsv`, así que después se comparan
  tal cual con `to_tsquery('simple', ...)` sin volver a aplicar el stemmer.
  Sin `ts_stat`: no admite parámetros.
- Lift: (frecuencia en el rubro) / (frecuencia en NOMBRES DE CA de los últimos 30
  días). Contra la población que se filtra, no contra licitaciones: castiga las
  palabras comunes en CA ("agua", "central", "salud"). [V, Paso 0 del 26-sep]
- Todo lo hace UNA sentencia agregada en la base: a Python solo vuelven los
  top-K por familia (regla 12). El reemplazo va en una transacción: nadie ve
  la tabla vacía a la mitad. Idempotente: re-ejecutar deja el mismo resultado.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.tiempo import ahora_utc
from app.models.tables import RubroVocabulario

_log = get_logger(__name__)

# Lexemas que se aceptan en la tsquery del explorador. Se valida acá Y al
# leerlos: la tsquery se arma uniéndolos con " | " y no debe poder colarse un
# operador (`&`, `!`, `:*`, comillas, paréntesis).
LEXEMA_RE = re.compile(r"^[a-zñ]+$")
LEXEMA_LARGO_MIN = 3
LEXEMA_LARGO_MAX = 60
# Frecuencia mínima en el rubro (en ítems) para tomar un lexema en cuenta.
DF_RUBRO_MIN = 3
VENTANA_CA_DIAS = 30

_FAMILIA_RE = re.compile(r"^\d{4}$")

_SQL_VOCABULARIO = """
WITH items AS (
    SELECT left(li.codigo_producto, 4) AS fam,
           to_tsvector('spanish', inmutable_unaccent(li.nombre)) AS tsv
    FROM licitacion_items li
    WHERE li.codigo_producto ~ '^[0-9]{{4}}'
      {filtro_familias}
),
n_fam AS (
    SELECT fam, count(*) AS n FROM items GROUP BY fam
),
df_fam AS (
    SELECT i.fam, x.lexeme, count(*) AS df
    FROM items i, unnest(i.tsv) AS x(lexeme, positions, weights)
    GROUP BY i.fam, x.lexeme
    HAVING count(*) >= :df_min
),
ca AS (
    SELECT to_tsvector('spanish', inmutable_unaccent(c.nombre)) AS tsv
    FROM compras_agiles c
    WHERE c.creado_en >= :desde AND c.creado_en <= :hasta
),
n_ca AS (
    SELECT count(*) AS n FROM ca
),
df_ca AS (
    SELECT x.lexeme, count(*) AS df
    FROM ca, unnest(ca.tsv) AS x(lexeme, positions, weights)
    GROUP BY x.lexeme
),
candidatos AS (
    SELECT d.fam, d.lexeme, d.df,
           (d.df::float8 / f.n) / ((coalesce(c.df, 0) + 1)::float8 / greatest(nc.n, 1)) AS lift
    FROM df_fam d
    JOIN n_fam f ON f.fam = d.fam
    LEFT JOIN df_ca c ON c.lexeme = d.lexeme
    CROSS JOIN n_ca nc
    WHERE d.lexeme ~ '^[a-zñ]{{{largo_min},{largo_max}}}$'
),
ranking AS (
    SELECT fam, lexeme, df, lift,
           row_number() OVER (PARTITION BY fam ORDER BY df DESC, lift DESC, lexeme) AS rn
    FROM candidatos
    WHERE lift >= :lift_min
)
SELECT fam, lexeme, df, lift FROM ranking WHERE rn <= :k ORDER BY fam, rn
"""

_SQL_CONTEO = """
SELECT (SELECT count(*) FROM licitacion_items li
        WHERE li.codigo_producto ~ '^[0-9]{{4}}' {filtro_familias}) AS n_items,
       (SELECT count(*) FROM compras_agiles c
        WHERE c.creado_en >= :desde AND c.creado_en <= :hasta) AS n_ca
"""


def _sql(plantilla: str, familias: Collection[str] | None) -> str:
    # Solo se interpolan constantes de este módulo: los valores viajan como parámetros.
    filtro = "AND left(li.codigo_producto, 4) = ANY(CAST(:familias AS text[]))" if familias else ""
    return plantilla.format(
        filtro_familias=filtro, largo_min=LEXEMA_LARGO_MIN, largo_max=LEXEMA_LARGO_MAX
    )


def construir_vocabulario(
    session: Session,
    *,
    k: int = 10,
    lift_min: float = 10.0,
    ahora: datetime | None = None,
    familias: Collection[str] | None = None,
) -> dict[str, int]:
    """Recalcula `rubro_vocabulario` y lo reemplaza en una transacción.

    `familias=None` (el job): todas las familias, y se reemplaza la tabla entera.
    Con una lista (tests): solo esas familias de 4 dígitos, y solo sus filas se
    reemplazan. `ahora` fija el borde de la ventana de 30 días de nombres de CA.
    """
    if familias is not None:
        familias = sorted({f for f in familias if _FAMILIA_RE.match(f)})
        if not familias:
            return {"familias": 0, "lexemas": 0, "items_analizados": 0, "ca_ventana": 0}
    ahora = ahora or ahora_utc()
    params: dict[str, Any] = {
        "desde": ahora - timedelta(days=VENTANA_CA_DIAS),
        "hasta": ahora,
    }
    if familias:
        params["familias"] = list(familias)

    conteo = session.execute(text(_sql(_SQL_CONTEO, familias)), params).one()
    if not conteo.n_items:
        # Sin ítems no hay de qué aprender: no se toca el vocabulario vigente.
        _log.warning("vocabulario-rubros: sin licitacion_items; se conserva el vocabulario actual")
        return {"familias": 0, "lexemas": 0, "items_analizados": 0, "ca_ventana": int(conteo.n_ca)}

    filas = session.execute(
        text(_sql(_SQL_VOCABULARIO, familias)),
        {**params, "df_min": DF_RUBRO_MIN, "lift_min": lift_min, "k": k},
    ).all()

    marca = ahora_utc()
    nuevas = [
        {
            "prefijo": f.fam,
            "lexema": f.lexeme,
            "df_rubro": int(f.df),
            "lift": float(f.lift),
            "actualizado_en": marca,
        }
        for f in filas
        if LEXEMA_RE.match(f.lexeme)
    ]

    # Una sola transacción: delete + insert + commit. Los lectores concurrentes
    # ven el vocabulario anterior completo o el nuevo completo, nunca vacío.
    borrar = delete(RubroVocabulario)
    if familias:
        borrar = borrar.where(RubroVocabulario.prefijo.in_(familias))
    session.execute(borrar)
    if nuevas:
        session.execute(insert(RubroVocabulario), nuevas)
    session.commit()

    resultado = {
        "familias": len({n["prefijo"] for n in nuevas}),
        "lexemas": len(nuevas),
        "items_analizados": int(conteo.n_items),
        "ca_ventana": int(conteo.n_ca),
    }
    _log.info("vocabulario-rubros: %s", resultado)
    return resultado
