from datetime import datetime, timedelta, timezone

from systems.scheduler import due


def test_due_when_never_run():
    assert due(None, interval_seconds=3600, now=datetime.now(timezone.utc)) is True


def test_not_due_before_interval_elapses():
    now = datetime.now(timezone.utc)
    last_run = now - timedelta(seconds=10)

    assert due(last_run, interval_seconds=3600, now=now) is False


def test_due_once_interval_elapses():
    now = datetime.now(timezone.utc)
    last_run = now - timedelta(seconds=3601)

    assert due(last_run, interval_seconds=3600, now=now) is True


def test_due_exactly_at_interval_boundary():
    now = datetime.now(timezone.utc)
    last_run = now - timedelta(seconds=3600)

    assert due(last_run, interval_seconds=3600, now=now) is True
