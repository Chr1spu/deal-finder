"""RQ job queue setup. Actual job logic lives in connectors/ (ingest_all,
check_all_sources); this module just knows how to enqueue them. Running a
worker is `rq worker deal-finder` (via the redis connection configured
here), no custom worker code needed.
"""

from __future__ import annotations

from redis import Redis
from rq import Queue
from rq.timeouts import TimerDeathPenalty
from rq.worker import SimpleWorker

from api.settings import settings
from connectors.disappearance_check import check_all_sources
from connectors.ingest_ebay import ingest_all

QUEUE_NAME = "deal-finder"


class WindowsWorker(SimpleWorker):
    """rq's default Worker forks (os.fork) and even SimpleWorker's job-timeout
    enforcement uses SIGALRM, neither of which exist on Windows. TimerDeathPenalty
    uses threading.Timer instead, which works cross-platform. Use this for local
    dev on Windows: `rq worker deal-finder --worker-class systems.queue.WindowsWorker`.
    Plain `rq worker` is fine once this runs inside a Linux container (stage 7)."""

    death_penalty_class = TimerDeathPenalty


def get_queue() -> Queue:
    return Queue(QUEUE_NAME, connection=Redis.from_url(settings.redis_url))


def enqueue_ingest_all(queue: Queue | None = None):
    queue = queue or get_queue()
    return queue.enqueue(ingest_all)


def enqueue_disappearance_check(queue: Queue | None = None):
    queue = queue or get_queue()
    return queue.enqueue(check_all_sources)
