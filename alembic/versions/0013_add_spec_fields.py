"""add spec extraction fields and the accessory flag

Two problems, both invisible to epid and to CLIP.

Spec. Residual price spread inside a comp set filtered by 0012 stayed at a
median of 4.3x, and 15.0x for Memory (RAM), because every DDR4 stick looks
identical whether it is 16GB or 64GB. Capacity lives in the title and not in
eBay's aspects precisely where it matters: RAM 97% of titles against 12% of
aspects, graphics cards 79% against 0.3%. Capacity plus generation plus form
factor takes 32GB RAM from 82.7x spread to 2.7-4.4x.

Accessories. Grouping graphics cards by chipset gave rtx-3090 a spread of
1428x, and the low end was entirely parts *for* the card: a $6.61 manual, a
$34.99 backplate, an $88 heatsink assembly, a $187 NVLink bridge. They match
on model string and on image, so both identity mechanisms accept them, at
2-20% of the real price.

All five columns are indexed because comp selection filters on them in SQL.

See docs/decisions/0013-spec-extraction.md.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "listing",
        sa.Column("is_accessory", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("listing", sa.Column("capacity_gb", sa.Integer(), nullable=True))
    op.add_column("listing", sa.Column("spec_generation", sa.String(), nullable=True))
    op.add_column("listing", sa.Column("form_factor", sa.String(), nullable=True))
    op.add_column("listing", sa.Column("model_key", sa.String(), nullable=True))

    # Dropped so new inserts use the application default, matching every other
    # boolean added since 0005.
    op.alter_column("listing", "is_accessory", server_default=None)

    op.create_index("ix_listing_is_accessory", "listing", ["is_accessory"])
    op.create_index("ix_listing_capacity_gb", "listing", ["capacity_gb"])
    op.create_index("ix_listing_spec_generation", "listing", ["spec_generation"])
    op.create_index("ix_listing_form_factor", "listing", ["form_factor"])
    op.create_index("ix_listing_model_key", "listing", ["model_key"])

    # Not backfilled here, same reasoning as 0012: extraction is application
    # logic that will be revised, and encoding a snapshot of it in SQL would
    # both duplicate the rules and freeze them at this revision.
    # `python -m ml.extract_listings` fills these in and is re-runnable.


def downgrade() -> None:
    for name in (
        "ix_listing_model_key",
        "ix_listing_form_factor",
        "ix_listing_spec_generation",
        "ix_listing_capacity_gb",
        "ix_listing_is_accessory",
    ):
        op.drop_index(name, table_name="listing")
    for column in ("model_key", "form_factor", "spec_generation", "capacity_gb", "is_accessory"):
        op.drop_column("listing", column)
