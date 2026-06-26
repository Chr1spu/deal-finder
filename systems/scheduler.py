"""Sleep-loop scheduler: enqueues ingest and disappearance-check jobs on
independent intervals. A simple loop rather than rq-scheduler or cron,
matching the project's existing "RQ not Celery, keep it simple" reasoning
(see LEARNING_LOG.md). Run via `python -m systems.scheduler`.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from api.settings import settings
from systems.queue import enqueue_disappearance_check, enqueue_ingest_all

POLL_INTERVAL_SECONDS = 30


def due(last_run: datetime | None, interval_seconds: int, now: datetime) -> bool:
    """True if `interval_seconds` have passed since last_run (or it's never run)."""
    if last_run is None:
        return True
    return (now - last_run).total_seconds() >= interval_seconds


def run_forever() -> None:
    last_ingest: datetime | None = None
    last_disappearance_check: datetime | None = None

    while True:
        now = datetime.now(timezone.utc)

        if due(last_ingest, settings.ingest_interval_seconds, now):
            enqueue_ingest_all()
            last_ingest = now

        if due(last_disappearance_check, settings.disappearance_check_interval_seconds, now):
            enqueue_disappearance_check()
            last_disappearance_check = now

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_forever()
