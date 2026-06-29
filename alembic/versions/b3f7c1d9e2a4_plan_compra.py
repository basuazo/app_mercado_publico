"""plan_compra

Revision ID: b3f7c1d9e2a4
Revises: 9a1e6b2c5d7f
Create Date: 2026-06-29

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3f7c1d9e2a4"
down_revision: str | Sequence[str] | None = "9a1e6b2c5d7f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "plan_compra_lineas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("codigo_entidad", sa.Integer(), nullable=False),
        sa.Column("agno", sa.Integer(), nullable=False),
        sa.Column("institucion_nombre", sa.String(500), nullable=False, server_default=""),
        sa.Column("codigo_producto", sa.String(50), nullable=False, server_default=""),
        sa.Column("descripcion_producto", sa.Text(), nullable=False, server_default=""),
        sa.Column("cantidad_estimada", sa.Float(), nullable=True),
        sa.Column("monto_unitario_clp", sa.Float(), nullable=True),
        sa.Column("monto_estimado_clp", sa.Float(), nullable=True),
        sa.Column("mes_estimado", sa.Integer(), nullable=True),
        sa.Column("trimestre_estimado", sa.Integer(), nullable=True),
        sa.Column("estado_planificacion", sa.String(30), nullable=False, server_default="desconocido"),
        sa.Column("creado_en", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_plan_compra_lineas_entidad_agno",
        "plan_compra_lineas",
        ["codigo_entidad", "agno"],
    )

    op.create_table(
        "plan_compra_sync",
        sa.Column("codigo_entidad", sa.Integer(), primary_key=True),
        sa.Column("agno", sa.Integer(), primary_key=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("fuente_last_modified", sa.String(100), nullable=True),
        sa.Column("n_filas", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("estado", sa.String(20), nullable=False, server_default="sin_plan"),
    )

    op.create_table(
        "instituciones_pac",
        sa.Column("codigo_entidad", sa.Integer(), primary_key=True),
        sa.Column("razon_social", sa.String(500), nullable=False, server_default=""),
        sa.Column("rut", sa.String(20), nullable=True),
    )
    op.create_index(
        "ix_instituciones_pac_razon_social",
        "instituciones_pac",
        ["razon_social"],
    )


def downgrade() -> None:
    op.drop_index("ix_instituciones_pac_razon_social", table_name="instituciones_pac")
    op.drop_table("instituciones_pac")
    op.drop_table("plan_compra_sync")
    op.drop_index("ix_plan_compra_lineas_entidad_agno", table_name="plan_compra_lineas")
    op.drop_table("plan_compra_lineas")
