"""initial schema: all tables, row-level security enabled with no policies

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = [
    "users",
    "sessions",
    "meetings",
    "meeting_members",
    "guest_invites",
    "recordings",
    "recording_parts",
    "audit_log",
]

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_login_at", TS, nullable=True),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("csrf_secret", sa.String(64), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("last_seen_at", TS, nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)

    op.create_table(
        "meetings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("scheduled_start", TS, nullable=False),
        sa.Column("scheduled_end", TS, nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("host_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("room_name", sa.Uuid(), nullable=False, unique=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS, nullable=True),
        sa.Column("ended_at", TS, nullable=True),
    )
    op.create_index("ix_meetings_host_id", "meetings", ["host_id"])

    op.create_table(
        "meeting_members",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_id", sa.Uuid(), sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(10), nullable=False),
        sa.UniqueConstraint("meeting_id", "user_id"),
    )
    op.create_index("ix_meeting_members_meeting_id", "meeting_members", ["meeting_id"])
    op.create_index("ix_meeting_members_user_id", "meeting_members", ["user_id"])

    op.create_table(
        "guest_invites",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_id", sa.Uuid(), sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("valid_from", TS, nullable=False),
        sa.Column("valid_until", TS, nullable=False),
        sa.Column("revoked_at", TS, nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
    )
    op.create_index("ix_guest_invites_meeting_id", "guest_invites", ["meeting_id"])
    op.create_index("ix_guest_invites_token_hash", "guest_invites", ["token_hash"], unique=True)

    op.create_table(
        "recordings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_id", sa.Uuid(), sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("mime_type", sa.String(40), nullable=False),
        sa.Column("part_seconds", sa.Integer(), nullable=False),
        sa.Column("parts_expected", sa.Integer(), nullable=True),
        sa.Column("total_bytes", sa.BigInteger(), nullable=False),
        sa.Column("total_duration_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("consent_at", TS, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
    )
    op.create_index("ix_recordings_meeting_id", "recordings", ["meeting_id"])

    op.create_table(
        "recording_parts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("recording_id", sa.Uuid(), sa.ForeignKey("recordings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("part_number", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(300), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(10), nullable=False),
        sa.Column("uploaded_at", TS, nullable=True),
        sa.UniqueConstraint("recording_id", "part_number"),
    )
    op.create_index("ix_recording_parts_recording_id", "recording_parts", ["recording_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("at", TS, nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(60), nullable=False),
        sa.Column("target_type", sa.String(40), nullable=True),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("details", sa.JSON(), nullable=True),
    )
    op.create_index("ix_audit_log_at", "audit_log", ["at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])

    if op.get_bind().dialect.name == "postgresql":
        # Deny-all: RLS on, and no policies at all. The public API keys can read nothing.
        for table in TABLES:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        # Belt and braces on Supabase: the public roles get no table privileges either.
        op.execute(
            """
            DO $$
            BEGIN
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM anon;
              END IF;
              IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM authenticated;
              END IF;
            END
            $$;
            """
        )


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
