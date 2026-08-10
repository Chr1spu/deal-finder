"""Answer "is the scheduler actually working" without inspecting the data.

Every previous way of asking this was wrong in a way that mattered:

- **Process names lie.** `uv run` wrappers mean one logical scheduler shows up
  as several processes, and a stale one from a different venv is
  indistinguishable from the current one at a glance. A 48-hour-old duplicate
  scheduler ran unnoticed on 2026-08-07, quietly doubling eBay quota use.
- **A resident process is not a running loop.** The scheduler had no exception
  handling, so any error ended all scheduling while the process could stay
  resident. `ps` said healthy; nothing had been enqueued for nine hours.
- **Missing rows in Postgres is a lagging indicator.** It is how the last three
  outages were found, hours after the fact.

The heartbeat is the authoritative signal, for the same reason the worker
health check uses `redis.ttl(worker.key)` rather than heartbeat age: the key is
written with a TTL, so its mere existence is a liveness claim that expires on
its own if the writer stops.

Run: `python -m systems.scheduler_health`
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from systems.queue import get_redis
from systems.scheduler import HEARTBEAT_KEY


def read_heartbeat() -> dict | None:
    """The scheduler's last published state, or None if it is not running."""
    raw = get_redis().get(HEARTBEAT_KEY)
    # redis-py's stubs allow an awaitable here for the async client; this is the
    # sync one, so narrow rather than cast and stay honest about the shape.
    if not isinstance(raw, str | bytes | bytearray):
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def describe() -> str:
    state = read_heartbeat()
    if state is None:
        return (
            "SCHEDULER DOWN: no heartbeat in Redis.\n"
            "  The key expires on its own, so this means the loop is not running,\n"
            "  whatever the process list says. Restart it with:\n"
            "    python -u -m systems.scheduler"
        )

    now = datetime.now(UTC)
    beat_at = datetime.fromisoformat(state["at"])
    age = (now - beat_at).total_seconds()
    errors = state.get("consecutive_errors", 0)

    lines = [f"SCHEDULER UP: heartbeat {age:.0f}s old"]
    if errors:
        # Isolated failures keep the loop alive, which is the point, but a
        # rising count means every enqueue is failing and nothing is running.
        lines.append(f"  WARNING: {errors} consecutive enqueue failures")

    for name, iso in sorted((state.get("last_run") or {}).items()):
        if iso is None:
            lines.append(f"  {name:<20} never run this session")
            continue
        ago = (now - datetime.fromisoformat(iso)).total_seconds()
        lines.append(f"  {name:<20} last enqueued {ago / 60:.0f} min ago")
    return "\n".join(lines)


if __name__ == "__main__":
    print(describe())
