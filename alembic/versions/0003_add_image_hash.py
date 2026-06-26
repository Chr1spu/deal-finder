"""add image_hash column to listing

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-26

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("listing", sa.Column("image_hash", sa.String(), nullable=True))
    op.create_index("ix_listing_image_hash", "listing", ["image_hash"])


def downgrade() -> None:
    op.drop_index("ix_listing_image_hash", table_name="listing")
    op.drop_column("listing", "image_hash")
