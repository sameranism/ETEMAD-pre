"""Add central-owned durable P07 ingress; foreign never opens this database."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "006_foreign_inbox"
down_revision = "005_source_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "foreign_inbox",
        sa.Column("news_id", sa.String(64), primary_key=True),
        sa.Column("event_id", sa.String(80), nullable=False, unique=True),
        sa.Column("idempotency_key", sa.String(80), nullable=False, unique=True),
        sa.Column("sender", sa.String(64), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("envelope", postgresql.JSONB(), nullable=False),
        sa.Column("raw", postgresql.JSONB(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("raw->>'stream' = 'foreign' AND raw->>'news_id' = news_id",
                           name="ck_foreign_inbox_raw"),
        sa.CheckConstraint("envelope->>'classification' = 'internal' AND envelope->>'producer_project' = 'P06'",
                           name="ck_foreign_inbox_event"),
        schema="processing",
    )
    op.create_index("ix_foreign_inbox_pending", "foreign_inbox", ["received_at", "news_id"],
                    schema="processing", postgresql_where=sa.text("processed_at IS NULL"))


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE processing.foreign_inbox IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM processing.foreign_inbox) THEN
            RAISE EXCEPTION 'foreign inbox is not empty; preserve received originals';
          END IF;
        END $$;
    """)
    op.drop_table("foreign_inbox", schema="processing")
