"""P27: durable, subject-scoped saved workbench views."""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "016_saved_views"
down_revision = "015_executive_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA ui")
    op.create_table(
        "saved_views",
        sa.Column("view_id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("dashboard_type", sa.String(32), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("filters_json", JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "dashboard_type", "name", name="uq_saved_view_name"),
        sa.CheckConstraint("dashboard_type IN ('owner','editor_in_chief','staff')", name="ck_saved_view_role"),
        sa.CheckConstraint("jsonb_typeof(filters_json)='object'", name="ck_saved_view_filter"),
        schema="ui",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE ui.saved_views IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM ui.saved_views)
          THEN RAISE EXCEPTION 'saved views are not empty'; END IF;
        END $$;
    """)
    op.drop_table("saved_views", schema="ui")
    op.execute("DROP SCHEMA ui")
