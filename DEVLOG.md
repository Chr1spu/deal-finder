# Devlog

## 2026-07-25 - Multi-variant listings, and making the deal feed reachable

**Did:**
- **Multi-variant detection** (ADR `0015`, migration `0014`). One eBay listing offering several configurations shows the price of the CHEAPEST one, so its price and its title describe different items. 671 listings (5.0%), and they concentrate at the top of a deal ranking because a from-price beside a full-spec title produces the largest apparent discounts in the dataset. `price_is_from` joins lots, defects and accessories in `usable_as_comp`.
- **The deal feed is reachable.** `GET /deals` (cached, from the scheduled scan) and `GET /deals/{id}` (computed live). `systems/deal_scan.py` runs the scan on the queue, caches to Redis, and posts new deals to a Discord webhook. Scheduler gained a fourth branch. Before this the whole pipeline worked and you needed a Python REPL to see a single result.
- 318 tests passing, mypy clean across 64 files.

**Decided:**
- Deal results cache in **Redis, not a table**. A deal score is derived and goes stale the moment a comp arrives, unlike `sale_confidence` which is frozen at disappearance time because it describes a moment. A TTL says exactly that: recomputable snapshot, not fact. No migration, and no stale table outliving the logic that wrote it.
- The feed always ships the **comps and the caveats**, not just a percentage. `CLAUDE.md` requires a match to be surfaced as a best guess with its evidence, and a deal score with no visible comps is exactly the verdict shape that forbids.
- Alerts track what has already been announced in a Redis set. Re-announcing the same deal every hour until it sells trains the reader to ignore the channel, and an alert nobody reads is worse than none.
- **Category became a hard comp filter**, unlike the spec fields which only apply when stated. Measured: 16% of comps were cross-category, including a $1,500 "ASUS ROG Strix Gaming Desktop Tower PC" comping a graphics card, because CLIP matched two black boxes with RGB and the PC's title names no GPU model so every spec filter passed it through as "unstated". Spread 2.94x to 2.70x, only 9 of 120 comp sets thinned.

**Broke / debugged, all of it found by reading the deal ranking:**
- **A trademark symbol was silently disabling the most selective filter in the pipeline.** "ROG Strix GeForce RTX(tm) 4080" stored `model_key` as NULL, because the regex required the family and number to be adjacent. NULL then passed the "unstated" comp rule, putting a $1,200 RTX 4080 into the comp set for an RTX 5070 Ti asking $49.99. 18 listings affected. One character.
- `"*MISSING CORE*"` and `"NO MEM NO HDD"` were surfacing as 89% and 64% discounts. Adding them as defects immediately broke the other direction, flagging a $4,854 HP Z8 workstation sold "NO GPU" as broken. The honest distinction turned out to be semantic: **a machine missing a component is a reduced configuration (`bare`), a component missing its own core is broken (`defect`)**. Both then leave comp sets built for complete units, by different and correct routes.
- The capacity-count rule for multi-variant fired on 191 real machines, because "Gaming PC 16GB RAM 512GB SSD RTX 4060 8GB" is three capacities describing three components. Gating multi-component categories out fixed it exactly.
- A heredoc wrote `` into a regex as a literal backspace for the second time this session. The file looks correct and the pattern silently never matches.

**The pattern that keeps paying:** every correctness bug in this entry was found by running `find_deals` and reading the top of the list, not by a test. A mispriced or mis-parsed listing is indistinguishable from a huge discount, so the ranking sorts the pipeline's worst failures to the top for free. That is now written into `CLAUDE.md` as a step to run after any extraction or valuation change.

**Still outstanding:**
- Auth. `POST /capture` writes to the database unauthenticated, which is fine local-only and must land before anything is exposed.
- Comps still admit listings whose `model_key` is NULL because the title omits the family ("Gigabyte 3060 Ti" with no "RTX"), so a 3060 Ti can comp a 3080 Ti. Requiring an exact model match would shrink comp sets sharply; worth measuring the trade rather than guessing.
- The browser extension has still never run against a live page.


## 2026-07-25 - Stage 4: valuation, and the deal ranking that audited the extractor

**Did:**
- Built `ml/valuation.py`: sold comps, a weighted-median estimate, an ordinal confidence, and a deal score. ADR `0014`. This is the first module that answers the question the project exists for.
- Stage 4 became buildable on a measurement that corrected an earlier reading. Counting `epid`-keyed comp groups suggested only 14 products had 2+ sales, which looked far too thin. But `epid` covers just 13% of sold listings, and the actual retrieval path finds far more: sampling 100 active listings, **69% find three or more sold comps and the median is five**.
- `0007`'s two confidence scores are finally used, exactly as that ADR specified: **gate membership on `sale_confidence`, weight influence by `price_confidence`.** A comp that probably never sold is excluded outright; one that sold at an unclear price is included and counts less.
- Refuses to answer below three comps. 42% of sampled listings get no estimate, which is the intended behaviour: a confident-looking number gets acted on, a missing one does not.
- Fixed the quota reserve that had silently halted sold-detection (previous entry), plus two config-invariant tests.
- 302 tests passing, mypy clean across 60 files.

**The measurement worth keeping:** the median deal score is **-10.8%**. The typical active listing sits *above* what comparable items sold for, which is exactly the selection effect you would predict, since things that already sold cleared the market and things still listed have not. It is a good sign the pipeline is measuring something real rather than noise.

**The deal ranking turned out to be an extractor audit, which was not the plan.** The first real scan returned ten "deals" and **every single one was an accessory the extractor had missed**:

```
+96.3%  *EMPTY BOX* RTX 5070
+94.2%  PNY RTX 5080 Original Retail Box ONLY & Packing
+94.1%  GIGABYTE RTX 4080 4090 GPU Cooling Fan
+93.7%  GIGABYTE RTX 3090 Cooler Heatsinks Fans + 2 Backplates
+93.3%  ZOTAC RTX 3080 Trinity - Shell ONLY
```

A mispriced accessory looks identical to a 95% discount, so **sorting by biggest discount surfaces your worst false negatives first**. That is a genuinely useful diagnostic and it is free: the ranking that is the product is also the test of everything feeding it.

**What that exposed, and the fix that mattered most:**
- The `is_accessory` gate required "for", "replacement" or "assembly". None of those five titles contain any of them. They simply say what they are.
- Loosening it overcorrected immediately, flagging a **$4,999 RTX 5090 "With EKWB Waterblock"** and a **$3,000 RTX 4090 "With Retail Box"** as parts. Same lesson for the third time this session: `"X only"` and `"X for Y"` are accessories, `"Y with X"` is a product including one. An inclusion-prefix guard fixed it, after a first attempt anchored the pattern to the end of the window and silently never matched.
- **The real answer was to stop writing vocabulary and use eBay's own taxonomy.** "Alphacool Eisblock Aurora Acryl GPX-N RTX 4090" is unrecognisable as a cooler from its title unless you happen to know Alphacool makes coolers, and eBay files it under **"Water Cooling"** without being asked. Matching on category tokens (cooling, parts, accessor, cases/covers, charger, attachment) took accessories from 305 to 489 and caught a class no regex was going to reach. `"Mixed Lots"` does the same for lots.
- Conversely, "retail box" and "original box" came *out* of the title vocabulary: a card advertised "with Original Box" is a real $4,750 card whose seller kept the packaging.

**Broke / debugged:**
- `find_deals` initially valued all 12,433 active listings at a k-NN query each, roughly ten minutes. Narrowed to listings sharing a `model_key` or `epid` with something already sold: 4,326 candidates, ~3.6 minutes for a full scan. It is a batch job, not an interactive call, and now says so.
- A heredoc wrote `\b` into a regex as a literal backspace (`\x08`). The file *looked* correct and the pattern silently never matched, which is what let the $4,999 card stay flagged through two attempted fixes. Worth remembering: when a regex mysteriously does not match, print its `.pattern` repr.

**Known gaps, measured not guessed:**
- **Multi-variant listings are the biggest remaining source of fake deals.** 563 listings say "All Colors", "choose", or list two capacities ("128GB 256GB"), and eBay shows the *lowest* variant price. An iPhone 14 at "$259.99" against a $650 estimate is that, not a bargain. This needs a "price is a from-price" concept and is the top item next.
- A few cooling accessories still slip through when a seller files them under Graphics/Video Cards.

## 2026-07-25 - Spec extraction, and the accessories that were pretending to be products

**Did:**
- Extended `ml/extract.py` with five more fields: `is_accessory`, `capacity_gb`, `spec_generation`, `form_factor`, `model_key`. Migration `0013`, wired through ingest, capture and comp selection. `docs/decisions/0013-spec-extraction.md`.
- **Comp-set price spread across all categories fell from 4.28x to 2.74x, with zero comp sets thinned below 3 candidates.** By category, on the ones that were broken:

| Category | Before | After |
|---|---|---|
| Graphics/Video Cards | 10.1x | **3.5x** |
| Memory (RAM) | 8.0x | **1.9x** |
| Solid State Drives | 4.2x | **1.9x** |

- **Found a comp poisoner worse than lots, and it was invisible until listings were grouped by something that should have made them comparable.** Grouping graphics cards by extracted chipset gave `rtx-3090` a price spread of **1428x**. The low end was entirely parts *for* the card: a $6.61 manual, a $34.99 backplate, a $50 empty box, an $88 heatsink assembly, a $187 NVLink bridge, a $199 case. They match on model string **and** on image (a photo of a GPU cooler looks like a GPU), so neither `epid` nor CLIP rejects them, and they sit at 2-20% of the real price. 193 listings (1.5%), median $108 against a corpus median of $400.
- Extended the defect vocabulary with "no display / no power / no boot / does not post", found on RTX 3090 listings sitting at half price inside an otherwise-clean model group.
- Fixed the quota reserve that had silently halted sold-detection (see the previous entry), and added two config-invariant tests, since a budget of zero is indistinguishable from "nothing to check".
- 302 tests passing (up from 250), mypy clean across 59 files.

**Decided:**
- Typed indexed columns, not a JSON `specs` blob. Every one of these is a comp filter, and filtering in SQL on JSON keys is both slower and easier to get subtly wrong. Matches the existing convention from `0006`: typed for what is queried, JSON for what is not.
- Capacity normalized to GB so 1TB and 1024GB compare, taking the largest value mentioned since titles list several ("64GB RAM 1TB SSD").
- Spec filters apply only when the *query* listing states a value, and unstated candidates are always kept. Requiring a match on a field that is silent 89% of the time would discard the corpus rather than sharpen it. The zero-thin-comp-sets result confirms the rule holds in practice.
- Accessories are excluded outright rather than down-weighted, for the same reason as lots: a heatsink is not a noisy measurement of a graphics card's value.

**Broke / debugged:**
- **"No GPU" means two opposite things.** On a graphics-card listing it means the box is empty. On a computer it means a working machine sold without a card, which is very much the product. An earlier version flagged a **$4,854 HP Z8 workstation**, a $1,800 gaming PC and a $999 Ryzen barebones build as accessories. Whole-machine words in the title fixed most of it, but the HP Z8's title contains no such word at all: `"HP Z8 G4 W10 GOLD 5120 14C 2.2GHZ 256GB 16TB SATA 512GB NVME NO GPU"`. Only its category ("PC Desktops & All-In-Ones") reveals what it is, so `extract_variant` now takes category as context.
- `"CASE for GIGABYTE AORUS RTX 3090"` was missed because "case" is not in the accessory vocabulary, and adding it bare would have false-positived on "console with carrying case". Resolved with a start-anchored rule: an accessory noun that is the *subject* of the title is an accessory; the same noun later is an inclusion.
- `"ASUS TUF RTX 3090 3-Fan Heatsink Cooler Assembly"` slipped through because it names no product it is *for*. "Assembly" and "replacement" beside an accessory noun are the same claim in different words.
- The `changed` counter in `extract_listings` reported 0 after a run that rewrote five new columns, because it compared only the original three fields. That is the one number telling you a rule change did anything.

**Tested how this behaves on categories the corpus does not contain**, since the whole vocabulary was written against PC parts, consoles and phones. The generic rules transfer correctly with no tuning: "Lot of 12 Vintage Vinyl Records" reads as a 12-item lot, "Fender Stratocaster - cracked neck for parts" as a defect, "Canon EOS R6 Camera Body Only" and "Dyson V11 - no battery, unit only" as bare units. Sneakers, jackets, chairs, LEGO and watches come back completely unflagged, which is the correct answer.

Three false positives turned up that only appear outside PC parts, and one of them mattered:

- **"lots of character" and "a lot of storage space" were read as two-item lots.** `is_lot` excludes a listing from comps entirely, so idiomatic English would have silently deleted clothing and furniture listings from every comp set. The word "lot" is now required not to be followed by a non-numeric "of".
- "Heels 2.5 inch" got a 2.5-inch **drive** form factor, and "Case Logic Laptop Backpack" got a laptop form factor from a brand name. Form factor is now gated on memory/storage context.
- Fixing that exposed a pre-existing miss in the opposite direction: `2.5" SATA SSD` never matched at all, because a word boundary between a quote mark and a space cannot match, so only the spelled-out "2.5 inch" worked.

17 regression tests now cover unfamiliar categories, since the requirement is that extraction degrades to "unstated" rather than producing confident wrong answers.

**Known limits:**
- DDR5 desktop memory keeps a 19.1x spread even fully segmented, most likely single sticks against multi-stick kits. That needs a quantity-*within*-listing concept distinct from `lot_size`, and is deferred.
- `model_key` is GPU-shaped. Extending it is more vocabulary, not a different design, and should be driven by a measured spread problem rather than added speculatively.

**Worth keeping:** the accessory class was invisible in aggregate, invisible to `epid`, invisible to CLIP, and became obvious the moment listings were grouped by a key that *should* have produced agreement and did not. Grouping by something that ought to make items comparable, then reading the disagreements, is a cheap and repeatable way to find this kind of problem.

## 2026-07-25 - Variant extraction, and pricing on catalog id instead of neighbours

**Did:**
- Built `ml/extract.py`: pull `lot_size`, `completeness` and `has_defect` out of listing titles, because nothing structured carries them. eBay's `localizedAspects` gives Brand, Model, Color, MPN and Storage Capacity and says nothing about what is in the box. Migration `0012`, plus `ml/extract_listings.py` to backfill. `docs/decisions/0012-variant-extraction.md`.
- These are comp **filters, not weights**. A lot of fifty and a for-parts unit are not noisy measurements of a working single item's value, they measure something else, so they leave the comp set rather than being discounted. Same reasoning `0007` used to split sale confidence from price confidence.
- **Then measured that filtering alone was not enough**, and found something better. Comp sets keyed on `epid` have a median price spread of **1.42x** against **4.83x** for raw CLIP neighbours, 3.4x tighter. So `ml/match.py` became a two-hop: identify the product with image hash or CLIP, then price it on the matched listing's catalog id. 24% of matches now price this way.
- Wired extraction into eBay ingest (insert and refresh) and into capture, so classification happens once at write time rather than per query.
- 248 tests passing (up from 210), mypy clean across 58 files.

**Measured, and the numbers drove every decision here:**

| Hazard | Evidence | Corpus share |
|---|---|---|
| Multi-item lots | "Lot of 50 SK Hynix 64GB" at **$113,000** in the same category as single sticks. Excluding 2% of the RAM category drops its mean 28% and its max from $113,000 to $21,936 | 0.7% |
| Defects | graphics cards flagged for-parts/cracked median **$151.08** vs **$420.00** clean | 4.3% |
| Bundling | consoles: bare **$129.99**, unstated **$174.99**, with-extras **$190.00** | 5.7% stated |

**Decided:**
- Rule-based extraction, not a model. The vocabulary is small and stable, the signals are sparse, there are no labels, and a regex that fires on "for parts" is auditable in a way a decision boundary is not.
- Never divide a lot's price by its size to recover a unit price. Bulk pricing is not linear, and the units are often not identical ("LOT OF 54 SAMSUNG HYNIX MICRON" is three manufacturers). A derived number with unknown error is worse than an excluded row.
- `completeness=None` means **unstated** (89% of titles), never "complete". Reading silence as a full bundle is exactly the error that puts bare units in the wrong comp set.
- Completeness filters comps only when the *query* listing states its own, since requiring a match on a field that is usually silent would discard most of the corpus.
- When pricing from `epid`, the displayed candidates become the epid peers rather than the neighbours that found them. Otherwise the payload shows three rows beside a median computed from twelve, and a reader cannot reconcile the two.

**Broke / debugged:**
- **The lot regex was catastrophically wrong for this corpus and only running it over all 12,678 titles revealed it.** Accepting "Nx"/"xN" quantity forms classified **1,789 listings (14.1%) as lots**, essentially all false: `"RX 6700 XT"` → "6700 x", `"Gen 4.0 x 4"` → PCIe lanes, `"Ryzen 5 7600X"` → a CPU model, `"VENTUS 3X PLUS"` → a product line, `"PCIe 4.0 x16"` → lane count. In PC hardware an x beside a number is almost never a count. Dropped those forms entirely; lots fell to 0.7% and every remaining match is genuine. A real "2x RTX 3090" is lost, which is the right trade: a false lot silently deletes a valid comp.
- `"Gaming PC works with 4K monitors"` classified as a bundle, because the accessory lookahead accepted any digit after "with". Replaced with a strict accessory vocabulary.
- My first attempt to measure the filter's benefit showed **zero improvement across 30 samples**, which was the measurement being wrong rather than the filter. With lots at 0.7%, a random 10-neighbour set rarely contains one. Re-measured properly: 36% of comp sets change, and when they do the median comp value moves 7.7%, up to 197%.
- A test caught a real design flaw rather than a test bug: `candidates` held the k-NN neighbours while `price_context` was computed from the epid peers, so the API returned one set of listings beside a median from another.

**Known limits, recorded rather than papered over:**
- Extraction is high-precision and low-recall by design. 89% of listings stay unstated, so this improves the comp sets it touches and leaves the majority alone.
- Residual spread inside filtered comp sets is still **4.3x** median, and much worse in commodity categories: **RAM 15.0x**, GPUs 8.0x. Every DDR4 stick looks identical to CLIP whether it is 16GB or 64GB, so visual similarity is close to worthless there and the discriminating attribute (capacity, speed, generation) sits in the title and in `aspects`. That is the next extraction target.
- "READ" in a title (305 listings) is a strong seller-side signal that something is wrong but says nothing about what, so it is captured and deliberately not acted on. Negations like "no charger" (425 listings) are captured but do not yet downgrade completeness, because negation scope in title-case fragments is a harder problem than the rest of this.

**Fixed a stale quota reserve that had silently halted sold-detection.**
- `quota_reserve` was 2,000, sized when ingest ran hourly and needed 1,536 calls/day. Ingest is 2-hourly now and needs 768, so the reserve was 2.6x a full day of it. `resolve_budget` returns `min(budget, remaining - reserve)`, so once remaining hit exactly 2,000 the checker's budget became **0**.
- Observed live: remaining sat at 2,000 with 15.2 hours until reset, the disappearance check doing nothing, and ~1,550 calls due to expire unused. Nothing logged a problem, because a budget of zero is indistinguishable from "nothing to check".
- The reserve also capped total check capacity at 5,000 - 2,000 - 768 = **2,232/day**, below the 2,800 the settings comment's own arithmetic assumes. The configuration contradicted its own documentation.
- Lowered to 1,000: covers a full day of ingest with 30% headroom, leaves 3,232/day for checking. Two tests now enforce both bounds (above a day of ingest, below what the planned check volume needs), since the failure mode is silent.
- Restarting the worker and scheduler was required for this to take effect: `settings` is a module-level singleton read at import, so running processes held the old value. Same class of trap as the stale-code worker found yesterday.

**Measured what stage 3b actually needs next**, rather than assuming "extract brand and model". Brand and model are largely handled already by `epid` and `localizedAspects`. What is missing is **spec**, and the evidence points at exactly which attributes:

- Capacity lives in the title, not in aspects, precisely where it matters most: RAM has it in 97% of titles and 12% of aspects, graphics cards 79% of titles and **0.3%** of aspects. Phones, which do not need it, have 99% aspect coverage.
- Capacity alone separates RAM medians cleanly ($59.50 / $75 / $181 / $800 / $1,975 for 8/16/32/64/128GB) but leaves 30-80x spread *within* each capacity.
- Adding generation and form factor collapses it. 32GB RAM went from **82.7x** overall to 2.8x (DDR4 laptop), 4.4x (DDR4 desktop), 2.7x (DDR4 server), 3.8x (DDR5 laptop), 3.7x (DDR5 server). Only DDR5 desktop stays high at 19.1x, likely kits versus single sticks.

So the remaining 3b work is a three-attribute extraction (capacity, generation, form factor), all regex-able from titles, and it is worth roughly a 20x tightening in the worst category.

**Next:**
- Spec extraction: capacity, generation, form factor. Evidence above.
- Keep accumulating sold history. Stage 4 still wants more comps per product: 85 distinct products, only 14 with 2+ sold.

## 2026-07-25 - The sold-detection signal was wrong, and Depop went push-based

**Did:**
- **Fixed the bug that made the entire project's core mechanism unable to work.** Sold-price history is built by re-checking listings and detecting when they leave the market. The detection was `get_item()` returning `None`, i.e. HTTP 404. **eBay does not 404 ended listings.** Probed eight listings that had dropped out of search coverage: zero returned 404, four were plainly gone (`OUT_OF_STOCK` and/or a past `itemEndDate`) and returned HTTP 200 with a full body. The sold branch was unreachable and had been since stage 2, which is why the database held 12,420 listings and zero `likely_sold` rows.
- Replaced it with `normalizer.listing_has_ended`, which reads the body: `estimatedAvailabilityStatus == OUT_OF_STOCK`, `estimatedAvailableQuantity == 0`, or an `itemEndDate` at or before now. A 404 stays as a third signal. Deliberately conservative, so an unrecognised shape leaves a listing active. `docs/decisions/0011-ebay-does-not-404-ended-listings.md`.
- **Result: 241 listings marked `likely_sold`, 43 of them caught as relists rather than sales.** Sale confidence spans 0.045 to 0.900, the low end being the relists. First comp data the project has ever produced.
- Moved item-body enrichment ahead of the branch so it runs unconditionally. An *ending* listing's body carries the real `itemEndDate` and `estimatedSoldQuantity`, and it was being discarded for exactly the listings that mattered most.
- **Fixed a second endpoint-confusion bug in the same area.** `is_gtc` was derived at ingest from an absent `itemEndDate`. That inference is valid for a `getItem` body and invalid for a search response, which never carries the field at all: every non-auction listing was marked GTC, producing a measured 98.9% that described the endpoint rather than the market. `is_gtc` is now `bool | None`, set only from a real item body, and migration `0011` nulls the old values because a wrong value is worse than a missing one.
- **Depop moved from pull-based to push-based.** ADR 0001 put it on the pull side on the premise that its unofficial JSON endpoints were unofficial-but-unenforced. Measured: every Depop host now returns **403 to server-side requests, including `robots.txt`**, behind Cloudflare Bot Management (`Server: cloudflare`, `__cf_bm`), while a control request to `example.com` returned 200. Depop's only official API is a partner-gated Selling API for managing your own inventory, not marketplace data. `docs/decisions/0010-depop-is-push-based-now.md`.
- Built the push path instead: `connectors/capture.py` (validation, normalization, upsert), `POST /capture` and `GET /capture/{id}/match`, and a Manifest V3 browser extension in `extension/` with per-site parsers for Depop and Facebook Marketplace. One capture path serves both, since it is the same problem twice.
- Built `ml/match.py`, the cross-source bridge: `image_hash` exact match first (a reused stock photo is the same product, provably, for one indexed lookup), then CLIP k-NN against the eBay index. Returns candidates with a `PriceContext`.
- Ran a full ingest to backfill the post-`0005` fields on the stale-code corpus: 64 searches in 326s, 1,924 new, 9,346 updated, 0 failed. The update-path fix from earlier today worked.
- Started the pipeline: one `deal-finder` worker, one `deal-finder-ml` worker, one scheduler, plus the API. Verifying this turned out to need care: process listings are misleading because `uv run` wrappers show several processes per logical worker, and raw heartbeat age is misleading in the other direction because an idle RQ worker only refreshes its heartbeat each dequeue cycle, so 3-minute gaps are normal. A naive 120s staleness check reported two healthy workers as dead. The authoritative signal is the TTL on the worker's Redis key, which RQ expires at `worker_ttl`; both showed positive TTLs, one `busy` and one `idle` with completed jobs.
- 208 tests passing (up from 175), mypy clean across 54 files.

**Measured, finally:**

| Metric | Value | Why it matters |
|---|---|---|
| `epid` coverage | 46.0% | The number CLAUDE.md gated stage 3 on. Moderate, not high |
| Shipping known | 73.2% | A quarter of listings still have no delivered cost |
| Auctions | 1.1% | The auction-specific scoring is near dead code here |
| Best Offer | 42.6% | Price confidence discounting matters a lot |
| GTC | remeasuring | Old 98.9% was an artefact; real figure accruing per check |

**`epid` coverage by category is the striking part**, because it is the exact inverse of nothing and the exact match of something:

```
Cell Phones & Smartphones   90.2%      PC Desktops & All-In-Ones    3.9%
Memory (RAM)                60.2%      PC Laptops & Netbooks       10.6%
Solid State Drives          53.1%      Video Game Consoles         37.3%
CPUs/Processors             51.9%      Graphics/Video Cards        38.1%
```

Prebuilt PCs are 3.9% `epid` **and** were the category where CLIP retrieval failed worst ($578 to $3,000 spread). **The two identity mechanisms fail on the same items**, so they do not cover for each other. A custom-built PC is a one-off configuration: no catalog entry, and an anonymous black-box photo. It is intrinsically unidentifiable by catalog id or by image, which leaves text as the only route. That is a much stronger argument for stage 3b than "extraction is also useful", and it says where to point it.

**Decided:**
- End-detection reads the response body, never the status code. A 404 is one signal among three, not the signal.
- `listing_has_ended` is conservative by design: an unknown shape means "not ended". A false negative costs one re-check; a false positive writes a fabricated comp into the dataset permanently.
- Two-strike confirmation is kept and matters more than before, since `OUT_OF_STOCK` can be transient for a multi-quantity seller in a way a 404 never was.
- No Depop scraping, and no commercial Depop scraper API either. Getting past Cloudflare Bot Management means fingerprint-level evasion, and this project already refuses to scrape eBay's sold pages on the strength of a `robots.txt` rule alone. Depop is now the stronger case against, not the weaker one, and a paid scraper API relocates the evasion without changing it.
- `ml/match.py` stops at identification and deliberately does not compute a deal score. Candidate prices are *asking* prices; a "% below market" figure would imply precision the data does not have. That is stage 4's job once sold history accumulates.
- The API response carries the caveat in words, not just numbers, and a wide candidate spread (>3x) adds an explicit warning, because the measured signature of a bad match is exactly a wide spread.
- CORS is a fixed origin list, not `*`. The capture endpoint is unauthenticated while the stack is local-only, and a wildcard would let any page the user visits write to their database.

**Broke / debugged:**
- Ten tests failed after the `is_gtc` fix, all of them asserting the old wrong behaviour. Worse, **the fixture was complicit**: `tests/fixtures/ebay_item_summary.json` was hand-built and included an `itemEndDate` that real search responses never carry, so every test agreed with the broken inference. Removed the field from the fixture and rewrote the tests against measured reality.
- `_refresh_from_summary` was recomputing `is_gtc` from search data, reintroducing the same bad inference on every ingest. It now leaves `item_end_date` and `is_gtc` alone entirely.

**Uncomfortable, and worth recording:** a mechanism was elaborated across four ADRs, 180 tests and several weeks while its foundational assumption went unchecked against a single real response. The probe that found it cost one API call. Verifying the *signal* before building machinery on top of it is cheap, and nothing in the previous design forced it.

**Next:**
- Let it run. Sold-price history is the long pole and only accrues with wall-clock time.
- Cross-source verification with a real phone photo is still outstanding.
- Stage 3b, now well-aimed: text extraction is the only identity route for the categories where both `epid` and CLIP fail.

## 2026-07-25 - Stage 3a: CLIP embeddings into pgvector

**Did:**
- Built the embedding pipeline: `ml/embeddings.py` (pure, image in and vector out), `ml/embed_listings.py` (owns the DB), `ml/similar.py` (the k-NN read side). CLIP ViT-B/32 from LAION via `open_clip_torch`, 512 dimensions, stored in a nullable `vector(512)` column. Migration `0010`, hand-written, since autogenerate never emits `CREATE EXTENSION` and does not know the `vector` type. `docs/decisions/0009-clip-embeddings-pgvector.md` records the reasoning.
- The purpose is narrower than "a similarity feature": under ADR 0008 the eBay corpus is a **reference index**, and listings found on other sources are **queries against it**. `epid` is exact and free but is an eBay catalog id, so a foreign listing never carries one. Embeddings are the only available bridge, which is what justifies this stage at all.
- Backfill and go-forward are one code path. `embed_pending()` selects `WHERE embedded_at IS NULL`, which is simultaneously the existing corpus and every row ingest lands from now on, so there is no separate one-off script to drift out of sync.
- `embedded_at` is stamped on **every attempt**, success or failure. Keying the queue on `embedding IS NULL` instead would hand back the imageless listings and every dead image URL on every run, forever.
- Added a second RQ queue, `deal-finder-ml`. The reason is capability, not throughput: RQ hands a worker whatever job is next, so on a shared queue the deliberately torch-free ingest worker would eventually be handed an embed job and die on `import torch`. Scheduler gained a third branch on `embed_interval_seconds`.
- torch and `open_clip` are imported **inside functions**, never at module scope, so `systems/queue.py` can reference the job by name without dragging 3 GB of CUDA libraries into the scheduler, the API process and every test run. A test asserts the whole `ml` package imports with torch absent from `sys.modules`.
- Images are fetched at eBay's `s-l500` CDN variant rather than the stored `s-l225`, by URL substitution. Costs no quota, since the CDN is not the Browse API. Non-eBay URLs pass through the substitution untouched.
- **Fixed the ingest update path**, which was refreshing six fields while the model had grown to thirty-six. Anything added after stage 2 was captured on insert and never again, so a listing already in the table could never acquire a column added later. Extracted `_refresh_from_summary` so the field list lives in one place instead of being duplicated across the insert and update branches.
- Rewrote `GET /listings`, which had no pagination and already serialized every column of all 10,496 rows. Added `ListingRead` (excludes `embedding` and `sale_signals`, exposes `total_cost` alongside `price`), plus `limit`/`offset` and `source`/`status` filters.
- Moved the venv out of the OneDrive-synced folder to `C:\venvs\deal-finder` via `UV_PROJECT_ENVIRONMENT`, before installing torch. OneDrive does not read `.gitignore`, and the venv was about to go from 294 MB to ~3 GB. Repo dropped from ~389 MB to 95 MB on disk. The repo itself did not move, so git is unaffected.
- 175 tests passing (up from 136), mypy clean across 48 files.

**Decided:**
- The embedding column is **nullable**, not NOT NULL with a zero-vector default. An all-zeros sentinel sits equidistant from every other vector, forming a fake cluster that turns up in every k-NN result. "Not embedded yet" has to be representable.
- **No ANN index yet.** Measured over the full 10,484-vector corpus: **29 ms in Postgres, ~68 ms end to end** through `find_similar_to_listing` (the gap is hydrating ten `Listing` objects, not the search), at 100% recall. The ADR originally asserted "single-digit milliseconds" from estimate rather than measurement, which was wrong by roughly an order of magnitude; corrected in place once there was real data. The conclusion survives, and it is now a number rather than a guess. Stage 4's queries are also *filtered* k-NN (source, sale confidence, category), which HNSW handles badly since it walks the graph first and filters after. Revisit near 100k rows or a query measured above ~100 ms.
- L2-normalize at write time, cosine at read. Cosine is self-defending if a row lands unnormalized; inner product would silently rank by magnitude. And `1 - (a <=> b)` lands in `[0,1]`, which the stage 4 match-confidence score needs.
- `CLIP_MODEL` and `EMBEDDING_DIM` are code constants, not settings. They must agree with the column the migration created, and a `.env` key that can silently disagree with the schema is a footgun.
- The migration hardcodes `512` rather than importing `EMBEDDING_DIM`. A migration describes the schema at its revision; importing a live constant would let a later checkpoint swap silently rewrite history.
- Embedding is **not** inline in ingestion, which is the opposite call from `image_hash` in ADR 0002. Hashing is cheap CPU work with no heavy dependency; embedding is neither. Inline would let a CUDA fault take down data ingestion, put torch in the ingest worker, and force batch size 1.
- `find_similar_*` restricts to eBay by default. Only eBay is a comp source, so matching a foreign photo against other foreign listings would price against a source with no usable history.
- Matches carry a similarity score rather than being reduced to a boolean, because stage 4 has to surface a cross-source match as a best guess with its evidence, and it cannot do that if this layer discards the distances.

**Broke / debugged:**
- **A leaked Session in `ml/similar.py` hung the test suite past 120 seconds.** `Session(db_engine).exec(...)` without a context manager never closes, so every query left an idle transaction open in Postgres. Five exhausted the default connection pool and the sixth blocked forever, with a concurrent `DELETE` waiting behind them. Context-managed; the same tests now run in 0.24s. This would have hung the scheduler in production, not just tests.
- The SQLite test fixture created its schema on one connection and the FastAPI `TestClient` queried another, since an in-memory SQLite database belongs to its connection and the default pool hands out one per thread. Every route test failed with "no such table: listing". Fixed with `StaticPool`.
- Making `embed_image_urls` take an injectable `embedder` (matching the project's `client=` / `image_hasher=` convention) broke two tests that had been monkeypatching the module attribute, because the default argument was bound at definition time. They were downloading and running the real 600 MB checkpoint without anyone noticing. Rewritten to inject.
- A `# type: ignore` on the pgvector distance call was flagged unused inside a test, since mypy does not check untyped function bodies by default. Kept only the one in `ml/similar.py`, which is genuinely needed: pgvector attaches `cosine_distance` via a comparator factory that is invisible through SQLModel's `Mapped[...]` wrapper.

**Verified, with the actual output:**

Full backfill across the live corpus: **10,496 attempted, 10,484 embedded, 12 failed.** All twelve failures are listings with zero images, and **not one was a dead image URL**, so the `fetch_image` failure path and the `embedded_at`-on-failure stamping were both exercised by exactly the cases they exist for. That matches the 10,484 predicted before the run. Roughly 850 MB of images at the `s-l500` variant, no eBay quota spent, since the CDN is not the Browse API.


Querying the index with a prebuilt gaming PC listing (Core i9-12900K, RTX 3090, $1999.99) returns its ten nearest neighbours:

```
0.877  $1099.99  PowerSpec Custom PC Core i9-12900K 64GB RAM 1TB SSD XFX Radeon RX 6700
0.869  $1149.99  PowerSpec Custom PC Ryzen 5 5600X3D 32GB RAM 1TB SSD GeForce R...
0.858  $1400.00  Custom/Whitebox Ryzen 9 7900X RTX 3070 32GB 2TB HDD+SSD RGB WiFi Win11
0.843  $1999.99  Gaming PC Ryzen 7 7800X3D 32GB RAM 1TB SSD RTX 4070 Ti 12GB Windows 11
0.840  $1550.00  Gaming Pc i7 13700K Radeon RX 7900XT 20GB
0.839  $ 999.99  High end Gaming PC - Ryzen 5 7600X, Trident32GB DDR5-6400, 1TB NVMe
0.838  $1800.00  Custom Gaming PC - AMD 7900xt GPU & AMD RYZEN 5800x3d CPU
0.836  $1200.00  Gaming PC, 3060, Ryzen 5 7600x3d CPU, DDR5 32GB, ASUS TUF Gaming Mobo
0.834  $1299.99  Asus PC Core i7-14700K 32GB RAM 1TB SSD ASUS GeForce RTX 4070
0.834  $ 875.00  CyberPowerPC Gaming PC AMD Ryzen 5 5600X 16GB 512GB SSD + 1TB HDD RTX...
```

Every neighbour is genuinely a prebuilt gaming PC, so category retrieval works. But not one has the same GPU as the query, and the spread is $875 to $1999.99 for something asking $1999.99. Averaging that set would produce a confident, meaningless number. The cause is not a weak checkpoint and a bigger one would not help: **the GPU is not visible in the photo.** These are all black towers with RGB lighting, and CLIP is correctly reporting that they look alike. The component setting nearly all of the price appears only in the title.

**Retrieval quality turns out to be strongly category-dependent, which a single spot-check would have hidden.** Probing three categories:

| Category | Neighbour spread | Similarity | Usable as comps? |
|---|---|---|---|
| Prebuilt gaming PC | $578 to $3,000 | 0.84 to 0.87 | No |
| iPhone | $299 to $785 | 0.75 to 0.77 | With model extraction |
| Nintendo Switch | $136 to $242 | 0.85 to 0.88 | Nearly directly |

The Switch query returned nothing but Switch consoles, tightly priced. The iPhone query returned nothing but iPhones and surfaced the correct model (iPhone Air) in its top three. The predictor is whether the price-determining attribute is *visible*: a console is the product, iPhone generations differ subtly but consistently, and a PC case reveals nothing about what is inside it.

**The warning for stage 4, which is the opposite of the intuitive reading:** the useless PC neighbours scored **higher** similarity (0.87) than the useful iPhone ones (0.77). Cosine similarity measures how alike the images are, and identical-looking boxes score well precisely when they are least informative. **Similarity is therefore not a proxy for comp validity and must never be used as the confidence weight on a comp.** It is a candidate-generation score. Something else, an extracted model string, has to decide whether a candidate is actually the same product.

That is the concrete argument for stage 3b, and it also says where to aim it: extraction matters most in exactly the categories where the photo carries least, so the two components are complementary rather than redundant.

**A second probe, not in the plan, that changes what stage 3b could be.** CLIP places text and images in the *same* vector space, so the index built here can be queried with a **title** rather than a photo. Tried with deliberately scrappy strings, the way a Depop listing actually reads:

```
"nintendo switch console"            0.346  ->  4/4 Nintendo Switch consoles
"gaming pc with rtx graphics card"   0.354  ->  4/4 gaming PCs with RTX cards
"airpods pro"                        0.277  ->  iPhones (corpus holds 1 AirPods listing)
"vintage leather jacket"             0.193  ->  iPhones (corpus holds 0 jackets)
```

The last two are not retrieval failures. The saved searches only cover Switches, iPhones and PC components, so there is genuinely nothing to return, and **the score says so**: 0.34-0.35 when the item exists, 0.19-0.28 when it does not.

That contrast matters more than the retrieval itself. Image-to-image similarity was shown above to be *anti*-correlated with comp quality, so it cannot be thresholded. Text-to-image similarity appears to behave the opposite way, carrying real signal about whether the corpus contains the queried item at all. Note the two run on different scales (0.19-0.35 for text-to-image against 0.75-0.88 for image-to-image), so they must never be compared to each other or share a threshold.

The practical consequence for stage 3b: a foreign listing's title can query the eBay index directly, with no photo and no regex, giving a second independent matching route to cross-check against the image one. Worth designing against, though it needs validating on a corpus broad enough that "nothing relevant exists" is not the common case. Not implemented here, since stage 3a was scoped to image embeddings; `embed_text` would be about ten lines on top of what already exists.

**Found (not fixed here):**
- **The live corpus was written by stale code.** All 10,496 rows have every stage-1 field populated and everything from migration `0005` onward empty: `epid` 0/10,496, `shipping_cost` 0, and `is_gtc` False on every row where current code would set it True. RQ's failed-job registry shows a pre-stage-2.5 worker was resident and running: hourly ingest when the configured interval is 2h, no per-search isolation, unbudgeted `getItem` calls. It burned the full 5,000-call day and stopped around 2026-07-25T23:34Z. The update-path fix above is what lets these rows recover; they backfill on the next ingest after the quota resets.
- `.env.example` had drifted from `settings.py` (`INGEST_INTERVAL_SECONDS=3600` against a 7200 default, `PROVEN_ALIVE_SECONDS=7200` against 10800). Corrected, and the 1.5x margin rule is now stated in the file rather than only in a test.
- `ruff check` reports 80 violations, 70 of which predate this work. They are almost entirely `UP007`/`UP017` style rules that a newer ruff applies to code written in the older idiom, plus two `B008` that FastAPI's `Depends`/`Query` defaults require. New code follows the existing house style rather than diverging from it. Worth one deliberate sweep at some point, as its own change.

**Next:**
- Quota resets 2026-07-25T07:00Z. Then: one ingest to backfill the post-`0005` fields, and the deferred measurements (`epid` coverage, GTC share, auction share, and whether the live `shippingOptions`/`buyingOptions`/`localizedAspects` shapes match the fixture, which has never been checked against a real response).
- Before restarting anything, confirm no stale worker or scheduler survives and start exactly one of each from the current tree.
- Cross-source verification is the one that matters: embed a real phone photo and query the eBay index. eBay-to-eBay similarity proves much less, since it dodges the domain shift between clean CDN product shots and a real-world photo.

## 2026-07-18 - eBay is the price oracle, everything else is a client

**Did:**
- Reworked the source model. **eBay is the price oracle** (ingested, disappearance-tracked, builds sold history, answers "what is this worth"), and **Depop and Facebook Marketplace are valuation clients** whose listings get scored *against* eBay value and contribute zero comps. Item variety on those sites is too high for per-item price history built from them to mean anything, and mixing them in would contaminate the one source that does have the volume and catalog structure to support it.
- That makes `docs/decisions/0001-multi-source-connector-strategy.md` wrong rather than imprecise: it says outright that Depop "gets checked periodically by `disappearance_check.py` ... to build sold-price history", and `CLAUDE.md` and `PROJECT_PLAN.md` repeated it. Wrote `docs/decisions/0008-price-oracle-and-valuation-clients.md` superseding that half, and left 0001 unedited with a pointer at the top so the reasoning at the time stays readable.
- The underlying mistake in 0001 was conflating two independent properties: *how a source is accessed* (pull vs push) and *whether its prices are trustworthy enough to price against*. Split them in code before Depop exists to get it wrong: `PULL_BASED_SOURCES` still means "polled on a schedule", and a new `COMP_SOURCES` frozenset gates disappearance checking. `check_all_sources` intersects them. A test asserts a polled non-comp source costs **zero** API calls and isn't even marked as checked, and another guards that `COMP_SOURCES` is still just eBay, since adding to it is an ADR-level decision.
- **The hard problem of the project moved.** It is no longer "build comps from several sources", it is **cross-source item identification**: given a Facebook photo and a scrappy title, which eBay product is this? Nothing in the codebase addresses that yet.
- That also settles the question that had stage 3 stuck. A Facebook or Depop listing has no `epid`, because that is an eBay catalog id. So `epid` is excellent for eBay-internal comps and **useless for the bridge**, which means image embeddings and text extraction are the only way to value a foreign listing. Stage 3 isn't weakened by the `epid` discovery; it is the mechanism the core use case depends on.
- **Closed the delivered-cost gap.** Shipping was being captured carefully and used nowhere: there was no `price + shipping` anywhere in the codebase, and `Listing.price` (item price only) is the obvious field for stage 4 to reach for, which is exactly the correctness bug ADR 0004 flagged. Added a `Listing.total_cost` property.
- Also found `shippingCostType` being ignored. eBay marks each option `FIXED` or `CALCULATED`, and a calculated cost depends on the buyer's location, so the figure returned may have been worked out for somewhere else. `_cheapest_shipping_cost` was reading the value without checking the type. It now prefers a `FIXED` option even when a `CALCULATED` one is cheaper, and flags `shipping_estimated` when only calculated options exist. Migration `0009`.
- 136 tests passing (up from 126), mypy clean across 18 files.

**Decided:**
- `total_cost` returns `None` when shipping is unknown, deliberately rather than falling back to `price`. Silently treating unknown shipping as free is the same bug relocated, and it biases in the dangerous direction by making items look cheaper than they are. Callers must decide; `price_confidence` already records the doubt.
- A `FIXED` option beats a cheaper `CALCULATED` one. A firm price that is slightly higher is better information than a cheaper guess made for an unknown location.
- An absent or unrecognised `shippingCostType` is treated as firm, not estimated. eBay uses `FIXED` for the common case, so that is the better default than assuming the worst.
- Estimated shipping costs less price confidence than unknown shipping (x0.95 versus x0.9). A calculated figure is at least the right order of magnitude.
- `COMP_SOURCES` is a frozenset in one place rather than `WHERE source = 'ebay'` scattered through stage 4. The rule is a project-level decision and deserves one place to read it and one to change it.

**Broke / debugged:**
- `test_check_all_sources_loops_every_registered_source` failed immediately, which was the new behaviour working: it adds a fake `depop` to `PULL_BASED_SOURCES` and that source is now correctly excluded. Split it into two tests, one for the loop shape (patching both sets) and one for the exclusion itself.

**Next:**
- Stage 3a (CLIP embeddings), now with a clear purpose: **the eBay corpus becomes the reference index that items found elsewhere are matched against.** That asymmetry matters. eBay listings are the index and foreign listings are the queries, so eBay embedding coverage is worth more, and the two sides have different image distributions (clean CDN product shots versus a phone photo in a garage). The verification that matters is cross-source, not eBay-to-eBay.
- `image_hash` also finally has its real purpose. ADR 0002 deferred cross-source matching as duplicate-merging; the actual use is identification, since a foreign seller reusing a stock photo can be matched instantly before any model runs.
- Quota still resets 07:00Z. Unchanged: measure `epid` coverage, GTC share, auction share.

## 2026-07-18 - Splitting sale confidence from price confidence

**Did:**
- Audited the confidence multipliers in `sale_confidence.py` by printing real scores across scenarios. Two problems fell out, one a bug and one structural.
- **The bug:** an auction with 12 bids that ended at its scheduled end date scored **0.324**, below a listing about which nothing was known. Cause: the "ran to its published end date, so nobody bought it" penalty was being applied to auctions, and **auctions end at their scheduled date by definition**, sold or not. Every auction was being penalised for behaving like an auction. Fixed by restricting that signal to non-auction listings. Same case now scores 0.810.
- **The structural problem the bug was hiding:** the score was answering **two different questions at once**. "Did this sell?" (relists, bid counts, running to term) and "is the recorded price what was paid?" (Best Offer discounts, mid-auction snapshots) are different uncertainties with different consequences, and multiplying them into one number makes them indistinguishable. A relisted item that probably never sold and a Best Offer sale that definitely happened at an unclear price both landed near 0.6-0.7.
- Split them, per `docs/decisions/0007-two-confidences.md`. `sale_confidence` now answers only whether a sale happened; `price_confidence` answers only whether `price + shipping` is what the buyer handed over. Migration `0008`. The difference is stark: a Best Offer sale is now `sale 0.900 / price 0.750` while a relist is `sale 0.135 / price 1.000`. Both were around 0.135 to 0.675 before, on the same axis.
- `price_confidence` starts at **1.0**, not at a hedge, because for a plain fixed-price listing with no offers the asking price simply *is* the price. Only specific known ambiguities reduce it. `sale_confidence` keeps its 0.75 base, because a sale is never actually observed.
- Recorded the **direction** of price error, since it isn't symmetric: a Best Offer asking price is biased **over** (it sold at some discount), while an auction's last observed bid is biased **under** (bidding only goes up, so the hammer price is at least that). Stored as `price_bias` in signals. That leaves stage 4 the option of *correcting* a comp rather than merely discounting it, which is deliberately not attempted yet.
- Added `shipping_unknown` as a mild price-confidence penalty, since what a buyer paid is price plus shipping and one of those can be null.
- 126 tests passing, mypy clean, migrations through `0008` applied.

**Decided:**
- Two stored scores, and deliberately **no combined score alongside them**. A combined field would be the easiest thing to reach for and the one that reintroduces exactly the conflation this fixes. Stage 4 should be made to think about which question it's asking.
- `sale_confidence` gates whether a listing enters the comp set; `price_confidence` weights how much its price moves the estimate. Written into the ADR so stage 4 doesn't have to reinvent the intent.
- Migration blanks any existing `sale_confidence`, since its meaning narrowed. Nothing was actually lost (no sale has ever been confirmed in production), but leaving values whose definition silently changed would be worse than having none.
- Keeping the multipliers as they are. The split doesn't improve their calibration and doesn't pretend to; what it does is make them **separately falsifiable**, so the relist weight can be corrected later without disturbing the Best Offer weight.

**Broke / debugged:**
- Renaming `SaleConfidence.score` to two fields broke 15 tests at once. Mechanical, but a reminder that a widely-asserted-on attribute is an interface.
- Known limitation, left in deliberately: an auction with bids that vanished fast reaches `sale_confidence` 1.000 by clamping (0.75 x 1.5 x 1.2 = 1.35, clipped). The result is defensible, since bids on an ended auction really are close to proof of sale, but the clamp is doing work the multipliers should be doing.

**Next:**
- Unchanged: quota resets 07:00Z, then measure epid coverage, GTC share, and auction share.
- Auction share matters more now. If auctions are a meaningful slice, they're the only comps in the system backed by a price somebody actually paid, and auction close polling moves up the list.

## 2026-07-18 - Reading the rest of the response: epid, real end dates, and the getItem body whose quota we already spent

**Did:**
- Audited the Browse API's documented response schema against what the normalizer actually reads (couldn't verify live, quota at zero until 07:00Z). Found four fields worth having, none of which cost a single extra API call.
- **`epid`, eBay's catalog product id.** Two listings sharing one are *definitively* the same product. That is exactly what stage 3a's CLIP embeddings and stage 3b's brand/model regex are both built to approximate, and eBay has been handing it over in every search response this whole time. Captured and indexed. Nothing consumes it yet on purpose: the first thing worth doing once real data lands is measuring coverage, because if it's high then exact catalog matching is a better comp key than either embeddings or extraction, and **stage 3 becomes much less load-bearing than the build order assumes.**
- **`itemEndDate`, which fixes a heuristic I guessed at yesterday.** `sale_confidence.py` was inferring "ran to term" from a 30-day guess with a tolerance window. eBay publishes the actual scheduled end. Better still, **eBay omits the field for Good 'Til Cancelled listings**, so its absence identifies GTC, which is exactly the auto-renewing listing type that manufactures the false sales relist detection exists to catch. Recorded that reading as `is_gtc` so the meaning of a null isn't rediscovered every time someone reads the table. The 30-day guess stays as a fallback for GTC listings and for rows ingested before this change.
- **`bidCount`, which is close to decisive for auctions.** An auction that disappears having never received a bid did not sell; one that disappears with bids did, and somebody actually paid roughly that. That is the strongest comp data available anywhere in this system, and it needs no inference at all. Scores 0.05 with no bids, 1.5x with bids.
- **The `getItem` response body.** `check_listings_for_source` has been calling `getItem` on every candidate and reading only whether it 404s, throwing away `localizedAspects` (structured Brand / Model / Storage Capacity name-value pairs) and `estimatedAvailabilities.estimatedSoldQuantity` (real units sold, not inferred). Now harvested via `enrich_from_item_body()`. This was the single most wasteful thing in the codebase: the call was already made and its quota already spent.
- Also captured seller feedback score and percentage, and `qualifiedPrograms` (Authenticity Guarantee and similar move price materially in some categories).
- Wrote `docs/decisions/0006-capture-what-ebay-already-sends.md` first. Migration `0007`. 25 new tests, 121 passing, mypy clean across 18 files.

**Decided:**
- `epid` gets captured and indexed but nothing reads it yet. Measuring coverage has to come before designing around it, and building a catalog-matching path against an assumed coverage rate would be exactly the kind of guessing this project keeps avoiding.
- `aspects` and `qualified_programs` stay raw JSON, unindexed. Aspect names vary by category ("Model" here, "Chipset/GPU Model" there) and coverage is unknown, so stage 3b should decide what earns a typed column against real data.
- `enrich_from_item_body` is strictly additive: it fills fields but never blanks one that already has a value. A getItem response missing a field must not erase what ingestion previously learned.
- `_sold_quantity` handles `estimatedAvailabilities` as both an array and a bare object, because eBay's docs show it both ways and it hasn't been seen live. Picking one and being silently wrong would leave the column null forever with nothing to notice.
- Deferred polling auctions just before `itemEndDate` to capture a near-final bid. It would produce the best comp data in the system and the budget has room, but it needs per-listing timed scheduling the sleep-loop scheduler doesn't do, and `bid_count` at disappearance already delivers most of the signal. Revisit once auctions are measured as a real share of the corpus.

**Broke / debugged:**
- Nothing broke. Changing `search_items` to return `SearchResult` touched 11 test files' fakes at once, and the existing suite caught every one immediately.

**Reversed:**
- The stage 3b plan had rejected fetching `localizedAspects` because the call budget forbade an extra `getItem` per listing. That only applies to *adding* calls, and the disappearance check already makes them. For any listing the checker touches, structured aspects cost nothing extra, which materially weakens the case for regex-extracting brand and model out of titles. Stage 3b gets replanned once there's data on how many listings carry useful aspects.

**Next:**
- Quota resets 07:00Z. First real ingest populates all of this. Three numbers worth reading immediately: **epid coverage** (decides how much stage 3 matters), the share of listings that are GTC (decides how big the relist problem is), and what fraction is auctions (decides whether auction close polling is worth building).
- None of these field shapes have been seen in a live response from this app. Confirm before trusting them, same as the shipping and buying-option columns.

## 2026-07-18 - A disappearance is not a sale: scoring comp confidence

**Did:**
- Attacked the dominant weakness in the valuation data: **the sold prices aren't sold prices.** A listing vanishes when it sells, but also when it expires unsold, when the seller pulls it, and when a Best Offer is accepted well below the asking price we record. Every one of those biases the comp *upward*, so the median comp sits above true market and everything scores as a better deal than it is. Bias toward false positives is the worst direction for a deal finder.
- Investigated the obvious fix, scraping the sold prices eBay publishes in its own web UI, and **rejected it on the evidence rather than on principle.** eBay's `robots.txt` disallows `/sch/` with three separate rules covering the sold-search path specifically, and the file header states that automated access without express permission is prohibited, pointing to the official API instead. That is materially more explicit than the undocumented endpoints ADR 0001 accepted for Depop, and the eBay account at risk is the same identity as the developer keyset.
- So: get the accuracy from data already held, which turned out to be more than expected. Wrote `docs/decisions/0005-sale-confidence.md`, then built `connectors/sale_confidence.py` around three signals, none of which cost an API call.
- **Relist detection, the strongest one.** When a listing disappears, look for another listing from the same source using the same `image_hash`. eBay fixed-price listings auto-renew under brand new item ids constantly, so a matching photo elsewhere means the seller relisted and the item never sold. This finally uses the `image_hash` column ADR 0002 built and left unused: that ADR deferred *cross-source* matching pending Depop, but *same-source relist detection* is a different and more immediately valuable use of the same data.
- **Listing lifetime.** `missing_since - posted_at` separates a listing gone in three days (probably sold) from one gone at roughly a 30 day term (probably lapsed). Checked at multiples too, since GTC auto-renews.
- **Best Offer and auction discounts.** Both were captured in the last session and unused. A Best Offer listing sold at an unknown discount to the recorded price; an auction's recorded price is a mid-bid snapshot.
- These combine multiplicatively into `sale_confidence` (a float in [0,1]) plus a `sale_signals` JSON breakdown, written at the moment a sale is confirmed. Migration `0006`. 20 new tests, 96 passing, mypy clean across all 18 source files.
- Also reframed `CLAUDE.md`'s "Constraints to respect" section, which read as absolute. Most of those constraints are engineering judgments with reasons and are revisitable; the eBay access boundary is the one that isn't, and it now says why, with the robots.txt evidence, so the question doesn't get re-litigated from memory later.

**Decided:**
- Score confidence, don't filter. A relisted item still gets marked `likely_sold` (the original listing genuinely is gone) but carries a confidence under 0.2, so stage 4 can weight it near zero without the pipeline having to make an irreversible keep/discard call on a heuristic.
- Multiplicative rather than additive penalties. The signals are close to independent, and a listing that is both a probable relist *and* vanished at term should score worse than either alone; additive penalties would let a strong signal get diluted by weak ones.
- A relist scores 0.15, not 0.0. `image_hash` matches on stock photography, which is common for boxed retail goods, so a photo match is strong evidence rather than proof.
- Store the score at confirmation time, not compute it in stage 4. Relist detection depends on what the database looked like around the disappearance, and the same query run months later would be looking at a different world.
- A missing `posted_at` or `image_hash` skips its signal rather than guessing. Absence of evidence must not read as "definitely sold".
- Keep the per-signal breakdown in `sale_signals`. These thresholds are guesses until there's real data to fit them against, and a single opaque number would make a bad heuristic impossible to spot.

**Broke / debugged:**
- The new DB tests hit `DetachedInstanceError`: the seed helper called `session.refresh()` and then let the `with` block close the session, so the first attribute read afterwards failed. Fixed with `refresh` then `expunge`, which loads the values before detaching.

**Next:**
- This makes the problem **measurable for the first time**, which is most of the point. Once real sales land, the two numbers worth reading immediately are the share of confirmed sales flagged as relists, and the distribution of `lifetime_days`. Both are currently unknown, and the honest accuracy of the whole deal scanner depends on them.
- If relists turn out to be a large fraction, that's a real argument for the user-driven browser-extension route to sold prices (the ADR 0001 pattern, applied to eBay), backed by numbers rather than a hunch.
- Stage 3 still next after that.

## 2026-07-12 - Auditing the intake path: capturing what was being thrown away

**Did:**
- Audited the whole eBay intake path looking for correctness and efficiency gaps, rather than waiting for the next thing to break. Data quality itself came back clean (all 10,496 listings USD, no null prices or categories, only 12 with no images), but the code was discarding several things it already had.
- **Price history.** `ingest_saved_search` overwrote `existing.price` on every run, so a price *drop*, one of the strongest deal signals there is, left no trace, and the stage 6 "price chart" in `PROJECT_PLAN.md` was unbuildable. Added a `PriceObservation` table, append-only, written on insert and thereafter only when price or shipping actually changes. Recording every run instead would add roughly 250,000 rows a day that all say "nothing happened". Migration seeds one opening observation per existing listing, backdated to its `first_seen_at`, so existing listings don't get charts that look like nothing was known before their first change.
- **Shipping cost.** eBay returns `shippingOptions[].shippingCost` in the same payload ingestion already fetches, and the normalizer was ignoring it, so capturing it cost zero extra API calls. `PROJECT_PLAN.md` had this on the backlog as future work while flagging it as "a real correctness bug once stage 4 is scoring deals". Takes the *cheapest* option, and keeps unknown as `None` rather than `0.0`, since collapsing unknown to free would make an item look cheaper than it is in exactly the comparison stage 4 depends on.
- **Auctions, the worst of the three.** `buyingOptions` was also being discarded. For an `AUCTION` listing eBay's `price` is the *current bid*, not an asking price, so when the auction ends and disappearance marks it sold, the recorded "sale price" is whatever mid-auction bid happened to be observed last. That is not a sale, and it goes straight into stage 4's comp set. Added `is_auction` (indexed) and `accepts_best_offer` as typed booleans rather than a raw JSON list, because the comp query has to filter on them. There was previously no way to even measure how much of the corpus this affects.
- **Two-strike sold confirmation.** `likely_sold` is terminal and turns a row into comp data, so a single transient 404 meant a permanent false comp. Now the first miss records `missing_since` and leaves the listing active; a second consecutive miss confirms it. Costs one extra call per genuine sale (cheap, since sales are a small fraction of checks) and pays for itself twice over: `missing_since` is a closer estimate of when the item actually left the market than the confirming check is, and a listing that reappears (in a check *or* in a search) clears its strike for free. Pending confirmations sort to the front of the check queue, since they'd otherwise land at the back having just been checked, and confirmation would take a full rotation.
- **Efficiency.** Replaced the per-item `SELECT` in the ingest loop with one batched `WHERE source_id IN (...)` per search, cutting roughly 12,800 DB round trips per run down to 64. Gave `EbayClient` a persistent `httpx.Client` and threaded one shared client through image hashing, so a run reuses connections instead of completing a fresh TCP+TLS handshake per call, which was a real part of why the first full ingest took 30-35 minutes.
- **Observability.** `search_items` now returns a `SearchResult(items, total)`, and each `SavedSearch` records `last_run_at` and `last_result_total`. Which searches are being truncated by the 200-result cap is now a query instead of a guess, which is what the deferred pagination work needs to be prioritized sensibly.
- Moved ingest to a 2 hour interval (from hourly) and raised `proven_alive_seconds` to 3 hours to keep the required margin. Budget is now 768 ingest plus 2,800 checking = 3,568 of 5,000. Added a test enforcing that `proven_alive_seconds` stays comfortably above the ingest interval, because if it ever drops below, every listing expires between runs, the whole corpus becomes check candidates every pass, and the budget silently stops meaning anything.
- Wrote `docs/decisions/0004-trustworthy-comp-data.md` before implementing. Migration `0005`. 76 tests passing, up from 59. Applied against the real Postgres: 10,496 price observations seeded correctly.

**Decided:**
- 2 hours for ingest rather than hourly. Halving ingest cost roughly doubles how many saved searches fit in the same quota, and breadth of coverage should find more deals than polling the same 64 searches twice as often. eBay sorts by Best Match, not recency, so a new listing does not necessarily enter the top 200 the moment it is posted anyway. Worth revisiting in a week with the real inserted/updated numbers rather than treating this as settled.
- Keep `search_items` on Best Match sort, deliberately, and write down why. `sort=newlyListed` is the obvious "improvement" for a deal finder, and it does have a real advantage (it guarantees every new listing is seen, where Best Match may never surface a poorly-ranking one). It still loses on two counts. It destroys the meaning of a disappearance: under Best Match a listing dropping out of results correlates with it having ended, while under newlyListed it drops out purely because 200 newer listings exist, which says nothing about whether it sold, so checks would be spent confirming listings that are almost certainly alive. And Best Match lets a well-matching listing stay in coverage indefinitely (free liveness forever), where newlyListed ages every listing out on a clock so every one eventually costs a call. The genuine cost of this choice is coverage, not correctness, and the fix for that is pagination or narrower keywords, not a different sort.
- Auctions get flagged, not excluded at ingest. An auction's final price is genuinely useful comp data once disappearance confirms it ended; dropping the listing outright would throw that away along with the problem.
- Typed indexed columns for `is_auction` / `accepts_best_offer` over a raw `buyingOptions` JSON list, same reasoning as the embedding-granularity decision: `images` already shows that plain `sa.JSON` gets no index, and stage 4's hot query needs to filter on exactly these two facts.

**Broke / debugged:**
- Changing `search_items` to return `SearchResult` broke 11 tests at once, all of them fakes returning a bare list. Worth it for making truncation measurable, but a good reminder that a widely-used return type is a real interface.
- `session.flush()` is needed after adding a new `Listing` before its `PriceObservation`, since the observation needs a `listing_id` and the id isn't assigned until flush.
- The migration needed a temporary `server_default` on the two new non-nullable booleans, because 10,496 existing rows have no value for them, then drops it again so new inserts go through the application default rather than the database's.

**Next:**
- Everything above is verified against tests and the real database, but the shipping and buying-option field shapes come from eBay's documented schema and have **not** been seen in a live response from this app, because the quota was exhausted. Confirm against one real response before trusting those columns.
- The first genuinely interesting question once data lands: what fraction of the corpus is `is_auction`? If it's large, stage 4 needs a weighting or exclusion rule, and ADR 0004 deliberately does not decide that.
- Then stage 3a (CLIP embeddings), still unblocked and independent (CDN, not API).

## 2026-07-12 - Stage 2.5: the pipeline had been silently down for hours

**Did:**
- Found the live pipeline dead, during a routine check before starting stage 3. The RQ worker read `successful_job_count: 1, failed_job_count: 9`. Every job since 16:36 UTC had failed with `429 Too Many Requests` on the very first saved search. Nothing was watching, so it sat dead for about 7 hours.
- Measured the real quota instead of guessing at it, via eBay's Developer Analytics `getRateLimits`: **5,000 Browse calls/day**, shared between `search_items` and `get_item`, `remaining: 0`, resets 07:00 UTC. The disappearance check as built wanted one `get_item` per active listing, so 10,496 listings x 4 passes/day = ~42,000 calls against an allowance of 5,000. It blew the entire day's budget partway through its first pass and took ingestion down with it.
- Probed the two eBay APIs that would have solved this outright, before designing around either. **Both are unavailable to this app.** The bulk `getItems` endpoint (20 ids per call, a 20x reduction) returns `403 errorId 1100`, and eBay refuses to even mint a token for the `buy.item.bulk` scope (`invalid_scope, exceeds the scope granted to the client`). Marketplace Insights, which returns *real sold prices* for the last 90 days and would largely replace disappearance tracking altogether, fails identically. Both are Limited Release APIs needing a separate application. Worth applying for; the rate-limit table even shows allocations for both, but an allocation is not a grant.
- Wrote `docs/decisions/0003-ebay-call-budget.md` before touching any code, then rebuilt the check around three ideas. **One:** ingestion already refreshes `last_seen_at` whenever a listing turns up in a saved search, for free, and eBay only returns *active* listings from search, so a recently-seen listing is provably alive and never costs a `get_item`. It's a one-sided oracle (presence proves alive, absence proves nothing), which is exactly right, because it justifies skipping checks without ever justifying a false "sold". **Two:** whatever's left gets checked oldest-`last_seen_at` first, capped by a budget derived from the real remaining quota. **Three:** listings that never sell and never end get retired to a new `stale` status, which is the only thing that actually bounds the candidate set.
- Fixed the two things that turned one bad call into a total outage. `ingest_all` now isolates each saved search, so one failure costs one search instead of all 64. And `call_with_backoff` now tells a "slow down" 429 apart from a "daily allowance gone" 429 (errorId 2001) and raises `QuotaExhaustedError` immediately instead of retrying: retrying burned 5x the quota it needed and dug the hole deeper.
- Split `ingest_saved_search`'s single `upserted` count into `inserted` / `updated` / `reactivated`. The insert rate is the arrival rate, which is the number that determines whether any of this budgeting stays sustainable, and it was previously unmeasurable.
- Made the budget arithmetic executable rather than a comment, as `estimate_daily_calls()`, with tests asserting the current configuration fits in 5,000/day and that the reserve covers a full day of ingest. Current numbers: **1,536 ingest + 2,800 check = 4,336 of 5,000, 664 headroom.** Adding 60 more saved searches now fails a test instead of silently exhausting the quota at 3am.
- Migration `0004`: `last_checked_at`, plus indexes on it and `last_seen_at` (both are on the hot path now). The new `stale` status needed no DDL, since `status` is a plain String column rather than a native Postgres enum.
- 26 new/updated tests, 56 passing. Applied the migration against the real Postgres and ran a real check pass: it correctly resolved a budget of **0** against the exhausted quota, spent nothing, retired nothing, and exited cleanly, where the old code would have thrown an unhandled 429.
- Surveyed every other eBay API that could possibly give more calls or better data, by calling each one rather than reading about it. **All closed.** Bulk `getItems`: 403, scope refused. Marketplace Insights: 403, scope refused. Buy Feed (the 10,000 and 75,000/day buckets in the rate-limit table): scope refused, 404. Legacy Shopping API `GetMultipleItems`: `open.api.ebay.com` no longer resolves in DNS at all. Legacy Finding API `findCompletedItems`, which returned genuinely sold listings: HTTP 418, retired. Worth writing down the general lesson: **eBay publishes rate-limit meters for APIs an application has not been granted**, so a big allowance in `getRateLimits` is not evidence of access. Browse's 5,000/day is the whole budget, and applying for the Limited Release scopes is the only way to change that.
- **Retirement was unreachable, a bug introduced earlier this session.** A confirmed-alive listing was having `last_seen_at` refreshed by the check (carried over from stage 2's behavior). But `last_seen_at` now drives both the proven-alive skip *and* retirement, so any listing in the check rotation had its unseen clock reset every pass and could never age past `unseen_after_days`. Retirement was unreachable for exactly the listings it exists for: alive, permanently below eBay's 200-result cap, costing a call every pass forever. Fixed by keeping the two signals separate (`last_seen_at` means seen in search, `last_checked_at` means we spent a call on it), and reordering candidates by least-recently-checked, which also spreads a fixed budget fairly instead of re-picking the same few listings. Two regression tests, 58 passing.

**Capacity, worked out properly:**
- Current spend is 1,536 ingest plus 2,800 checking = 4,336 of 5,000, so it runs continuously and indefinitely with 664 to spare.
- Saved-search ceiling is a tradeoff against ingest frequency, not a fixed number: at hourly ingest it's about 91 searches; every 2 hours about 183; every 3 hours about 275; every 6 hours about 550. Current 64 is comfortable.
- Sold-detection latency depends on how many listings are out of search coverage, since only those cost a call: 700 of them cycle in one pass (6h), 2,800 in 24h, and the whole 10,496 corpus would take about 90h if coverage collapsed entirely. Retirement is what stops that tail growing without bound.

**Decided:**
- Never delete listings, stated explicitly in the ADR because the instinct to do it is natural and wrong here. `likely_sold` rows *are* the product, they're the comp data stage 4 consumes. They also cost nothing in the loop that matters, since the check only queries `status = 'active'`, and disk isn't the constraint at ~3.4 KB/row.
- Retire zombies instead, to `stale`. The unbounded growth was never sold listings (they leave the rotation permanently), it's listings that never sell and never end, since eBay fixed-price listings auto-renew indefinitely. Retired rows stay in the database and ingestion reactivates them for free if they reappear.
- A search hit reactivates a `stale` listing but deliberately does **not** resurrect a `likely_sold` one. Stale is a cost-saving guess; likely_sold is a confirmed outcome backed by a real API call, and undoing it on a search hit would destroy a comp.
- An unparseable 429 body is treated as retryable rather than as quota exhaustion. Guessing "your quota is gone" from a body we can't read would abort a whole run over what might be a transient blip.
- Reserve 2,000 calls for ingestion, above a full day of hourly ingest (1,536). Checking stops before ingestion ever suffers, because ingest is both the product and the cheapest liveness signal available at 200 listings per call.
- Retirement thresholds (60 days active, 30 days unseen) are deliberately conservative guesses. There's no data behind them yet, and retiring too eagerly silently costs comp data.

**Broke / debugged:**
- The actual root cause took a while to surface because RQ stores tracebacks zlib-compressed in the job's result stream, not as a readable field. Had to pull the raw value and decompress it to see the 429.
- Killed a stray duplicate worker and scheduler. There were two of each running, only one worker registered in Redis. The scheduler holds last-run times in local variables, so a second instance doubles every job.
- My first process-cleanup check used a regex that missed the workers (the command line has a quote between `rq.exe` and `worker`), so it reported "none remaining" when two were still up. Caught it on a second look with a looser pattern.

**Next:**
- Nothing is running until then, deliberately. The full live verification (a real ingest, then a check pass that actually marks listings `likely_sold`) has to wait for the reset.
- Apply to eBay for `buy.item.bulk` and Marketplace Insights. Insights especially would change the shape of the whole project, since sold prices are exactly what stages 3 and 4 are being built to approximate.
- Then stage 3a (CLIP embeddings). It's unblocked by all of this and independent of it: image downloads hit the CDN, which has no OAuth and no quota.
- Still zero `likely_sold` rows. Every day the pipeline is down is a day of comp data that doesn't exist, and that's the only asset here that can't be built faster later.

## 2026-07-03 - Going to real production data, at real scale

**Did:**
- Switched `.env` from eBay sandbox to production (real Client ID/Secret), kept the old sandbox keyset commented out for reference.
- Added a default condition filter to `EbayClient.search_items()` (`exclude_new=True`), excluding condition IDs 1000/1500/1750 (New, New other, New with defects) and keeping refurbished/used/for-parts. Verified the exact filter syntax against the real production API before committing to it, rather than trusting docs/memory. This is a secondhand deal finder, brand-new listings have no resale depreciation to find a deal in.
- Raised `search_items()`'s default `limit` from 50 to 200, eBay Browse API's actual per-call max. Verified live that eBay actually honors 200.
- Seeded 63 new saved searches: every current Nintendo Switch variant (Lite, OLED, 2), iPhone 15 and newer (through the 17 line, plus 16e/Air), and a representative spread of recent CPUs, GPUs, RAM, and storage. 64 total now, up from 1.
- Bumped RQ's per-job timeout to 1 hour for both the ingest and disappearance-check jobs (`systems/queue.py`), since the default 180s would've killed a real run of this size partway through. Found this before it became a real problem, not after.
- Ran the first full ingest across all 64 searches for real: 11,263 upserts processed, 10,073 unique listings landed (the gap is overlap between different searches matching the same real item, expected), 99.9% got a real `image_hash`. Took about 30-35 minutes for this first run (every listing was new, so every one triggered an image download). Table size after: 9.16 MB for 10,073 rows, ~930 bytes/row at real scale, well below the earlier small-sample estimate.
- Added a "Post-completion backlog" section to `PROJECT_PLAN.md`: parallelizing ingestion (currently fully sequential, a real opportunity now that there's real volume), pagination past the 200-result cap, capturing shipping cost (flagged as more than a nice-to-have, a real correctness gap for stage 4's scoring), rate limiting at larger scale, and eventual data retention.

**Decided:**
- Condition filter excludes New/New-other/New-with-defects only, keeps all refurbished and used grades plus for-parts, since those are exactly where secondhand deals actually exist.
- 200 as the per-search cap, not more: it's eBay's actual maximum per call, going beyond it needs real pagination, deferred to the backlog above rather than solved today.
- Job timeouts bumped to a round, generous 1 hour rather than tuned precisely, since the actual runtime at this scale wasn't known ahead of time.

**Broke / debugged:**
- N/A this session, though the job-timeout issue above was caught proactively (reasoned about job duration at the new scale before running it for real) rather than discovered by a job dying mid-run.

**Next:**
- Scheduler + worker aren't running continuously yet, on request, until the user is ready. Once turned on: hourly ingestion, disappearance-checking every 6 hours, both against all 64 searches.
- The "Post-completion backlog" items are explicitly deferred, not needed to keep using the system as-is.

## 2026-06-26 - Stage 2: systems layer built and verified end to end

**Did:**
- Built `systems/ratelimit.py` (retry-with-backoff for 429/5xx/transport errors) and wrapped all three of `connectors/ebay.py`'s HTTP calls with it.
- Built `systems/queue.py` (RQ queue bound to Redis, `enqueue_ingest_all`/`enqueue_disappearance_check`) and `systems/scheduler.py` (a sleep-loop that enqueues both on independent, configurable intervals).
- Generalized `connectors/disappearance_check.py` from eBay-only to a `PULL_BASED_SOURCES` registry plus `check_all_sources()`, per the ADR from the last session.
- Added the compute-only half of image-hash dedup: `image_hash` column + migration, `connectors/image_hash.py` (perceptual hash via `imagehash`/Pillow), wired into `ingest_ebay.py` so new (or previously unhashed) listings get their primary photo hashed. Wrote `docs/decisions/0002-image-hash-dedup.md` first, since it's a new dependency plus a schema change.
- Added 26 new/updated tests (ratelimit, queue, scheduler, generalized disappearance-check, image hashing, ingest). Full suite: 30 passed.
- Fixed a pre-existing broken test (`test_search_items_without_credentials_raises_clear_error`): it passed `client_id=""` expecting that to simulate "no credentials," but that's falsy, so `EbayClient`'s `or` fallback was silently picking up the real sandbox credentials `.env` has had since the last session. Not something this session's changes caused, just never caught since no one re-ran the full suite after those credentials were added.
- Verified everything for real against the actual Docker Postgres/Redis: ran the migration, ran a real ingest (sandbox item has no images, so nothing to hash there, but confirmed the real network+Pillow+imagehash path against a live public image URL separately), enqueued both jobs against real Redis and ran an `rq worker` to process them.

**Decided:**
- Scheduler is a plain sleep-loop, not `rq-scheduler`, keeping the same "RQ not Celery" minimal-moving-parts reasoning.
- Image-hash dedup is compute-and-store only this stage. The actual cross-source duplicate-matching logic is deferred until Depop exists and there's a real second source to design the matching rules against.
- Perceptual hash (`imagehash.phash`) over an exact byte hash, since eBay/Depop each re-encode photos independently and an exact hash would basically never match cross-source.

**Broke / debugged:**
- Plain `rq worker` doesn't run at all on Windows: RQ's default `Worker` calls `os.fork()`, which doesn't exist on this platform. Its `SimpleWorker` (no fork) gets further but then fails enforcing job timeouts via `signal.SIGALRM`, also missing on Windows. Fixed with `systems/queue.py::WindowsWorker` (`SimpleWorker` + `TimerDeathPenalty`, which uses `threading.Timer` instead), confirmed working by actually running both queued jobs to completion. Expected to be a non-issue once this runs in a Linux container (stage 7).

**Next:**
- Stage 2 is done and verified. Stage 3 (feature pipeline: CLIP embeddings, NLP extraction) is next per the build order, once `connectors/depop.py` (which stage 2 was explicitly meant to unblock) is decided on, or straight into stage 3 if Depop stays deferred a while longer.

## 2026-06-21 - Stage 1 fully verified: real eBay sandbox data end to end

**Did:**
- Registered eBay sandbox credentials, filled `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` into `.env`.
- Brought Postgres/Redis back up (`docker compose up -d`), confirmed both healthy and already at migration head (the named volume survived the container recreate).
- Ran `python -m connectors.ingest_ebay` for real against eBay's sandbox API: upserted 1 listing (a Joy-Con set matching the seeded "nintendo switch" saved search).
- Confirmed `GET /listings` returns that real row and `GET /health` returns ok.

**Decided:**
- N/A, this was verification, not new design.

**Broke / debugged:**
- Port 8000 was held by a stale `uvicorn` process left running from an earlier session, which made the first `/listings` check return a 500. Not a bug in this session's code, just an old server still holding the port. Killed it and started a clean one.

**Next:**
- Stage 1 is done. Stage 2 (Redis job queue + scheduler, rate limiting/backoff, dedup) is next per the build order, before Depop or the Facebook extension get built (see `docs/decisions/0001-multi-source-connector-strategy.md`).
- Consider switching `.env`'s `EBAY_ENV` from `sandbox` to `production` once ready to pull real, non-test eBay data.

## 2026-06-21 - Multi-source connector strategy: Depop and Facebook Marketplace

**Did:**
- Wrote `docs/decisions/0001-multi-source-connector-strategy.md`, the first real ADR, deciding how Depop and Facebook Marketplace actually get ingested given neither has an official API and Facebook's ToS additionally prohibits scraping.
- Updated `PROJECT_PLAN.md` (practical notes, repo structure) and `CLAUDE.md` (constraints, repo structure) to match.
- Added Session 5 / Step 22 to `LEARNING_LOG.md` explaining the decision in build-guide detail.

**Decided:**
- Depop stays pull-based like eBay: a scheduled connector, following the same client/normalizer/ingest pattern as `connectors/ebay.py`, hitting Depop's unofficial endpoints. Low-investment, expect breakage, same framing as before, now made concrete as "pull-based."
- Facebook Marketplace goes push-based instead: a browser extension running in the user's own logged-in session captures one listing at a time on click and posts it to the API. No server-side Facebook connector, and no automated disappearance-tracking for Facebook-sourced listings, only manual re-capture. Full reasoning and alternatives considered are in the ADR.
- OfferUp is deferred, not addressed this session.

**Broke / debugged:**
- N/A.

**Next:**
- Finish verifying stage 1 end to end (the live eBay API call is still the one open item).
- Once stage 1 is verified and stage 2's systems layer (queue, scheduler, rate limiting, dedup, and generalizing `disappearance_check.py` to loop per source) is in place, implement `connectors/depop.py` first, since it reuses the eBay pattern almost directly, then the Facebook browser extension.

## 2026-06-13 - Real verification: Docker, uv, migrations, tests, live API

**Did:**
- Installed WSL2, Docker Desktop, and `uv` on this machine (none were present before). WSL2 needed an elevated terminal and a restart, so this was a stop-and-resume across two turns.
- Brought up Postgres (pgvector) and Redis for real via `docker compose up -d`, both report healthy.
- Ran `uv sync`: installed Python 3.12.14 (uv manages its own Python installs) and all 42 dependencies, generating `uv.lock` for the first time.
- Ran both Alembic migrations against the real database. Confirmed the `savedsearch` table has its seeded "nintendo switch" row.
- Ran the full test suite for real for the first time: all 13 tests passed on the first try.
- Fixed a Pydantic deprecation warning in `api/settings.py` (`class Config` to `model_config = SettingsConfigDict(...)`, the Pydantic v2-native way of doing the same thing).
- Started the API with `uvicorn` and confirmed `GET /health` and `GET /listings` respond correctly against the real database (`/listings` correctly returns `[]`, since no eBay ingestion has run yet).

**Decided:**
- N/A, this session was verification, not new design decisions.

**Broke / debugged:**
- `wsl --install` silently failed with a confusing "not installed" message when run from this non-elevated shell instead of a clear permissions error. Root cause: WSL2 setup needs administrator rights, which this session's shell access doesn't have. Fixed by having the user run it themselves from an elevated PowerShell.

**Next:**
- Register an eBay Developer sandbox app, fill in `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` in `.env`, run `python -m connectors.ingest_ebay` for real, and confirm `GET /listings` returns live eBay data. That's the one remaining piece before stage 1 counts as fully done end to end.
- Commit `uv.lock` now that it actually exists.

## 2026-06-08 - Closing out stage 1: saved-search config

**Did:**
- Added a `SavedSearch` model (`keyword`, `location`, `created_at`) to `api/models.py` and a migration (`0002_create_saved_search.py`) that creates the table and seeds one default row ("nintendo switch").
- Refactored `connectors/ingest_ebay.py`: replaced the single hardcoded `SEARCH_QUERY` constant with `ingest_saved_search()` (runs one saved search) and `ingest_all()` (loops over every row in the `savedsearch` table). `python -m connectors.ingest_ebay` now runs `ingest_all()`.
- Rewrote `tests/test_ingest_ebay.py` for the new interface, including a test that running two saved searches which happen to return the same eBay item still upserts a single row instead of two.
- Added real checkboxes to `PROJECT_PLAN.md`'s roadmap (they were referenced in `CLAUDE.md`'s "Current phase" note but never actually existed) and checked off everything now done in stage 1.

**Decided:**
- `SavedSearch.location` is stored but not yet passed to eBay's API. eBay's Browse API only supports country-level delivery/pickup filters, not the free-text proximity search this field implies. Documented as a known gap rather than faking a filter that wouldn't really work, revisit once a genuinely local source (Facebook Marketplace) needs it.
- Saved searches are seeded via migration data for now, not a CRUD interface. Full CRUD is explicitly stage 5 scope; stage 1 only needed the config to exist as data, per "no UI yet."

**Broke / debugged:**
- N/A.

**Next:**
- Run stage 1 for real on a machine with Docker and Python installed: `docker compose up`, `alembic upgrade head`, `python -m connectors.ingest_ebay` against live eBay data, confirm `GET /listings` returns real rows. Stage 1 is code-complete but not yet verified end to end, so stage 2 shouldn't start until that happens.
- Install `uv` and generate `uv.lock` (carried over from last session).

## 2026-06-02 - Self-review: DB tests, uv migration, embedding schema decision

**Did:**
- Added `tests/conftest.py` (in-memory SQLite fixture) and tests for `ingest_ebay.py`'s upsert logic and `disappearance_check.py`'s status-flipping logic. Both were previously untested despite being the two DB-touching connector scripts.
- Refactored both to take injectable `client`/`db_engine` params so tests don't need real eBay/Postgres.
- Replaced `requirements.txt` with `pyproject.toml` + `uv` (dependencies, Ruff, mypy, and pytest config now all live in one file).
- Created `LEARNING_LOG.md`, a standing reference doc (repo map, tool glossary, decision log), and added a note to `CLAUDE.md` to keep it updated every session, including whenever a "locked-in" choice gets swapped for a better one.

**Decided:**
- One embedding per listing, not per image, for the Stage 3 CLIP work. See `LEARNING_LOG.md`'s decision log for the full reasoning. Not implemented yet, just settled ahead of time.
- `CLAUDE.md` and `PROJECT_PLAN.md` choices are defaults, not commitments. They're changeable any time a better option turns up, as long as it's explained in `LEARNING_LOG.md`.

**Broke / debugged:**
- N/A.

**Next:**
- Install `uv` and generate `uv.lock`.
- Register an eBay Developer sandbox app, fill in `.env`, and run the full Stage 1 setup end to end with real data.

## 2026-06-02 - Repo scaffold + Stage 1 vertical slice

**Did:**
- Renamed planning docs (`Claude.md` to `CLAUDE.md`, `Project Plan.md` to `PROJECT_PLAN.md`) so the cross-reference between them actually resolves.
- Ran `git init`, laid out the repo skeleton (`connectors/`, `api/`, `tests/`, `infra/`, `docs/decisions/`).
- `infra/docker-compose.yml`: Postgres (pgvector image) and Redis only, matching stage 1 scope.
- `Listing` SQLModel (`api/models.py`) plus the first Alembic migration.
- eBay Browse API client (`connectors/ebay.py`), OAuth client-credentials flow, `search_items`, `get_item`.
- Normalizer (`connectors/normalizer.py`) mapping raw eBay item summaries into `Listing`.
- `connectors/ingest_ebay.py`: one hardcoded search query, normalized, then upserted.
- `connectors/disappearance_check.py`: re-checks active listings and marks vanished ones `likely_sold`. Not wired to a scheduler yet (that's stage 2); run it manually or via cron for now so sold-price data starts accumulating early.
- `GET /listings` route plus `GET /health`.
- Fixture-based tests for the normalizer and the eBay client, respx-mocked HTTP, no live API calls.

**Decided:**
- Disappearance checking runs as a plain script for now instead of waiting on the stage-2 RQ scheduler, since the plan calls for starting it as early as possible so it has time to accumulate data.
- Repo lives directly in this folder rather than in a nested `deal-finder/` subdirectory.

**Broke / debugged:**
- N/A, first commit.

**Next:**
- Register an eBay Developer sandbox app, fill in `.env`, run `alembic upgrade head` and `ingest_ebay.py` against real data.
- Verify `GET /listings` returns real rows before moving to stage 2 (Redis queue and scheduler, rate limiting, dedup).
