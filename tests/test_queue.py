from connectors.disappearance_check import check_all_sources
from connectors.ingest_ebay import ingest_all
from systems.queue import enqueue_disappearance_check, enqueue_ingest_all


class FakeQueue:
    """Stands in for rq.Queue. No Redis connection, just records calls."""

    def __init__(self):
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
