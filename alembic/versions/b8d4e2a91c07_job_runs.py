"""Historial de corridas de jobs (observabilidad + dead-man's switch).

Revision ID: b8d4e2a91c07
Revises: f3a9b8c7d6e5
Create Date: 2026-09-20 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b8d4e2a91c07"
down_revision: str | None = "f3a9b8c7d6e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "job_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("job", sa.String(50), nullable=False),
        sa.Column("iniciado_en", sa.DateTime, nullable=False),
        sa.Column("terminado_en", sa.DateTime, nullable=True),
        sa.Column("estado", sa.String(20), nullable=False),
        sa.Column("resultado_json", postgresql.JSONB, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
    )
    # Sirve a /api/salud/jobs (último "ok" por job) y a la purga de retención.
    op.create_index(
        "ix_job_runs_job_iniciado",
        "job_runs",
        ["job", sa.text("iniciado_en DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_job_runs_job_iniciado", table_name="job_runs")
    op.drop_table("job_runs")
