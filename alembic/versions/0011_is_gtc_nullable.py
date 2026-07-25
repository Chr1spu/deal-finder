"""make is_gtc nullable, because it was never actually known at ingest

is_gtc was derived from an absent itemEndDate on a non-auction listing. That
inference is valid for a getItem body and invalid for a search response, and
ingestion only ever sees search responses: measured 2026-07-25, itemEndDate is
never present in an itemSummary. So every non-auction listing was marked GTC,
and the resulting 98.9% was an artefact of the wrong endpoint rather than a
fact about eBay.

Nullable because "unknown" is the honest state for any listing the
disappearance check has not yet fetched a full body for, and a bool cannot
express it. Every existing row is reset to NULL rather than kept, since the
stored values were produced by the broken inference and a wrong value is worse
than a missing one: sale_confidence branches on this.

See docs/decisions/0011-ebay-does-not-404-ended-listings.md.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("listing", "is_gtc", existing_type=sa.Boolean(), nullable=True)
    # Discard the values the broken inference produced. They are not merely
    # stale, they are wrong for every non-auction row, and the check will
    # refill them correctly as it works through the corpus.
    op.execute("UPDATE listing SET is_gtc = NULL")


def downgrade() -> None:
    # NOT NULL needs every row to have a value; false is the column's original
    # default and the only safe choice, though it re-introduces the ambiguity
    # this migration exists to remove.
    op.execute("UPDATE listing SET is_gtc = false WHERE is_gtc IS NULL")
    op.alter_column("listing", "is_gtc", existing_type=sa.Boolean(), nullable=False)
