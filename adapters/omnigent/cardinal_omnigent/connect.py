"""cardinal-connect for omnigent server operators.

Runs Cardinal's device-code consent flow (core deviceflow, client_id
"cardinal-omnigent-policy"), verifies the minted ingest credential, then
wires the omnigent server:

1. Writes core AgentPaths state files under `--state-dir` (default
   ~/.omnigent): cardinal.json (endpoint, org, limits status_url) +
   cardinal-secrets.json (ingest key, 0600) — the files the spend_limits
   verdict machinery reads.
2. Merges a cardinalManaged-tagged block into the omnigent server
   config.yaml: the `policy_modules:` registration plus the `cardinal:`
   config block both policies consume.

The YAML merge is marker-delimited text (stdlib only — no YAML parser is
vendored): everything between the BEGIN/END cardinalManaged markers is
ours to rewrite; foreign content is preserved byte-for-byte. If the
operator's config already declares `policy_modules:` outside our block we
do NOT emit a duplicate top-level key (YAML parsers would let one clobber
the other) — we print the one line to add by hand instead.

Labels convention (document to every team you onboard): omnigent agent
specs / operator config should stamp `cardinal.repo` and `cardinal.branch`
into session labels — policies never see the workspace, so labels are the
initiative-attribution channel. Unlabeled sessions roll up as
initiative=None, type=research.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cardinal_core import deviceflow
from cardinal_core.paths import AgentPaths, atomic_write, atomic_write_secret, backup

CLIENT_ID = "cardinal-omnigent-policy"
DEFAULT_HOST = "https://app.cardinalhq.io"
DEFAULT_STATE_DIR = "~/.omnigent"

BEGIN_MARKER = "# BEGIN cardinalManaged: cardinal-omnigent-policy"
END_MARKER = "# END cardinalManaged: cardinal-omnigent-policy"

_POLICY_MODULES_RE = re.compile(r"(?m)^policy_modules\s*:")


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return json.dumps(str(value))  # JSON string quoting is valid YAML


def render_block(cardinal_cfg: dict[str, Any], include_policy_modules: bool) -> str:
    """The managed config.yaml span: policy_modules registration (unless
    the operator already has the key) + the cardinal: block."""
    lines = [BEGIN_MARKER]
    if include_policy_modules:
        lines += ["policy_modules:", "  - cardinal_omnigent"]
    lines.append("cardinal:")
    for key, value in cardinal_cfg.items():
        if value is None or value == "":
            continue
        lines.append(f"  {key}: {_yaml_scalar(value)}")
    lines.append(END_MARKER)
    return "\n".join(lines) + "\n"


def strip_managed_block(text: str) -> str:
    """Remove a previous managed span (markers inclusive). Unlike the codex
    TOML writer, the span is wholly ours — omnigent does not rewrite its
    own config file — so marker-delimited removal is safe."""
    begin = text.find(BEGIN_MARKER)
    if begin < 0:
        return text
    end = text.find(END_MARKER, begin)
    if end < 0:
        # Torn block (END lost): drop from BEGIN to end-of-file rather than
        # leave half a stale block behind.
        return text[:begin].rstrip("\n") + "\n" if text[:begin].strip() else ""
    end += len(END_MARKER)
    remainder = text[:begin] + text[end:].lstrip("\n")
    return remainder


def merge_config_text(existing: str, cardinal_cfg: dict[str, Any]) -> tuple[str, bool]:
    """(new config.yaml text, policy_modules_included). Re-running is
    idempotent: the previous managed block is replaced in place-ish
    (stripped, block appended)."""
    base = strip_managed_block(existing).rstrip("\n")
    has_foreign_policy_modules = bool(_POLICY_MODULES_RE.search(base))
    include = not has_foreign_policy_modules
    prefix = base + "\n\n" if base.strip() else ""
    return prefix + render_block(cardinal_cfg, include), include


def write_server_config(config_path: Path, cardinal_cfg: dict[str, Any]) -> bool:
    """Merge the managed block into config.yaml (backup first). Returns
    whether the policy_modules registration was included."""
    existing = config_path.read_text() if config_path.exists() else ""
    merged, included = merge_config_text(existing, cardinal_cfg)
    backup(config_path)
    atomic_write(config_path, merged)
    return included


def write_state_files(state_dir: Path, bundle: dict[str, Any], host: str) -> AgentPaths:
    """The AgentPaths state layout the spend_limits verdict machinery
    reads: cardinal.json (facts) + cardinal-secrets.json (key, 0600)."""
    paths = AgentPaths(home=state_dir)
    org = bundle.get("org") or {}
    user = bundle.get("user") or {}
    ingest = bundle.get("ingest") or {}
    state: dict[str, Any] = {
        "connected_at": datetime.now(timezone.utc).isoformat(),
        "host": host,
        "deployment_environment": deviceflow.derive_deployment_env(host),
        "org_id": org.get("id"),
        "org_slug": org.get("slug"),
        "user_email": user.get("email"),
        "ingest_endpoint": ingest.get("endpoint"),
        "plugin_version": _plugin_version(),
    }
    limits = bundle.get("limits")
    if isinstance(limits, dict) and limits.get("status_url"):
        state["limits"] = {
            "status_url": limits["status_url"],
            "enabled": bool(limits.get("enabled", True)),
        }
    atomic_write(paths.state_path, json.dumps(state, indent=2) + "\n")
    atomic_write_secret(paths.secrets_path, json.dumps({
        "ingest_api_key": ingest.get("api_key"),
        "ingest_api_header": ingest.get("api_header") or "x-cardinalhq-api-key",
    }, indent=2) + "\n")
    return paths


def _plugin_version() -> str:
    try:
        from . import PLUGIN_VERSION
        return PLUGIN_VERSION
    except Exception:
        return "unknown"


def cardinal_config_from_bundle(
    bundle: dict[str, Any], host: str, state_dir: str
) -> dict[str, Any]:
    """The `cardinal:` block written into config.yaml — the config both
    policies receive. user_email deliberately NOT here: identity is
    per-event from actor.run_as."""
    org = bundle.get("org") or {}
    ingest = bundle.get("ingest") or {}
    return {
        "ingest_endpoint": ingest.get("endpoint"),
        "ingest_api_key": ingest.get("api_key"),
        "ingest_api_header": ingest.get("api_header") or "x-cardinalhq-api-key",
        "deployment_environment": deviceflow.derive_deployment_env(host),
        "org": org.get("slug") or org.get("id"),
        "state_dir": state_dir,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m cardinal_omnigent.connect",
        description="Connect an omnigent server to Cardinal telemetry + spend limits.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--config", required=True,
        help="Path to the omnigent server config.yaml to merge into.",
    )
    parser.add_argument("--state-dir", default=DEFAULT_STATE_DIR)
    args = parser.parse_args(argv)

    try:
        grant = deviceflow.start_device_code(args.host, ["ingest:write"], CLIENT_ID)
        print(f"Open {grant.get('verification_uri')} and enter code: {grant.get('user_code')}")
        bundle = deviceflow.poll_device_token(
            args.host, grant["device_code"], CLIENT_ID,
            grant.get("interval", deviceflow.DEFAULT_POLL_INTERVAL_SECS),
            grant.get("expires_in", deviceflow.DEFAULT_POLL_TIMEOUT_SECS),
        )
    except deviceflow.DeviceFlowError as exc:
        print(f"cardinal-connect failed: {exc}", file=sys.stderr)
        return 1

    ingest = bundle.get("ingest")
    if not ingest:
        print("Cardinal did not return an ingest credential.", file=sys.stderr)
        return 1
    ok, msg = deviceflow.verify_ingest_reachable(ingest)
    if not ok:
        print(f"ingest endpoint not reachable: {msg}", file=sys.stderr)
        return 1

    state_dir = str(Path(args.state_dir).expanduser())
    write_state_files(Path(state_dir), bundle, args.host)
    cardinal_cfg = cardinal_config_from_bundle(bundle, args.host, state_dir)
    included = write_server_config(Path(args.config).expanduser(), cardinal_cfg)

    user = bundle.get("user") or {}
    org = bundle.get("org") or {}
    print(f"Connected as {user.get('email') or 'unknown'} ({org.get('slug') or org.get('id')})")
    print(f"Managed block written to {args.config}")
    if not included:
        print(
            "NOTE: your config already declares policy_modules; add this "
            "entry to it by hand:\n  - cardinal_omnigent"
        )
    print(
        "Labels convention: stamp cardinal.repo + cardinal.branch into "
        "session labels for initiative attribution (unlabeled sessions "
        "roll up as research)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
