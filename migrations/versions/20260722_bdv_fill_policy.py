"""bdv — opponent fill policy and open seats.

A creator now chooses what happens to the seats they did not fill: agents take
them immediately, they stay open for humans indefinitely, or they stay open for
a bounded wait and then agents take them.

Revision ID: 20260722_bdv_fill_policy
Revises: 20260721_bdv_initial
"""
import sqlalchemy as sa
from alembic import op

revision = "20260722_bdv_fill_policy"
down_revision = "20260721_bdv_initial"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "bdv_match",
        sa.Column(
            "fill_policy",
            sa.String(length=30),
            nullable=False,
            server_default="agents_now",
        ),
    )
    op.add_column(
        "bdv_match",
        sa.Column("lobby_deadline_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Widen the seat constraint: an 'open' seat has no occupant yet.
    op.drop_constraint("ck_bdv_seat_one_occupant", "bdv_seat", type_="check")
    op.create_check_constraint(
        "ck_bdv_seat_one_occupant",
        "bdv_seat",
        "(user_id IS NULL) <> (agent_profile_id IS NULL)"
        " OR (user_id IS NULL AND agent_profile_id IS NULL"
        "     AND kind IN ('baseline', 'open'))",
    )


def downgrade():
    op.drop_constraint("ck_bdv_seat_one_occupant", "bdv_seat", type_="check")
    op.create_check_constraint(
        "ck_bdv_seat_one_occupant",
        "bdv_seat",
        "(user_id IS NULL) <> (agent_profile_id IS NULL)"
        " OR (user_id IS NULL AND agent_profile_id IS NULL AND kind = 'baseline')",
    )
    op.drop_column("bdv_match", "lobby_deadline_at")
    op.drop_column("bdv_match", "fill_policy")
