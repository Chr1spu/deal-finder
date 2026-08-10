# The Python side of the stack: API, ingest worker, ML worker, scheduler.
#
# One Dockerfile, two targets, and the split is the same capability boundary
# ADR 0009 drew in the queue. `runtime` has no torch and measures 1.04 GB on
# disk (249 MB compressed); `ml-runtime` adds the ~3 GB CLIP stack on top of
# that. Verified torch-free by importing it: building one image for everything
# would put 3 GB of CUDA libraries into the API and the scheduler to serve a
# job neither of them can ever be handed, and would quietly undo the reason
# the second queue exists.
#
#   docker compose -f infra/docker-compose.yml up -d
#
# Builds from the repo root as context (see the compose file), because uv
# needs pyproject.toml and uv.lock and the code sits beside them.

FROM python:3.12-slim AS base

# uv, pinned. Copying the binary out of the official image is the documented
# install and avoids a curl-to-shell in a build.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /bin/

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependencies before source, so editing a Python file does not invalidate the
# dependency layer. `--no-dev` keeps pytest and mypy out of the runtime image.
COPY pyproject.toml uv.lock ./


# ---------------------------------------------------------------- runtime
# API, ingest worker, scheduler. Deliberately torch-free: a plain `uv sync`
# does not pull the ml group, which is exactly the property pyproject.toml's
# default-groups comment is protecting.
FROM base AS runtime

RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY alembic ./alembic
COPY api ./api
COPY connectors ./connectors
COPY ml ./ml
COPY systems ./systems

# Not root. Nothing here needs it, and the containers mount nothing writable.
RUN useradd --system --create-home undercut && chown -R undercut:undercut /app
USER undercut

# No --worker-class here, unlike the Windows dev instructions in the README.
# WindowsWorker exists because rq's default worker forks and Windows cannot;
# inside this image it can, so the default worker is correct and the override
# would only cost the isolation forking gives.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]


# ------------------------------------------------------------- ml-runtime
# The embedding worker only. Same source, one extra dependency group.
FROM base AS ml-runtime

# Two syncs rather than one, so the enormous torch layer is cached separately
# from everything else and a change to the ordinary dependencies does not
# re-download 3 GB.
RUN uv sync --frozen --no-dev --no-install-project
RUN uv sync --frozen --no-dev --no-install-project --group ml

COPY alembic.ini ./
COPY alembic ./alembic
COPY api ./api
COPY connectors ./connectors
COPY ml ./ml
COPY systems ./systems

RUN useradd --system --create-home undercut && chown -R undercut:undercut /app
USER undercut

# The model is downloaded on first use and cached in the home directory. Left
# as a named volume in compose rather than baked in, so the image does not
# carry model weights and a rebuild does not re-download them.
CMD ["python", "-m", "systems.rqworker", "ml"]
