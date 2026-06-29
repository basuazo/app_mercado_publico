"""sector_organismos

Revision ID: c4a8e0f7b1d3
Revises: b3f7c1d9e2a4
Create Date: 2026-06-29

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4a8e0f7b1d3"
down_revision: str | Sequence[str] | None = "b3f7c1d9e2a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("instituciones_pac", sa.Column("sector", sa.String(200), nullable=True))
    op.add_column("instituciones_pac", sa.Column("id_sector", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("instituciones_pac", "id_sector")
    op.drop_column("instituciones_pac", "sector")
