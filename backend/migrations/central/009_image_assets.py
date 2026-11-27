"""P14 immutable internal image rendition records linked to article revisions."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "009_image_assets"
down_revision = "008_news_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_assets",
        sa.Column("image_id", sa.String(64), primary_key=True),
        sa.Column("news_id", sa.String(64), nullable=False),
        sa.Column("article_version", sa.Integer(), nullable=False),
        sa.Column("article_id", sa.String(64), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["news_id", "article_version"], ["editorial.articles.news_id", "editorial.articles.version"]),
        sa.CheckConstraint("manifest->>'article_id' = article_id", name="ck_image_article"),
        schema="editorial",
    )
    op.create_index("ix_image_news_version", "image_assets", ["news_id", "article_version", "created_at"], schema="editorial")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE editorial.image_assets IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM editorial.image_assets)
          THEN RAISE EXCEPTION 'image history is not empty; preserve renditions'; END IF;
        END $$;
    """)
    op.drop_table("image_assets", schema="editorial")
