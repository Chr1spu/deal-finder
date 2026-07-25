"""flag multi-variant listings whose displayed price is a from-price

One eBay listing offering several configurations shows the CHEAPEST variant's
price, so its price and its title describe different items. That manufactures
fake bargains, and they sort to the TOP of a deal ranking because that is where
the largest apparent discounts are: an "iPhone 14 128GB 256GB - All Colors" at
$259.99 against a $650 estimate is the entry price, not a discount.

860 listings (6.4% of the corpus). Excluded from comp sets and from deal
scanning, joining lots, defects and accessories in `usable_as_comp`.

See docs/decisions/0015-multi-variant-listings.md.

Note the migration number (0014) and the ADR number (0015) differ: migrations
and ADRs are independent sequences, and ADR 0014 (valuation) needed no schema
change because valuation is computed on read.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "listing",
        sa.Column("price_is_from", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.alter_column("listing", "price_is_from", server_default=None)
    op.create_index("ix_listing_price_is_from", "listing", ["price_is_from"])
    # Not backfilled in SQL: the rules are application logic and will be
    # revised. `python -m ml.extract_listings` fills this in.


def downgrade() -> None:
    op.drop_index("ix_listing_price_is_from", table_name="listing")
    op.drop_column("listing", "price_is_from")
