"""Explorador de Compras Ágiles (F-ca-explorar): rubros favoritos y vocabulario.

Dos tablas chicas:
- `rubros_favoritos`: rubro (prefijo UNSPSC) que cada usuario marca como favorito;
  único por (owner_id, prefijo); se borra con el usuario.
- `rubro_vocabulario`: lexemas típicos de cada familia UNSPSC (4 dígitos),
  aprendidos de `licitacion_items` por el job `vocabulario-rubros`. Es un dato
  DERIVADO: se regenera entero cada semana, así que no guarda historia.

Revision ID: a4c9e2b7d1f3
Revises: d7f2a4c8b6e1
Create Date: 2026-09-30 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a4c9e2b7d1f3"
down_revision: str | None = "d7f2a4c8b6e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rubros_favoritos",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "owner_id",
            sa.BigInteger,
            sa.ForeignKey("usuarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prefijo", sa.String(8), nullable=False),
        sa.Column("creado_en", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("owner_id", "prefijo", name="uq_rubro_favorito"),
    )

    op.create_table(
        "rubro_vocabulario",
        sa.Column("prefijo", sa.String(8), nullable=False),
        sa.Column("lexema", sa.String(60), nullable=False),
        sa.Column("df_rubro", sa.Integer, nullable=False),
        sa.Column("lift", sa.Float, nullable=False),
        sa.Column("actualizado_en", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
        sa.PrimaryKeyConstraint("prefijo", "lexema", name="pk_rubro_vocabulario"),
    )


def downgrade() -> None:
    op.drop_table("rubro_vocabulario")
    op.drop_table("rubros_favoritos")
