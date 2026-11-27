"""P18/P19: separate immutable report collections, never a shared public query."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "013_daily_reports"
down_revision = "012_ai_cost_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, slots, visibility in (
        ("public_report_drafts", "1 AND 4", "public"),
        ("executive_reports", "5 AND 7", "executive_private"),
    ):
        op.create_table(
            name,
            sa.Column("subject_id", sa.String(64), primary_key=True),
            sa.Column("report_id", sa.String(64), primary_key=True),
            sa.Column("slot", sa.Integer(), nullable=False),
            sa.Column("payload", JSONB(), nullable=False),
            sa.Column("evidence", JSONB(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.CheckConstraint("slot BETWEEN " + slots, name="ck_" + name + "_slot"),
            sa.CheckConstraint("payload->>'visibility' = '" + visibility + "'",
                               name="ck_" + name + "_visibility"),
            sa.CheckConstraint("payload->>'report_id' = report_id", name="ck_" + name + "_identity"),
            schema="insights",
        )
        op.create_index("ix_" + name + "_latest", name, ["subject_id", "slot", "created_at"], schema="insights")
    op.execute("""ALTER TABLE insights.executive_reports ADD CONSTRAINT ck_executive_nonpublishable
        CHECK (payload->>'publishable' = 'false' AND payload->>'owner_role' = 'editor_in_chief')""")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE insights.public_report_drafts, insights.executive_reports IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM insights.public_report_drafts)
             OR EXISTS (SELECT 1 FROM insights.executive_reports)
          THEN RAISE EXCEPTION 'report history is not empty'; END IF;
        END $$;
    """)
    op.drop_table("executive_reports", schema="insights")
    op.drop_table("public_report_drafts", schema="insights")
