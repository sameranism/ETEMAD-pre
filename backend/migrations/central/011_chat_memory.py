"""P16 opt-in personal topic memory, distinct from evidence and reports."""
import sqlalchemy as sa
from alembic import op

revision = "011_chat_memory"
down_revision = "010_publication_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA insights")
    op.create_table(
        "chat_memory",
        sa.Column("subject_id", sa.Text(), primary_key=True),
        sa.Column("role", sa.Text(), primary_key=True),
        sa.Column("topic", sa.String(500), nullable=False),
        sa.Column("days", sa.Integer(), nullable=False),
        sa.Column("stream", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.CheckConstraint("length(btrim(topic)) BETWEEN 1 AND 500", name="ck_memory_topic"),
        sa.CheckConstraint("days IN (1,7,30)", name="ck_memory_days"),
        sa.CheckConstraint("stream IN ('all','domestic','foreign')", name="ck_memory_stream"),
        schema="insights",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE insights.chat_memory IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM insights.chat_memory)
          THEN RAISE EXCEPTION 'chat memory is not empty'; END IF;
        END $$;
    """)
    op.drop_table("chat_memory", schema="insights")
    op.execute("DROP SCHEMA insights")
