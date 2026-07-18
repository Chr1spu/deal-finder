"""flag shipping costs that are only estimates

eBay marks each shipping option FIXED or CALCULATED. A calculated cost depends
on the buyer's location, so the figure returned may have been worked out for
somewhere else. The normalizer now prefers FIXED options and flags the listing
when only CALCULATED ones were available.

See docs/decisions/0008-price-oracle-and-valuation-clients.md.

Existing rows default to False, which is the honest reading: they were ingested
before shippingCostType was looked at, so nothing is known to be estimated. The
next ingest sets it correctly.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "listing",
        sa.Column("shipping_estimated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Dropped again so new inserts go through the application default rather
    # than the database's, same as the booleans added in 0005 and 0007.
    op.alter_column("listing", "shipping_estimated", server_default=None)


def downgrade() -> None:
    op.drop_column("listing", "shipping_estimated")
