"""Where the Cardinal ingest credential comes from.

Two sources, environment first:

- `CARDINAL_INGEST_ENDPOINT` + `CARDINAL_INGEST_API_KEY` (containers,
  Kubernetes Secrets; no interactive `connect`). `CARDINAL_ORG` and
  `CARDINAL_DEPLOYMENT_ENV` fill the `cardinal.org` and
  `deployment.environment` resource attributes that `connect` would
  otherwise have stored. Setting only one of the two credential variables
  is an error rather than a silent fall back to connect state.
- The connection `connect` stored under the state dir
  (`cardinal_core.otlp.connection_from_paths`).

Both produce the same `IngestConnection`: base endpoint (no trailing
slash, `/v1/logs` appended at send time) and the key in the
`x-cardinalhq-api-key` header. The key is never logged or printed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit

from cardinal_core import otlp
from cardinal_core.paths import AgentPaths

log = logging.getLogger(__name__)

ENV_ENDPOINT = "CARDINAL_INGEST_ENDPOINT"
ENV_API_KEY = "CARDINAL_INGEST_API_KEY"
ENV_ORG = "CARDINAL_ORG"
ENV_DEPLOYMENT_ENV = "CARDINAL_DEPLOYMENT_ENV"

SOURCE_ENV = "environment"
SOURCE_STATE = "connect state"


class IngestConfigError(ValueError):
    pass


@dataclass(frozen=True)
class IngestConfig:
    connection: Optional[otlp.IngestConnection]
    # Resource facts in the shape `connect` writes to cardinal.json
    # (org_slug, deployment_environment), read by telemetry.resource().
    connection_state: Dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None


def _get(environ: Mapping[str, str], name: str) -> str:
    return (environ.get(name) or "").strip()


def from_env(environ: Optional[Mapping[str, str]] = None) -> Optional[IngestConfig]:
    """The environment credential, None when neither variable is set."""
    environ = os.environ if environ is None else environ
    endpoint, key = _get(environ, ENV_ENDPOINT), _get(environ, ENV_API_KEY)
    if not endpoint and not key:
        return None
    if not endpoint or not key:
        missing = ENV_API_KEY if endpoint else ENV_ENDPOINT
        present = ENV_ENDPOINT if endpoint else ENV_API_KEY
        raise IngestConfigError(
            f"{present} is set but {missing} is not; set both to use environment credentials, "
            "or unset both to use the connection from `cardinal-devin connect`"
        )
    parts = urlsplit(endpoint)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise IngestConfigError(
            f"{ENV_ENDPOINT} must be an http(s) URL with a host (the OTLP/HTTP base, without /v1/logs)"
        )
    if parts.query or parts.fragment:
        raise IngestConfigError(f"{ENV_ENDPOINT} must not include a query string or fragment")
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/v1/logs"):
        raise IngestConfigError(f"{ENV_ENDPOINT} is the OTLP/HTTP base URL; drop the trailing /v1/logs")
    if parts.scheme == "http":
        log.warning("%s uses plain http; the ingest key is sent unencrypted", ENV_ENDPOINT)
    state: Dict[str, Any] = {}
    if _get(environ, ENV_ORG):
        state["org_slug"] = _get(environ, ENV_ORG)
    if _get(environ, ENV_DEPLOYMENT_ENV):
        state["deployment_environment"] = _get(environ, ENV_DEPLOYMENT_ENV)
    return IngestConfig(
        connection=otlp.IngestConnection(endpoint=endpoint, api_key=key, api_header=otlp.DEFAULT_API_HEADER),
        connection_state=state,
        source=SOURCE_ENV,
    )


def resolve(paths: AgentPaths, environ: Optional[Mapping[str, str]] = None) -> IngestConfig:
    """Environment over connect state. Raises IngestConfigError for a
    partial or malformed environment credential."""
    env = from_env(environ)
    if env is not None:
        return env
    connection = otlp.connection_from_paths(paths)
    return IngestConfig(
        connection=connection,
        connection_state=paths.read_state(),
        source=SOURCE_STATE if connection is not None else None,
    )


def resource_env_ignored(environ: Optional[Mapping[str, str]] = None) -> list:
    """CARDINAL_ORG / CARDINAL_DEPLOYMENT_ENV set without environment
    credentials: they only apply alongside them."""
    environ = os.environ if environ is None else environ
    return [name for name in (ENV_ORG, ENV_DEPLOYMENT_ENV) if _get(environ, name)]
