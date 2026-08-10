"""capture eBay's numeric category id alongside its name

ml/similar.py filters comps on `category` with a hard `==`, and the name is
locale-dependent while the id is not. The corpus already holds
`Grafik-/Videokarten` beside `Graphics/Video Cards` and `PC Desktops &
All-in-Ones` beside `All-In-Ones`: one eBay category under several names,
which `==` treats as separate pools too small to reach the three-comp minimum.

NULLABLE, and nothing filters on it yet. Every existing row has no value, so
moving the filter today would split the corpus into "has an id" and "does not"
rather than merging the locales, which is worse than the status quo. Ingestion
fills it for new rows; the disappearance check backfills active ones as it
enriches them.

Nullable is also what makes this migration safe against the failure
systems/preflight.py exists for: a long-lived RQ worker holding pre-migration
models omits the column on INSERT, and a NOT NULL column would make every
insert fail silently behind ingest_all's per-search error isolation. That has
happened three times.

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-10

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("listing", sa.Column("category_id", sa.String(), nullable=True))
    op.create_index("ix_listing_category_id", "listing", ["category_id"])


def downgrade() -> None:
    op.drop_index("ix_listing_category_id", table_name="listing")
    op.drop_column("listing", "category_id")
