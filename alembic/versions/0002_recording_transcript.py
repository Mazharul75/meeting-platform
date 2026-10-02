"""add transcript columns to recordings

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recordings",
        sa.Column("transcript_status", sa.String(12), nullable=False, server_default="none"),
    )
    op.add_column("recordings", sa.Column("transcript_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("recordings", "transcript_text")
    op.drop_column("recordings", "transcript_status")
