"""The worker launcher must not be able to disagree with the code it runs.

A worker listening to the wrong queue is invisible from both sides: an idle
worker is healthy and an unconsumed queue is healthy, so nothing errors. That
combination took intake down for thirteen hours on 2026-08-07, caused by a
queue name written as a literal on a command line. These tests exist to keep
the name derived rather than typed.
"""

from systems.queue import ML_QUEUE_NAME, QUEUE_NAME
from systems.rqworker import queue_for


def test_the_queue_names_come_from_the_code_not_a_literal():
    assert queue_for("main") == QUEUE_NAME
    assert queue_for("ml") == ML_QUEUE_NAME


def test_an_unrecognised_argument_falls_back_to_the_main_queue():
    """Never to a made-up name, which would be the silent failure again."""
    assert queue_for("") == QUEUE_NAME
    assert queue_for("nonsense") == QUEUE_NAME


def test_the_ml_queue_has_several_spellings_because_the_job_does():
    for spelling in ("ml", "ML", "embed", "embeddings"):
        assert queue_for(spelling) == ML_QUEUE_NAME


def test_the_two_queues_are_never_the_same_queue():
    """The split is a capability boundary: the ingest worker has no torch."""
    assert QUEUE_NAME != ML_QUEUE_NAME
