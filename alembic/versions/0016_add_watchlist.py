"""watchlist: listings the user is tracking individually

The last view from the stage 6 plan. Deliberately a thin table: everything it
displays (price history, sold status, sale confidence) is already recorded by
the ingest and disappearance jobs, so this stores only what those cannot know,
which is that a person cared about this row and what the price was when they
started caring.

`price_when_added` is NOT NULL with no server default, and this migration is
therefore safe only because the table is new and empty. Adding a NOT NULL
column to `listing` is the failure this project has hit three times: a
long-lived RQ worker holds the pre-migration model, omits the column on every
INSERT, and ingestion dies silently behind per-search error isolation. See
systems/preflight.py, which now fails fast on exactly that drift.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "watchlistitem",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.Column("price_when_added", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique, not merely indexed: watching a listing twice means nothing, and
    # the constraint is what lets the POST route be idempotent rather than
    # every read having to de-duplicate.
    op.create_index(
        "ix_watchlistitem_listing_id", "watchlistitem", ["listing_id"], unique=True
    )
    op.create_index("ix_watchlistitem_added_at", "watchlistitem", ["added_at"])


def downgrade() -> None:
    op.drop_index("ix_watchlistitem_added_at", table_name="watchlistitem")
    op.drop_index("ix_watchlistitem_listing_id", table_name="watchlistitem")
    op.drop_table("watchlistitem")
