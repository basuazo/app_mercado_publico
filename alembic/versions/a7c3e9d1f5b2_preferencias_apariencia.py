"""Preferencias de apariencia y filtros recordados por pantalla (F-dis-1).

- `usuarios.tema` (claro | oscuro | sistema), `usuarios.densidad` (comoda | compacta) y
  `usuarios.inicio` (hoy | mesa | trabajo | ventas). NOT NULL con default de servidor: las filas
  existentes quedan en claro, cómoda y Hoy (lo mismo que ven hoy).
- Tabla `usuario_vista` (usuario_id, pantalla, datos JSONB ≤ 2 KB, actualizado_en), única por
  usuario + pantalla. Nace vacía; la usan las fases de pantalla.

Revision ID: a7c3e9d1f5b2
Revises: e9a4c2f7b1d3
Create Date: 2026-11-07 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7c3e9d1f5b2"
down_revision: str | None = "e9a4c2f7b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "usuarios", sa.Column("tema", sa.String(10), nullable=False, server_default="claro")
    )
    op.add_column(
        "usuarios", sa.Column("densidad", sa.String(10), nullable=False, server_default="comoda")
    )
    op.add_column(
        "usuarios", sa.Column("inicio", sa.String(10), nullable=False, server_default="hoy")
    )
    op.create_table(
        "usuario_vista",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "usuario_id",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("pantalla", sa.String(40), nullable=False),
        sa.Column(
            "datos",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("usuario_id", "pantalla", name="uq_usuario_vista"),
    )


def downgrade() -> None:
    op.drop_table("usuario_vista")
    op.drop_column("usuarios", "inicio")
    op.drop_column("usuarios", "densidad")
    op.drop_column("usuarios", "tema")
