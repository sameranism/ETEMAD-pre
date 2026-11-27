"""S01-015: durable, content-free AI budget reservations and charge records."""
import sqlalchemy as sa
from alembic import op

revision = "012_ai_cost_ledger"
down_revision = "011_chat_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_cost_reservations",
        sa.Column("request_id", sa.String(36), primary_key=True),
        sa.Column("task", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("model", sa.String(160), nullable=False),
        sa.Column("budget_day", sa.Date(), nullable=False),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("charged_microusd", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("reserved_microusd >= 0 AND charged_microusd >= 0 "
                           "AND charged_microusd <= reserved_microusd", name="ck_ai_cost_bound"),
        sa.CheckConstraint("state IN ('reserved','confirmed','unknown','released')", name="ck_ai_cost_state"),
        sa.CheckConstraint("(state = 'reserved' AND settled_at IS NULL) OR "
                           "(state <> 'reserved' AND settled_at IS NOT NULL)", name="ck_ai_cost_settlement"),
        sa.CheckConstraint("state NOT IN ('reserved','unknown') OR "
                           "charged_microusd = reserved_microusd", name="ck_ai_cost_reserve"),
        sa.CheckConstraint("state <> 'released' OR charged_microusd = 0", name="ck_ai_cost_release"),
        schema="control",
    )
    op.create_index("ix_ai_cost_day_task", "ai_cost_reservations", ["budget_day", "task"], schema="control")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE control.ai_cost_reservations IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM control.ai_cost_reservations)
          THEN RAISE EXCEPTION 'AI cost history is not empty; preserve billing evidence'; END IF;
        END $$;
    """)
    op.drop_table("ai_cost_reservations", schema="control")
