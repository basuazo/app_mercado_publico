"""Construcción segura de queries para websearch_to_tsquery.

Regla crítica: build_tsquery y build_exclude_tsquery solo producen el STRING
que se pasa como parámetro :q a websearch_to_tsquery('spanish', :q).
NUNCA se interpola directamente en SQL.
"""

from __future__ import annotations


def build_tsquery(keywords: list[str]) -> str:
    """Combina keywords con OR para websearch_to_tsquery.

    - Keywords simples: 'cable' 'eléctrico' → 'cable OR eléctrico'
    - Frases entre comillas: '"cable eléctrico"' se pasan tal cual;
      websearch_to_tsquery las trata como búsqueda de frase exacta.
    - El resultado va SIEMPRE como parámetro :q, nunca interpolado en SQL.
    """
    parts = [k.strip() for k in keywords if k.strip()]
    return " OR ".join(parts)


def build_exclude_tsquery(keywords_excluir: list[str]) -> str:
    """Misma semántica que build_tsquery; el resultado se usa para exclusión."""
    parts = [k.strip() for k in keywords_excluir if k.strip()]
    return " OR ".join(parts)
