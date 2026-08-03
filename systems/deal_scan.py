"""The scheduled deal scan, its result cache, and alerting.

`find_deals` is a batch job: thousands of k-NN queries, minutes of wall clock.
That cannot run inside an HTTP request, so it runs on the queue and writes its
results to Redis, which `GET /deals` reads.

Redis rather than a database table, on purpose. A deal score is *derived* and
goes stale the moment a comp arrives, unlike `sale_confidence` which is frozen
at disappearance time because it describes a moment. Caching it in Redis with
a TTL says exactly that: this is a recomputable snapshot, not a fact. It also
means no migration and no risk of a stale table outliving the logic that
produced it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import cast

import httpx
from redis import Redis

from api.settings import settings
from ml.valuation import find_deals

logger = logging.getLogger(__name__)

DEALS_KEY = "undercut:deals"
SCAN_AT_KEY = "undercut:deals:scanned_at"
ALERTED_KEY = "undercut:deals:alerted"

# Twice the scan interval, so a feed is never empty just because a scan is
# mid-flight, but a scan that stops running does eventually go quiet rather
# than serving stale deals forever.
CACHE_TTL_SECONDS = 2 * 60 * 60


def _redis() -> Redis:
    return Redis.from_url(settings.redis_url)


def cached_deals() -> list:
    """Deals from the last completed scan, newest first. Empty if none ran."""
    from api.routes.deals import DealRead

    # cast because redis-py's stubs type get() as possibly-Awaitable to cover
    # the async client; this is the sync client and never returns a coroutine.
    raw = cast("bytes | None", _redis().get(DEALS_KEY))
    if not raw:
        return []
    try:
        return [DealRead.model_validate(d) for d in json.loads(raw)]
    except Exception:
        # A cache written by an older version of the schema is not worth
        # crashing the feed over; an empty list reads as "no scan yet".
        logger.warning("could not decode cached deals, treating as empty", exc_info=True)
        return []


def last_scan_at() -> datetime | None:
    raw = cast("bytes | None", _redis().get(SCAN_AT_KEY))
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.decode())
    except ValueError:  # pragma: no cover - defensive
        return None


def notify_discord(deals: list, webhook_url: str | None = None) -> bool:
    """Post new deals to a Discord webhook. Returns whether anything was sent.

    Only genuinely new listings are announced, tracked in a Redis set. Without
    that the same deal is re-announced every scan until it sells, which trains
    the reader to ignore the channel, and an alert nobody reads is worse than
    no alert.
    """
    webhook_url = webhook_url or settings.discord_webhook_url
    if not webhook_url or not deals:
        return False

    redis = _redis()
    fresh = [d for d in deals if d.listing_id and not redis.sismember(ALERTED_KEY, d.listing_id)]
    if not fresh:
        return False

    lines = []
    for deal in fresh[: settings.discord_max_alerts]:
        price = deal.total_cost or deal.asking_price
        lines.append(
            f"**{(deal.deal_score or 0):.0%} below comps** - ${price:,.2f} "
            f"(est ${deal.estimated_value or 0:,.2f}, {deal.comp_count} comps, "
            f"confidence {deal.confidence:.2f})\n{deal.title[:140]}\n{deal.url}"
        )

    # The caveat travels with the alert. This is the message most likely to be
    # acted on immediately, so it is the one that can least afford to imply
    # more certainty than the comps support.
    content = (
        "\n\n".join(lines)
        + "\n\n_Comps are listings that left the market, which is not the same as "
        "confirmed sales. Estimates are biased high._"
    )

    try:
        response = httpx.post(webhook_url, json={"content": content[:1900]}, timeout=15.0)
        response.raise_for_status()
    except httpx.HTTPError:
        # An alerting failure must not fail the scan: the results are already
        # cached and readable at GET /deals.
        logger.warning("could not post deals to Discord", exc_info=True)
        return False

    for deal in fresh:
        redis.sadd(ALERTED_KEY, deal.listing_id)
    return True


def run_deal_scan(scan_limit: int | None = None, alert: bool = True) -> int:
    """Scan for deals, cache the results, and alert on new ones.

    Returns how many deals were found. Runs on the `undercut` queue: it
    needs no torch, only Postgres and pgvector.
    """
    from api.routes.deals import _to_deal_read

    found = find_deals(
        limit=settings.deal_feed_size,
        min_deal_score=settings.deal_min_score,
        min_confidence=settings.deal_min_confidence,
        scan_limit=scan_limit,
    )
    deals = [_to_deal_read(listing, valuation) for listing, valuation in found]

    redis = _redis()
    payload = json.dumps([d.model_dump(mode="json") for d in deals])
    redis.set(DEALS_KEY, payload, ex=CACHE_TTL_SECONDS)
    redis.set(SCAN_AT_KEY, datetime.now(timezone.utc).isoformat(), ex=CACHE_TTL_SECONDS)

    if alert:
        notify_discord(deals)

    logger.info("deal scan complete: %d deals cached", len(deals))
    return len(deals)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    count = run_deal_scan()
    print(f"{count} deals cached, readable at GET /deals")
