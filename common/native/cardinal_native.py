#!/usr/bin/env python3
"""Shared Python boundary for the OpenCode and Pi packages.

The JS surfaces send normalized metadata, never prompts or tool output. Each
invocation handles a batch under a per-session lock; credentials stay on disk
and Cardinal's existing core owns the wire format and initiative semantics.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from datetime import datetime, timezone

from cardinal_core import bashclass, deviceflow, initiative, otlp, session
from cardinal_core.paths import AgentPaths, atomic_write_json, atomic_write_secret


def agent_paths(runtime: str) -> AgentPaths:
    override = os.environ.get(f"CARDINAL_{runtime.upper()}_HOME")
    if override:
        return AgentPaths(Path(override).expanduser())
    if runtime == "opencode":
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return AgentPaths(base / "opencode")
    return AgentPaths(Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent"))


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def records_for_event(runtime, paths, event, progress):
    """Translate the deliberately small, versioned JS -> Python contract."""
    sid, kind = event["session_id"], event["kind"]
    ts = otlp.parse_ts_ns(event.get("timestamp"), time.time_ns())
    base = {"session_id": sid, "agent_runtime": runtime,
            "user_email": paths.read_state().get("user_email"),
            "parent_session_id": event.get("parent_session_id")}
    records = []
    if kind == "user":
        session.begin_user_turn(progress)
    if kind in ("session", "user"):
        cwd = event.get("cwd")
        if isinstance(cwd, str) and cwd:
            head = initiative.git(["rev-parse", "HEAD"], cwd)
            if head:
                branch = initiative.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
                remote = initiative.git(["remote", "get-url", "origin"], cwd)
                name, category = initiative.resolve_initiative(branch)
                records.append(otlp.log_record("cardinal.git_state", {
                    **base, "cardinal_cwd": cwd, "cardinal_head_sha": head,
                    "cardinal_branch": branch, "cardinal_remote_url": remote,
                    "cardinal_repo": initiative.canonical_repo(remote),
                    "cardinal_initiative_name": name, "cardinal_initiative_type": category,
                }, ts))
    model_id = event.get("model_call_id")
    calls = progress.setdefault("model_calls", {})
    if kind in ("model_start", "model", "tool") and model_id and model_id not in calls:
        session.end_model_call(progress)
        calls[model_id] = [progress["user_turn_seq"], progress["turn_seq"], 0]
        if len(calls) > 2048:
            del calls[next(iter(calls))]
    seq = calls.get(model_id, [progress["user_turn_seq"], progress["turn_seq"], progress["tool_seq"]])
    ordered = {**base, "user_turn_seq": seq[0], "turn_seq": seq[1], "ts": ts}
    if kind == "model":
        usage = event.get("usage") or {}
        attrs = {**ordered, "model": event.get("model"), "provider": event.get("provider"),
                 "message_id": event.get("event_id"), "usage_granularity": "model_call"}
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens", "reasoning_tokens", "cost_usd"):
            attrs[key] = number(usage.get(key))
        if attrs["cost_usd"] is not None:
            attrs["cost_source"] = "runtime"
        # Missing usage is unknown, not a zero-token request.
        if attrs["model"] and any(attrs[k] is not None for k in ("input_tokens", "output_tokens")):
            records.extend(otlp.log_record(name, attrs, ts) for name in ("api_request", "cardinal.turn_usage"))
    if kind == "tool" and event.get("tool_name"):
        seq[2] += 1
        progress["tool_seq"] = seq[2]
        attrs = {**ordered, "tool_seq": seq[2], "tool_name": event["tool_name"],
                 "tool_call_id": event.get("event_id"), "success": event.get("success") is True,
                 "duration_ms": number(event.get("duration_ms")),
                 "mcp_server_name": event.get("mcp_server_name"),
                 "mcp_tool_name": event.get("mcp_tool_name")}
        if event["tool_name"].lower() in ("bash", "shell") and isinstance(event.get("command"), str):
            classification = bashclass.classify_bash_command(event["command"])
            if classification:
                attrs["bash_class"], attrs["bash_multi"] = classification
        records.extend(otlp.log_record(name, attrs, ts) for name in ("cardinal.turn_tool", "tool_result"))
    return records


def telemetry(runtime, paths, events, version):
    conn = otlp.connection_from_paths(paths)
    if conn is None:
        return
    resource_state = paths.read_state()
    resource = otlp.resource_attrs(service_name=runtime, agent_runtime=runtime,
        deployment_environment=resource_state.get("deployment_environment"),
        user_email=resource_state.get("user_email"),
        org=resource_state.get("org_slug") or resource_state.get("org_id"), plugin_version=version)
    # Advisory file locks cover multiple OpenCode plugin instances or Pi processes.
    import fcntl
    batch = []
    for event in events:
        if not isinstance(event, dict) or event.get("kind") not in ("session", "user", "model_start", "model", "tool"):
            continue
        sid, eid = event.get("session_id"), event.get("event_id")
        if not isinstance(sid, str) or not sid or not isinstance(eid, str) or not eid:
            continue
        digest = hashlib.sha256(sid.encode()).hexdigest()
        paths.telemetry_dir.mkdir(parents=True, exist_ok=True)
        with (paths.telemetry_dir / (digest + ".lock")).open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            progress = session.load_progress(paths, digest)
            seen = progress.setdefault("seen", [])
            key = event["kind"] + ":" + eid
            if key in seen:
                continue
            records = records_for_event(runtime, paths, event, progress)
            # Best-effort, at-most-once telemetry matches the existing adapters.
            batch.extend(records)
            seen.append(key)
            progress["seen"] = seen[-4096:]
            session.save_progress(paths, digest, progress)
    otlp.emit_records(batch, conn, resource, scope_name=f"cardinal-{runtime}-plugin", scope_version=version)


def save_bundle(paths, bundle, host, runtime, version, deployment_env):
    ingest, mcp = bundle.get("ingest") or {}, bundle.get("mcp") or {}
    stamp = datetime.now(timezone.utc).isoformat()
    atomic_write_secret(paths.secrets_path, json.dumps({
        "ingest_api_key": ingest.get("api_key"),
        "ingest_api_header": ingest.get("api_header") or otlp.DEFAULT_API_HEADER,
        "mcp_api_key": mcp.get("api_key"), "written_at": stamp,
    }, indent=2) + "\n")
    state = {
        "schema_version": 1, "runtime": runtime, "host": host,
        "plugin_version": version, "written_at": stamp,
        "org_id": (bundle.get("org") or {}).get("id"),
        "org_slug": (bundle.get("org") or {}).get("slug"),
        "user_email": (bundle.get("user") or {}).get("email"),
        "deployment_environment": deployment_env or deviceflow.derive_deployment_env(host),
        "ingest_endpoint": ingest.get("endpoint"), "ingest_key_id": ingest.get("key_id"),
        "mcp_url": mcp.get("url"), "mcp_key_id": mcp.get("key_id"),
        "mode": "telemetry-and-mcp" if mcp else "telemetry-only",
        "telemetry": {"enabled": True},
    }
    atomic_write_json(paths.state_path, state)


def connect(args, paths):
    if paths.state_path.exists():
        raise ValueError("Cardinal is already connected. Disconnect before connecting another account.")
    scopes = ["ingest:write"] + ([] if args.telemetry_only else ["mcp:invoke"])
    client = f"cardinal-{args.runtime}-plugin"
    grant = deviceflow.start_device_code(args.host, scopes, client)
    print(f"Approve Cardinal for {args.runtime}: {grant.get('verification_uri', '')}", flush=True)
    if grant.get("user_code"):
        print(f"Code: {grant['user_code']}", flush=True)
    atomic_write_json(paths.pending_path, {k: grant[k] for k in ("verification_uri", "user_code", "expires_in") if k in grant})
    try:
        bundle = deviceflow.poll_device_token(args.host, grant["device_code"], client,
            int(grant.get("interval") or 5), int(grant.get("expires_in") or 600))
        ingest, mcp = bundle.get("ingest") or {}, bundle.get("mcp") or {}
        if not ingest.get("endpoint") or not ingest.get("api_key"):
            raise ValueError("Cardinal did not return an ingest credential.")
        if not args.telemetry_only and (not mcp.get("url") or not mcp.get("api_key")):
            raise ValueError("Cardinal did not return an MCP credential.")
        # Persist minted credentials before probing so a failed probe is recoverable
        # with status/disconnect rather than leaving an untracked live credential.
        save_bundle(paths, bundle, args.host, args.runtime, args.version, args.deployment_env)
        result = status(paths)
        print(f"Restart {args.runtime} to load the connection.")
        return result
    finally:
        paths.pending_path.unlink(missing_ok=True)


def status(paths):
    state, secrets = paths.read_state(), paths.read_secrets()
    if not state or not secrets.get("ingest_api_key"):
        print("Cardinal is not connected.")
        return 1
    print(f"Connected as {state.get('user_email') or 'unknown'} ({state.get('org_slug') or state.get('org_id') or 'unknown'})")
    ok, msg = deviceflow.verify_ingest_reachable({"endpoint": state.get("ingest_endpoint"),
        "api_key": secrets.get("ingest_api_key"), "api_header": secrets.get("ingest_api_header")})
    print(f"Telemetry: {'reachable' if ok else 'unreachable'} ({msg})")
    if state.get("mcp_url"):
        mcp_ok, msg = deviceflow.verify_mcp_reachable(state["mcp_url"], secrets.get("mcp_api_key"))
        print(f"MCP: {'reachable' if mcp_ok else 'unreachable'} ({msg})")
        ok = ok and mcp_ok
    print("Spend enforcement: not enabled by this adapter.")
    return 0 if ok else 1


def disconnect(args, paths):
    state, secrets = paths.read_state(), paths.read_secrets()
    if not args.local_only:
        # Ingest credentials live in a separate backend table and cannot
        # authenticate on /maestro-keys/:id/revoke. Only MCP supports self-revoke.
        for kind in ("mcp",):
            if state.get(f"{kind}_key_id"):
                ok, msg = deviceflow.revoke_maestro_key(state["host"], state[f"{kind}_key_id"], secrets.get(f"{kind}_api_key"))
                if not ok:
                    raise ValueError(f"Could not revoke {kind} key ({msg}); credentials retained for retry. Use --local-only for local removal.")
                state.pop(f"{kind}_key_id")
                atomic_write_json(paths.state_path, state)
    if state.get("ingest_key_id") or (args.local_only and state.get("mcp_key_id")):
        receipt = {k: state[k] for k in ("host", "org_id", "ingest_key_id", "mcp_key_id") if state.get(k)}
        atomic_write_json(paths.home / "cardinal-revocations.json", receipt)
        print(f"Revoke the remaining keys in {state.get('host', '')}/settings/api-keys.")
        print(f"Key IDs saved in {paths.home / 'cardinal-revocations.json'} (no credentials).")
    for path in (paths.state_path, paths.secrets_path, paths.pending_path):
        path.unlink(missing_ok=True)
    print(f"Disconnected Cardinal. Restart {args.runtime} to unload any MCP connection.")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Cardinal native agent integration")
    parser.add_argument("--runtime", choices=("opencode", "pi"), required=True)
    parser.add_argument("--version", default="0.1.0")
    sub = parser.add_subparsers(dest="command", required=True)
    conn = sub.add_parser("connect", help="Connect through Cardinal browser consent")
    conn.add_argument("--host", default="https://app.cardinalhq.io")
    conn.add_argument("--telemetry-only", action="store_true")
    conn.add_argument("--deployment-env")
    sub.add_parser("status", help="Probe the saved telemetry and MCP endpoints")
    disc = sub.add_parser("disconnect", help="Revoke credentials and remove the local connection")
    disc.add_argument("--local-only", action="store_true")
    sub.add_parser("telemetry", help=argparse.SUPPRESS)
    args = parser.parse_args()
    paths = agent_paths(args.runtime)
    try:
        if args.command == "telemetry":
            payload = json.load(sys.stdin)
            if isinstance(payload, list):
                telemetry(args.runtime, paths, payload[:256], args.version)
            return 0
        if args.command == "connect":
            return connect(args, paths)
        if args.command == "disconnect":
            return disconnect(args, paths)
        return status(paths)
    except Exception as exc:
        if args.command != "telemetry":
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
