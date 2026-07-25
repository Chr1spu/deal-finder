"""add variant extraction fields: lot_size, completeness, has_defect

What a listing is actually offering, extracted from its title, because nothing
structured carries it. eBay's localizedAspects gives Brand, Model, Color, MPN
and Storage Capacity, and nothing at all about what is in the box.

The three hazards these guard against, measured on the real corpus:

  lots        "Lot of 50 SK Hynix 64GB" at $113,000 sits in the same Memory
              (RAM) category as single sticks. Removing 2% of that category
              drops its mean 28% and its maximum from $113,000 to $21,936.
  defects     graphics cards flagged for-parts/cracked have a median of
              $151.08 against $420.00 for clean ones, on 268 of 2,544.
  bundling    consoles: bare $129.99, unstated $174.99, with-extras $190.00.

All three columns are indexed because comp selection filters on them in SQL.

See docs/decisions/0012-variant-extraction.md.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # lot_size NULL means a single item, which is ~99% of the corpus.
    op.add_column("listing", sa.Column("lot_size", sa.Integer(), nullable=True))
    # completeness NULL means UNSTATED (89% of titles), not "complete".
    op.add_column("listing", sa.Column("completeness", sa.String(), nullable=True))
    op.add_column(
        "listing",
        sa.Column("has_defect", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("listing", sa.Column("variant_signals", sa.JSON(), nullable=True))

    # Dropped again so new inserts go through the application default rather
    # than the database's, matching the booleans added in 0005, 0007 and 0009.
    op.alter_column("listing", "has_defect", server_default=None)

    op.create_index("ix_listing_lot_size", "listing", ["lot_size"])
    op.create_index("ix_listing_completeness", "listing", ["completeness"])
    op.create_index("ix_listing_has_defect", "listing", ["has_defect"])

    # Deliberately NOT backfilled here. Extraction is application logic that
    # will be revised as the vocabulary grows, and encoding a snapshot of it
    # in SQL inside a migration would both duplicate the rules and freeze them
    # at this revision. `python -m ml.extract_listings` fills these in and can
    # be re-run whenever the rules change.


def downgrade() -> None:
    op.drop_index("ix_listing_has_defect", table_name="listing")
    op.drop_index("ix_listing_completeness", table_name="listing")
    op.drop_index("ix_listing_lot_size", table_name="listing")
    op.drop_column("listing", "variant_signals")
    op.drop_column("listing", "has_defect")
    op.drop_column("listing", "completeness")
    op.drop_column("listing", "lot_size")
