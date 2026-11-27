"""S04-008: additive central editorial revision storage."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "007_editorial_revisions"
down_revision = "006_foreign_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA editorial")
    op.create_table(
        "translations",
        sa.Column("translation_id", sa.String(64), primary_key=True),
        sa.Column("news_id", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("provenance", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint("payload->>'translation_id' = translation_id AND payload->>'news_id' = news_id",
                           name="ck_translation_identity"),
        schema="editorial",
    )
    op.create_table(
        "articles",
        sa.Column("news_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.String(64), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("source_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("quality", postgresql.JSONB(), nullable=False),
        sa.Column("policy", postgresql.JSONB(), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("translation_id", sa.String(64), sa.ForeignKey("editorial.translations.translation_id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("news_id", "version", name="pk_editorial_revision"),
        sa.UniqueConstraint("news_id", "request_id", name="uq_editorial_request"),
        sa.UniqueConstraint("article_id", "version", name="uq_editorial_article_version"),
        sa.CheckConstraint("version > 0", name="ck_editorial_version"),
        sa.CheckConstraint("payload->>'news_id' = news_id AND payload->>'article_id' = article_id "
                           "AND payload->>'status' IN ('draft','held')", name="ck_editorial_article"),
        schema="editorial",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE editorial.articles, editorial.translations IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM editorial.articles) OR EXISTS (SELECT 1 FROM editorial.translations)
          THEN RAISE EXCEPTION 'editorial history is not empty; preserve revisions'; END IF;
        END $$;
    """)
    op.drop_table("articles", schema="editorial")
    op.drop_table("translations", schema="editorial")
    op.execute("DROP SCHEMA editorial")
