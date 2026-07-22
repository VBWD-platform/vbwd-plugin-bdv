"""Agent roster: a stable handle per agent, and a recorded result per seat.

``final_cash`` is written once, when the match finishes. The alternative was to
derive lifetime statistics by reading every finished match's JSON state snapshot
in Python, which turns one admin list into an N+1 over whole match histories —
and re-derives, on every page load, a fact that stopped changing the moment the
match ended.
"""
import sqlalchemy as sa
from alembic import op

revision = "20260722_bdv_agent_roster"
down_revision = "20260722_bdv_table_chat"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("bdv_agent_profile", sa.Column("slug", sa.String(120), nullable=True))
    # Backfill from the name so existing rows get a usable handle rather than a
    # NULL the admin list would have to special-case forever.
    op.execute(
        """
        UPDATE bdv_agent_profile
           SET slug = regexp_replace(lower(trim(name)), '[^a-z0-9]+', '-', 'g')
         WHERE slug IS NULL
        """
    )
    op.execute(
        """
        UPDATE bdv_agent_profile SET slug = 'agent-' || left(id::text, 8)
         WHERE slug IS NULL OR slug = '' OR slug = '-'
        """
    )
    op.create_unique_constraint(
        "uq_bdv_agent_profile_slug", "bdv_agent_profile", ["slug"]
    )
    op.alter_column("bdv_agent_profile", "slug", nullable=False)

    op.add_column("bdv_seat", sa.Column("final_cash", sa.Integer(), nullable=True))
    op.add_column("bdv_seat", sa.Column("is_winner", sa.Boolean(), nullable=True))


def downgrade():
    op.drop_column("bdv_seat", "is_winner")
    op.drop_column("bdv_seat", "final_cash")
    op.drop_constraint("uq_bdv_agent_profile_slug", "bdv_agent_profile", type_="unique")
    op.drop_column("bdv_agent_profile", "slug")
