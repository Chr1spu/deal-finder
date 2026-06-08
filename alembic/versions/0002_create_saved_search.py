"""create savedsearch table, seed a default search

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-08

"""
from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

saved_search_table = sa.table(
    "savedsearch",
    sa.column("keyword", sa.String),
    sa.column("location", sa.String),
    sa.column("created_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    op.create_table(
        "savedsearch",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("keyword", sa.String(), nullable=False),
        sa.Column("location", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.bulk_insert(
        saved_search_table,
        [{"keyword": "nintendo switch", "location": None, "created_at": datetime.now(timezone.utc)}],
    )


def downgrade() -> None:
    op.drop_table("savedsearch")
