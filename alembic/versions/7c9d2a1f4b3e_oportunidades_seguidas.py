"""oportunidades_seguidas

Revision ID: 7c9d2a1f4b3e
Revises: 616613c3d7cf
Create Date: 2026-06-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "7c9d2a1f4b3e"
down_revision: str | Sequence[str] | None = "616613c3d7cf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oportunidades_seguidas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "owner_id",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fuente", sa.String(30), nullable=False),
        sa.Column("codigo_oportunidad", sa.String(50), nullable=False),
        sa.Column("estado_visto", sa.String(50), nullable=False, server_default=""),
        sa.Column("archivada", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("owner_id", "fuente", "codigo_oportunidad", name="uq_seguida"),
    )

    op.add_column(
        "alertas",
        sa.Column(
            "seguimiento_id",
            sa.BigInteger(),
            sa.ForeignKey("oportunidades_seguidas.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.alter_column("alertas", "match_id", existing_type=sa.BigInteger(), nullable=True)


def downgrade() -> None:
    op.alter_column("alertas", "match_id", existing_type=sa.BigInteger(), nullable=False)
    op.drop_column("alertas", "seguimiento_id")
    op.drop_table("oportunidades_seguidas")
