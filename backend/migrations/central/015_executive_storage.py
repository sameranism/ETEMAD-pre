"""P19: independent executive index, encrypted objects outside the public store."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "015_executive_storage"
down_revision = "014_durable_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Do not silently drop, copy or expose legacy plaintext. Existing records
    # require an explicitly authorized encrypted migration with verified keys.
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE insights.executive_reports IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM insights.executive_reports)
          THEN RAISE EXCEPTION 'legacy private reports require encrypted migration'; END IF;
        END $$;
    """)
    op.execute("CREATE SCHEMA executive")
    op.execute("REVOKE ALL ON SCHEMA executive FROM PUBLIC")
    op.create_table(
        "private_reports",
        sa.Column("subject_id", sa.String(64), primary_key=True),
        sa.Column("report_id", sa.String(64), primary_key=True),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("content_ref", sa.String(96), nullable=False, unique=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("publishable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("slot BETWEEN 5 AND 7", name="ck_private_report_slot"),
        sa.CheckConstraint("publishable = false", name="ck_private_report_nonpublishable"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_private_report_hash"),
        sa.CheckConstraint("content_ref = 'reports/' || sha256 || '.bin'", name="ck_private_report_ref"),
        schema="executive",
    )
    op.create_index("ix_private_report_latest", "private_reports",
                    ["subject_id", "slot", "created_at"], schema="executive")
    op.execute("REVOKE ALL ON TABLE executive.private_reports FROM PUBLIC")
    op.drop_table("executive_reports", schema="insights")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE executive.private_reports IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM executive.private_reports)
          THEN RAISE EXCEPTION 'report history is not empty'; END IF;
        END $$;
    """)
    op.drop_table("private_reports", schema="executive")
    op.execute("DROP SCHEMA executive")
    op.create_table(
        "executive_reports",
        sa.Column("subject_id", sa.String(64), primary_key=True),
        sa.Column("report_id", sa.String(64), primary_key=True),
        sa.Column("slot", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("slot BETWEEN 5 AND 7", name="ck_executive_reports_slot"),
        sa.CheckConstraint("payload->>'visibility' = 'executive_private'", name="ck_executive_reports_visibility"),
        sa.CheckConstraint("payload->>'report_id' = report_id", name="ck_executive_reports_identity"),
        sa.CheckConstraint("payload->>'publishable' = 'false' AND payload->>'owner_role' = 'editor_in_chief'",
                           name="ck_executive_nonpublishable"),
        schema="insights",
    )
    op.create_index("ix_executive_reports_latest", "executive_reports",
                    ["subject_id", "slot", "created_at"], schema="insights")
