# 0021 - What breaks when the corpus leaves PC hardware

**Context:** The saved searches are 64 keywords covering graphics cards, CPUs,
memory, SSDs, phones and consoles. The obvious next move is to broaden them:
clothing is the case that prompted this, and it is the right case to reason
about because Depop and Facebook, the two capture sources this project exists
to serve, are overwhelmingly clothing marketplaces. Today a captured Depop
jacket is matched against a corpus containing no clothing at all.

This ADR records what was measured before adding anything, because the failure
mode is not an error. A new category ingests cleanly, embeds cleanly, extracts
cleanly, and produces nothing.

**Decision:** Do not add clothing saved searches yet. The blocking gap is
identity, and it is one field.

`ml/valuation.py:deal_candidates` selects active listings whose `model_key` or
`epid` matches that of some *sold* listing. Everything else is never valued.
Measured on 2026-08-10: **9,490 of 21,716 active listings, 43.7%, are ever
considered by the deal scan.** The remaining 12,226 cost eBay quota, disk,
image hashing and GPU time, and can never appear in the feed.

`_model_key` recognises **graphics-card chipsets and nothing else**. Its
docstring says so plainly ("returns None outside the categories the vocabulary
covers, which is most of the corpus"). So outside GPUs, scannability is
entirely `epid` coverage, and `epid` is an eBay *catalog* id:

| category | active | scannable | epid |
|---|---|---|---|
| Cell Phones & Smartphones | 5,301 | 4,563 | 86.1% |
| Graphics/Video Cards | 3,601 | 3,494 | 36.6% |
| Video Game Consoles | 2,513 | 829 | 32.9% |
| PC Desktops & All-In-Ones | 1,842 | 1,324 | 3.3% |

Used clothing is almost entirely non-catalog, so its `epid` coverage will sit
near the bottom of that table, and no GPU regex will fire on it. **Clothing
searches would ingest thousands of listings that are structurally incapable of
producing a deal.** Consoles already show the shape of this: two thirds of
them are unscannable today, which was not previously written down.

**What clothing needs, in order:**

1. **A `model_key` analogue.** Brand plus garment type
   (`carhartt-detroit-jacket`). The raw material is better here than for
   components: eBay's `localizedAspects` carry Brand, Size, Colour, Material
   and Style reliably on clothing, where they carry capacity on 0.3% of
   graphics cards. This is the one hard blocker.
2. **Size as a *hard* filter, in a new column.** Size is to a jacket what
   capacity is to memory, except it cannot be a soft filter that keeps
   unstated rows: a Small is not a noisy measurement of a Large, it is a
   different product, and the same argument `0012` used for lots applies. It
   is also not orderable across brands, so it is categorical, not numeric.
3. **Condition vocabulary.** `has_defect` is electronics-shaped ("no display",
   "bad motherboard"). Clothing needs pilling, staining, holes, fading, and
   condition drives used-clothing price far harder than it drives component
   price.

**What already works, and one thing that gets better:** the extraction gates
anticipated this. `_COMPONENT_CONTEXT_RE` keeps `form_factor` and
`spec_generation` from firing outside memory and storage, and
`tests/test_extract.py` already carries the cross-category cases ("lots of
character" on a jacket, "2.5 inch" on a heel). The generic rules (lots,
for-parts, bundles) transfer without tuning.

CLIP gets **stronger**, not weaker. Stage 3a measured that retrieval quality
depends on whether the price-setting attribute is visible in the photo:
prebuilt PCs retrieved neighbours spanning $578 to $3,000 because their
photos are interchangeable boxes whose prices are set by invisible internals.
A jacket's photo carries most of what sets its price. The half of this system
that works worst on PC hardware is the half clothing plays to.

**The uncomfortable part, and it is the reason this is an ADR rather than a
backlog item.** ADR `0008` refused Depop and Facebook as comp sources on the
grounds that "item variety on those sites is too high for per-item price
history built from them to mean anything." That is a claim about **item
variety, not about the site**, and eBay clothing has the same variety. eBay
earns its place as the price oracle for catalogued fungible goods, where one
RTX 4090 is interchangeable with another. A used Carhartt jacket in size L
with three years of wear is not interchangeable with anything, and `0014`
refuses to estimate below three comps.

So the honest expectation is that clothing comps will be thin even after
identity is solved, and that thinness is a property of the goods rather than a
bug to engineer away. Two consequences worth stating in advance:

- If clothing goes ahead, `0008`'s reasoning needs revisiting in the same
  breath, because "eBay has the volume and catalog structure to support price
  history" is a claim that holds by category and was tested only on hardware.
- The reasonable near-term target is not a clothing *deal scanner* but
  clothing *valuation*: answering "is this Depop jacket underpriced" against
  eBay comps, with a confidence that is often low and comps that are often
  too few, and saying so. That is what the capture path already does, and it
  needs the corpus to contain clothing to work at all.

**Alternatives considered:** Adding a few clothing searches to see what
happens, rejected because the outcome is predictable from the table above and
each search costs 12 Browse calls/day forever against a 5,000/day budget with
1,432 spare. Relaxing `deal_candidates` to scan unkeyed listings, rejected
because it makes the scan far more expensive while producing comp sets whose
identity is guesswork, which is the thing `model_key`'s exact match was
tightened to prevent on 2026-08-01.

**A smaller finding recorded here because it will bite harder as categories
broaden.** `ml/similar.py` filters candidates on `category` with `==`, and the
corpus already contains locale variants of the same category:
`Grafik-/Videokarten` (23 listings), `CPUs/Prozessoren` (14),
`Arbeitsspeicher (RAM)` (7), `Schede video e grafiche` (4), plus case variants
like `PC Desktops & All-in-Ones` against `All-In-Ones`. 287 listings across 89
categories sit in pools of fewer than 20 and effectively cannot reach three
comps. The marketplace is set to `EBAY_US`; these arrive from international
sellers with localized category names. Clothing has far more category names
than components do, so this gets worse rather than better.
