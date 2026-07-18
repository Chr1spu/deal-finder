"""epid, listing end date, bids, seller quality, aspects, sold quantity

All of these were already present in responses the connector was already
fetching. See docs/decisions/0006-capture-what-ebay-already-sends.md.

No backfill is possible: these values were never received into the database,
and the only way to get them for existing rows is to re-ingest, which happens
naturally on the next run.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # epid is indexed because it's potentially the best comp key in the whole
    # system: an exact product match beats any similarity measure.
    op.add_column("listing", sa.Column("epid", sa.String(), nullable=True))
    op.create_index("ix_listing_epid", "listing", ["epid"])

    op.add_column("listing", sa.Column("item_end_date", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "listing", sa.Column("is_gtc", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.alter_column("listing", "is_gtc", server_default=None)
    op.create_index("ix_listing_is_gtc", "listing", ["is_gtc"])

    op.add_column("listing", sa.Column("bid_count", sa.Integer(), nullable=True))

    op.add_column("listing", sa.Column("seller_feedback_score", sa.Integer(), nullable=True))
    op.add_column("listing", sa.Column("seller_feedback_percent", sa.Float(), nullable=True))

    # JSON and unindexed on purpose: nothing queries these yet, and stage 3b
    # should decide what deserves promotion to a typed column using real data.
    op.add_column("listing", sa.Column("qualified_programs", sa.JSON(), nullable=True))
    op.add_column("listing", sa.Column("aspects", sa.JSON(), nullable=True))

    op.add_column("listing", sa.Column("sold_quantity", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("listing", "sold_quantity")
    op.drop_column("listing", "aspects")
    op.drop_column("listing", "qualified_programs")
    op.drop_column("listing", "seller_feedback_percent")
    op.drop_column("listing", "seller_feedback_score")
    op.drop_column("listing", "bid_count")
    op.drop_index("ix_listing_is_gtc", table_name="listing")
    op.drop_column("listing", "is_gtc")
    op.drop_column("listing", "item_end_date")
    op.drop_index("ix_listing_epid", table_name="listing")
    op.drop_column("listing", "epid")
