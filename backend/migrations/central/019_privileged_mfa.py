"""P28: additive, durable MFA factors, challenges, sessions and rate limits."""
import sqlalchemy as sa
from alembic import op

revision = "019_privileged_mfa"
down_revision = "018_analyses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mfa_factors",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("identity.users.user_id"), primary_key=True),
        sa.Column("credential_ref", sa.String(128), nullable=False, unique=True),
        sa.Column("generation", sa.String(64), nullable=False),
        sa.Column("confirmed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_step", sa.BigInteger(), nullable=False, server_default="-1"),
        sa.CheckConstraint("last_step >= -1", name="ck_mfa_last_step"),
        schema="identity",
    )
    op.create_table(
        "auth_challenges",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("identity.users.user_id"), nullable=False),
        sa.Column("marker", sa.String(64), nullable=False),
        sa.Column("generation", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("parent_hash", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "(purpose = 'login' AND parent_hash IS NULL) OR "
            "(purpose = 'p19' AND parent_hash IS NOT NULL)", name="ck_mfa_challenge_purpose",
        ),
        schema="identity",
    )
    op.create_index("ix_auth_challenge_user", "auth_challenges", ["user_id"], schema="identity")
    op.create_table(
        "privileged_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("identity.users.user_id"), nullable=False),
        sa.Column("marker", sa.String(64), nullable=False),
        sa.Column("generation", sa.String(64), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("parent_hash", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "(purpose = 'login' AND parent_hash IS NULL) OR "
            "(purpose = 'p19' AND parent_hash IS NOT NULL)", name="ck_priv_session_purpose",
        ),
        schema="identity",
    )
    op.create_index("ix_priv_session_user", "privileged_sessions", ["user_id"], schema="identity")
    op.create_table(
        "mfa_throttle",
        sa.Column("bucket", sa.String(80), primary_key=True),
        sa.Column("window_started", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.CheckConstraint("attempts BETWEEN 1 AND 30", name="ck_mfa_attempt_count"),
        schema="identity",
    )


def downgrade() -> None:
    # Never erase enrolled factors or live session/replay history via rollback.
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM identity.mfa_factors)
        OR EXISTS (SELECT 1 FROM identity.auth_challenges)
        OR EXISTS (SELECT 1 FROM identity.privileged_sessions)
      THEN RAISE EXCEPTION 'MFA state must be preserved; use a protected forward fix'; END IF;
    END $$""")
    for table in ("mfa_throttle", "privileged_sessions", "auth_challenges", "mfa_factors"):
        op.drop_table(table, schema="identity")
