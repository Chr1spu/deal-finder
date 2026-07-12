"""add last_checked_at to listing, and index last_seen_at

The disappearance check now prioritizes by last_seen_at (refreshed for free by
ingestion) rather than sweeping every active listing, so both timestamps are
read on the hot path and need indexes. See
docs/decisions/0003-ebay-call-budget.md.

The new 'stale' ListingStatus value needs no DDL: status is a plain String
column, not a native Postgres enum, so a new value is a code-level change only.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-12

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("listing", sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_listing_last_checked_at", "listing", ["last_checked_at"])
    op.create_index("ix_listing_last_seen_at", "listing", ["last_seen_at"])


def downgrade() -> None:
    op.drop_index("ix_listing_last_seen_at", table_name="listing")
    op.drop_index("ix_listing_last_checked_at", table_name="listing")
    op.drop_column("listing", "last_checked_at")
