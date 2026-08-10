# 0019 - A shared unit hides a variant list, and max/min was measuring the wrong thing

**Context:** Two separate problems, found in one session on 2026-08-10, that
turn out to share a cause with `0018` and with each other.

First, ADR `0015` added `price_is_from` because a multi-variant eBay listing
shows the price of its *cheapest* option, so its price and its title describe
different items. The mechanism worked, and four of the deal feed's top twelve
were still multi-variant iPhone listings. `_is_multi_variant` counts distinct
capacities in a title, and `_CAPACITY_RE` requires the unit adjacent to each
number, so `8/16/32GB` yields the single capacity `32`. The 8 and the 16 are
invisible. That is exactly the defect `0018` fixed for `2x16GB`, in a second
place, and here it is worse: for a kit it put one product in the wrong bucket,
while here it hid the signal that the listing should not be a comp at all.

Second, `0013` reported that DDR5 desktop memory held a 19.1x price spread
after every filter, `0018` named multi-stick kits as the suspect and was
written to fix it, and the corpus measurement showed the spread did not move.
Median within-bucket spread was 8.34 before and after, worst 19.64 before and
after, to two decimal places.

**Decision:**

1. `_shared_unit_capacities` reads slash-joined capacity lists that share one
   trailing unit, and feeds them to `_distinct_capacities`. Every number in
   the list must be a power of two. That guard is the whole safety argument:
   without it `Apple iPhone 5 / 16GB` reads as capacities `[5, 16]` and
   `RTX 5080 / 32GB` as `[5080, 32]`, and both would then look like
   two-capacity variant listings. Real storage sizes are powers of two, model
   numbers essentially never are.

2. A run of three or more slash-joined colour words marks a variant listing,
   **only in phone categories**. Both limits came from a probe, not taste. Two
   colours joined by a slash is usually one two-tone object (a $1,499
   CyberPowerPC "White/Black RGB Gaming Tower"), and 322 listings match at
   two. Three still misfires outside phones, where a $486 "Nintendo Switch 2
   ... Black/Blue/Orange Joy-Cons" is one console.

3. `_kit_capacities` is gated on category as well as on vocabulary.

4. **`max/min` is no longer reported alone.** `ml/measure_kit_rule.py` prints
   a trimmed `p90/p10` beside it, and the docstring says to read that one.

**Alternatives considered:** For (1), accepting any slash-joined number list
without the power-of-two guard: probed at 196 listings, and the expensive end
was PC spec lists (`Ryzen 9 9950x3d / RTX 5080 / 32GB / 2TB`), so it was
rejected. For (2), two colours instead of three, or three without the category
restriction: both rejected on the counts above. For (4), redefining `spread()`
in place: rejected because every number in `0012`, `0013` and `0018` is quoted
in max/min and silently changing the definition would make them incomparable.

**Consequences:** 81 listings (0.31%) newly carry `price_is_from` and leave
comp sets, all of them genuine variant listings on inspection. The kit rule
went from 56 changed listings to 39, losing all 15 whole machines: a laptop
advertising `128GB RAM 2x2TB SSD` was coming out at 4096GB and an HP Z8 at
49152GB, because `_capacity_gb` takes the maximum and a machine's drives
outranked the number a buyer compares.

The fourth point is the one that changes future work. DDR5 desktop 32GB
reports 42.31x on max/min and **1.83x trimmed**, and the two listings
producing that gap are a $5,500 asking price on a G.SKILL kit that retails
near $200, and a $130 single stick. Across all DDR5 buckets the median goes
8.34 to 2.12 and the worst 19.64 to 3.02. So the "19.1x DDR5 spread" that
`0013` recorded as a defect and `0018` was written to repair **is the
statistic, not the comp sets.** On a corpus of any size, max/min converges on
the most absurdly priced listing present, and eBay always contains one.

Two things follow. The DDR5 question is closed, not deferred: trimmed, those
comp sets are tighter than the 2.74x that `0013` reported as a success. And
every earlier spread number in this project deserves re-reading before it is
used to justify anything, because they are all max/min. `0018` was written to
fix a number that did not need fixing; the rule it produced is still correct,
which is luck rather than method.
