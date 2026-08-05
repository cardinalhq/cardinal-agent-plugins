# syntax=docker/dockerfile:1.7
#
# sentinel-executor image.
#
# Built as ghcr.io/cardinalhq/sentinel-executor:v<X.Y.Z> and referenced by
# Sentinel CRs' `spec.runtime.image` (see docs/specs/sentinel-crd.md §
# "Runtime shape"). One image, many Sentinels — the Sentinel directory
# itself is git-cloned into the pod at startup by an initContainer, never
# baked in.
#
# Base: python:3.12-slim. Chosen deliberately — matches CORE.md's
# function-node runtime commitment (Python 3.12). Do not bump to 3.14+
# without updating that contract.
#
# Build context is the REPO ROOT (not spike/executor/) so that COPY can
# reach both `spike/executor/` and `common/mechanize/` — lint.py resolves
# `_REPO_ROOT = Path(__file__).resolve().parents[2]` to import
# `common.mechanize.ratification`, so the on-disk layout must be
# preserved inside the image.
#
#   docker build -f spike/executor/Dockerfile -t sentinel-executor:dev .

FROM python:3.12-slim AS runtime

# Runtime deps: git so the initContainer's clone works if any Sentinel
# happens to shell out to git; ca-certificates for HTTPS to sinks/MCP.
# Kept minimal — no compilers, no bash tooling, no editors.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
 && rm -rf /var/lib/apt/lists/*

# Non-root user for the pod. UID 10001 is arbitrary but stable — avoids
# collisions with common host UIDs when volumes are mounted for dev.
RUN groupadd --system --gid 10001 sentinel \
 && useradd  --system --uid 10001 --gid sentinel --home /home/sentinel --create-home sentinel

WORKDIR /opt/sentinel-executor

# Copy requirements first so the pip layer caches across code changes.
COPY spike/executor/requirements.txt spike/executor/requirements.txt

RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r spike/executor/requirements.txt

# Now the source tree. The .dockerignore next to this Dockerfile lists
# scratch dirs (.venv/, __pycache__/, tests/, runs/, fixtures/) — those
# are excluded when build context is `spike/executor/`; when building
# from repo root a root-level .dockerignore should mirror the same
# patterns under `spike/executor/**`.
COPY spike/executor/ spike/executor/
COPY common/         common/

# Console-script wrapper. Small Python file dispatching subcommands to
# the right underlying module.
COPY spike/executor/bin/sentinel-executor /usr/local/bin/sentinel-executor
RUN chmod +x /usr/local/bin/sentinel-executor

# Record the image version for `kubectl describe pod` / debug shells.
COPY spike/executor/VERSION /opt/sentinel-executor/VERSION
ENV SENTINEL_EXECUTOR_VERSION_FILE=/opt/sentinel-executor/VERSION

# Sensible defaults for a container runtime — unbuffered logs surface in
# `kubectl logs` immediately, no stray .pyc files in read-only layers.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER sentinel:sentinel
WORKDIR /sentinel

# ENTRYPOINT only. No default CMD — the Sentinel CR's projected args
# (`run . --inputs ... --deployment ...` etc.) drive invocation.
ENTRYPOINT ["sentinel-executor"]
