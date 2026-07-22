"""LLM seats: per-seat budget state, and the reasoning behind a move.

``advance_agents`` rebuilds its seat drivers on every request, so anything an
LLM seat learns during a match has to live in the database or it resets between
polls: a spent token budget would never bind, and a seat that had failed hard
would be retried forever instead of staying degraded.

``reasoning`` is a COLUMN, not part of ``payload``, because payload is what the
engine folds on replay. Mixing a model's prose into it would put untrusted,
non-deterministic text on the replay path.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260722_bdv_llm_seats"
down_revision = "20260722_bdv_agent_roster"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bdv_action", sa.Column("reasoning", sa.Text(), nullable=True))
    op.add_column(
        "bdv_seat",
        sa.Column(
            "llm_tokens_spent",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "bdv_seat",
        sa.Column(
            "llm_degraded",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )


def downgrade():
    op.drop_column("bdv_seat", "llm_degraded")
    op.drop_column("bdv_seat", "llm_tokens_spent")
    op.drop_column("bdv_action", "reasoning")
