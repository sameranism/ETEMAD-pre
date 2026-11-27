"""P15: immutable role- and subject-scoped analysis history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "018_analyses"
down_revision = "017_metric_snapshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "analyses",
        sa.Column("subject_id", sa.String(64), primary_key=True),
        sa.Column("role", sa.String(32), primary_key=True),
        sa.Column("analysis_id", sa.String(64), primary_key=True),
        sa.Column("result", JSONB(), nullable=False),
        sa.Column("evidence", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("role IN ('owner','editor_in_chief','staff')", name="ck_analysis_role"),
        sa.CheckConstraint("result->>'schema_version' = '1.0'", name="ck_analysis_version"),
        sa.CheckConstraint("result->>'analysis_id' = analysis_id", name="ck_analysis_identity"),
        sa.CheckConstraint("jsonb_typeof(evidence) = 'array'", name="ck_analysis_evidence"),
        schema="insights",
    )
    op.create_index(
        "ix_analyses_latest", "analyses",
        ["subject_id", "role", "created_at", "analysis_id"], schema="insights",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE insights.analyses IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM insights.analyses)
          THEN RAISE EXCEPTION 'analysis history is not empty'; END IF;
        END $$;
    """)
    op.drop_table("analyses", schema="insights")
