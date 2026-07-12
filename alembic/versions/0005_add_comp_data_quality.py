"""price history table, shipping/buying-option capture, two-strike sold confirmation

Everything here exists to make the data stage 4 will treat as comparable sales
actually trustworthy. See docs/decisions/0004-trustworthy-comp-data.md.

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Listing: capture what the API already sends us ---
    op.add_column("listing", sa.Column("shipping_cost", sa.Float(), nullable=True))
    # server_default is needed for the 10,496 existing rows, which have no
    # value for a NOT NULL column. Dropped again right after, so new inserts
    # go through the application default rather than the database's.
    op.add_column(
        "listing",
        sa.Column("is_auction", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "listing",
        sa.Column("accepts_best_offer", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("listing", "is_auction", server_default=None)
    op.alter_column("listing", "accepts_best_offer", server_default=None)
    op.create_index("ix_listing_is_auction", "listing", ["is_auction"])

    op.add_column("listing", sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True))

    # --- SavedSearch: observability for the deferred pagination work ---
    op.add_column("savedsearch", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("savedsearch", sa.Column("last_result_total", sa.Integer(), nullable=True))

    # --- PriceObservation: append-only price history ---
    op.create_table(
        "priceobservation",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("shipping_cost", sa.Float(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["listing_id"], ["listing.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_priceobservation_listing_id", "priceobservation", ["listing_id"])
    op.create_index("ix_priceobservation_observed_at", "priceobservation", ["observed_at"])

    # Seed one observation per existing listing from its current price. Without
    # this every pre-existing listing would have a price chart that starts at
    # whenever it next happens to change, which reads as though nothing was
    # known before then. shipping_cost is left null on purpose: it genuinely
    # wasn't captured for these rows, and inventing 0 would be a lie.
    op.execute(
        """
        INSERT INTO priceobservation (listing_id, price, shipping_cost, observed_at)
        SELECT id, price, NULL, first_seen_at FROM listing
        """
    )


def downgrade() -> None:
    op.drop_index("ix_priceobservation_observed_at", table_name="priceobservation")
    op.drop_index("ix_priceobservation_listing_id", table_name="priceobservation")
    op.drop_table("priceobservation")

    op.drop_column("savedsearch", "last_result_total")
    op.drop_column("savedsearch", "last_run_at")

    op.drop_column("listing", "missing_since")
    op.drop_index("ix_listing_is_auction", table_name="listing")
    op.drop_column("listing", "accepts_best_offer")
    op.drop_column("listing", "is_auction")
    op.drop_column("listing", "shipping_cost")
