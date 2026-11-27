"""P26 archive of immutable source bytes and searchable references."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "008_news_archive"
down_revision = "007_editorial_revisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA archive")
    op.create_table(
        "news_records",
        sa.Column("news_id", sa.String(128), primary_key=True),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("source_id", sa.String(128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("original_url", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.String(256), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("classification", sa.String(32), nullable=False),
        sa.CheckConstraint("classification = 'internal'", name="ck_archive_news_internal"),
        sa.CheckConstraint("stream IN ('domestic','foreign','manual')", name="ck_archive_news_stream"),
        schema="archive",
    )
    op.create_index("ix_archive_news_stream_time", "news_records", ["stream", "received_at"], schema="archive")
    op.create_table(
        "artifact_index",
        sa.Column("artifact_id", sa.String(80), primary_key=True),
        sa.Column("news_id", sa.String(128), sa.ForeignKey("archive.news_records.news_id"), nullable=False),
        sa.Column("object_type", sa.String(32), nullable=False),
        sa.Column("owner_project", sa.String(3), nullable=False),
        sa.Column("object_uri", sa.String(256), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("visibility", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("news_id", "sha256", name="uq_archive_news_digest"),
        sa.CheckConstraint("visibility = 'internal' AND object_type = 'raw_news' AND owner_project = 'P26'",
                           name="ck_archive_raw_only"),
        schema="archive",
    )
    op.create_index("ix_archive_artifact_news", "artifact_index", ["news_id", "created_at"], schema="archive")


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE archive.news_records, archive.artifact_index IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM archive.news_records) OR EXISTS (SELECT 1 FROM archive.artifact_index)
          THEN RAISE EXCEPTION 'news archive is not empty; preserve source material'; END IF;
        END $$;
    """)
    op.drop_table("artifact_index", schema="archive")
    op.drop_table("news_records", schema="archive")
    op.execute("DROP SCHEMA archive")
