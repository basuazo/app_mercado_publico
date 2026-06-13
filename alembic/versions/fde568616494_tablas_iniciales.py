"""tablas_iniciales

Revision ID: fde568616494
Revises:
Create Date: 2026-06-12

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "fde568616494"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- Extensiones ---
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    # Función inmutable para usar unaccent en índices generados
    op.execute("""
        CREATE OR REPLACE FUNCTION inmutable_unaccent(text)
        RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT AS
        $$ SELECT public.unaccent('public.unaccent', $1) $$
    """)

    # --- quota_log (ya creada por QuotaTracker, pero la declaramos aquí para coherencia) ---
    op.create_table(
        "quota_log",
        sa.Column("fecha", sa.Date, primary_key=True),
        sa.Column("requests_usadas", sa.Integer, nullable=False, server_default="0"),
    )

    # --- usuarios ---
    op.create_table(
        "usuarios",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("rol", sa.String(20), nullable=False, server_default="usuario"),
        sa.Column("activo", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("creado_en", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("email", name="uq_usuarios_email"),
    )

    # --- organismos ---
    op.create_table(
        "organismos",
        sa.Column("codigo", sa.String(50), primary_key=True),
        sa.Column("nombre", sa.String(500), nullable=False),
        sa.Column("rut", sa.String(20), nullable=True),
        sa.Column("actualizado_en", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
    )

    # --- licitaciones ---
    op.create_table(
        "licitaciones",
        sa.Column("codigo", sa.String(50), primary_key=True),
        sa.Column("nombre", sa.String(1000), nullable=False, server_default=""),
        sa.Column("descripcion", sa.Text, nullable=False, server_default=""),
        sa.Column("estado_codigo", sa.Integer, nullable=True),
        sa.Column("estado", sa.String(50), nullable=False, server_default="desconocido"),
        sa.Column("tipo", sa.String(10), nullable=True),
        sa.Column("fecha_publicacion", sa.DateTime, nullable=True),
        sa.Column("fecha_cierre", sa.DateTime, nullable=True),
        sa.Column("moneda", sa.String(10), nullable=True),
        sa.Column("monto_estimado", sa.Float, nullable=True),
        sa.Column("monto_clp", sa.Float, nullable=True),
        sa.Column("codigo_organismo", sa.String(50), nullable=True),
        sa.Column("raw_json", postgresql.JSONB, nullable=True),
        sa.Column("detalle_obtenido", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("creado_en", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
        sa.Column("actualizado_en", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_licitaciones_estado", "licitaciones", ["estado"])
    op.create_index("ix_licitaciones_fecha_cierre", "licitaciones", ["fecha_cierre"])

    # Columna tsvector generada + índice GIN para FTS
    op.execute("""
        ALTER TABLE licitaciones
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('spanish',
                inmutable_unaccent(coalesce(nombre, '')) || ' ' ||
                inmutable_unaccent(coalesce(descripcion, ''))
            )
        ) STORED
    """)
    op.create_index("ix_licitaciones_tsv", "licitaciones", ["tsv"], postgresql_using="gin")

    # --- licitacion_items ---
    op.create_table(
        "licitacion_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "licitacion_codigo",
            sa.String(50),
            sa.ForeignKey("licitaciones.codigo", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("codigo_producto", sa.String(100), nullable=False, server_default=""),
        sa.Column("nombre", sa.String(500), nullable=False, server_default=""),
        sa.Column("cantidad", sa.Float, nullable=True),
        sa.Column("unidad", sa.String(50), nullable=False, server_default=""),
    )

    # --- compras_agiles ---
    op.create_table(
        "compras_agiles",
        sa.Column("codigo", sa.String(50), primary_key=True),
        sa.Column("nombre", sa.String(1000), nullable=False, server_default=""),
        sa.Column("descripcion", sa.Text, nullable=False, server_default=""),
        sa.Column("estado", sa.String(50), nullable=False, server_default="desconocido"),
        sa.Column("estado_convocatoria", sa.Integer, nullable=True),
        sa.Column("fecha_publicacion", sa.DateTime, nullable=True),
        sa.Column("fecha_cierre", sa.DateTime, nullable=True),
        sa.Column("fecha_ultimo_cambio", sa.DateTime, nullable=True),
        sa.Column("moneda", sa.String(10), nullable=True),
        sa.Column("monto_disponible_clp", sa.Float, nullable=True),
        sa.Column("organismo_nombre", sa.String(500), nullable=True),
        sa.Column("organismo_rut", sa.String(20), nullable=True),
        sa.Column("region", sa.Integer, nullable=True),
        sa.Column("total_ofertas", sa.Integer, nullable=False, server_default="0"),
        sa.Column("id_orden_compra", sa.String(50), nullable=True),
        sa.Column("raw_json", postgresql.JSONB, nullable=True),
        sa.Column("creado_en", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
        sa.Column("actualizado_en", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index("ix_compras_agiles_estado", "compras_agiles", ["estado"])
    op.create_index("ix_compras_agiles_region", "compras_agiles", ["region"])
    op.create_index(
        "ix_compras_agiles_fecha_ultimo_cambio", "compras_agiles", ["fecha_ultimo_cambio"]
    )

    op.execute("""
        ALTER TABLE compras_agiles
        ADD COLUMN tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('spanish',
                inmutable_unaccent(coalesce(nombre, '')) || ' ' ||
                inmutable_unaccent(coalesce(descripcion, ''))
            )
        ) STORED
    """)
    op.create_index("ix_compras_agiles_tsv", "compras_agiles", ["tsv"], postgresql_using="gin")

    # --- ca_productos ---
    op.create_table(
        "ca_productos",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "ca_codigo",
            sa.String(50),
            sa.ForeignKey("compras_agiles.codigo", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("codigo_producto", sa.String(100), nullable=False, server_default=""),
        sa.Column("nombre", sa.String(500), nullable=False, server_default=""),
        sa.Column("descripcion", sa.String(1000), nullable=False, server_default=""),
        sa.Column("cantidad", sa.Float, nullable=True),
        sa.Column("unidad", sa.String(50), nullable=False, server_default=""),
    )

    # --- ordenes_compra ---
    op.create_table(
        "ordenes_compra",
        sa.Column("codigo", sa.String(50), primary_key=True),
        sa.Column("nombre", sa.String(1000), nullable=False, server_default=""),
        sa.Column("tipo_oc", sa.Integer, nullable=True),
        sa.Column("estado", sa.String(50), nullable=False, server_default="desconocido"),
    )

    # --- perfiles_busqueda ---
    op.create_table(
        "perfiles_busqueda",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "owner_id",
            sa.BigInteger,
            sa.ForeignKey("usuarios.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nombre", sa.String(255), nullable=False),
        sa.Column("keywords", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("keywords_excluir", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("regiones", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("monto_min_clp", sa.Float, nullable=True),
        sa.Column("monto_max_clp", sa.Float, nullable=True),
        sa.Column(
            "fuentes",
            postgresql.JSONB,
            nullable=False,
            server_default='["licitaciones","compras_agiles"]',
        ),
        sa.Column("frecuencia_alerta", sa.String(20), nullable=False, server_default="digest"),
        sa.Column("activo", sa.Boolean, nullable=False, server_default="true"),
    )

    # --- oportunidades_match ---
    op.create_table(
        "oportunidades_match",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "perfil_id",
            sa.BigInteger,
            sa.ForeignKey("perfiles_busqueda.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fuente", sa.String(30), nullable=False),
        sa.Column("codigo_oportunidad", sa.String(50), nullable=False),
        sa.Column("score", sa.Float, nullable=False, server_default="0"),
        sa.Column("razones", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("fecha_match", sa.DateTime, nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("perfil_id", "fuente", "codigo_oportunidad", name="uq_match"),
    )
    op.create_index("ix_oportunidades_match_perfil", "oportunidades_match", ["perfil_id"])

    # --- alertas ---
    op.create_table(
        "alertas",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "match_id",
            sa.BigInteger,
            sa.ForeignKey("oportunidades_match.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tipo", sa.String(50), nullable=False),
        sa.Column("enviada_en", sa.DateTime, nullable=True),
        sa.Column("canal", sa.String(20), nullable=False, server_default="email"),
        sa.Column("estado", sa.String(20), nullable=False, server_default="pendiente"),
    )

    # --- sync_state ---
    op.create_table(
        "sync_state",
        sa.Column("fuente", sa.String(50), primary_key=True),
        sa.Column("cursor", sa.String(500), nullable=True),
        sa.Column("ultima_ejecucion", sa.DateTime, nullable=True),
        sa.Column("ultimo_ok", sa.DateTime, nullable=True),
        sa.Column("requests_usadas_hoy", sa.Integer, nullable=False, server_default="0"),
        sa.Column("fecha_contador", sa.String(10), nullable=True),
        sa.Column("notas", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("sync_state")
    op.drop_table("alertas")
    op.drop_table("oportunidades_match")
    op.drop_table("perfiles_busqueda")
    op.drop_table("ordenes_compra")
    op.drop_table("ca_productos")
    op.drop_index("ix_compras_agiles_tsv", table_name="compras_agiles")
    op.drop_index("ix_compras_agiles_fecha_ultimo_cambio", table_name="compras_agiles")
    op.drop_index("ix_compras_agiles_region", table_name="compras_agiles")
    op.drop_index("ix_compras_agiles_estado", table_name="compras_agiles")
    op.drop_table("compras_agiles")
    op.drop_table("licitacion_items")
    op.drop_index("ix_licitaciones_tsv", table_name="licitaciones")
    op.drop_index("ix_licitaciones_fecha_cierre", table_name="licitaciones")
    op.drop_index("ix_licitaciones_estado", table_name="licitaciones")
    op.drop_table("licitaciones")
    op.drop_table("organismos")
    op.drop_table("usuarios")
    op.drop_table("quota_log")
    op.execute("DROP FUNCTION IF EXISTS inmutable_unaccent(text)")
    op.execute("DROP EXTENSION IF EXISTS unaccent")
