"""The scheduler must survive an enqueue that raises.

Written after the loop went silent for nine hours on 2026-08-07. It had no
exception handling, so one transient error in any of four enqueue calls ended
all scheduling, permanently, sometimes with the process still resident so the
process list looked healthy.

These tests drive `run_forever` for a bounded number of polls by making
`time.sleep` raise a sentinel, which is the least invasive way to test an
infinite loop without adding a parameter that exists only for tests.
"""

import pytest

from systems import scheduler


class StopLoop(Exception):
    """Raised from the patched sleep to end run_forever deterministically."""


@pytest.fixture()
def drive_loop(monkeypatch):
    """Run the scheduler for `polls` iterations, then stop."""

    def run(polls: int = 1) -> None:
        remaining = {"n": polls}

        def fake_sleep(_seconds):
            remaining["n"] -= 1
            if remaining["n"] <= 0:
                raise StopLoop
        monkeypatch.setattr(scheduler.time, "sleep", fake_sleep)
        with pytest.raises(StopLoop):
            scheduler.run_forever()

    return run


@pytest.fixture(autouse=True)
def no_heartbeat(monkeypatch):
    """The heartbeat needs Redis; these tests are about the loop."""
    monkeypatch.setattr(scheduler, "_write_heartbeat", lambda state: None)


def _stub_enqueues(monkeypatch, **overrides):
    """Replace all four enqueue calls, recording which ran."""
    calls = []

    def make(name):
        def enqueue():
            calls.append(name)
            return type("Job", (), {"id": name + "-1"})()
        return enqueue

    for attr, name in (
        ("enqueue_ingest_all", "ingest"),
        ("enqueue_disappearance_check", "disappearance_check"),
        ("enqueue_embed_pending", "embed"),
        ("enqueue_deal_scan", "deal_scan"),
    ):
        monkeypatch.setattr(scheduler, attr, overrides.get(name) or make(name))
    return calls


def test_all_four_tasks_are_enqueued_on_the_first_poll(monkeypatch, drive_loop):
    calls = _stub_enqueues(monkeypatch)
    drive_loop(polls=1)
    assert sorted(calls) == ["deal_scan", "disappearance_check", "embed", "ingest"]


def test_one_failing_enqueue_does_not_stop_the_others(monkeypatch, drive_loop):
    """The nine-hour outage in one test. Before the fix, the exception escaped
    run_forever and every later task never ran again."""

    def boom():
        raise RuntimeError("redis blipped")

    calls = _stub_enqueues(monkeypatch, ingest=boom)
    drive_loop(polls=1)

    assert "ingest" not in calls, "the failing task did not enqueue"
    # ...but the other three still did, which is the entire point.
    assert sorted(calls) == ["deal_scan", "disappearance_check", "embed"]


def test_a_failed_task_is_retried_on_the_next_poll(monkeypatch, drive_loop):
    """last_run is deliberately not advanced on failure, so recovery takes one
    poll (30s) rather than a full two-hour interval."""
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        raise RuntimeError("still down")

    _stub_enqueues(monkeypatch, ingest=flaky)
    drive_loop(polls=3)

    assert attempts["n"] == 3, "retried every poll, not once per interval"


def test_a_persistently_failing_enqueue_never_kills_the_loop(monkeypatch, drive_loop):
    def boom():
        raise RuntimeError("down hard")

    _stub_enqueues(
        monkeypatch, ingest=boom, disappearance_check=boom, embed=boom, deal_scan=boom
    )
    # Reaching the sleep on poll 5 means the loop survived 20 failed enqueues.
    drive_loop(polls=5)


def test_a_skipped_enqueue_still_counts_as_run(monkeypatch, drive_loop):
    """The helpers return None when an identical job is already queued. That is
    a normal outcome, not a failure, so it must advance last_run or the
    scheduler would retry it every 30 seconds forever."""
    attempts = {"n": 0}

    def already_queued():
        attempts["n"] += 1
        return None

    _stub_enqueues(monkeypatch, ingest=already_queued)
    drive_loop(polls=3)

    assert attempts["n"] == 1, "not retried, because skipping is not failing"
