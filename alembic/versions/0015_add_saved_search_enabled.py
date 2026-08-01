"""let a saved search be disabled without deleting it

Each enabled saved search costs one Browse call per ingest run, which at a
2-hour interval is 12 calls/day forever. Disabling is how you free that
capacity without discarding `last_result_total` and `last_run_at`, which are
accumulated observability rather than config, and without losing the row for
a search you mean to bring back.

Defaults to true so every existing search keeps running unchanged.

See docs/decisions/0016-saved-search-crud.md.

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-01

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "savedsearch",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # Dropped so new inserts use the application default, matching every other
    # boolean added since 0005.
    op.alter_column("savedsearch", "enabled", server_default=None)
    op.create_index("ix_savedsearch_enabled", "savedsearch", ["enabled"])


def downgrade() -> None:
    op.drop_index("ix_savedsearch_enabled", table_name="savedsearch")
    op.drop_column("savedsearch", "enabled")
