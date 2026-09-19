# syntax=docker/dockerfile:1
FROM python:3.14-slim-bookworm

# uv is build-time only -- nothing at runtime calls it, so take the binary
# rather than basing the image on uv's. Pinned like the langchain-* packages
# in pyproject.toml: `latest` would make the build unreproducible.
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /bin/uv

WORKDIR /app

# COMPILE_BYTECODE: compile .pyc at build time. Cloud Run scales to zero, so a
#   cold start is the normal case here, not the exception, and the __pycache__
#   a container writes on first import dies with it.
# LINK_MODE: uv hardlinks from its cache by default, which cannot cross the
#   cache mount's filesystem boundary below -- it would warn once per package
#   and fall back to copying anyway. Copying outright also leaves .venv owning
#   its files rather than pointing into a cache layer the build discards.
# PYTHON_DOWNLOADS: use the interpreter this image already ships. Without it uv
#   may fetch a second Python to build the venv against, and PATH below would
#   then resolve to a different one than expected.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependency layer: rebuilt only when the lock changes, which is rare --
# source below changes every commit. .python-version is copied here so a
# mismatch with FROM fails the build instead of the deploy.
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Named one directory at a time, not `COPY . .`: what is not listed cannot
# reach the image even if .dockerignore is wrong. db/ is here for its
# migrations -- 001_init.sql is the schema's source of truth.
COPY app ./app
COPY query ./query
COPY ingest ./ingest
COPY db ./db

# Ahead of the system Python, so CMD's uvicorn resolves inside the venv.
ENV PATH="/app/.venv/bin:$PATH"

# After every COPY: as appuser those COPYs could not write /app. The files
# stay root-owned and read-only to this user on purpose -- the process has no
# reason to rewrite its own code.
#
# --create-home, though: pythainlp calls os.makedirs on $HOME/pythainlp-data at
# import time, so a user whose home does not exist crashes the process during
# startup, not on first Thai input. The home is the one writable place the
# process gets; /app stays read-only to it.
RUN useradd --system --create-home --shell /usr/sbin/nologin --uid 1000 appuser
USER appuser

# Local default; Cloud Run injects its own PORT over this. EXPOSE documents
# the port, it does not publish it.
ENV PORT=8080
EXPOSE 8080

# sh -c because exec form does not expand ${PORT}; exec so uvicorn replaces
# the shell as PID 1 and receives Cloud Run's SIGTERM itself.
#
# --workers 1 is correctness, not tuning: app/main.py counts the rate limit in
# process memory, so a second worker would be a second counter and every key
# would get double its allowance. max-instances=1 constrains instances, not
# processes.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1"]
