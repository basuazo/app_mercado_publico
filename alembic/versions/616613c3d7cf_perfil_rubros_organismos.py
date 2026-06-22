"""perfil_rubros_organismos

Revision ID: 616613c3d7cf
Revises: a1b2c3d4e5f6
Create Date: 2026-06-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "616613c3d7cf"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "perfiles_busqueda",
        sa.Column("categorias_unspsc", postgresql.JSONB, nullable=False, server_default="[]"),
    )
    op.add_column(
        "perfiles_busqueda",
        sa.Column("organismos_seguidos", postgresql.JSONB, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("perfiles_busqueda", "organismos_seguidos")
    op.drop_column("perfiles_busqueda", "categorias_unspsc")
