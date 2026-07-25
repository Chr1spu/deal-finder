"""Apply variant extraction across the corpus and store the results.

Same shape as ml/embed_listings.py: pure logic lives in ml/extract.py, this
module owns the Session and decides what to process.

Unlike embedding, this is cheap (a handful of regexes, no network, no GPU) and
the rules will change as the vocabulary grows. So the default is to re-run
everything rather than only untouched rows: `--only-new` exists for the
scheduled path, where re-deriving 12,000 rows every fifteen minutes would be
pointless work.

See docs/decisions/0012-variant-extraction.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from api.db import engine as default_engine
from api.models import Listing
from ml.extract import extract_variant

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 2000


@dataclass
class ExtractResult:
    processed: int = 0
    lots: int = 0
    defects: int = 0
    accessories: int = 0
    multi_variant: int = 0
    classified_completeness: int = 0
    changed: int = 0
    by_class: dict = field(default_factory=dict)

    @property
    def excluded_from_comps(self) -> int:
        """Rows that can no longer stand as a comparable. The headline number:
        these are the listings that would otherwise have quietly poisoned a
        comp set."""
        return self.lots + self.defects + self.accessories + self.multi_variant


def extract_all(
    only_new: bool = False,
    limit: int | None = None,
    db_engine: Engine | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> ExtractResult:
    """Derive lot_size / completeness / has_defect for listings.

    only_new restricts to rows never processed (`variant_signals IS NULL`),
    which is what the scheduler wants. The default re-derives everything,
    which is what you want after changing a rule: the extraction is cheap and
    the alternative is a corpus classified by several different vintages of
    the ruleset with no way to tell which is which.
    """
    db_engine = db_engine or default_engine
    result = ExtractResult()
    last_id = 0

    while limit is None or result.processed < limit:
        remaining = chunk_size if limit is None else min(chunk_size, limit - result.processed)
        if remaining <= 0:
            break

        with Session(db_engine) as session:
            statement = select(Listing).where(col(Listing.id) > last_id)
            if only_new:
                statement = statement.where(col(Listing.variant_signals).is_(None))
            listings = session.exec(
                statement.order_by(col(Listing.id).asc()).limit(remaining)
            ).all()

            if not listings:
                break

            for listing in listings:
                last_id = listing.id or last_id
                variant = extract_variant(listing.title, listing.aspects, listing.category)

                # Every derived field, not just the original three. Comparing
                # a subset made `changed` report 0 after a run that rewrote
                # five new columns, which is the one number telling you a rule
                # change actually did something.
                before = (
                    listing.lot_size,
                    listing.completeness,
                    listing.has_defect,
                    listing.is_accessory,
                    listing.price_is_from,
                    listing.capacity_gb,
                    listing.spec_generation,
                    listing.form_factor,
                    listing.model_key,
                )
                listing.lot_size = variant.lot_size
                listing.completeness = variant.completeness
                listing.has_defect = variant.has_defect
                listing.is_accessory = variant.is_accessory
                listing.price_is_from = variant.price_is_from
                listing.capacity_gb = variant.capacity_gb
                listing.spec_generation = variant.spec_generation
                listing.form_factor = variant.form_factor
                listing.model_key = variant.model_key
                # Always a dict, never None, so `variant_signals IS NULL`
                # cleanly means "never processed" rather than "processed and
                # found nothing", which only_new depends on.
                listing.variant_signals = variant.signals or {}

                if before != (
                    variant.lot_size,
                    variant.completeness,
                    variant.has_defect,
                    variant.is_accessory,
                    variant.price_is_from,
                    variant.capacity_gb,
                    variant.spec_generation,
                    variant.form_factor,
                    variant.model_key,
                ):
                    result.changed += 1

                result.processed += 1
                if variant.is_lot:
                    result.lots += 1
                if variant.has_defect:
                    result.defects += 1
                if variant.is_accessory:
                    result.accessories += 1
                if variant.price_is_from:
                    result.multi_variant += 1
                if variant.completeness:
                    result.classified_completeness += 1
                    result.by_class[variant.completeness] = (
                        result.by_class.get(variant.completeness, 0) + 1
                    )
                session.add(listing)

            session.commit()

        logger.info("extracted %d listings so far", result.processed)

    return result


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    started = datetime.now(timezone.utc)
    outcome = extract_all(only_new="--only-new" in sys.argv)
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    print(
        f"Processed {outcome.processed} listings in {elapsed:.0f}s: "
        f"{outcome.lots} lots, {outcome.defects} defective, {outcome.accessories} accessories, "
        f"{outcome.multi_variant} multi-variant, "
        f"{outcome.classified_completeness} with stated completeness "
        f"({outcome.by_class}), {outcome.changed} changed. "
        f"{outcome.excluded_from_comps} are now excluded from comp sets."
    )
