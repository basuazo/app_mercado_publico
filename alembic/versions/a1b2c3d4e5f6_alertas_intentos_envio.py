"""alertas_intentos_envio

Revision ID: a1b2c3d4e5f6
Revises: fde568616494
Create Date: 2026-06-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "fde568616494"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("alertas", sa.Column("intentos_envio", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("alertas", sa.Column("max_intentos", sa.Integer(), nullable=False, server_default="3"))


def downgrade() -> None:
    op.drop_column("alertas", "max_intentos")
    op.drop_column("alertas", "intentos_envio")
