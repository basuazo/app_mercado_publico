"""Índices de FK, FTS de ítems/productos y fechas de CA (F-indices).

Solo índices; no cambia datos ni resultados. `IF NOT EXISTS` / `IF EXISTS` para
que sea re-ejecutable. Los GIN usan expresiones textualmente idénticas a las de
app/matching/engine.py (si no, el planner no los usa).

Revision ID: b8c2d5e9a1f7
Revises: a4e7c2d9f1b3
Create Date: 2026-10-09 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b8c2d5e9a1f7"
down_revision: str | None = "a4e7c2d9f1b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDICES = [
    "CREATE INDEX IF NOT EXISTS ix_licitacion_items_licitacion_codigo ON licitacion_items (licitacion_codigo)",
    "CREATE INDEX IF NOT EXISTS ix_ca_productos_ca_codigo ON ca_productos (ca_codigo)",
    "CREATE INDEX IF NOT EXISTS ix_alertas_match_id ON alertas (match_id)",
    "CREATE INDEX IF NOT EXISTS ix_alertas_seguimiento_id ON alertas (seguimiento_id)",
    "CREATE INDEX IF NOT EXISTS ix_oportunidades_match_fuente_codigo "
    "ON oportunidades_match (fuente, codigo_oportunidad)",
    "CREATE INDEX IF NOT EXISTS ix_compras_agiles_fecha_cierre ON compras_agiles (fecha_cierre)",
    "CREATE INDEX IF NOT EXISTS ix_compras_agiles_fecha_publicacion ON compras_agiles (fecha_publicacion)",
    "CREATE INDEX IF NOT EXISTS ix_licitacion_items_nombre_tsv ON licitacion_items "
    "USING gin (to_tsvector('spanish', inmutable_unaccent(nombre)))",
    "CREATE INDEX IF NOT EXISTS ix_ca_productos_texto_tsv ON ca_productos "
    "USING gin (to_tsvector('spanish', inmutable_unaccent(nombre || ' ' || descripcion)))",
]

_NOMBRES = [
    "ix_ca_productos_texto_tsv",
    "ix_licitacion_items_nombre_tsv",
    "ix_compras_agiles_fecha_publicacion",
    "ix_compras_agiles_fecha_cierre",
    "ix_oportunidades_match_fuente_codigo",
    "ix_alertas_seguimiento_id",
    "ix_alertas_match_id",
    "ix_ca_productos_ca_codigo",
    "ix_licitacion_items_licitacion_codigo",
]


def upgrade() -> None:
    for sql in _INDICES:
        op.execute(sql)


def downgrade() -> None:
    for nombre in _NOMBRES:
        op.execute(f"DROP INDEX IF EXISTS {nombre}")
