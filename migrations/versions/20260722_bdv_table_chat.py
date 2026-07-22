"""bdv — table chat messages.

Plugin-owned rather than routed through meinchat: meinchat's migrations anchor
on a `subscription` revision, so a hard dependency would break
`alembic upgrade heads` wherever the whole chain is not cloned. The bot bridge
still uses meinchat as a transport for tappable cards.

Revision ID: 20260722_bdv_table_chat
Revises: 20260722_bdv_match_slug
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260722_bdv_table_chat"
down_revision = "20260722_bdv_match_slug"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bdv_message",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("match_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seat_index", sa.Integer(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("body", sa.String(length=500), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["bdv_match.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["vbwd_user.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_bdv_message_match_id", "bdv_message", ["match_id"])


def downgrade():
    op.drop_index("ix_bdv_message_match_id", table_name="bdv_message")
    op.drop_table("bdv_message")
