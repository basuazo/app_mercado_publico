"""Descarga automática de adjuntos de CA: una fila por documento (F-plat-anexos-auto).

- Tabla `ca_adjunto_descargas` (ca_codigo, documento_id, nombre, estado listado | bajado |
  fallido | pedido, intentos, motivo, anexo_id, pedido_por, actualizado_en), única por CA +
  documento; la fila `documento_id = "(listar)"` guarda el estado del listado de la CA. Nace
  vacía: la llena el job `mercado-anexos` y el botón «Volver a bajar y leer» de la ficha. Borra en
  cascada con la CA; `anexo_id` y `pedido_por` quedan en NULL si se borra el anexo o el usuario.

Revision ID: c4e8a2f6d0b3
Revises: a7c3e9d1f5b2
Create Date: 2026-11-07 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4e8a2f6d0b3"
down_revision: str | None = "a7c3e9d1f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ca_adjunto_descargas",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "ca_codigo",
            sa.String(50),
            sa.ForeignKey(
                "compras_agiles.codigo",
                ondelete="CASCADE",
                name=op.f("fk_ca_adjunto_descargas_ca_codigo_compras_agiles"),
            ),
            nullable=False,
        ),
        sa.Column("documento_id", sa.String(100), nullable=False),
        sa.Column("nombre", sa.String(200), nullable=True),
        sa.Column("estado", sa.String(10), nullable=False),
        sa.Column("intentos", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("motivo", sa.String(40), nullable=True),
        sa.Column(
            "anexo_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "ca_anexos.id",
                ondelete="SET NULL",
                name=op.f("fk_ca_adjunto_descargas_anexo_id_ca_anexos"),
            ),
            nullable=True,
        ),
        sa.Column(
            "pedido_por",
            sa.BigInteger(),
            sa.ForeignKey(
                "usuarios.id",
                ondelete="SET NULL",
                name=op.f("fk_ca_adjunto_descargas_pedido_por_usuarios"),
            ),
            nullable=True,
        ),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ca_adjunto_descargas")),
        sa.CheckConstraint(
            "estado IN ('listado', 'bajado', 'fallido', 'pedido')",
            name=op.f("ck_ca_adjunto_descargas_estado"),
        ),
        sa.UniqueConstraint(
            "ca_codigo", "documento_id", name=op.f("uq_ca_adjunto_descargas_ca_codigo")
        ),
    )


def downgrade() -> None:
    op.drop_table("ca_adjunto_descargas")
