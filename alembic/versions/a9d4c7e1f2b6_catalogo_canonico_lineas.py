"""Catálogo canónico, formatos, alias y líneas 1:1 por Compra Ágil (F-plat-1).

Agrega `producto_canonico`, `producto_formato`, `producto_alias`, `ca_lineas`, la bandera
`compras_agiles.detalle_lineas` ("CA sin detalle") y las FK de `proveedor_productos` al
catálogo. `proveedor_productos.producto_id` existía SIN FK (F-plat-3): como nada pudo
referenciar un catálogo que no existía, se pone en NULL antes de crear la FK (así la
migración no falla con filas previas). Ninguna tabla nueva se purga.

Downgrade: borra las tablas nuevas y las columnas (el catálogo y las líneas se pierden).

Revision ID: a9d4c7e1f2b6
Revises: d3b7a9e1c5f8
Create Date: 2026-11-04 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9d4c7e1f2b6"
down_revision: str | None = "d3b7a9e1c5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "producto_canonico",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column("nombre", sa.String(200), nullable=False, unique=True),
        sa.Column("clave_semilla", sa.String(100), nullable=True, unique=True),
        sa.Column("familia", sa.String(100), nullable=True),
        sa.Column("unspsc_sugerido", sa.String(20), nullable=True),
        sa.Column("unidad_base", sa.String(3), nullable=False),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "creado_por", _BIGINT, sa.ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "producto_formato",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column(
            "producto_id",
            _BIGINT,
            sa.ForeignKey("producto_canonico.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nombre", sa.String(100), nullable=False),
        sa.Column("factor_base", sa.Float(), nullable=False),
        sa.Column("ean", sa.String(20), nullable=True),
        sa.UniqueConstraint("producto_id", "nombre", name="uq_producto_formato"),
        sa.CheckConstraint("factor_base > 0", name="factor"),
    )
    op.create_table(
        "producto_alias",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column("texto_normalizado", sa.String(300), nullable=False, unique=True),
        sa.Column(
            "producto_id",
            _BIGINT,
            sa.ForeignKey("producto_canonico.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "formato_id",
            _BIGINT,
            sa.ForeignKey("producto_formato.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("fuente", sa.String(10), nullable=False),
        sa.Column("confianza", sa.Float(), nullable=False),
        sa.Column(
            "revisado_por",
            _BIGINT,
            sa.ForeignKey("usuarios.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revisado_en", sa.DateTime(), nullable=True),
        sa.Column("veces_visto", sa.Integer(), nullable=False),
    )
    op.create_index("ix_producto_alias_producto_id", "producto_alias", ["producto_id"])

    op.create_table(
        "ca_lineas",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column(
            "ca_codigo",
            sa.String(50),
            sa.ForeignKey("compras_agiles.codigo", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("n", sa.Integer(), nullable=False),
        sa.Column(
            "producto_id",
            _BIGINT,
            sa.ForeignKey("producto_canonico.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "formato_id",
            _BIGINT,
            sa.ForeignKey("producto_formato.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("cantidad", sa.Float(), nullable=True),
        sa.Column("cantidad_base", sa.Float(), nullable=True),
        sa.Column("texto_origen", sa.Text(), nullable=False),
        sa.Column("texto_norm", sa.String(300), nullable=False, server_default=""),
        sa.Column("codigo_unspsc", sa.String(20), nullable=True),
        sa.Column("origen", sa.String(10), nullable=False),
        sa.Column("estado", sa.String(10), nullable=False),
        sa.Column(
            "revisado_por",
            _BIGINT,
            sa.ForeignKey("usuarios.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("revisado_en", sa.DateTime(), nullable=True),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("ca_codigo", "n", name="uq_ca_linea"),
    )
    op.create_index("ix_ca_lineas_producto_id", "ca_lineas", ["producto_id"])
    op.create_index("ix_ca_lineas_estado", "ca_lineas", ["estado"])
    op.create_index("ix_ca_lineas_texto_norm", "ca_lineas", ["texto_norm"])

    with op.batch_alter_table("compras_agiles") as batch:
        batch.add_column(
            sa.Column("detalle_lineas", sa.String(12), nullable=False, server_default="sin_detalle")
        )
    op.create_index("ix_compras_agiles_detalle_lineas", "compras_agiles", ["detalle_lineas"])

    # proveedor_productos.producto_id: de Integer sin FK a BigInt con FK. Antes, a NULL
    # (nada pudo apuntar a un catálogo que no existía).
    op.execute("UPDATE proveedor_productos SET producto_id = NULL")
    with op.batch_alter_table("proveedor_productos") as batch:
        batch.alter_column("producto_id", existing_type=sa.Integer(), type_=_BIGINT, nullable=True)
        batch.add_column(sa.Column("formato_id", _BIGINT, nullable=True))
        batch.create_foreign_key(
            "fk_proveedor_productos_producto_id",
            "producto_canonico",
            ["producto_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_foreign_key(
            "fk_proveedor_productos_formato_id",
            "producto_formato",
            ["formato_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("proveedor_productos") as batch:
        batch.drop_constraint("fk_proveedor_productos_formato_id", type_="foreignkey")
        batch.drop_constraint("fk_proveedor_productos_producto_id", type_="foreignkey")
        batch.drop_column("formato_id")
        batch.alter_column("producto_id", existing_type=_BIGINT, type_=sa.Integer(), nullable=True)
    op.drop_index("ix_compras_agiles_detalle_lineas", table_name="compras_agiles")
    with op.batch_alter_table("compras_agiles") as batch:
        batch.drop_column("detalle_lineas")
    op.drop_index("ix_ca_lineas_texto_norm", table_name="ca_lineas")
    op.drop_index("ix_ca_lineas_estado", table_name="ca_lineas")
    op.drop_index("ix_ca_lineas_producto_id", table_name="ca_lineas")
    op.drop_table("ca_lineas")
    op.drop_index("ix_producto_alias_producto_id", table_name="producto_alias")
    op.drop_table("producto_alias")
    op.drop_table("producto_formato")
    op.drop_table("producto_canonico")
