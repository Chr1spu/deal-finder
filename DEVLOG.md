# Devlog

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
