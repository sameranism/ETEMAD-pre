"""Durable bounded authentication throttle (P28 central identity).

Revision ID: 004_login_throttle
Revises: 003_processing_history
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "004_login_throttle"
down_revision = "003_processing_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "login_throttle",
        sa.Column("bucket", sa.Integer(), primary_key=True),
        sa.Column("window_started", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("bucket = 1", name="ck_login_throttle_singleton"),
        sa.CheckConstraint("attempts BETWEEN 1 AND 30", name="ck_login_throttle_attempts"),
        schema="identity",
    )


def downgrade() -> None:
    # Derived rate-limit state only; no users, grants or audit records removed.
    op.drop_table("login_throttle", schema="identity")
