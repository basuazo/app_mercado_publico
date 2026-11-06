"""Catálogo en tres capas: `empresa_productos` y su log de movimientos (F-plat-capas).

La capa «Inventario» (lo que la empresa tiene o vende) comparte la llave
`producto_canonico × producto_formato` con la demanda y con el catálogo de proveedores.
Solo tablas nuevas: ninguna columna ni dato existente cambia. El enganche de
`proveedor_productos` al canónico NO es migración de datos: lo hace la acción «Vincular
pendientes» de `/proveedores`, por empresa, cuando se pulsa.

Revision ID: a3c8e5d1b7f2
Revises: d9a4e2b7c1f6
Create Date: 2026-11-05 23:59:45.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a3c8e5d1b7f2"
down_revision: str | None = "d9a4e2b7c1f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "empresa_productos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_id",
            sa.BigInteger(),
            sa.ForeignKey("empresas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "producto_id",
            sa.BigInteger(),
            sa.ForeignKey("producto_canonico.id"),
            nullable=False,
        ),
        sa.Column(
            "formato_id",
            sa.BigInteger(),
            sa.ForeignKey("producto_formato.id"),
            nullable=False,
        ),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("stock", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("reservado", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("stock_minimo", sa.Numeric(14, 3), nullable=True),
        sa.Column("plazo_reposicion_dias", sa.Integer(), nullable=True),
        sa.Column(
            "proveedor_habitual_id",
            sa.BigInteger(),
            sa.ForeignKey("proveedores.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("marca", sa.String(100), nullable=True),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
        sa.Column(
            "actualizado_por_id",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("empresa_id", "producto_id", "formato_id", name="uq_empresa_producto"),
        sa.CheckConstraint("stock >= 0", name="stock_no_negativo"),
        sa.CheckConstraint("reservado >= 0", name="reservado_no_negativo"),
    )
    op.create_index(
        "ix_empresa_productos_empresa_activo", "empresa_productos", ["empresa_id", "activo"]
    )
    op.create_table(
        "empresa_producto_movimientos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_producto_id",
            sa.BigInteger(),
            sa.ForeignKey("empresa_productos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("delta", sa.Numeric(14, 3), nullable=False),
        sa.Column("motivo", sa.String(15), nullable=False),
        sa.Column("referencia", sa.String(100), nullable=True),
        sa.Column(
            "usuario_id",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "motivo IN ('ajuste','compra','entrega','reserva','liberacion')",
            name="motivo",
        ),
    )
    op.create_index(
        "ix_empresa_producto_mov_ep",
        "empresa_producto_movimientos",
        ["empresa_producto_id", "creado_en"],
    )


def downgrade() -> None:
    op.drop_index("ix_empresa_producto_mov_ep", table_name="empresa_producto_movimientos")
    op.drop_table("empresa_producto_movimientos")
    op.drop_index("ix_empresa_productos_empresa_activo", table_name="empresa_productos")
    op.drop_table("empresa_productos")
