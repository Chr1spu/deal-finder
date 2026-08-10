"""Re-measure comp-set price spread under both metrics, side by side.

ADR 0019 established that `max/min`, the measure every earlier number in this
project is quoted in, converges on the single most absurdly priced listing
present rather than describing the comp set. Since eBay always contains an
absurdly priced listing, that makes max/min close to useless above a few
hundred rows, and it means the conclusions in ADRs 0012, 0013 and 0018 need
re-reading rather than trusting.

This is the tool for that re-reading. It prints max/min and p90/p10 beside each
other for the groupings those ADRs argued from, so a claim like "RAM 15.0x" can
be checked against what the same grouping says today with the tails removed.

    python -m ml.measure_spread
    python -m ml.measure_spread --min-group 10

It reads titles, prices and extracted fields. It writes nothing.

Note it cannot reproduce the historical figures: the corpus was rebuilt on
2026-08-26 when the eBay credentials moved to production, so every listing
postdates that. What it can do, and what matters, is show the size of the gap
between the two metrics on the same data.
"""

from __future__ import annotations

import argparse
import statistics
from collections import defaultdict

from sqlalchemy import text
from sqlmodel import Session

from api.db import engine as default_engine


def quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return ordered[index]


def max_over_min(prices: list[float]) -> float | None:
    usable = [p for p in prices if p and p > 0]
    if len(usable) < 2:
        return None
    return max(usable) / min(usable)


def trimmed(prices: list[float], min_size: int = 5) -> float | None:
    """p90/p10. Needs a few rows to mean anything, hence min_size."""
    usable = [p for p in prices if p and p > 0]
    if len(usable) < min_size:
        return None
    low = quantile(usable, 0.10)
    return quantile(usable, 0.90) / low if low > 0 else None


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}x"


def report(label: str, groups: dict[str, list[float]], min_group: int) -> None:
    big = {k: v for k, v in groups.items() if len(v) >= min_group}
    if not big:
        print(f"\n{label}: no group reaches {min_group} listings")
        return

    raws = [r for r in (max_over_min(v) for v in big.values()) if r is not None]
    trims = [t for t in (trimmed(v) for v in big.values()) if t is not None]

    print(f"\n{label}  ({len(big)} groups of {min_group}+)")
    print(f"   {'':22} {'max/min':>10} {'p90/p10':>10}")
    if raws and trims:
        print(f"   {'median group':22} {_fmt(statistics.median(raws)):>10} "
              f"{_fmt(statistics.median(trims)):>10}")
        print(f"   {'worst group':22} {_fmt(max(raws)):>10} {_fmt(max(trims)):>10}")
    everything = [p for v in big.values() for p in v]
    print(f"   {'all groups pooled':22} {_fmt(max_over_min(everything)):>10} "
          f"{_fmt(trimmed(everything)):>10}")

    worst = sorted(big.items(), key=lambda kv: -(max_over_min(kv[1]) or 0))[:5]
    print("   worst five by max/min, with what they say trimmed:")
    for key, prices in worst:
        print(f"      {key[:30]:30} n={len(prices):>4} "
              f"{_fmt(max_over_min(prices)):>9} {_fmt(trimmed(prices)):>9} "
              f"median ${statistics.median(prices):>9,.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--min-group", type=int, default=5)
    args = parser.parse_args()

    with Session(default_engine) as session:
        # session.connection().execute rather than session.exec: SQLModel's
        # select() is only typed up to four entities (measure_kit_rule.py hits
        # the same wall) and this wants eleven columns. Going through the
        # connection also keeps the 512-float embedding column out of memory,
        # which matters at 28,000 rows.
        rows = session.connection().execute(
            text(
                """select title, price, category, model_key, capacity_gb,
                          spec_generation, form_factor, is_accessory, lot_size,
                          has_defect, price_is_from
                   from listing where price > 0"""
            )
        ).all()

    comparable = [
        r
        for r in rows
        if r.lot_size is None and not r.has_defect and not r.is_accessory and not r.price_is_from
    ]
    print(f"corpus {len(rows)}, comparable {len(comparable)}")
    print(
        "\nEvery figure in ADRs 0012, 0013 and 0018 is the max/min column. "
        "\nRead the p90/p10 column beside it: ADR 0019 records why."
    )

    # 0013's headline: spread by category, filtered.
    by_category: dict[str, list[float]] = defaultdict(list)
    for r in comparable:
        by_category[r.category or "(none)"].append(r.price)
    report("by category (0013 argued from this)", by_category, args.min_group)

    # 0013's fix: spec-segmented groups.
    segmented: dict[str, list[float]] = defaultdict(list)
    for r in comparable:
        if r.category and "memory" in r.category.lower():
            key = f"{r.capacity_gb}GB/{r.spec_generation}/{r.form_factor}"
            segmented[key].append(r.price)
    report("memory, segmented by capacity+generation+form", segmented, args.min_group)

    # 0013's model-key ladder, and the 1428x that exposed accessories.
    by_model: dict[str, list[float]] = defaultdict(list)
    for r in comparable:
        if r.model_key:
            by_model[r.model_key].append(r.price)
    report("by model_key", by_model, args.min_group)

    # The comp set as ml/similar.py actually builds it: category AND model key.
    as_built: dict[str, list[float]] = defaultdict(list)
    for r in comparable:
        if r.model_key and r.category:
            as_built[f"{r.category}|{r.model_key}"].append(r.price)
    report("as ml/similar.py filters (category + model_key)", as_built, args.min_group)


if __name__ == "__main__":  # pragma: no cover - operator entry point
    main()
