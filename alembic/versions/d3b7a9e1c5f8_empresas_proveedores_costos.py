"""Empresas, proveedores, productos con precio y costo_historial, tarifas de logística (F-plat-3).

Pivote plataforma de Compra Ágil: todo dato comercial pertenece a una empresa (regla 26).
`usuarios.empresa_id` es nullable (los usuarios actuales quedan sin empresa). Ninguna de estas
tablas se purga. `proveedor_productos.producto_id` queda sin FK: F-plat-1 la agrega cuando
exista `producto_canonico`.

Downgrade: borra las tablas nuevas y la columna (los datos comerciales se pierden).

Revision ID: d3b7a9e1c5f8
Revises: b8d4f1a6c2e9
Create Date: 2026-11-04 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "d3b7a9e1c5f8"
down_revision: str | None = "b8d4f1a6c2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "empresas",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column("nombre", sa.String(200), nullable=False),
        sa.Column("rut", sa.String(20), nullable=True, unique=True),
        sa.Column("comuna_base", sa.String(100), nullable=True),
        sa.Column("regiones_prioritarias", _JSONB, nullable=False),
        sa.Column("comunas_prioritarias", _JSONB, nullable=False),
        sa.Column("activa", sa.Boolean(), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
    )
    with op.batch_alter_table("usuarios") as batch:
        batch.add_column(sa.Column("empresa_id", _BIGINT, nullable=True))
        batch.create_foreign_key(
            "fk_usuarios_empresa_id", "empresas", ["empresa_id"], ["id"], ondelete="SET NULL"
        )

    op.create_table(
        "proveedores",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_id", _BIGINT, sa.ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("nombre", sa.String(300), nullable=False),
        sa.Column("rut", sa.String(20), nullable=True),
        sa.Column("comuna", sa.String(100), nullable=True),
        sa.Column("direccion", sa.String(300), nullable=True),
        sa.Column("lat", sa.Float(), nullable=True),
        sa.Column("lon", sa.Float(), nullable=True),
        sa.Column("contacto_nombre", sa.String(200), nullable=True),
        sa.Column("telefono", sa.String(50), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("rubros", _JSONB, nullable=False),
        sa.Column("plazo_pago_dias", sa.Integer(), nullable=True),
        sa.Column("minimo_compra_clp", _BIGINT, nullable=True),
        sa.Column("despacha", sa.Boolean(), nullable=False),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.Column("activo", sa.Boolean(), nullable=False),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_proveedores_empresa_nombre", "proveedores", ["empresa_id", "nombre"])

    op.create_table(
        "proveedor_productos",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column(
            "proveedor_id",
            _BIGINT,
            sa.ForeignKey("proveedores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("producto_id", sa.Integer(), nullable=True),
        sa.Column("producto_texto", sa.String(300), nullable=False),
        sa.Column("codigo_unspsc", sa.String(20), nullable=True),
        sa.Column("formato_texto", sa.String(200), nullable=False, server_default=""),
        sa.Column("factor_base", sa.Float(), nullable=True),
        sa.Column("unidad_base", sa.String(10), nullable=True),
        sa.Column("precio_neto_actual", sa.Float(), nullable=False),
        sa.Column("vigente_desde", sa.Date(), nullable=False),
        sa.Column("fuente", sa.String(20), nullable=False),
        sa.Column("disponible", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("creado_en", sa.DateTime(), nullable=False),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "proveedor_id", "producto_texto", "formato_texto", name="uq_proveedor_producto_formato"
        ),
    )

    op.create_table(
        "costo_historial",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column(
            "proveedor_producto_id",
            _BIGINT,
            sa.ForeignKey("proveedor_productos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("precio_neto", sa.Float(), nullable=False),
        sa.Column("vigente_desde", sa.Date(), nullable=False),
        sa.Column("fuente", sa.String(20), nullable=False),
        sa.Column(
            "usuario_id", _BIGINT, sa.ForeignKey("usuarios.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("registrado_en", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_costo_historial_pp_vigente", "costo_historial", ["proveedor_producto_id", "vigente_desde"]
    )

    op.create_table(
        "tarifas_logistica",
        sa.Column("id", _BIGINT, primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_id", _BIGINT, sa.ForeignKey("empresas.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("ambito", sa.String(20), nullable=False),
        sa.Column("nombre", sa.String(100), nullable=False),
        sa.Column("costo_fijo_clp", _BIGINT, nullable=False),
        sa.Column("costo_por_kg_clp", sa.Float(), nullable=False),
        sa.Column("dias", sa.Integer(), nullable=True),
        sa.Column("notas", sa.Text(), nullable=True),
        sa.UniqueConstraint("empresa_id", "ambito", "nombre", name="uq_tarifa_empresa_ambito_nombre"),
    )


def downgrade() -> None:
    op.drop_table("tarifas_logistica")
    op.drop_index("ix_costo_historial_pp_vigente", table_name="costo_historial")
    op.drop_table("costo_historial")
    op.drop_table("proveedor_productos")
    op.drop_index("ix_proveedores_empresa_nombre", table_name="proveedores")
    op.drop_table("proveedores")
    with op.batch_alter_table("usuarios") as batch:
        batch.drop_constraint("fk_usuarios_empresa_id", type_="foreignkey")
        batch.drop_column("empresa_id")
    op.drop_table("empresas")
