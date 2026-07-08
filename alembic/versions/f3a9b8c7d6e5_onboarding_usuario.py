"""Onboarding de usuario: tutorial y novedades vistas.

Revision ID: f3a9b8c7d6e5
Revises: d2f8a6c1b9e0
Create Date: 2026-07-07 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f3a9b8c7d6e5"
down_revision: str | None = "d2f8a6c1b9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usuarios",
        sa.Column("tutorial_visto", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "usuarios",
        sa.Column("novedades_visto_hasta", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("usuarios", "novedades_visto_hasta")
    op.drop_column("usuarios", "tutorial_visto")
