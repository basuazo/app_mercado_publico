"""Búsqueda inversa en el Plan Anual de Compra (F-plan-busqueda).

Aditiva sobre plan_compra_lineas:
- `lote_id`: marca las filas que pertenecen a una carga del año completo (job
  `plan-anual`); NULL para filas cacheadas on-demand por institución (flujo
  existente, sin cambios). Permite reemplazar el año completo sin ventana
  vacía: se insertan las filas nuevas con un lote_id fresco y luego se borran,
  en una sola sentencia corta, las filas viejas de ese año con lote_id
  distinto (ver app/ingest/plan_compra.py::sync_plan_anual_completo).
- Índice GIN de EXPRESIÓN (sin columna generada, a diferencia de
  licitaciones.tsv/compras_agiles.tsv) sobre descripcion_producto: ahorra
  espacio en Neon (regla 11) a costa de recalcular el tsvector en cada query,
  aceptable porque el filtro por agno ya acota el conjunto candidato.
- Índices btree (agno, codigo_entidad) y monto_estimado_clp para el orden por
  monto de la nueva búsqueda.

Revision ID: d7f2a4c8b6e1
Revises: c7e3a9f1d5b2
Create Date: 2026-09-26 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d7f2a4c8b6e1"
down_revision: str | None = "c7e3a9f1d5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("plan_compra_lineas", sa.Column("lote_id", sa.BigInteger(), nullable=True))

    op.create_index(
        "ix_plan_compra_lineas_agno_entidad",
        "plan_compra_lineas",
        ["agno", "codigo_entidad"],
    )
    op.create_index(
        "ix_plan_compra_lineas_monto",
        "plan_compra_lineas",
        ["monto_estimado_clp"],
    )

    # inmutable_unaccent ya existe desde fde568616494_tablas_iniciales.
    op.execute(
        """
        CREATE INDEX ix_plan_compra_lineas_desc_tsv
        ON plan_compra_lineas
        USING gin (to_tsvector('spanish', inmutable_unaccent(descripcion_producto)))
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_plan_compra_lineas_desc_tsv")
    op.drop_index("ix_plan_compra_lineas_monto", table_name="plan_compra_lineas")
    op.drop_index("ix_plan_compra_lineas_agno_entidad", table_name="plan_compra_lineas")
    op.drop_column("plan_compra_lineas", "lote_id")
