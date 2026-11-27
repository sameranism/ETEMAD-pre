"""P17: immutable, subject-scoped measured metric snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "017_metric_snapshots"
down_revision = "016_saved_views"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metric_snapshots",
        sa.Column("subject_id", sa.String(64), primary_key=True),
        sa.Column("snapshot_id", sa.String(64), primary_key=True),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "stream IN ('mixed','domestic','foreign','manual')", name="ck_metric_stream"
        ),
        sa.CheckConstraint("period_start < period_end", name="ck_metric_period"),
        sa.CheckConstraint("payload->>'snapshot_id' = snapshot_id", name="ck_metric_identity"),
        sa.CheckConstraint("payload->>'stream' = stream", name="ck_metric_payload_stream"),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'object'", name="ck_metric_evidence"),
        schema="insights",
    )
    op.create_index(
        "ix_metric_snapshots_latest",
        "metric_snapshots",
        ["subject_id", "stream", "period_start", "period_end", "created_at"],
        schema="insights",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE insights.metric_snapshots IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM insights.metric_snapshots)
          THEN RAISE EXCEPTION 'metric snapshots are not empty'; END IF;
        END $$;
    """)
    op.drop_table("metric_snapshots", schema="insights")
