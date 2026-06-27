"""Construcción segura de queries para websearch_to_tsquery.

Regla crítica: build_tsquery y build_exclude_tsquery solo producen el STRING
que se pasa como parámetro :q a websearch_to_tsquery('spanish', :q).
NUNCA se interpola directamente en SQL.

Invariante recall/score (F9c): keywords_validas() es el ÚNICO punto que decide
qué keywords participan en la tsquery. build_tsquery la usa para el OR
combinado del recall; app.matching.engine la reutiliza para las tsquery
individuales que detectan keywords_hit (score). Mismo criterio en ambos casos
→ recall y score quedan unificados en una sola fuente de verdad (Postgres FTS).
"""

from __future__ import annotations


def keywords_validas(keywords: list[str]) -> list[str]:
    """Filtra keywords vacías/blancas y recorta espacios.

    Usado tanto por build_tsquery/build_exclude_tsquery (OR combinado del
    recall) como por la detección de keywords_hit en app.matching.engine
    (tsquery individual por keyword) — mismo criterio en ambos casos.
    """
    return [k.strip() for k in keywords if k.strip()]


def build_tsquery(keywords: list[str]) -> str:
    """Combina keywords con OR para websearch_to_tsquery.

    - Keywords simples: 'cable' 'eléctrico' → 'cable OR eléctrico'
    - Frases entre comillas: '"cable eléctrico"' se pasan tal cual;
      websearch_to_tsquery las trata como búsqueda de frase exacta.
    - El resultado va SIEMPRE como parámetro :q, nunca interpolado en SQL.
    """
    return " OR ".join(keywords_validas(keywords))


def build_exclude_tsquery(keywords_excluir: list[str]) -> str:
    """Misma semántica que build_tsquery; el resultado se usa para exclusión."""
    return " OR ".join(keywords_validas(keywords_excluir))
