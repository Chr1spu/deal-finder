from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")

    database_url: str = "postgresql+psycopg://dealfinder:dealfinder@localhost:5432/dealfinder"
    redis_url: str = "redis://localhost:6379/0"

    ebay_env: str = "sandbox"
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    ebay_marketplace_id: str = "EBAY_US"

    # 2h rather than hourly. Halving ingest cost (1,536 -> 768 calls/day)
    # roughly doubles how many saved searches fit in the same quota, and
    # breadth of coverage finds more deals than polling the same 64 searches
    # twice as often: eBay sorts by Best Match, not recency, so a new listing
    # doesn't necessarily enter the top 200 the moment it's posted anyway.
    # Worth revisiting once IngestResult's inserted/updated split has a week
    # of real data to show how fast the result set actually turns over.
    ingest_interval_seconds: int = 7200
    disappearance_check_interval_seconds: int = 21600

    # Call budgeting, see docs/decisions/0003-ebay-call-budget.md. eBay gives
    # this app 5,000 Browse calls/day total, shared by search and get_item.
    #
    # The arithmetic these defaults are sized against, with 64 saved searches:
    #   ingest    64 calls/run x 12 runs/day (every 2h)      =   768
    #   checking  700 calls/pass x 4 passes/day (every 6h)   = 2,800
    #                                                   total 3,568 of 5,000
    #
    disappearance_check_budget: int = 700

    # quota_reserve is held back for ingestion, which must never be starved by
    # the checker: ingest is both the product (finding a deal early is most of
    # the value) and by far the cheapest liveness signal available, at 200
    # listings per call against get_item's 1.
    #
    # Was 2,000, which was correct when ingest ran hourly and needed 1,536
    # calls/day. Ingest is 2-hourly now and needs 768, so a 2,000 reserve was
    # 2.6x a full day of it and silently capped total check capacity at
    # 5,000 - 2,000 - 768 = 2,232/day, below the 2,800 the arithmetic above
    # assumes. Observed live on 2026-07-25: remaining hit exactly 2,000 and
    # resolve_budget returned 0, halting sold-detection for 15 hours with
    # ~1,550 calls left to expire unused.
    #
    # 1,000 covers a full day of ingest (768) with 30% headroom, and leaves
    # 5,000 - 1,000 - 768 = 3,232 for checking, which comfortably fits the
    # 2,800 plan. Raise it again if ingest_interval_seconds ever drops.
    quota_reserve: int = 1000

    # A listing seen in a saved search within this window is treated as
    # provably alive and skipped, since eBay only returns active listings.
    # MUST stay comfortably above ingest_interval_seconds, or every listing
    # expires between ingest runs, the whole corpus becomes check candidates
    # on every pass, and the budget above stops meaning anything. That failure
    # is silent, so a test enforces the margin.
    proven_alive_seconds: int = 10800

    # Zombie retirement. Deliberately conservative: retiring too eagerly
    # silently costs comp data, and these thresholds have no real data behind
    # them yet (the corpus is days old). Revisit with a month of history.
    stale_after_days: int = 60
    unseen_after_days: int = 30

    # Sale confidence, see docs/decisions/0005-sale-confidence.md. A
    # disappearance is not a sale, so every inferred sale gets scored rather
    # than trusted. Both of these are guesses until there's real data to fit
    # them against, which is much of the point of storing the breakdown.
    #
    # relist_grace_days: how far back to look for another listing reusing the
    # same photo. Too short misses relists posted before the old one lapsed;
    # too long starts matching genuinely different sales of similar items.
    relist_grace_days: int = 21
    # A listing gone this fast is more likely to have really sold.
    quick_sale_days: float = 7.0
    # How close to its published end date a disappearance counts as "ran to
    # term" (i.e. nobody bought it). Generous because the check runs on a
    # 6-hourly interval, so it notices a disappearance well after it happened.
    scheduled_end_tolerance_hours: float = 12.0

    # Valuation, see docs/decisions/0014-valuation-and-deal-scoring.md.
    #
    # Both are guesses until there is enough history to fit them against, which
    # is much of why the signals breakdown is stored.
    #
    # A comp below this sale confidence is EXCLUDED, not down-weighted: a
    # relisted item that probably never sold is not weak evidence of a sale
    # price, it is evidence of nothing. 0.3 is deliberately permissive, since
    # relists score around 0.135 and ordinary sales around 0.75.
    min_sale_confidence: float = 0.3
    # Below this many usable comps, no estimate is produced at all. Measured:
    # 15% of listings currently find zero sold comps and another 16% find one
    # or two, so this refuses to answer for roughly a third of the corpus.
    # That is the intended behaviour: a confident-looking number gets acted
    # on, a missing one does not.
    min_comps_for_valuation: int = 3

    # Deal feed and alerting.
    #
    # BOTH thresholds are required together, deliberately: a large apparent
    # discount computed from two shaky comps is the single most likely thing
    # to be wrong, and is exactly what an unfiltered "biggest discounts" list
    # surfaces first. Three separate correctness bugs were found that way.
    deal_min_score: float = 0.2
    deal_min_confidence: float = 0.3
    deal_feed_size: int = 50
    # A full scan is thousands of k-NN queries and takes minutes, so it runs
    # far less often than ingest and never inside a request.
    deal_scan_interval_seconds: int = 3600
    # Empty disables alerting entirely, which is the default: nothing should
    # post to a webhook nobody configured.
    discord_webhook_url: str = ""
    discord_max_alerts: int = 5

    # CLIP embeddings, see docs/decisions/0009-clip-embeddings-pgvector.md.
    #
    # Operational knobs only. The model name and its dimension are NOT here:
    # they must agree with the column the migration created, and a .env value
    # that can silently disagree with the schema is a footgun. They live as
    # constants in ml/embeddings.py and api/models.py.
    #
    # "auto" picks cuda when torch reports it, else cpu. Pin it to "cpu" to
    # rule the GPU out while debugging something else.
    embedding_device: str = "auto"
    embedding_batch_size: int = 64
    # Ceiling on one scheduled embed job, so a run is bounded and restartable
    # rather than a single job that owns the worker for hours. Backfill is
    # resume-safe, so a capped job simply picks up where the last one stopped.
    embed_job_max_listings: int = 2000
    embed_interval_seconds: int = 900

    # Postgres for the tests that genuinely need it (pgvector distance
    # operators, the real column type). Tests skip rather than fail when it's
    # unreachable, so `uv run pytest` stays one green command on a machine
    # with no container running.
    test_database_url: str = (
        "postgresql+psycopg://dealfinder:dealfinder@localhost:5432/dealfinder_test"
    )


settings = Settings()
