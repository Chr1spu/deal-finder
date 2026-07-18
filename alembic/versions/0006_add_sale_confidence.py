"""sale_confidence and sale_signals on listing

A disappearance is not a sale. See docs/decisions/0005-sale-confidence.md.

Deliberately no backfill: the existing rows have never been through a
disappearance check (there are zero likely_sold listings), so there is nothing
to score yet. Leaving them null is honest, where writing a default confidence
would invent evidence that was never gathered.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("listing", sa.Column("sale_confidence", sa.Float(), nullable=True))
    op.add_column("listing", sa.Column("sale_signals", sa.JSON(), nullable=True))
    # Stage 4's comp query filters to sold listings and orders by confidence,
    # so the two are read together.
    op.create_index("ix_listing_sale_confidence", "listing", ["sale_confidence"])


def downgrade() -> None:
    op.drop_index("ix_listing_sale_confidence", table_name="listing")
    op.drop_column("listing", "sale_signals")
    op.drop_column("listing", "sale_confidence")
