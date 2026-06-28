"""ofertas_competencia

Revision ID: 9a1e6b2c5d7f
Revises: 7c9d2a1f4b3e
Create Date: 2026-06-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9a1e6b2c5d7f"
down_revision: str | Sequence[str] | None = "7c9d2a1f4b3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ofertas_competencia",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "licitacion_codigo",
            sa.String(50),
            sa.ForeignKey("licitaciones.codigo", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("codigo_item", sa.String(50), nullable=False, server_default=""),
        sa.Column("rut_proveedor", sa.String(20), nullable=False, server_default=""),
        sa.Column("nombre_proveedor", sa.String(500), nullable=False, server_default=""),
        sa.Column("monto_unitario", sa.Float(), nullable=True),
        sa.Column("monto_linea_adjudicada", sa.Float(), nullable=True),
        sa.Column("cantidad", sa.Float(), nullable=True),
        sa.Column("seleccionada", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_ofertas_competencia_licitacion_codigo",
        "ofertas_competencia",
        ["licitacion_codigo"],
    )

    op.add_column("usuarios", sa.Column("rut_proveedor", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("usuarios", "rut_proveedor")
    op.drop_index("ix_ofertas_competencia_licitacion_codigo", table_name="ofertas_competencia")
    op.drop_table("ofertas_competencia")
