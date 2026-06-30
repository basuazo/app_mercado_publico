"""match_feedback

Revision ID: e1f4a7c9b2d6
Revises: c4a8e0f7b1d3
Create Date: 2026-06-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e1f4a7c9b2d6"
down_revision: str | Sequence[str] | None = "c4a8e0f7b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "match_feedback",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "usuario_id",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fuente", sa.String(30), nullable=False),
        sa.Column("codigo_oportunidad", sa.String(50), nullable=False),
        sa.Column("valor", sa.String(20), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("usuario_id", "fuente", "codigo_oportunidad", name="uq_match_feedback"),
    )
    op.create_index("ix_match_feedback_usuario", "match_feedback", ["usuario_id"])


def downgrade() -> None:
    op.drop_index("ix_match_feedback_usuario", table_name="match_feedback")
    op.drop_table("match_feedback")
