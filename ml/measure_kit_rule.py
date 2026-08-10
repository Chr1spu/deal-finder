"""Measure the memory-kit rule against the whole corpus before trusting it.

Exists because of a rule this project learned the expensive way: an extraction
change that looks right on a dozen hand-picked titles can misclassify 14.1% of
the corpus (the "Nx" quantity forms, see _LOT_COUNT_RE). Corpus-wide frequency
is the cheap check, and a rate far off the expected one is the signal.

Run it on the machine holding the real database, before and after re-running
extraction:

    python -m ml.measure_kit_rule
    python -m ml.measure_kit_rule --sample 40

It reads titles and prices only. It writes nothing.

Two questions, because the rule was made for the second and could only be
justified by the first:

  1. How many listings does it change, and are the changes right? A kit rule
     firing on 14% of a corpus that is mostly graphics cards would be wrong on
     its face, whatever the sample looked like.
  2. Does DDR5 desktop memory's price spread actually fall? That spread stayed
     at 19.1x after every other filter, and single sticks sitting in the same
     capacity bucket as multi-stick kits is the standing suspect.
"""

from __future__ import annotations

import argparse
import random
from collections import defaultdict

from sqlmodel import Session, col, select

from api.db import engine as default_engine
from api.models import Listing
from ml.extract import _CAPACITY_RE, MAX_PLAUSIBLE_CAPACITY_GB, _kit_capacities


def capacity_without_kits(title: str) -> int | None:
    """What _capacity_gb returned before the kit rule: the largest single
    capacity token, with no notion of a total."""
    best: int | None = None
    for match in _CAPACITY_RE.finditer(title):
        value = int(match.group(1))
        if match.group(2).upper() == "TB":
            value *= 1024
        if value > MAX_PLAUSIBLE_CAPACITY_GB or value <= 0:
            continue
        best = value if best is None else max(best, value)
    return best


def capacity_with_kits(title: str, category: str | None = None) -> int | None:
    best = capacity_without_kits(title)
    for _, total in _kit_capacities(title, category):
        best = total if best is None else max(best, total)
    return best


def spread(prices: list[float]) -> float | None:
    """max/min, the same measure ADR 0013 reported comp-set quality with.

    Kept because every earlier number in this project is quoted in it, and
    changing the definition silently would make those numbers incomparable.
    Read it beside `trimmed_spread` below, never alone.
    """
    usable = [p for p in prices if p > 0]
    if len(usable) < 2:
        return None
    return max(usable) / min(usable)


def trimmed_spread(prices: list[float]) -> float | None:
    """p90/p10: the same ratio with both tails removed.

    This exists because max/min turned out to be measuring the wrong thing,
    and it took until 2026-08-10 to notice. DDR5 desktop 32GB reports a
    42.31x max/min spread, which two ADRs treated as evidence that comps were
    contaminated. The two listings producing it are a $5,500 asking price on a
    G.SKILL Trident Z5 kit that retails near $200, and a $130 single stick.
    Trim those and the same bucket sits at 1.83x, better than the 2.74x that
    ADR 0013 reported as a success.

    max/min over any set containing one absurd asking price measures the
    absurd price. eBay always contains absurd asking prices, so on a large
    corpus max/min converges on the worst listing rather than on comp quality.
    A trimmed ratio answers the question the metric was meant to ask: how
    tightly do the *typical* comps agree.
    """
    usable = sorted(p for p in prices if p > 0)
    if len(usable) < 5:
        return None

    def at(fraction: float) -> float:
        return usable[max(0, min(len(usable) - 1, round(fraction * (len(usable) - 1))))]

    low = at(0.10)
    return at(0.90) / low if low > 0 else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=int, default=20, help="changed titles to print")
    parser.add_argument("--seed", type=int, default=0, help="sampling seed, for a repeatable read")
    args = parser.parse_args()

    # Two queries rather than one of five columns, because SQLModel's select()
    # is only typed up to four entities. Also keeps the embedding column out of
    # memory, which matters at 20,000 rows of 512 floats.
    with Session(default_engine) as session:
        rows = session.exec(
            select(Listing.title, Listing.category).where(col(Listing.title).is_not(None))
        ).all()
        memory_rows = session.exec(
            select(Listing.title, Listing.price, Listing.spec_generation, Listing.form_factor)
            .where(col(Listing.title).is_not(None))
        ).all()

    changed: list[tuple[str, int | None, int]] = []
    by_category: dict[str, int] = defaultdict(int)
    for title, category in rows:
        before, after = capacity_without_kits(title), capacity_with_kits(title, category)
        if after != before and after is not None:
            changed.append((title, before, after))
            by_category[category or "(none)"] += 1

    total = len(rows)
    print(f"corpus: {total} listings")
    print(f"changed by the kit rule: {len(changed)} ({100 * len(changed) / max(total, 1):.2f}%)")
    print()
    print("A rate in the low single digits is the expected shape: memory kits are a")
    print("small slice of a corpus that is mostly graphics cards. Double digits would")
    print("mean the pattern is catching something other than kits, which is exactly")
    print("how the generic Nx forms failed at 14.1%.")
    print()

    print("top categories affected:")
    for category, count in sorted(by_category.items(), key=lambda kv: -kv[1])[:10]:
        print(f"  {count:5d}  {category}")
    print()

    print(f"sample of changed titles (seed {args.seed}):")
    random.Random(args.seed).shuffle(changed)
    for title, before, after in changed[: args.sample]:
        was = "unstated" if before is None else f"{before}GB"
        print(f"  {was:>9} -> {after:>6}GB   {title[:88]}")
    print()

    # Question 2: the spread this rule was built to attack. Bucket DDR5
    # desktop memory by capacity and measure within each bucket, which is what
    # a comp set for one of those listings actually looks like.
    print("DDR5 desktop memory, price spread within each capacity bucket:")
    for label, capacity_of in (("before", capacity_without_kits), ("after", capacity_with_kits)):
        buckets: dict[int, list[float]] = defaultdict(list)
        unstated: list[float] = []
        for title, price, generation, form in memory_rows:
            if generation != "DDR5" or form != "desktop" or price is None:
                continue
            capacity = capacity_of(title)
            (buckets[capacity] if capacity is not None else unstated).append(price)

        spreads = [s for s in (spread(prices) for prices in buckets.values()) if s is not None]
        trimmed = [s for s in (trimmed_spread(p) for p in buckets.values()) if s is not None]
        all_prices = [p for prices in buckets.values() for p in prices] + unstated
        overall = spread(all_prices)
        overall_trimmed = trimmed_spread(all_prices)
        worst = max(spreads) if spreads else None
        worst_trimmed = max(trimmed) if trimmed else None
        median_spread = sorted(spreads)[len(spreads) // 2] if spreads else None
        median_trimmed = sorted(trimmed)[len(trimmed) // 2] if trimmed else None
        print(
            f"  {label:>6}: {len(buckets)} buckets, "
            f"ungrouped (capacity unstated) {len(unstated)}"
        )
        print(
            f"          max/min : median {median_spread and round(median_spread, 2)}, "
            f"worst {worst and round(worst, 2)}, overall {overall and round(overall, 2)}"
        )
        print(
            f"          p90/p10 : median {median_trimmed and round(median_trimmed, 2)}, "
            f"worst {worst_trimmed and round(worst_trimmed, 2)}, "
            f"overall {overall_trimmed and round(overall_trimmed, 2)}"
        )
    print()
    print("Read the p90/p10 line. Measured 2026-08-10: DDR5 desktop 32GB reports")
    print("42.31x on max/min and 1.83x trimmed, and the gap is two listings, a")
    print("$5,500 asking price on a ~$200 kit and a $130 single stick. The 19.1x")
    print("that ADR 0013 blamed on kits, and 0018 was written to fix, is that")
    print("statistic rather than a property of the comp sets. On a corpus this")
    print("size max/min converges on the most absurdly priced listing present.")
    print()
    print("For the kit rule itself the number that matters is 'ungrouped'. A kit")
    print("written '2x16GB' had NO capacity at all before this rule (the capacity")
    print("pattern needs a word boundary, and 'x16GB' has none), so it sat in")
    print("every capacity bucket at once, since unstated rows are kept.")


if __name__ == "__main__":  # pragma: no cover - operator entry point
    main()
