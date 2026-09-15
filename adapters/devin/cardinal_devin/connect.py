"""`connect` and `status`.

connect runs Cardinal's device-code consent flow (cardinal_core.deviceflow),
asks only for `ingest:write`, probes the ingest endpoint, and stores the
credential the way the codex adapter does: `cardinal.json` (state) and
`cardinal-secrets.json` (0600) under the state dir. There is no MCP
server or hook config to write — Devin has no surface for either.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

from cardinal_core import deviceflow
from cardinal_core.paths import AgentPaths, atomic_write_json, atomic_write_secret

from . import __version__, ingest
from .state import STATE_FILE, PollState, StateError

CLIENT_ID = "cardinal-devin-poller"
SCOPES = ["ingest:write"]
DEFAULT_HOST = "https://app.cardinalhq.io"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_home(paths: AgentPaths) -> None:
    paths.home.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(paths.home, 0o700)
    except OSError:
        pass


def run_connect(
    paths: AgentPaths,
    *,
    host: str = DEFAULT_HOST,
    rotate: bool = False,
    dry_run: bool = False,
    deployment_env: Optional[str] = None,
    out: Callable[[str], None] = print,
    probe_sleeps: Sequence[float] = deviceflow.INGEST_PROBE_RETRY_SLEEPS,
) -> int:
    if paths.state_path.exists() and not rotate and not dry_run:
        who = paths.read_state().get("user_email") or "this account"
        out(f"Cardinal is already connected as {who}. Re-run with --rotate to overwrite.")
        return 2

    out(f"Requesting Cardinal consent for scopes: {', '.join(SCOPES)}")
    try:
        grant = deviceflow.start_device_code(host, SCOPES, CLIENT_ID)
        if not dry_run:
            _ensure_home(paths)
            atomic_write_json(paths.pending_path, {
                "verification_uri": grant.get("verification_uri", ""),
                "user_code": grant.get("user_code", ""),
                "expires_in": grant.get("expires_in", deviceflow.DEFAULT_POLL_TIMEOUT_SECS),
                "written_at": _now(),
                "plugin_version": __version__,
            })
        try:
            return _finish(paths, grant, host, dry_run, deployment_env, out, probe_sleeps)
        finally:
            try:
                paths.pending_path.unlink()
            except FileNotFoundError:
                pass
    except deviceflow.DeviceFlowError as exc:
        out(f"error: {exc}")
        return 1


def _finish(
    paths: AgentPaths,
    grant: Dict[str, Any],
    host: str,
    dry_run: bool,
    deployment_env: Optional[str],
    out: Callable[[str], None],
    probe_sleeps: Sequence[float],
) -> int:
    device_code = grant.get("device_code")
    if not device_code:
        out("error: Cardinal returned no device code")
        return 1
    expires_in = int(grant.get("expires_in") or deviceflow.DEFAULT_POLL_TIMEOUT_SECS)
    interval = int(grant.get("interval") or deviceflow.DEFAULT_POLL_INTERVAL_SECS)
    out("")
    out("Open this URL in your browser to approve Cardinal for the Devin poller:")
    out(f"  {grant.get('verification_uri') or ''}")
    if grant.get("user_code"):
        out(f"  code: {grant['user_code']}")
    out(f"Waiting up to {max(1, expires_in // 60)} minute(s) for approval...")

    bundle = deviceflow.poll_device_token(host, device_code, CLIENT_ID, interval, expires_in)
    ingest = bundle.get("ingest")
    if not isinstance(ingest, dict) or not ingest.get("endpoint") or not ingest.get("api_key"):
        out("error: Cardinal did not return an ingest credential. Check the requested scopes.")
        return 1
    org = bundle.get("org") or {}
    user = bundle.get("user") or {}
    env = deployment_env or deviceflow.derive_deployment_env(host)

    if dry_run:
        out(json.dumps({
            "org": org,
            "user": user,
            "deployment_env": env,
            "would_write": {
                "state": str(paths.state_path),
                "secrets": str(paths.secrets_path),
                "ingest_endpoint": ingest.get("endpoint"),
            },
        }, indent=2))
        return 0

    ok, msg = deviceflow.verify_ingest_reachable(ingest, log=out, sleeps=tuple(probe_sleeps))
    if not ok:
        out(f"error: ingest reachability failed: {msg}")
        return 1
    out(f"Ingest endpoint reachable ({msg})")

    _ensure_home(paths)
    atomic_write_secret(paths.secrets_path, json.dumps({
        "schema_version": 1,
        "ingest_api_key": ingest.get("api_key"),
        "ingest_api_header": ingest.get("api_header") or "x-cardinalhq-api-key",
        "written_at": _now(),
    }, indent=2) + "\n")
    atomic_write_json(paths.state_path, {
        "schema_version": 2,
        "host": host,
        "mode": "telemetry-only",
        "org_id": org.get("id"),
        "org_slug": org.get("slug"),
        # The admin who connected. Telemetry is attributed per Devin
        # session (user.email from the session), not to this account.
        "user_email": user.get("email"),
        "deployment_environment": env,
        "plugin_version": __version__,
        "written_at": _now(),
        "secrets_path": str(paths.secrets_path),
        "telemetry": {"enabled": True},
        "ingest_endpoint": ingest.get("endpoint"),
        "ingest_key_id": ingest.get("key_id"),
        "ingest_key_prefix": str(ingest.get("api_key") or "")[:8],
    })
    out(f"Connected as {user.get('email') or 'unknown'} ({org.get('slug') or org.get('id')})")
    out(f"Telemetry endpoint: {ingest.get('endpoint')}")
    out("Next: set DEVIN_API_KEY (and DEVIN_ORG_ID for the v3 API), then run `cardinal-devin poll`.")
    return 0


def run_status(
    paths: AgentPaths,
    *,
    devin_key_source: Optional[str],
    devin_org_id: Optional[str],
    devin_base_url: str,
    api_version: str,
    github_token_set: bool,
    out: Callable[[str], None] = print,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    state = paths.read_state()
    secrets = paths.read_secrets()
    state_connected = bool(state.get("ingest_endpoint") and secrets.get("ingest_api_key"))
    out(f"State dir: {paths.home}")
    config_error = False
    try:
        env_config = ingest.from_env(environ)
    except ingest.IngestConfigError as exc:
        env_config = None
        config_error = True
        out(f"Cardinal: error: {exc}")
    connected = False
    if env_config is not None and env_config.connection is not None:
        connected = True
        facts = env_config.connection_state
        # The key itself is never printed, not even a prefix.
        out(f"Cardinal: credentials from environment ({ingest.ENV_ENDPOINT}, {ingest.ENV_API_KEY}; "
            "these take precedence over connect state)")
        out(f"  ingest endpoint: {env_config.connection.endpoint} (key from ${ingest.ENV_API_KEY}, not shown)")
        out(f"  cardinal.org: {facts.get('org_slug') or 'unknown (set ' + ingest.ENV_ORG + ')'}")
        out(f"  deployment.environment: "
            f"{facts.get('deployment_environment') or 'unknown (set ' + ingest.ENV_DEPLOYMENT_ENV + ')'}")
        if state_connected:
            out(f"  connect state in {paths.home} is ignored while these are set")
    elif state_connected and not config_error:
        connected = True
        out(f"Cardinal: connected to {state.get('host')} "
            f"(org {state.get('org_slug') or state.get('org_id')}, by {state.get('user_email') or 'unknown'})")
        out(f"  ingest endpoint: {state.get('ingest_endpoint')} (key {state.get('ingest_key_prefix') or '?'}...)")
        out(f"  deployment.environment: {state.get('deployment_environment')}")
        ignored = ingest.resource_env_ignored(environ)
        if ignored:
            out(f"  {', '.join(ignored)} ignored: only used with {ingest.ENV_ENDPOINT} and {ingest.ENV_API_KEY}")
    elif not config_error:
        out(f"Cardinal: Not connected. Run: cardinal-devin connect, "
            f"or set {ingest.ENV_ENDPOINT} and {ingest.ENV_API_KEY}")

    resolved = api_version if api_version != "auto" else ("v3" if devin_org_id else "v1")
    out(f"Devin: key {'from ' + devin_key_source if devin_key_source else 'MISSING (set DEVIN_API_KEY)'}; "
        f"API {resolved} at {devin_base_url}" + (f"; org {devin_org_id}" if devin_org_id else ""))
    out("GitHub: " + ("GITHUB_TOKEN set (PR head branch/sha resolved)" if github_token_set
                      else "no GITHUB_TOKEN (git_state carries repo + PR only)"))

    state_path = paths.runtime_dir / STATE_FILE
    try:
        poll_state = PollState(state_path, readonly=True)
    except StateError as exc:
        out(f"Poll state: {exc}")
        return 1
    sessions = poll_state.sessions
    active = sum(1 for e in sessions.values() if not e.get("terminal") and not e.get("stale"))
    last = poll_state.data.get("last_poll")
    last_text = datetime.fromtimestamp(last, timezone.utc).isoformat() if isinstance(last, int) else "never"
    out(f"Poll state: {state_path} — {len(sessions)} tracked, {active} active; last poll {last_text}")
    disabled = poll_state.data.get("v3_filter_disabled")
    if isinstance(disabled, dict):
        out(f"  v3 updated_after filter DISABLED: {disabled.get('reason')}")
    if config_error:
        return 2
    return 0 if connected and devin_key_source else 1
