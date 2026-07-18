"""split price confidence out of sale confidence

sale_confidence now answers only "did it sell"; price_confidence answers only
"is the recorded price what was paid". See docs/decisions/0007-two-confidences.md.

No data migration needed: no disappearance has ever been confirmed in
production, so every sale_confidence is still null and there is nothing whose
meaning would silently change underneath it.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-18

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("listing", sa.Column("price_confidence", sa.Float(), nullable=True))
    # Not indexed, unlike sale_confidence: stage 4 filters comp membership on
    # the sale score and only then weights by price, so this is read on rows
    # already narrowed rather than used to narrow them.
    op.execute("UPDATE listing SET sale_confidence = NULL, sale_signals = NULL")


def downgrade() -> None:
    op.drop_column("listing", "price_confidence")
