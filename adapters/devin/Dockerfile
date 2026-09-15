# syntax=docker/dockerfile:1
#
# ghcr.io/cardinalhq/cardinal-devin-poller
#
# Build from the repository root (the image vendors core/cardinal_core):
#
#   docker build -f adapters/devin/Dockerfile -t cardinal-devin-poller .
#
# adapters/devin/Dockerfile.dockerignore keeps the context to the files
# copied below (BuildKit reads a <Dockerfile>.dockerignore next to the
# Dockerfile ahead of any root .dockerignore).
#
# Layout matches the release tarball (build/devin.py): cardinal_core/ sits
# next to cardinal_devin/, which is where bin/cardinal-devin looks first.
# Standard library only, so no pip install.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CARDINAL_DEVIN_HOME=/data \
    PATH=/opt/cardinal-devin/bin:$PATH

# Non-root. /data holds cardinal.json (if `connect` is used) and the poll
# state; it is created owned by the runtime user so a fresh named volume
# inherits that ownership. On Kubernetes set fsGroup: 10001.
RUN groupadd --system --gid 10001 cardinal \
 && useradd --system --uid 10001 --gid cardinal --home-dir /data --no-create-home \
      --shell /usr/sbin/nologin cardinal \
 && mkdir -p /data \
 && chown cardinal:cardinal /data \
 && chmod 0700 /data

WORKDIR /opt/cardinal-devin
COPY LICENSE adapters/devin/README.md ./
COPY core/cardinal_core/ cardinal_core/
COPY adapters/devin/playbook/ playbook/
COPY adapters/devin/cardinal_devin/ cardinal_devin/
COPY --chmod=0755 adapters/devin/bin/cardinal-devin bin/cardinal-devin

USER 10001:10001
WORKDIR /data
VOLUME /data

# Logs go to stderr; `poll` exits non-zero on a configuration error
# (missing Devin key, partial CARDINAL_INGEST_* pair, unreadable state).
ENTRYPOINT ["cardinal-devin"]
CMD ["poll", "--interval", "300"]
