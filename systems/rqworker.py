"""Start an RQ worker on one of this project's queues.

    python -m systems.rqworker main   # ingest, disappearance check, deal scan
    python -m systems.rqworker ml     # embeddings, needs --group ml

Three things this exists for, all of them scars.

1. `rq.exe` will not exec under nohup on this machine (the same Windows
   Application Control shape that blocks rollup's native binary), so the
   console script is invoked in-process instead of as a subprocess.

2. **The queue name is read from `systems.queue`, never passed as a literal.**
   Passing literals is what caused a thirteen-hour intake outage on
   2026-08-07: the queues were renamed `deal-finder` to `undercut`, the
   workers were restarted from a command line copied off the old processes,
   and they sat listening to a queue nothing published to while jobs piled up
   unprocessed in the real one. Neither side errors in that situation. An idle
   worker and an unconsumed queue are both healthy states, which is exactly
   what made it silent.

3. It lives in the repo. The version this replaces sat in a temporary
   scratchpad directory belonging to a since-finished session, under
   AppData\\Local\\Temp, which is not a location that survives cleanup: the
   running workers depended on a file nothing in the project referenced and
   nothing guaranteed to still exist. A launcher is part of the system it
   launches.

Redis comes from `api.settings` for the same reason the queue name comes from
`systems.queue`: one copy of each fact.
"""

from __future__ import annotations

import sys

from rq.cli import main

from api.settings import settings
from systems.queue import ML_QUEUE_NAME, QUEUE_NAME

MAIN = "main"
ML = "ml"


def queue_for(which: str) -> str:
    return ML_QUEUE_NAME if which.lower() in (ML, "embed", "embeddings") else QUEUE_NAME


def run(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    queue_name = queue_for(argv[0] if argv else MAIN)

    print(f"starting worker on queue {queue_name!r}", flush=True)

    # rq's CLI reads sys.argv rather than taking arguments, so this is the
    # supported way to drive it in-process.
    sys.argv = [
        "rq",
        "worker",
        queue_name,
        # SimpleWorker with a threading-based death penalty. rq's default
        # worker forks, which Windows cannot do. Drop this once the workers
        # run inside Linux containers.
        "--worker-class",
        "systems.queue.WindowsWorker",
        "--url",
        settings.redis_url,
    ]
    main()


if __name__ == "__main__":
    run()
