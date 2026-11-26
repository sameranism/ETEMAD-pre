"""Durable P03 snapshot imports and crash-safe P05 polling claims.

Revision ID: 003_source_snapshots
Revises: 002_raw_content_identity
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "003_source_snapshots"
down_revision = "002_raw_content_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_snapshots",
        sa.Column("snapshot_id", sa.Text(), primary_key=True),
        sa.Column("stream", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "imported_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint("stream = 'domestic'", name="ck_snapshot_stream_domestic"),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_snapshot_content_hash"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object' AND "
            "(payload->>'registry_id') IS NOT DISTINCT FROM snapshot_id",
            name="ck_snapshot_identity",
        ),
        schema="domestic",
    )
    op.create_table(
        "source_snapshot_entries",
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(
            "snapshot_id", "source_id", name="pk_source_snapshot_entries"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["domestic.source_snapshots.snapshot_id"],
            name="fk_snapshot_entry_snapshot", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(config) = 'object' AND "
            "(config->>'source_id') IS NOT DISTINCT FROM source_id AND "
            "config->>'stream' = 'domestic' AND "
            "config->>'enabled' = 'true' AND "
            "config->>'license_scope' <> 'unknown'",
            name="ck_snapshot_entry_config",
        ),
        schema="domestic",
    )
    op.create_table(
        "current_source_snapshot",
        sa.Column("stream", sa.String(16), primary_key=True),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column(
            "activated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["domestic.source_snapshots.snapshot_id"],
            name="fk_current_snapshot", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("stream = 'domestic'", name="ck_current_snapshot_stream"),
        schema="domestic",
    )
    op.create_table(
        "poll_claims",
        sa.Column("source_id", sa.String(64), primary_key=True),
        sa.Column("snapshot_id", sa.Text(), nullable=False),
        sa.Column("claim_token", sa.String(36), nullable=False, unique=True),
        sa.Column("worker_id", sa.String(128), nullable=False),
        sa.Column(
            "claimed_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["domestic.poll_state.source_id"],
            name="fk_poll_claim_state", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["domestic.source_snapshots.snapshot_id"],
            name="fk_poll_claim_snapshot", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "claimed_until > claimed_at", name="ck_poll_claim_window"
        ),
        schema="domestic",
    )
    op.create_index(
        "ix_poll_claims_expiry", "poll_claims", ["claimed_until"], schema="domestic"
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            LOCK TABLE domestic.source_snapshots,
                       domestic.source_snapshot_entries,
                       domestic.current_source_snapshot,
                       domestic.poll_claims IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM domestic.source_snapshots)
                OR EXISTS (SELECT 1 FROM domestic.source_snapshot_entries)
                OR EXISTS (SELECT 1 FROM domestic.current_source_snapshot)
                OR EXISTS (SELECT 1 FROM domestic.poll_claims) THEN
                RAISE EXCEPTION 'source scheduling state is not empty; preserve it before downgrade';
            END IF;
        END $$;
    """)
    op.drop_index("ix_poll_claims_expiry", table_name="poll_claims", schema="domestic")
    op.drop_table("poll_claims", schema="domestic")
    op.drop_table("current_source_snapshot", schema="domestic")
    op.drop_table("source_snapshot_entries", schema="domestic")
    op.drop_table("source_snapshots", schema="domestic")
