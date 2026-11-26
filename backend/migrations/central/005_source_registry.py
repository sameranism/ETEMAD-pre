"""P03 source registry and immutable revisions; additive central migration."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "005_source_registry"
down_revision = "004_login_throttle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA sources")
    op.create_table(
        "source_registry",
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("url_normalized", sa.Text(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("stream", "url_normalized", name="uq_source_stream_url"),
        sa.CheckConstraint("stream IN ('domestic', 'foreign')", name="ck_source_stream"),
        sa.CheckConstraint("revision_no > 0", name="ck_source_revision"),
        sa.CheckConstraint("octet_length(url_normalized) BETWEEN 1 AND 2048",
                           name="ck_source_url_length"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object' AND "
            "(snapshot->>'source_id') IS NOT DISTINCT FROM source_id AND "
            "(snapshot->>'stream') IS NOT DISTINCT FROM stream",
            name="ck_source_identity",
        ),
        schema="sources",
    )
    op.create_table(
        "source_history",
        sa.Column("source_id", sa.Text(), nullable=False),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("source_id", "revision_no"),
        sa.ForeignKeyConstraint(["source_id"], ["sources.source_registry.source_id"],
                                ondelete="RESTRICT", name="fk_source_history_registry"),
        sa.CheckConstraint("revision_no > 0", name="ck_source_history_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object' AND "
            "(snapshot->>'source_id') IS NOT DISTINCT FROM source_id",
            name="ck_source_history_identity",
        ),
        schema="sources",
    )


def downgrade() -> None:
    # Preserve rights/provenance; a populated registry is not disposable.
    op.execute("""
        DO $$ BEGIN
            LOCK TABLE sources.source_registry, sources.source_history IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM sources.source_registry)
                OR EXISTS (SELECT 1 FROM sources.source_history) THEN
                RAISE EXCEPTION 'source registry is not empty; preserve it before downgrade';
            END IF;
        END $$;
    """)
    op.drop_table("source_history", schema="sources")
    op.drop_table("source_registry", schema="sources")
    op.execute("DROP SCHEMA sources")
