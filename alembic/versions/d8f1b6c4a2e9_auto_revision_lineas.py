"""Confirmación automática de líneas de anexo por excepción (F-plat-auto-revision).

- `ca_lineas.confirmada_por` (`auto` | `humano`, NULL en las de la API, las manuales y las
  anteriores), `ca_lineas.puntaje_auto` (0–1) y `ca_lineas.control_en` (la línea auto-confirmada
  entró al muestreo de control). Todas nullable y sin default: ninguna fila existente cambia de
  significado.
- Tabla nueva `auto_control_semana` (semana ISO de Chile → controles revisados y errores): la
  precisión observada que muestra /salud y con la que el job sube el umbral.
- Índice parcial `ix_ca_lineas_auto` sobre las auto-confirmadas (el muestreo semanal y /salud
  las filtran por `creado_en`).

Revision ID: d8f1b6c4a2e9
Revises: c6d9a3f1e8b2
Create Date: 2026-11-06 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d8f1b6c4a2e9"
down_revision: str | None = "c6d9a3f1e8b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ca_lineas", sa.Column("confirmada_por", sa.String(10), nullable=True))
    op.add_column("ca_lineas", sa.Column("puntaje_auto", sa.Float(), nullable=True))
    op.add_column("ca_lineas", sa.Column("control_en", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_ca_lineas_auto",
        "ca_lineas",
        ["creado_en"],
        postgresql_where=sa.text("confirmada_por = 'auto'"),
    )
    op.create_table(
        "auto_control_semana",
        sa.Column("semana", sa.String(8), primary_key=True),
        sa.Column("controles", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errores", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actualizado_en", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("auto_control_semana")
    op.drop_index("ix_ca_lineas_auto", table_name="ca_lineas")
    op.drop_column("ca_lineas", "control_en")
    op.drop_column("ca_lineas", "puntaje_auto")
    op.drop_column("ca_lineas", "confirmada_por")
