"""Demanda por comprador, pronóstico y abastecimiento (F-plat-6).

- `demanda_series`: comprador × nivel (producto | unspsc | familia) × ítem × mes, desde las OC
  (y las líneas `ok` de las CA cerradas sin OC). Derivada: la reescribe `mercado-demanda`.
- `pronosticos`: pronóstico v1 a 3 meses con la línea base al lado. Derivada.
- `demanda_nombres`: nombres de compradores e ítems para la UI (ningún request recorre las OC).
- `recomendaciones`: de la empresa (regla 26); upsert que conserva el estado puesto por el usuario.

Solo tablas nuevas: no toca datos existentes.

Revision ID: c8f3a1e6d2b4
Revises: b3e7c1d9f4a2
Create Date: 2026-11-05 23:59:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8f3a1e6d2b4"
down_revision: str | None = "b3e7c1d9f4a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _int(nombre: str) -> sa.Column:
    return sa.Column(nombre, sa.Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    op.create_table(
        "demanda_series",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("comprador", sa.String(600), nullable=False),
        sa.Column("nivel", sa.String(8), nullable=False),
        sa.Column("item", sa.String(100), nullable=False),
        sa.Column("mes", sa.String(7), nullable=False),
        sa.Column("region", sa.Integer(), nullable=True),
        _int("n_oc"),
        _int("n_ca"),
        sa.Column("cantidad_base", sa.Float(), nullable=True),
        sa.Column("base", sa.String(3), nullable=True),
        sa.Column("monto_neto", sa.Float(), nullable=False, server_default="0"),
        sa.Column("precio_mediana", sa.Float(), nullable=True),
        _int("n_proveedores"),
    )
    op.create_index("ix_demanda_series_mes_region", "demanda_series", ["mes", "region"])
    op.create_index("ix_demanda_series_item", "demanda_series", ["nivel", "item"])
    op.create_index("ix_demanda_series_comprador", "demanda_series", ["comprador"])

    op.create_table(
        "pronosticos",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("comprador", sa.String(600), nullable=False),
        sa.Column("nivel", sa.String(8), nullable=False),
        sa.Column("item", sa.String(100), nullable=False),
        sa.Column("region", sa.Integer(), nullable=True),
        sa.Column("base", sa.String(3), nullable=True),
        sa.Column("mes_objetivo", sa.String(7), nullable=False),
        sa.Column("p_compra", sa.Float(), nullable=True),
        sa.Column("cantidad_esperada", sa.Float(), nullable=True),
        sa.Column("monto_esperado", sa.Float(), nullable=True),
        sa.Column("intervalo_min", sa.Float(), nullable=True),
        sa.Column("intervalo_max", sa.Float(), nullable=True),
        sa.Column("debil", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("compra_base", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cantidad_linea_base", sa.Float(), nullable=True),
        sa.Column("monto_linea_base", sa.Float(), nullable=True),
        _int("n_meses_con_compra"),
        sa.Column("metodo", sa.String(12), nullable=False),
        sa.Column("generado_en", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pronosticos_region", "pronosticos", ["region"])

    op.create_table(
        "demanda_nombres",
        sa.Column("tipo", sa.String(10), primary_key=True),
        sa.Column("clave", sa.String(620), primary_key=True),
        sa.Column("nombre", sa.String(600), nullable=False, server_default=""),
        sa.Column("region", sa.Integer(), nullable=True),
        sa.Column("datos", postgresql.JSONB(), nullable=True),
    )

    op.create_table(
        "recomendaciones",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "empresa_id",
            sa.BigInteger(),
            sa.ForeignKey("empresas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tipo", sa.String(20), nullable=False),
        sa.Column("nivel", sa.String(8), nullable=False),
        sa.Column("item", sa.String(100), nullable=False),
        sa.Column("comprador", sa.String(600), nullable=False, server_default=""),
        sa.Column("mes_objetivo", sa.String(7), nullable=False),
        sa.Column("cantidad", sa.Float(), nullable=True),
        sa.Column("texto", sa.String(1000), nullable=False),
        sa.Column("datos", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("generada_en", sa.DateTime(), nullable=False),
        sa.Column("estado", sa.String(12), nullable=False, server_default="nueva"),
        sa.Column(
            "usuario_id",
            sa.BigInteger(),
            sa.ForeignKey("usuarios.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actualizada_en", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "empresa_id",
            "tipo",
            "nivel",
            "item",
            "comprador",
            "mes_objetivo",
            name="uq_recomendacion",
        ),  # fmt: skip
    )
    op.create_index(
        "ix_recomendaciones_empresa_estado", "recomendaciones", ["empresa_id", "estado"]
    )


def downgrade() -> None:
    op.drop_index("ix_recomendaciones_empresa_estado", table_name="recomendaciones")
    op.drop_table("recomendaciones")
    op.drop_table("demanda_nombres")
    op.drop_index("ix_pronosticos_region", table_name="pronosticos")
    op.drop_table("pronosticos")
    op.drop_index("ix_demanda_series_comprador", table_name="demanda_series")
    op.drop_index("ix_demanda_series_item", table_name="demanda_series")
    op.drop_index("ix_demanda_series_mes_region", table_name="demanda_series")
    op.drop_table("demanda_series")
