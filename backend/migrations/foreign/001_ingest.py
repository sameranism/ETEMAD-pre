"""S02-014: first isolated foreign ingestion schema; no central/domestic writes."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "001_foreign_ingest"
down_revision = None
branch_labels = None
depends_on = None
SCHEMA = "foreign"


def _require_foreign() -> None:
    if op.get_context().config.get_main_option("etemad_role") != "foreign":
        raise RuntimeError("foreign migration requires explicit foreign role")


def upgrade() -> None:
    _require_foreign()
    op.execute('CREATE SCHEMA "foreign"')
    op.create_table(
        "source_candidates",
        sa.Column("candidate_id", sa.String(64), primary_key=True),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("feed_url", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="ck_candidate_country"),
        sa.CheckConstraint("payload->>'stream' = 'foreign' AND payload->>'enabled' = 'false' "
                           "AND payload->>'license_scope' = 'unknown'", name="ck_candidate_disabled"),
        schema=SCHEMA,
    )
    op.create_table(
        "poll_state",
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("etag", sa.String(256)),
        sa.Column("last_modified", sa.String(256)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision >= 1 AND consecutive_failures >= 0", name="ck_foreign_poll_revision"),
        schema=SCHEMA,
    )
    op.create_table(
        "raw_items",
        sa.Column("raw_id", sa.String(64), primary_key=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("country_code", sa.String(2), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_ref", sa.Text()),
        sa.Column("source_hash", sa.String(64), nullable=False),
        sa.Column("url_hash", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("source_id", "source_hash", name="uq_foreign_source_hash"),
        sa.UniqueConstraint("source_id", "url_hash", name="uq_foreign_source_url"),
        sa.CheckConstraint("country_code ~ '^[A-Z]{2}$'", name="ck_foreign_raw_country"),
        sa.CheckConstraint("payload->>'stream' = 'foreign'", name="ck_foreign_raw_stream"),
        schema=SCHEMA,
    )
    op.create_index("ix_foreign_raw_source_time", "raw_items", ["source_id", "fetched_at"], schema=SCHEMA)
    op.create_index("ix_foreign_poll_due", "poll_state", ["next_poll_at"], schema=SCHEMA)


def downgrade() -> None:
    _require_foreign()
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE "foreign".source_candidates, "foreign".poll_state,
                     "foreign".raw_items IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM "foreign".source_candidates)
             OR EXISTS (SELECT 1 FROM "foreign".poll_state)
             OR EXISTS (SELECT 1 FROM "foreign".raw_items) THEN
             RAISE EXCEPTION 'foreign storage is not empty; retain its data';
          END IF;
        END $$;
    """)
    for table in ("raw_items", "poll_state", "source_candidates"):
        op.drop_table(table, schema=SCHEMA)
    op.execute('DROP SCHEMA "foreign"')
