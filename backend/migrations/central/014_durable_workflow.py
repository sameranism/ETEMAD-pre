"""P29: authoritative workflow jobs and transactional outbox."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "014_durable_workflow"
down_revision = "013_daily_reports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA orchestration")
    op.create_table(
        "outbox",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("event_key", sa.String(160), nullable=False, unique=True),
        sa.Column("envelope", JSONB(), nullable=False),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("project_id", sa.String(3), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("stream IN ('domestic','foreign','manual')", name="ck_workflow_event_stream"),
        sa.CheckConstraint("project_id IN ('P12','P13')", name="ck_workflow_event_target"),
        sa.CheckConstraint("(envelope->>'classification' = 'internal' AND "
                           "envelope->>'event_id' = event_id AND "
                           "envelope->>'schema_version' = '1.0') IS TRUE", name="ck_workflow_event_internal"),
        schema="orchestration",
    )
    op.create_index("ix_workflow_outbox_pending", "outbox", ["created_at", "event_id"],
                    schema="orchestration", postgresql_where=sa.text("published_at IS NULL"))
    op.create_table(
        "jobs",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("orchestration.outbox.event_id"), nullable=False, unique=True),
        sa.Column("project_id", sa.String(3), nullable=False),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("payload_ref", sa.String(256), nullable=False),
        sa.Column("correlation_id", sa.String(36), nullable=False),
        sa.Column("state", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.String(32)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "idempotency_key", name="uq_workflow_job_identity"),
        sa.CheckConstraint("state IN ('queued','running','failed','held','done','cancelled')",
                           name="ck_workflow_job_state"),
        sa.CheckConstraint("project_id IN ('P12','P13') AND stream IN ('domestic','foreign','manual')",
                           name="ck_workflow_job_scope"),
        sa.CheckConstraint("attempts BETWEEN 0 AND max_attempts AND max_attempts BETWEEN 1 AND 10",
                           name="ck_workflow_job_attempts"),
        sa.CheckConstraint("(state='running' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) "
                           "OR (state<>'running' AND lease_token IS NULL AND lease_until IS NULL)",
                           name="ck_workflow_job_lease"),
        schema="orchestration",
    )
    op.create_index("ix_workflow_jobs_due", "jobs", ["project_id", "stream", "state", "created_at"],
                    schema="orchestration")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE orchestration.jobs, orchestration.outbox IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM orchestration.jobs) OR EXISTS (SELECT 1 FROM orchestration.outbox)
          THEN RAISE EXCEPTION 'workflow history is not empty'; END IF;
        END $$;
    """)
    op.drop_table("jobs", schema="orchestration")
    op.drop_table("outbox", schema="orchestration")
    op.execute("DROP SCHEMA orchestration")
