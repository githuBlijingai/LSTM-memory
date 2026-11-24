"""retrieval hit gain

Revision ID: 3c9d2a7e5b11
Revises: 74a69ff68f54
Create Date: 2026-11-24 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "3c9d2a7e5b11"
down_revision: str | Sequence[str] | None = "74a69ff68f54"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def enum(*values, name):
    return postgresql.ENUM(*values, name=name, create_type=False)


def upgrade() -> None:
    op.create_table(
        "retrieval_hit_gain",
        sa.Column("gain_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.String(length=128), nullable=False),
        sa.Column("query_hash", sa.CHAR(length=64), nullable=False),
        sa.Column("memory_type", enum("event", "wiki", name="memory_type"), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("gain_date", sa.Date(), nullable=False),
        sa.Column("gained_score", sa.Float(), nullable=False),
        sa.Column("hit_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "gained_score >= 0",
            name=op.f("ck_retrieval_hit_gain_retrieval_hit_gain_nonnegative"),
        ),
        sa.PrimaryKeyConstraint("gain_id", name=op.f("pk_retrieval_hit_gain")),
        sa.UniqueConstraint(
            "actor_id",
            "query_hash",
            "memory_type",
            "memory_id",
            "gain_date",
            name=op.f("uq_retrieval_hit_gain_retrieval_hit_daily_once"),
        ),
    )
    op.create_index(
        "ix_retrieval_hit_recent",
        "retrieval_hit_gain",
        ["actor_id", "memory_type", "memory_id", "hit_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_hit_recent", table_name="retrieval_hit_gain")
    op.drop_table("retrieval_hit_gain")
