# 0020 - Deployment topology: two images, one migration gate, exactly one scheduler

**Context:** Stage 7 asks for the full stack in Docker Compose. Until now
`infra/docker-compose.yml` started Postgres and Redis only, and the four
Python processes ran from a Windows venv, launched by hand. Every launch-time
outage this project has had came from that arrangement: a queue name copied
off an older process (thirteen hours of dead intake), a second scheduler left
running from a stale venv (48 hours of doubled eBay quota), and workers whose
imported models predated the migrations they were writing against (three
separate silent ingestion failures).

**Decision:** One Dockerfile with two targets, one one-shot migration service
that everything else waits on, and a scheduler pinned to a single replica.

- **`runtime` has no torch; `ml-runtime` adds it.** This is the same
  capability boundary ADR `0009` drew when it split the queues, carried into
  the images. A single image would put ~3 GB of CUDA libraries into the API
  and the scheduler to support a job neither can be handed.
- **`migrate` is a service, not an entrypoint step.** Four services each
  running `alembic upgrade head` at boot would race, and worse, a worker could
  start against a schema its own image predates. `depends_on:
  service_completed_successfully` makes that ordering structural instead of
  hoped for.
- **`scheduler` is `replicas: 1`, stated explicitly** even though it is the
  default, because the failure it prevents already happened and cost a day of
  quota.
- **The frontend is nginx serving the vite build, proxying `/api`.** Same
  contract as the vite dev proxy, so no fetch URL in `frontend/src/api.js`
  differs between dev and production and CORS stays a browser-extension
  concern only.
- **`API_KEY` has no default.** ADR `0017` makes an unset key refuse writes,
  so an unset key here is a working read-only deployment rather than an open
  one. The eBay credentials use `${VAR:?message}` and fail the build of the
  config instead, because a stack that starts without them looks healthy and
  ingests nothing.
- **The `dealfinder` database, user and password names stay**, despite the
  rename to Undercut. Renaming them orphans the `postgres_data` volume, and
  what is in it is months of disappearance tracking, which is the one asset
  here that cannot be regenerated on demand.

**Alternatives considered:** A single image with torch, rejected above. Running
migrations from the API's entrypoint, rejected for the race. `condition:
service_healthy` on Redis rather than `service_started`, which adds a
healthcheck for a dependency that fails loudly and immediately anyway. Baking
the CLIP weights into `ml-runtime`, rejected because a rebuild should not
re-download them and an image should not ship model weights; a named volume
does the job.

**Consequences:** `docker compose -f infra/docker-compose.yml up -d` is now
the whole stack, and the Windows venv path in `scripts/start-local.ps1`
becomes the local-development convenience rather than the only way to run
this. The `--worker-class systems.queue.WindowsWorker` override disappears
inside containers, since rq's default forking worker works on Linux and gives
better isolation.

Two things this does not solve. The images are built but the stack has not
been run end to end in containers against the real corpus, because the live
pipeline is mid-run on the host and pointing a second set of workers at the
same Redis would produce exactly the duplicate-scheduler failure this ADR
pins a replica count to prevent. And nothing here is deployed to a host yet;
this is the deployable artifact, not the deployment.
