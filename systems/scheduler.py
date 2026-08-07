"""Sleep-loop scheduler: enqueues ingest and disappearance-check jobs on
independent intervals. A simple loop rather than rq-scheduler or cron,
matching the project's existing "RQ not Celery, keep it simple" reasoning
(see LEARNING_LOG.md). Run via `python -u -m systems.scheduler`.

Three things here exist because this loop went silent for nine hours on
2026-08-07 and nothing noticed until a gap appeared in the data:

1. **Every enqueue is isolated.** The loop had no exception handling at all,
   so a single transient Redis error in any one of the four calls ended all
   scheduling. Not just that task, all of it, permanently, with the process
   sometimes still resident so `ps` looked healthy.
2. **Failures are logged loudly, never swallowed.** This is the counterweight
   to (1). `ingest_all`'s per-search isolation once hid a NotNullViolation on
   every insert while reporting success, which is the failure this project has
   been bitten by most. Isolation without a loud log just relocates the
   silence, so a failed enqueue logs its traceback and increments a counter
   that the heartbeat exposes.
3. **A heartbeat in Redis.** The loop previously had no output whatsoever, so
   "is the scheduler working" could only be answered by looking for missing
   rows in Postgres hours later. `systems.scheduler_health` reads this key.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from api.settings import settings
from systems.queue import (
    enqueue_deal_scan,
    enqueue_disappearance_check,
    enqueue_embed_pending,
    enqueue_ingest_all,
    get_redis,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30

# The heartbeat is refreshed every poll and expires well after it, so a missing
# or expired key means the loop is not running. Six polls of slack absorbs a
# slow enqueue without producing a false alarm.
HEARTBEAT_KEY = "undercut:scheduler:heartbeat"
HEARTBEAT_TTL_SECONDS = POLL_INTERVAL_SECONDS * 6


def due(last_run: datetime | None, interval_seconds: int, now: datetime) -> bool:
    """True if `interval_seconds` have passed since last_run (or it's never run)."""
    if last_run is None:
        return True
    return (now - last_run).total_seconds() >= interval_seconds


def _write_heartbeat(state: dict) -> None:
    """Publish liveness to Redis. Never raises: a heartbeat failure must not be
    the thing that stops the scheduler it exists to monitor."""
    try:
        connection = get_redis()
        connection.set(HEARTBEAT_KEY, json.dumps(state), ex=HEARTBEAT_TTL_SECONDS)
    except Exception:
        logger.exception("could not write scheduler heartbeat")


def run_forever() -> None:
    last_run: dict[str, datetime | None] = {
        "ingest": None,
        "disappearance_check": None,
        "embed": None,
        "deal_scan": None,
    }
    # Tasks are (name, interval-setting, enqueue callable). A table rather than
    # four near-identical if-blocks, so adding a task cannot accidentally skip
    # the error isolation.
    tasks = (
        ("ingest", settings.ingest_interval_seconds, enqueue_ingest_all),
        (
            "disappearance_check",
            settings.disappearance_check_interval_seconds,
            enqueue_disappearance_check,
        ),
        # Onto a separate queue, watched by a separate worker that has torch
        # installed. Runs far more often than the others because it costs no
        # eBay quota (images come from the CDN, not the Browse API), and
        # because enqueue_embed_pending skips when a run is already waiting,
        # so a frequent poll is cheap rather than a way to pile up jobs.
        ("embed", settings.embed_interval_seconds, enqueue_embed_pending),
        # Hourly rather than every few minutes: a full scan is thousands of
        # k-NN queries, and deals do not appear faster than ingest finds them.
        ("deal_scan", settings.deal_scan_interval_seconds, enqueue_deal_scan),
    )

    consecutive_errors = 0
    logger.info(
        "scheduler starting: ingest every %ss, disappearance check every %ss, "
        "embed every %ss, deal scan every %ss",
        settings.ingest_interval_seconds,
        settings.disappearance_check_interval_seconds,
        settings.embed_interval_seconds,
        settings.deal_scan_interval_seconds,
    )

    while True:
        now = datetime.now(timezone.utc)

        for name, interval_seconds, enqueue in tasks:
            if not due(last_run[name], interval_seconds, now):
                continue
            try:
                job = enqueue()
            except Exception:
                # Loud, with the traceback, and the loop survives. Before this
                # existed the same error ended scheduling entirely.
                consecutive_errors += 1
                logger.exception("enqueue failed for %s (consecutive errors: %d)",
                                 name, consecutive_errors)
                # last_run is deliberately NOT updated, so the next poll
                # retries in 30 seconds rather than waiting a full interval.
                continue

            consecutive_errors = 0
            last_run[name] = now
            if job is None:
                # The enqueue helpers return None when an identical job is
                # already waiting. Worth logging: a task that reports "skipped"
                # every cycle means a job is stuck, not that all is well.
                logger.info("%s skipped, an identical job is already queued", name)
            else:
                logger.info("%s enqueued as %s", name, job.id)

        _write_heartbeat(
            {
                "at": now.isoformat(),
                "consecutive_errors": consecutive_errors,
                "last_run": {
                    key: value.isoformat() if value else None
                    for key, value in last_run.items()
                },
            }
        )

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()
