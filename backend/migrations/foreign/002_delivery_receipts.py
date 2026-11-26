"""Persist acknowledged bridge receipts in the foreign-owned database."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "002_foreign_delivery"
down_revision = "001_foreign_ingest"
branch_labels = None
depends_on = None


def _require_foreign() -> None:
    if op.get_context().config.get_main_option("etemad_role") != "foreign":
        raise RuntimeError("foreign migration requires explicit foreign role")


def upgrade() -> None:
    _require_foreign()
    op.add_column("raw_items", sa.Column("delivery_receipt", sa.String(80)), schema="foreign")
    op.add_column("raw_items", sa.Column("delivered_at", sa.DateTime(timezone=True)), schema="foreign")
    op.create_check_constraint("ck_foreign_delivery_pair", "raw_items",
                               "(delivery_receipt IS NULL) = (delivered_at IS NULL)", schema="foreign")
    op.create_index("ix_foreign_pending_delivery", "raw_items", ["source_id", "fetched_at", "raw_id"],
                    schema="foreign", postgresql_where=sa.text("delivered_at IS NULL"))


def downgrade() -> None:
    _require_foreign()
    op.execute("""
        DO $$ BEGIN
          LOCK TABLE "foreign".raw_items IN ACCESS EXCLUSIVE MODE;
          IF EXISTS (SELECT 1 FROM "foreign".raw_items WHERE delivered_at IS NOT NULL) THEN
            RAISE EXCEPTION 'foreign delivery receipts are not empty; preserve acknowledgments';
          END IF;
        END $$;
    """)
    op.drop_index("ix_foreign_pending_delivery", table_name="raw_items", schema="foreign")
    op.drop_constraint("ck_foreign_delivery_pair", "raw_items", schema="foreign", type_="check")
    op.drop_column("raw_items", "delivered_at", schema="foreign")
    op.drop_column("raw_items", "delivery_receipt", schema="foreign")
