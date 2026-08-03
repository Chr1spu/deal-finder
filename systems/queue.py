"""RQ job queue setup. Actual job logic lives in connectors/ (ingest_all,
check_all_sources); this module just knows how to enqueue them. Running a
worker is `rq worker undercut` (via the redis connection configured
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

# Safe to import at module level despite being the GPU job: ml.embeddings
# imports torch and open_clip *inside* its functions, never at module scope,
# precisely so this line doesn't drag 3 GB of CUDA libraries into the
# scheduler, the API process and every test run.
from ml.embed_listings import embed_pending
from systems.deal_scan import run_deal_scan

QUEUE_NAME = "undercut"

# A second queue for embedding work, and the reason is capability rather than
# throughput. RQ hands a worker whatever job is next on the queues it watches,
# so on a single shared queue the ingest worker (which has no torch installed,
# deliberately) would sooner or later be handed an embed job and die on
# `import torch`. Splitting the queues is what lets one worker exist without
# a 3 GB dependency. See docs/decisions/0009-clip-embeddings-pgvector.md.
ML_QUEUE_NAME = "undercut-ml"


class WindowsWorker(SimpleWorker):
    """rq's default Worker forks (os.fork) and even SimpleWorker's job-timeout
    enforcement uses SIGALRM, neither of which exist on Windows. TimerDeathPenalty
    uses threading.Timer instead, which works cross-platform. Use this for local
    dev on Windows: `rq worker undercut --worker-class systems.queue.WindowsWorker`.
    Plain `rq worker` is fine once this runs inside a Linux container (stage 7)."""

    # rq types this as UnixSignalDeathPenalty on the base class, which is
    # exactly the assumption being overridden here, so the mismatch is the
    # point rather than a mistake.
    death_penalty_class = TimerDeathPenalty  # type: ignore[assignment]


def get_queue(name: str = QUEUE_NAME) -> Queue:
    return Queue(name, connection=Redis.from_url(settings.redis_url))


def get_ml_queue() -> Queue:
    return get_queue(ML_QUEUE_NAME)


# RQ's default job timeout (180s) isn't enough once there are dozens of saved
# searches, each potentially hashing images for many new listings - a single
# run can legitimately take much longer than a couple of small test searches
# did. Generous timeouts here, not tuned precisely, just enough to not get
# killed mid-run as the number of saved searches grows.
INGEST_JOB_TIMEOUT = "1h"
DISAPPEARANCE_CHECK_JOB_TIMEOUT = "1h"
# Longer, and capped from the other end by settings.embed_job_max_listings.
# Generous because the first run downloads a ~600 MB checkpoint before it
# embeds anything, and because TimerDeathPenalty firing in the middle of a
# CUDA call can leave the process in a bad state, so the timeout should be a
# genuine backstop rather than something a normal run flirts with.
EMBED_JOB_TIMEOUT = "2h"
# A full scan is minutes of k-NN queries; generous so a growing corpus does
# not start getting killed mid-scan.
DEAL_SCAN_JOB_TIMEOUT = "1h"


def _already_queued(queue: Queue, func_name: str) -> bool:
    """Whether an identical job is already waiting.

    Both eBay-facing jobs spend the scarcest resource in the project, so two
    of them running at once wastes quota and races on the same rows. Embedding
    and the deal scan already skip in this situation; these two did not, which
    was an inconsistency rather than a decision.
    """
    return any(job.func_name.endswith(func_name) for job in queue.jobs)


def enqueue_ingest_all(queue: Queue | None = None):
    queue = queue or get_queue()
    if _already_queued(queue, "ingest_all"):
        return None
    return queue.enqueue(ingest_all, job_timeout=INGEST_JOB_TIMEOUT)


def enqueue_disappearance_check(queue: Queue | None = None):
    queue = queue or get_queue()
    if _already_queued(queue, "check_all_sources"):
        return None
    return queue.enqueue(check_all_sources, job_timeout=DISAPPEARANCE_CHECK_JOB_TIMEOUT)


def enqueue_embed_pending(queue: Queue | None = None):
    """Enqueue a bounded embedding run, unless one is already waiting.

    The skip matters because the scheduler polls far more often than a full
    backfill takes: without it, a 15-minute interval against a run that takes
    an hour would pile up identical jobs, and every queued one would re-do
    work the running job had already claimed. Returns None when it skips, so
    the caller can tell "queued" from "already busy".
    """
    queue = queue or get_ml_queue()
    if queue.count:
        return None
    return queue.enqueue(
        embed_pending,
        kwargs={"limit": settings.embed_job_max_listings},
        job_timeout=EMBED_JOB_TIMEOUT,
    )


def enqueue_deal_scan(queue: Queue | None = None):
    """Enqueue a deal scan, unless one is already waiting.

    Same no-pile-up rule as embedding, and for the same reason: the scan takes
    minutes, so a scheduler polling every 30 seconds would otherwise stack
    identical jobs that all redo the same work.

    Goes on the main queue rather than the ML one: it needs Postgres and
    pgvector but no torch.
    """
    queue = queue or get_queue()
    if _already_queued(queue, "run_deal_scan"):
        return None
    return queue.enqueue(run_deal_scan, job_timeout=DEAL_SCAN_JOB_TIMEOUT)
