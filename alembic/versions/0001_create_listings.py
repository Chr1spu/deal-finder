"""create listings table

Revision ID: 0001
Revises:
Create Date: 2026-06-02

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "listing",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("source_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(), nullable=False, server_default="USD"),
        sa.Column("images", sa.JSON(), nullable=False),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("condition", sa.String(), nullable=True),
        sa.Column("category", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=False),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_id", name="uq_listing_source_id"),
    )
    op.create_index("ix_listing_source", "listing", ["source"])
    op.create_index("ix_listing_source_id", "listing", ["source_id"])
    op.create_index("ix_listing_status", "listing", ["status"])


def downgrade() -> None:
    op.drop_table("listing")
