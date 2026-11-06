"""oc_lineas_ca.id_oc: el `ID` numérico de la OC de oc-da (F-plat-6-cruce-2).

`ca_cotizaciones.id_oc` y `compras_agiles.id_orden_compra` traen el `ID` numérico de la OC
(`55672374`); `oc_lineas_ca` solo guardaba el `Codigo` (`2724-973-AG26`) y el cruce CA ↔ OC daba 0.
Columna nullable (texto) + índice no único (una OC tiene varias líneas). Las filas existentes
quedan en NULL hasta la recarga `run-once --job mercado-oc --desde 2024-10 --forzar`.

Revision ID: d9a4e2b7c1f6
Revises: c8f3a1e6d2b4
Create Date: 2026-11-05 23:59:30.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9a4e2b7c1f6"
down_revision: str | None = "c8f3a1e6d2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("oc_lineas_ca", sa.Column("id_oc", sa.String(20), nullable=True))
    op.create_index("ix_oc_lineas_ca_id_oc", "oc_lineas_ca", ["id_oc"])


def downgrade() -> None:
    op.drop_index("ix_oc_lineas_ca_id_oc", table_name="oc_lineas_ca")
    op.drop_column("oc_lineas_ca", "id_oc")
