# 0022 - model_key beyond graphics cards, and what the re-audit found

**Context:** `ml/valuation.py:deal_candidates` only considers an active listing
if its `model_key` or `epid` matches that of some sold listing. Everything else
is ingested, image-hashed, embedded, attribute-extracted, and then never
valued. Measured 2026-08-10, that was **44.8% of active listings**, and the
cause was one function: `_model_key` recognised graphics-card chipsets and
nothing else.

| category | active | keyed | note |
|---|---|---|---|
| Cell Phones & Smartphones | 5,146 | 0 | carried by `epid` at 86% |
| Graphics/Video Cards | 3,470 | 3,306 | the only family implemented |
| Video Game Consoles | 2,416 | 2 | `epid` only 33%, so mostly unvaluable |
| Memory (RAM) | 2,402 | 0 | |
| Solid State Drives | 1,746 | 14 | |
| CPUs/Processors | 1,335 | 2 | a CPU pattern existed and was never called |

`_CPU_MODEL_RE` had been written, left unwired, and was wrong in a way only
measurement showed: its lookahead required a CPU word *after* the model number,
but sellers write "AMD Ryzen 9 5900X" with the word first. It matched at all
only when a title happened to say "12-Core" later on.

**Decision:** `_model_key` now tries graphics card, then processor, then
console, then phone, first match wins. Each family was measured on the corpus
before being wired in, with spread reported as p90/p10 per ADR `0019`:

| family | keyed | category-wide spread | within-key spread |
|---|---|---|---|
| processors | 89.0% | 4.06x | 1.81x |
| consoles | 99.0% | 4.00x | 1.88x |
| phones | 99.3% | 3.08x | 1.58x |

**Two ordering hazards, both load-bearing.** "Nintendo Switch OLED" contains
"Nintendo Switch", and "iPhone 16e" contains "iPhone 16". If the bare form
matched first, a $200 OLED and a $120 Lite would both file as `switch`, which
is a $150 product. The qualified alternatives come first in both patterns and a
test asserts the four Switch variants stay four keys.

**GPU is tried before CPU, and that is a measurement not a preference.** A
gaming PC names both. Keying PC Desktops by graphics card takes their spread
from 5.08x to 2.0x and laptops from 3.86x to 1.71x, because the card is the
dominant price driver in a machine. Keying the same listings by processor
instead would group a $900 machine with a $3,000 one.

**Not gated on category, deliberately.** A key fires on any title naming a
product it recognises, including a server that names its Ryzen. That is
correct because `ml/similar.py` filters candidates on `category` with a hard
`==` first, so a keyed server can only ever meet another server, where "same
CPU" is exactly the comparison wanted. Measured leakage outside the obvious
category is 4.27% for processors and 2.82% for consoles, essentially all of it
machines naming their own components; phones leak at 0.07%.

**Alternatives considered:** gating each family on its category, which would
have removed the measured benefit the existing GPU key already provides on PC
Desktops and laptops. Extending to memory and storage, rejected: those are
priced by capacity and generation, which already have their own fields, and
`None` there remains the honest answer.

**Consequences:** scannable listings go from **9,438 (44.8%) to 14,068
(63.3%)**, and listings with neither a key nor an `epid` fall from 6,811 to
3,206. Two thirds of the console category becomes valuable for the first time.

The figure settles at **60.6%** once the two exclusions below land (eBay's
`condition` field, and auctions). That dip is the change working: those
listings are now keyed, found, and then correctly refused, where before they
were never looked at for the wrong reason.
The deal scan takes correspondingly longer, since it now has half again as many
candidates to run k-NN for.

This is also the prerequisite ADR `0021` named for clothing: `brand + garment
type` is the same mechanism, and building it for consoles first tested the
shape against a corpus that already exists.

---

## The re-audit that came with it

`0019` established that max/min converges on the worst listing present rather
than describing a comp set, and that every spread figure in `0012`, `0013` and
`0018` is quoted in it. `ml/measure_spread.py` now prints both metrics side by
side for the groupings those ADRs argued from. What it shows:

| grouping | max/min | p90/p10 |
|---|---|---|
| by category, median group | 44.41x | 4.26x |
| memory segmented by capacity+generation+form, median | 4.21x | 1.82x |
| by model_key, median group | 8.80x | 2.69x |
| **as ml/similar.py actually filters (category + model_key)** | **3.85x** | **1.74x** |

The last row is the one that matters, and it is the first time this project has
measured the comp set it actually builds rather than a proxy for it. **1.74x
typical spread** is a good number and was invisible under the old metric.

`0013`'s directional claims survive: memory is still the worst category (9.68x
trimmed) and graphics cards next (6.26x). Its magnitudes never meant anything.

**One group survived trimming and was a real defect.** `PC Desktops &
All-In-Ones` keyed `i9-12900k` sat at 164x p90/p10 across ten listings, and two
of them were empty retail boxes at $20.89 and $31.50 in a group whose median is
$1,300. `_ANY_ACCESSORY_ONLY_RE` already catches "Retail Box ONLY"; these say
it the other way round, a packaging noun plus the explicit absence of what
belongs in it. That combination is unambiguous where "no CPU" alone is not: a
barebones machine is sold without a processor all the time, but nobody
describes a machine as a "Box Wafer NO CPU INCLUDED". Three listings
corpus-wide, 0.011%, no false positives. One of them reads "EMPTU Box", so the
rule cannot lean on the word "empty".

That is the second time trimming a metric has found a real bug that the
untrimmed version buried, which is the argument for keeping both columns.

---

## Two fields that were stored and never read

Chasing one unexplained feed entry (a "PNY RTX 4090 Verto 24gb" at $107.87
against a $2,699.99 estimate, with nothing in its title to catch) turned up two
columns that decide comparability and were consulted by nothing.

**`condition`.** eBay's own dropdown value, stored since migration 0001. 1,668
listings read "For parts or not working" and **485 had `has_defect` false**,
because their titles never said so. `_DEFECT_RE` had grown to dozens of
alternatives across four sessions, each added after a broken item reached the
feed, all of them inferring from prose a fact the seller had already stated.
`extract_variant` now takes `condition` and consults it first, with locale
variants for the same reason category names need them: **486 listings newly
excluded, 1.73%, nothing wrongly reverted.**

This is the third time the answer has been a structured field rather than a
better regex (eBay's category taxonomy for accessories, `epid` for identity,
now `condition`). The rule that falls out: when the marketplace gives you a
structured field, a regex over the title is probably reconstructing it badly.
Title vocabulary stays as the fallback, because most listings are "Used"
whatever their title admits.

**`is_auction`.** ADR `0004` captured this flag *specifically* because
unflagged auctions "would have quietly poisoned stage 4's comps", and
deliberately deferred what to do until there was data. Nothing scheduled the
moment to look, and the flag was referenced by no filter in `valuation.py`,
`similar.py` or `deal_scan.py`. The data: 451 active auctions, **279 at zero
bids**, where `price` is an opening bid set low on purpose.

`deal_candidates` now excludes auctions, and the asymmetry is the point: they
are **not** excluded from the comp pool, because a *sold* auction's final price
is a real transaction between two people and therefore better evidence than any
unsold listing's asking price. The same field means opposite things on the two
sides of the comparison.

## The trailing-"box" rule, rejected a third time and finally explained

`0013`-era vocabulary work, the 2026-08-07 probe and a positional version tried
here have all failed to catch the $44.99 "MSI ... Graphics Card BOX" that leads
the feed. The positional attempt, restricted to "box" as an unqualified
trailing subject noun, still flags a $1,500 RTX 3090 "in original box.", a
$1,099 RTX 4080 and a $950 iPhone.

The sample explained why: `INTEL CORE I5-14600K BOX` is not a description, it
is Intel's official retail SKU designation, BOX as opposed to TRAY. The word
carries three meanings in listing titles (the product itself, a condition
statement, a manufacturer SKU suffix) and no position separates them.

Recorded as closed rather than open. The discriminator is not in the title.
The only thing distinguishing that listing from a real card is its price
against comps, which is the number being computed, so any rule using it would
be circular.

