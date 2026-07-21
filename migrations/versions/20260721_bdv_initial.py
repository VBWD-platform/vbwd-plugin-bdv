"""bdv (BizDevVibes) initial schema.

Own ROOT revision (``down_revision = None``) on purpose: anchoring a plugin
migration on another plugin's revision fragments the graph, and then
``alembic upgrade heads`` fails with a KeyError unless that exact plugin is
also cloned.

Revision ID: 20260721_bdv_initial
Create Date: 2026-07-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260721_bdv_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "bdv_board",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=140), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="draft"),
        sa.Column(
            "game_display_name",
            sa.String(length=120),
            nullable=False,
            server_default="BizDevVibes",
        ),
        sa.Column("currency_label", sa.String(length=20), nullable=False, server_default="cr"),
        sa.Column("starting_cash", sa.Integer(), nullable=False, server_default="15000"),
        sa.Column("go_salary", sa.Integer(), nullable=False, server_default="2000"),
        sa.Column("jail_fine", sa.Integer(), nullable=False, server_default="500"),
        sa.Column("jail_penalty_ev", sa.Integer(), nullable=False, server_default="1000"),
        sa.Column("k_price", sa.Numeric(6, 4), nullable=False, server_default="0.5000"),
        sa.Column("k_acquire", sa.Numeric(6, 4), nullable=False, server_default="0.3000"),
        sa.Column("cap_pct", sa.Numeric(6, 4), nullable=False, server_default="0.3000"),
        sa.Column(
            "fee_policy", sa.String(length=40), nullable=False, server_default="all_to_poorest"
        ),
        sa.Column("min_seats", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("max_seats", sa.Integer(), nullable=False, server_default="6"),
        sa.Column("default_seats", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("turn_timeout_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column(
            "negotiation_window_seconds", sa.Integer(), nullable=False, server_default="30"
        ),
        sa.Column("max_houses", sa.Integer(), nullable=False, server_default="5"),
        sa.UniqueConstraint("slug", name="uq_bdv_board_slug"),
    )
    op.create_index("ix_bdv_board_slug", "bdv_board", ["slug"])
    op.create_index("ix_bdv_board_status", "bdv_board", ["status"])

    op.create_table(
        "bdv_square",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("board_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("stage", sa.String(length=60), nullable=True),
        sa.Column("price", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rent_table", sa.JSON(), nullable=True),
        sa.Column("service_multipliers", sa.JSON(), nullable=True),
        sa.Column("house_cost", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("mortgage_value", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tax_amount", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["board_id"], ["bdv_board.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("board_id", "index", name="uq_bdv_square_board_index"),
    )
    op.create_index("ix_bdv_square_board_id", "bdv_square", ["board_id"])
    op.create_index("ix_bdv_square_stage", "bdv_square", ["stage"])

    op.create_table(
        "bdv_card",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("board_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deck", sa.String(length=20), nullable=False),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("flavor_text", sa.Text(), nullable=True),
        sa.Column("effect", sa.JSON(), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["board_id"], ["bdv_board.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_bdv_card_board_id", "bdv_card", ["board_id"])
    op.create_index("ix_bdv_card_deck", "bdv_card", ["deck"])

    op.create_table(
        "bdv_agent_profile",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("llm_connection_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("persona", sa.String(length=200), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("temperature", sa.Numeric(4, 2), nullable=False, server_default="0.70"),
        sa.Column(
            "max_tokens_per_match", sa.Integer(), nullable=False, server_default="60000"
        ),
        sa.Column("risk_bias", sa.Numeric(4, 2), nullable=False, server_default="0.50"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.UniqueConstraint("name", name="uq_bdv_agent_profile_name"),
    )

    op.create_table(
        "bdv_match",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("board_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="lobby"),
        sa.Column("seed", sa.String(length=64), nullable=False),
        sa.Column("spec_snapshot", sa.JSON(), nullable=False),
        sa.Column("spec_hash", sa.String(length=64), nullable=False),
        sa.Column("state_snapshot", sa.JSON(), nullable=True),
        sa.Column("state_seq", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chance_deck_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("community_deck_size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("chat_room_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("winner_seat_index", sa.Integer(), nullable=True),
        sa.Column("turn_deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["board_id"], ["bdv_board.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["created_by"], ["vbwd_user.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_bdv_match_board_id", "bdv_match", ["board_id"])
    op.create_index("ix_bdv_match_status", "bdv_match", ["status"])
    op.create_index("ix_bdv_match_spec_hash", "bdv_match", ["spec_hash"])
    op.create_index("ix_bdv_match_created_by", "bdv_match", ["created_by"])

    op.create_table(
        "bdv_seat",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("match_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seat_index", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="human"),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("agent_profile_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("display_name", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(["match_id"], ["bdv_match.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["vbwd_user.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["agent_profile_id"], ["bdv_agent_profile.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("match_id", "seat_index", name="uq_bdv_seat_match_index"),
        sa.CheckConstraint(
            "(user_id IS NULL) <> (agent_profile_id IS NULL)"
            " OR (user_id IS NULL AND agent_profile_id IS NULL AND kind = 'baseline')",
            name="ck_bdv_seat_one_occupant",
        ),
    )
    op.create_index("ix_bdv_seat_match_id", "bdv_seat", ["match_id"])
    op.create_index("ix_bdv_seat_user_id", "bdv_seat", ["user_id"])

    op.create_table(
        "bdv_action",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("match_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("seat_index", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("events", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(["match_id"], ["bdv_match.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("match_id", "seq", name="uq_bdv_action_match_seq"),
    )
    op.create_index("ix_bdv_action_match_id", "bdv_action", ["match_id"])

    op.create_table(
        "bdv_offer",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("match_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("turn_seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False, server_default="bribe_to_fate"),
        sa.Column("from_seat", sa.Integer(), nullable=False),
        sa.Column("to_seat", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["match_id"], ["bdv_match.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_bdv_offer_match_id", "bdv_offer", ["match_id"])
    op.create_index("ix_bdv_offer_status", "bdv_offer", ["status"])


def downgrade():
    for table in (
        "bdv_offer",
        "bdv_action",
        "bdv_seat",
        "bdv_match",
        "bdv_agent_profile",
        "bdv_card",
        "bdv_square",
        "bdv_board",
    ):
        op.drop_table(table)
