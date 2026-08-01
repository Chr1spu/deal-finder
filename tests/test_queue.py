from api.settings import settings
from connectors.disappearance_check import check_all_sources
from connectors.ingest_ebay import ingest_all
from ml.embed_listings import embed_pending
from systems.queue import (
    ML_QUEUE_NAME,
    QUEUE_NAME,
    enqueue_disappearance_check,
    enqueue_embed_pending,
    enqueue_ingest_all,
    get_ml_queue,
)


class FakeQueue:
    """Stands in for rq.Queue. No Redis connection, just records calls."""

    def __init__(self):
        self.count = 0
        # Real Queues expose the waiting jobs; the enqueue helpers read this
        # to skip when an identical job is already pending.
        self.jobs: list = []
        self.enqueued = []

    def enqueue(self, fn, *args, **kwargs):
        self.enqueued.append((fn, args, kwargs))
        return fn


def test_enqueue_ingest_all_schedules_the_real_ingest_function():
    queue = FakeQueue()

    enqueue_ingest_all(queue=queue)

    assert len(queue.enqueued) == 1
    fn, args, kwargs = queue.enqueued[0]
    assert fn is ingest_all
    assert "job_timeout" in kwargs, "needs a longer-than-default timeout, see systems/queue.py"


def test_enqueue_disappearance_check_schedules_the_real_check_function():
    queue = FakeQueue()

    enqueue_disappearance_check(queue=queue)

    assert len(queue.enqueued) == 1
    fn, args, kwargs = queue.enqueued[0]
    assert fn is check_all_sources
    assert "job_timeout" in kwargs, "needs a longer-than-default timeout, see systems/queue.py"


def test_enqueue_embed_pending_schedules_the_real_embed_function():
    queue = FakeQueue()

    enqueue_embed_pending(queue=queue)

    assert len(queue.enqueued) == 1
    fn, args, kwargs = queue.enqueued[0]
    assert fn is embed_pending
    assert "job_timeout" in kwargs, "needs a longer-than-default timeout, see systems/queue.py"
    assert kwargs["kwargs"]["limit"] == settings.embed_job_max_listings, (
        "a scheduled run must stay bounded so it's restartable"
    )


def test_enqueue_embed_pending_skips_when_a_run_is_already_waiting():
    """The scheduler polls every 15 minutes; a full backfill takes longer than
    that. Without the skip, identical jobs pile up and each queued one re-does
    work the running job already claimed."""
    queue = FakeQueue()
    queue.count = 1

    assert enqueue_embed_pending(queue=queue) is None
    assert queue.enqueued == []


def test_embedding_goes_on_its_own_queue():
    """Not a throughput decision: RQ hands a worker whatever job is next, so a
    shared queue would eventually hand the torch-free ingest worker an embed
    job and kill it on `import torch`."""
    assert ML_QUEUE_NAME != QUEUE_NAME
    assert get_ml_queue.__module__ == "systems.queue"


def test_ingest_and_check_skip_when_one_is_already_queued():
    """Both spend the scarcest resource in the project, so two running at once
    wastes eBay quota and races on the same rows. Embedding and the deal scan
    already skipped; these two did not, which was an inconsistency rather than
    a decision."""

    class Pending:
        def __init__(self, name):
            self.func_name = name

    queue = FakeQueue()
    queue.jobs = [Pending("connectors.ingest_ebay.ingest_all")]
    assert enqueue_ingest_all(queue=queue) is None
    assert queue.enqueued == []

    queue = FakeQueue()
    queue.jobs = [Pending("connectors.disappearance_check.check_all_sources")]
    assert enqueue_disappearance_check(queue=queue) is None
    assert queue.enqueued == []
