"""bdv — shareable match slug.

A player finds a table by typing "amber-hawk-42", not a UUID.

Revision ID: 20260722_bdv_match_slug
Revises: 20260722_bdv_fill_policy
"""
import sqlalchemy as sa
from alembic import op

revision = "20260722_bdv_match_slug"
down_revision = "20260722_bdv_fill_policy"
branch_labels = None
depends_on = None


def upgrade():
    # Added nullable first, backfilled, then tightened — existing rows have no
    # slug and the column is UNIQUE + NOT NULL.
    op.add_column("bdv_match", sa.Column("slug", sa.String(length=60), nullable=True))
    op.execute(
        "UPDATE bdv_match SET slug = 'table-' || substr(replace(id::text, '-', ''), 1, 10)"
        " WHERE slug IS NULL"
    )
    op.alter_column("bdv_match", "slug", nullable=False)
    op.create_unique_constraint("uq_bdv_match_slug", "bdv_match", ["slug"])
    op.create_index("ix_bdv_match_slug", "bdv_match", ["slug"])


def downgrade():
    op.drop_index("ix_bdv_match_slug", table_name="bdv_match")
    op.drop_constraint("uq_bdv_match_slug", "bdv_match", type_="unique")
    op.drop_column("bdv_match", "slug")
