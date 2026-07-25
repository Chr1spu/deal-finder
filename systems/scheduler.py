"""Sleep-loop scheduler: enqueues ingest and disappearance-check jobs on
independent intervals. A simple loop rather than rq-scheduler or cron,
matching the project's existing "RQ not Celery, keep it simple" reasoning
(see LEARNING_LOG.md). Run via `python -m systems.scheduler`.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from api.settings import settings
from systems.queue import (
    enqueue_deal_scan,
    enqueue_disappearance_check,
    enqueue_embed_pending,
    enqueue_ingest_all,
)

POLL_INTERVAL_SECONDS = 30


def due(last_run: datetime | None, interval_seconds: int, now: datetime) -> bool:
    """True if `interval_seconds` have passed since last_run (or it's never run)."""
    if last_run is None:
        return True
    return (now - last_run).total_seconds() >= interval_seconds


def run_forever() -> None:
    last_ingest: datetime | None = None
    last_disappearance_check: datetime | None = None
    last_embed: datetime | None = None
    last_deal_scan: datetime | None = None

    while True:
        now = datetime.now(timezone.utc)

        if due(last_ingest, settings.ingest_interval_seconds, now):
            enqueue_ingest_all()
            last_ingest = now

        if due(last_disappearance_check, settings.disappearance_check_interval_seconds, now):
            enqueue_disappearance_check()
            last_disappearance_check = now

        # Onto a separate queue, watched by a separate worker that has torch
        # installed. Runs far more often than the others because it costs no
        # eBay quota (images come from the CDN, not the Browse API), and
        # because enqueue_embed_pending skips when a run is already waiting,
        # so a frequent poll is cheap rather than a way to pile up jobs.
        if due(last_embed, settings.embed_interval_seconds, now):
            enqueue_embed_pending()
            last_embed = now

        # Hourly rather than every few minutes: a full scan is thousands of
        # k-NN queries, and deals do not appear faster than ingest finds them.
        if due(last_deal_scan, settings.deal_scan_interval_seconds, now):
            enqueue_deal_scan()
            last_deal_scan = now

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
