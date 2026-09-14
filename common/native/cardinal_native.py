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

from cardinal_core import bashclass, decisions, deviceflow, initiative, otlp, session
from cardinal_core.paths import AgentPaths, atomic_write_json, atomic_write_secret

# Telemetry batches run in a detached child the host never awaits, but they
# hold the per-session lock and the bridge kills a child after 10s, so a cache
# miss on `gh` must stay short.
PR_TIMEOUT_SEC = 1.5
# `decision record` worst case: 4 git calls at 1s + ls-tree + gh + emit
# = 4 + 3 + 1.5 + 3 = 11.5s, well inside the bridge's 20s kill.
CLUSTER_TIMEOUT_SEC = 3.0
DECISION_EMIT_TIMEOUT_SEC = 3.0
EXIT_USAGE = 2
EXIT_OFF = 3


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


def pull_request(cwd, repo, branch, paths, timeout=PR_TIMEOUT_SEC):
    """The branch's PR as (number, url), or (None, None). Never raises."""
    try:
        return decisions.resolve_pr(cwd, repo, branch, decisions.cache_dir(paths.runtime_dir), timeout=timeout)
    except Exception:
        return None, None


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
                repo = initiative.canonical_repo(remote)
                pr_number, pr_url = pull_request(cwd, repo, branch, paths)
                records.append(otlp.log_record("cardinal.git_state", {
                    **base, "cardinal_cwd": cwd, "cardinal_head_sha": head,
                    "cardinal_branch": branch, "cardinal_remote_url": remote,
                    "cardinal_repo": repo,
                    "cardinal_initiative_name": name, "cardinal_initiative_type": category,
                    "cardinal_pr_number": pr_number, "cardinal_pr_url": pr_url,
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


def resource_for(runtime, paths, version):
    state = paths.read_state()
    return otlp.resource_attrs(service_name=runtime, agent_runtime=runtime,
        deployment_environment=state.get("deployment_environment"),
        user_email=state.get("user_email"),
        org=state.get("org_slug") or state.get("org_id"), plugin_version=version)


def telemetry(runtime, paths, events, version):
    conn = otlp.connection_from_paths(paths)
    if conn is None:
        return
    resource = resource_for(runtime, paths, version)
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


def decisions_override():
    return os.environ.get(decisions.ENABLE_ENV)


def decision_context(runtime, session_id, entries, tool=None):
    """Instructions the host puts in front of the model while capture is on."""
    if tool:
        how = (f"call the `{tool}` tool with choice (the option chosen, 2-7 words), question "
               "(what had to be settled), why (one sentence), and where they apply: alt (rejected "
               "options), by (\"user\" when the user made the call), anchor (paths or "
               "path::Symbol the decision governs), follows/refines/supersedes (earlier decision ids).")
        usage = "Use by=user when the user made the call."
    else:
        cli = Path(__file__).resolve().parent.parent / "bin" / f"cardinal-{runtime}.js"
        how = ("run one shell command:\n"
               f'node "{cli}" decision record --session {session_id} --choice "<the option chosen, 2-7 words>" '
               '--question "<what had to be settled>" --why "<one sentence>" '
               '[--alt "<rejected option>"]... [--by user] [--anchor <path>[::Symbol]]... '
               "[--follows|--refines|--supersedes <id>]")
        usage = "Use --by user when the user made the call."
    return (
        "Cardinal decision capture is on for this session. When you make a choice that "
        "constrains later work (picking between approaches, settling an open question, or "
        f"the user deciding something), record it right away: {how}\n"
        f"Record choices, not progress, findings, or tool calls. {usage} Anchor the files or "
        "symbols the decision governs. Link a decision to an earlier one when it builds on, "
        "narrows, or replaces it.\n"
        "Decisions so far this session:\n"
        f"{decisions.render_ledger(entries)}"
    )


def bounded_clusters(anchors, repo_root, head_sha, cache):
    """decisions.code_clusters with a smaller ls-tree budget (it uses load_domains' 5s default)."""
    anchor_paths = [(a["path"], a.get("kind") == "directory") for a in anchors if a.get("path") is not None]
    if not anchor_paths or not repo_root or not head_sha:
        return [], None
    loaded = decisions.load_domains(repo_root, head_sha, cache, timeout=CLUSTER_TIMEOUT_SEC)
    if loaded is None:
        return [], None
    domains, scheme = loaded
    ids = []
    for path, is_dir in anchor_paths:
        for cluster_id in decisions.match_clusters(domains, path, is_dir):
            if cluster_id not in ids:
                ids.append(cluster_id)
    return ids[:decisions.MAX_CLUSTERS], scheme


def decision_record(args, paths):
    runtime = args.runtime
    if not decisions.is_enabled(paths.runtime_dir, decisions_override()):
        print(f"Decision capture is off. Run `cardinal-{runtime} decision on` to turn it on.", file=sys.stderr)
        return EXIT_OFF
    session_id = args.session
    cwd = os.getcwd()
    repo_root = initiative.git(["rev-parse", "--show-toplevel"], cwd)
    head_sha = branch = repo = None
    if repo_root:
        head_sha = initiative.git(["rev-parse", "HEAD"], cwd)
        branch = initiative.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
        repo = initiative.canonical_repo(initiative.git(["remote", "get-url", "origin"], cwd))
    ledger = decisions.read_ledger(paths.runtime_dir, session_id)
    try:
        anchors = [decisions.parse_anchor(spec, repo_root, cwd) for spec in args.anchor]
        decision = decisions.build_decision(
            choice=args.choice, question=args.question, rationale=args.why, decided_by=args.by,
            alternatives=args.alt, follows_from=args.follows, refines=args.refines,
            supersedes=args.supersedes, anchors=anchors, decision_id=args.id, existing=ledger,
        )
    except decisions.DecisionError as err:
        print(f"cardinal-{runtime} decision: {err}", file=sys.stderr)
        return EXIT_USAGE
    cache = decisions.cache_dir(paths.runtime_dir)
    clusters, scheme = bounded_clusters(decision["anchors"], repo_root, head_sha, cache)
    pr_number, pr_url = pull_request(cwd, repo, branch, paths)
    known = {entry["id"] for entry in ledger}
    unknown = [link["to"] for link in decision["links"] if link["to"] not in known]
    decisions.record_in_ledger(paths.runtime_dir, session_id, decision)

    conn = otlp.connection_from_paths(paths)
    if conn is not None:
        attrs = decisions.decision_attributes(
            session_id=session_id, decision=decision, code_clusters=clusters, cluster_scheme=scheme,
            repo=repo, branch=branch, head_sha=head_sha, pr_number=pr_number, pr_url=pr_url,
        )
        attrs["agent_runtime"] = runtime
        otlp.emit_records([otlp.log_record(decisions.DECISION_EVENT, attrs, time.time_ns())], conn,
            resource_for(runtime, paths, args.version), scope_name=f"cardinal-{runtime}-plugin",
            scope_version=args.version, timeout=DECISION_EMIT_TIMEOUT_SEC)

    tags = [f"PR #{pr_number}"] if pr_number else []
    if clusters:
        tags.append("clusters " + ", ".join(clusters))
    detail = f" ({'; '.join(tags)})" if tags else ""
    lines = [f"Recorded decision {decision['id']}: {decision['choice']}{detail}"]
    if unknown:
        lines.append(f"Note: no earlier decision in this session has id {', '.join(unknown)}; the link was kept as given.")
    if conn is None:
        lines.append(f"Cardinal telemetry isn't connected, so this decision was only saved locally (run cardinal-{runtime} connect).")
    message = "\n".join(lines)
    if args.json:
        # Hosts refresh their cached prompt context from this instead of spawning again.
        ledger = decisions.read_ledger(paths.runtime_dir, session_id)
        print(json.dumps({"message": message, "context": decision_context(runtime, session_id, ledger, args.tool)}))
    else:
        print(message)
    return 0


def decision(args, paths):
    runtime, action = args.runtime, args.decision_command
    if action in ("on", "off"):
        decisions.set_enabled(paths.runtime_dir, action == "on")
        print("Decision capture is on. New prompts will ask the agent to record its decisions."
              if action == "on" else "Decision capture is off.")
        return 0
    if action == "record":
        return decision_record(args, paths)
    override = decisions_override()
    enabled = decisions.is_enabled(paths.runtime_dir, override)
    if action == "context":
        if enabled:
            print(decision_context(runtime, args.session, decisions.read_ledger(paths.runtime_dir, args.session), args.tool))
        return 0
    source = f" ({decisions.ENABLE_ENV}={override})" if decisions.parse_override(override) is not None else ""
    print(f"Decision capture: {'on' if enabled else 'off'}{source}")
    connected = otlp.connection_from_paths(paths) is not None
    print(f"Cardinal telemetry: {'connected' if connected else f'not connected (run cardinal-{runtime} connect)'}")
    if args.session:
        print(f"Decisions in session {args.session}:")
        print(decisions.render_ledger(decisions.read_ledger(paths.runtime_dir, args.session), limit=50))
    return 0


def add_decision_parser(sub):
    dec = sub.add_parser("decision", help="Opt-in capture of the decisions the agent makes")
    dsub = dec.add_subparsers(dest="decision_command", metavar="{record,on,off,status}", required=True)
    record = dsub.add_parser("record", help="record one decision")
    record.add_argument("--session", required=True, help="session id (supplied by the plugin)")
    record.add_argument("--choice", required=True, help="the option chosen, in a few words")
    record.add_argument("--question", help="the question this decision settles")
    record.add_argument("--why", help="one sentence on why this option won")
    record.add_argument("--alt", action="append", default=[], metavar="OPTION",
                        help="an option that was considered and rejected (repeatable)")
    record.add_argument("--by", choices=decisions.DECIDED_BY, default="agent", help="who made the call (default: agent)")
    record.add_argument("--anchor", action="append", default=[], metavar="ANCHOR",
                        help="file, dir/, file::Symbol, or <kind>:<identifier>[@path] the decision governs (repeatable)")
    for flag, text in (("--follows", "an earlier decision this one only makes sense because of"),
                       ("--refines", "an earlier decision this one narrows"),
                       ("--supersedes", "an earlier decision this one replaces")):
        record.add_argument(flag, action="append", default=[], metavar="ID", help=text)
    record.add_argument("--id", help="decision id; reuse an existing id to revise that decision")
    record.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    record.add_argument("--tool", help=argparse.SUPPRESS)
    dsub.add_parser("on", help="turn decision capture on")
    dsub.add_parser("off", help="turn decision capture off")
    status_parser = dsub.add_parser("status", help="show whether capture is on")
    status_parser.add_argument("--session", help="also list this session's decisions")
    context = dsub.add_parser("context", help=argparse.SUPPRESS)
    context.add_argument("--session", required=True)
    context.add_argument("--tool")


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
    add_decision_parser(sub)
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
        if args.command == "decision":
            return decision(args, paths)
        return status(paths)
    except Exception as exc:
        if args.command != "telemetry":
            print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
