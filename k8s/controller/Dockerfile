# syntax=docker/dockerfile:1.7
#
# sentinel-controller image.
#
# Published as ghcr.io/cardinalhq/sentinel-controller:v<X.Y.Z> and
# installed on-cluster by the Helm chart at k8s/chart/. Watches Sentinel
# CRs (sentinels.cardinalhq.io/v1alpha1) and reconciles each into a Job or
# CronJob per docs/specs/sentinel-crd.md.
#
# Base: python:3.12-slim — matches the executor image (CORE.md's
# function-node runtime commitment) and keeps the kopf + kubernetes-py
# wheels on a stable interpreter.
#
# Build context is k8s/controller/. Unlike the executor image, the
# controller has no import into common/ or spike/, so a narrow context
# is enough.
#
#   docker build -f k8s/controller/Dockerfile -t sentinel-controller:dev k8s/controller

FROM python:3.12-slim AS runtime

# Runtime deps: git for the git-clone cache the handlers use to fetch a
# Sentinel directory's own sentinel.yaml at reconcile time; ca-certificates
# for HTTPS clones + API-server talk.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first so requirements.txt changes invalidate only
# this layer, not the source COPY below.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt \
 && pip install --no-cache-dir 'kopf>=1.37' 'kubernetes>=29.0'

# Source.
COPY reconciler.py projections.py capabilities.py handlers.py /app/

# kopf cache dir the handlers use for git clones (see handlers.py's
# CACHE_DIR = "/var/cache/sentinel-controller"). Pre-create so a
# read-only rootfs deployment can still mount an emptyDir on top of it.
RUN mkdir -p /var/cache/sentinel-controller

# The Helm chart may override with --standalone or --namespace flags via
# args:. Default is cluster-wide, kopf's own leader-election disabled
# because Phase (b) is single-replica (see docs/specs/sentinel-crd.md
# "What (b) explicitly defers to (c)").
ENTRYPOINT ["kopf", "run", "--standalone", "/app/handlers.py"]
