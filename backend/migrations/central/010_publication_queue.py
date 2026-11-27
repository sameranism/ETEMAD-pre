"""P22/P25 durable per-destination jobs and attempt reservations."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "010_publication_queue"
down_revision = "009_image_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA publish")
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.Text(), primary_key=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("destination_id", sa.Text(), nullable=False),
        sa.Column("object_type", sa.Text(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("record", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("object_type IN ('article','public_report')", name="ck_pub_object"),
        sa.CheckConstraint("state IN ('scheduled','held','in_progress','completed','cancelled')",
                           name="ck_pub_state"),
        sa.CheckConstraint("scheduled_at < expires_at", name="ck_pub_deadline"),
        sa.UniqueConstraint("idempotency_key", "destination_id", name="uq_pub_intent_dest"),
        sa.CheckConstraint("(record->>'object_type' = object_type AND record->>'job_id' = job_id "
                           "AND record->>'idempotency_key' = idempotency_key "
                           "AND record->'destination_ids' = jsonb_build_array(destination_id)) IS TRUE",
                           name="ck_pub_identity"),
        schema="publish",
    )
    op.create_index("ix_pub_due", "jobs", ["state", "scheduled_at"], schema="publish")
    op.create_table(
        "attempts",
        sa.Column("idempotency_key", sa.Text(), primary_key=True),
        sa.Column("destination_id", sa.Text(), primary_key=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("message_id", sa.Text()),
        sa.Column("error_code", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["idempotency_key", "destination_id"],
                                ["publish.jobs.idempotency_key", "publish.jobs.destination_id"]),
        sa.CheckConstraint("status IN ('unknown','success','failed','skipped')", name="ck_pub_attempt"),
        schema="publish",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE publish.jobs, publish.attempts IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM publish.jobs)
          THEN RAISE EXCEPTION 'publication history is not empty'; END IF;
        END $$;
    """)
    op.drop_table("attempts", schema="publish")
    op.drop_table("jobs", schema="publish")
    op.execute("DROP SCHEMA publish")
