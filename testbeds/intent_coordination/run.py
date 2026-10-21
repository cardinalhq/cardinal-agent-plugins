#!/usr/bin/env python3
"""Installed CLI intent acceptance; isolated artifacts, real routes, fixture auth.

The invoke helper forwards to the unmodified candidate CLI and records its public
JSON result. It gives assertions direct evidence of readiness without inspecting
private model reasoning. Normal runtime configuration/authentication is retained.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import shutil
import uuid
import sys
import time

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
spec = importlib.util.spec_from_file_location("automatic", REPO / "testbeds/automatic_coordination/run.py")
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
h, c, ie, rt = previous.h, previous.c, previous.ie, previous.rt
wait_for, stop = previous.wait_for, previous.stop
OLD = previous.OLD


def helper(root, operation, arguments):
    if operation == "invoke":
        cli = json.loads((root / "cli.json").read_text())["cli"]
        call = [sys.executable, cli, "investigation", *arguments]
        result = subprocess.run(call, capture_output=True, text=True)
        try: value = json.loads(result.stdout)
        except ValueError: value = None
        row = {"at": time.time_ns(), "argv": arguments, "exit": result.returncode, "result": value}
        with (root / "coordination-audit.jsonl").open("a") as stream:
            stream.write(json.dumps(row) + "\n")
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode
    if operation == "attempted":
        case = arguments[0]
        if case.startswith("CASE:"):
            case = case.split(":")[1]
        if case not in ("complete", "blocked", "abstain", "reject"):
            raise ValueError("attempted needs an outcome name: complete, blocked, abstain or reject")
        wait_for(lambda: any(x.get("result", {}).get("status") == "timeout" and
            "CASE:" + case + ":" in str(x.get("argv"))
            for x in h.jsonl(root.parent / "requester/coordination-audit.jsonl") if isinstance(x.get("result"), dict)))
        print("Requester actually waited and retained the dependency")
        return 0
    previous.helper(root, operation)
    return 0


def prompt(role, runtime, root, cli, suffix):
    helper_cmd = h.command(sys.executable, HERE / "run.py", "--root", root, "--helper")
    command = helper_cmd + " invoke --"
    native = "apply_patch" if runtime == "codex" else "Write"
    intro = (
        "This is authorized isolated intent coordination acceptance. Do not inspect test source, settings, routes, credentials or audits. Do not delegate. "
        f"Use {command} followed by Investigation arguments for ALL coordination operations. It invokes the actual candidate CLI and records only its public result. "
        f"First run {helper_cmd} identity to learn your SID and peer SID. Create {root/'startup-note.txt'} with STARTUP using native {native}; this must succeed even with unread feedback. "
        "There is no automatic action interception. Explicitly call sync --session SID --intent 'Understand initial evidence'. "
        "A needs_feedback response supplies a batch. Read ALL its chunks with gate-read --session SID --batch B, retain all hashes in order, "
        "acknowledge non-request control events as required using your SID, then gate-reviewed --session SID --batch B --chunk-hash H1 ... . "
        "Every sync/wait exit3 means not released, not a tool failure. Handle feedback and call again. Complete results require explicit acceptance; abstain/reject require cancellation. "
        "Never enable/disable a gate, set a mode, write policies or bypass the primitive. Use an exact unchanged intent string to resume work. "
        f"After initial handling sync that purpose to ready, create {root/'ready.txt'} with READY using native {native}, then run {helper_cmd} ready. "
    )
    if role == "requester":
        return intro + (
            f"For each CASE in complete, blocked, abstain, reject, use purpose 'CASE:CASE:{suffix} choose the explanation' (replace CASE with the outcome). "
            "Create one request --session SID --assignee PRINCIPAL --assignee-session PEER_SID --intent PURPOSE --text 'CASE:CASE evaluate the explanations'. "
            "Save its request sequence Q. Immediately call wait --session SID --intent PURPOSE --request Q --timeout 0.3; it must time out with released:false before the responder proceeds. "
            "Then wait again with --timeout 120. Handle any feedback completely and call sync --session SID --intent PURPOSE before resolving. "
            "That sync must show needs_attention with released:false after receiving the response; do not treat arrival or reading as acceptance. "
            f"For blocked, after seeing the blocked response create {root/'blocked-fix.txt'} containing BLOCKED_FIX via native {native}. "
            "Then wait --session SID --intent PURPOSE --request Q --after-response BLOCKED_RESPONSE_SEQ --timeout 120, handle feedback, and sync until the complete result is shown as needs_attention. "
            "For complete and blocked, resolve Q --session SID --outcome accepted --response EXACT_COMPLETE_SEQ --text 'Reviewed this exact result'. "
            "For abstain and reject, resolve Q --session SID --outcome cancelled --text 'Cancelled this dependency and will reassess'. "
            "Sync the same purpose again; only when ready and released:true, create a native decision note named decision-CASE.txt "
            f"under {root} containing RESULT_CASE_{suffix} (replace CASE). Do all four cases. Report the initial random peer witness and the four resolved results."
        )
    return intro + (
        "Handle exactly four incoming requests. When none is available, wait --session SID --intent 'Understand the next assignment' --incoming --timeout 120. "
        "Handle every needs_feedback batch and use requests --session SID to find your assigned unresolved request Q and its CASE marker. "
        f"Before responding to each case, run {helper_cmd} attempted OUTCOME (the bare word complete, blocked, abstain or reject) so the requester has actually entered its wait. "
        "For complete respond Q --session SID --outcome complete --text 'Evaluated the evidence'. "
        f"For blocked first respond blocked --text 'Need BLOCKED_FIX from requester', then run {helper_cmd} fixed, "
        "then respond complete --text 'Observed the requested fix and completed evaluation'. "
        "For abstain respond abstain --text 'Insufficient evidence to judge responsibly'. "
        "For reject respond reject --text 'Evaluation fails the requested acceptance condition'. "
        "Never resolve another participant's request. After all four replies report the initial random witness and your outcomes."
    )


def check(root, processes, events, audit, suffix, witness, before):
    checks = {"runtime_exits": all(p.poll() == 0 for p in processes.values()),
              "normal_config_unchanged": h.fingerprints() == before}
    for role in processes:
        directory = root / role
        calls, hooks = h.jsonl(directory / "coordination-audit.jsonl"), h.jsonl(directory / "hook-audit.jsonl")
        inputs = [str(row["argv"]) for row in calls]
        checks[role + "_native_tools_not_intercepted"] = bool(hooks) and all(x.get("decision") != "deny" for x in hooks) and (directory / "startup-note.txt").exists()
        checks[role + "_explicit_catchup"] = any(x.get("result", {}).get("status") == "needs_feedback" for x in calls if isinstance(x.get("result"), dict))
        checks[role + "_read_and_hash_review"] = any("gate-read" in x and "--batch" in x for x in inputs) and any("gate-reviewed" in x and "--chunk-hash" in x for x in inputs)
        checks[role + "_no_enable"] = not any("gate-enable" in x or "gate-disable" in x for x in inputs)
        sid = c.private_read(directory / "session.json")["session"]
        checks[role + "_own_ack"] = any(e["type"] == "acknowledged" and e["payload"].get("ack_of") == 1 and e["producer"].get("sessionId") == sid for e in events)
        public = []
        for row in h.jsonl(directory / "stdout.jsonl"):
            if row.get("type") == "result": public.append(str(row.get("result", "")))
            item = row.get("item", {})
            if row.get("type") == "item.completed" and item.get("type") == "agent_message": public.append(str(item.get("text", "")))
        checks[role + "_public_witness"] = any(witness in value for value in public)
    requests = [e for e in events if e["type"] == "request.created"]
    checks["four_intent_requests"] = len(requests) == 4 and all(e["payload"].get("intent") and "actions" not in e["payload"] for e in requests)
    calls = h.jsonl(root / "requester/coordination-audit.jsonl")
    hooks = h.jsonl(root / "requester/hook-audit.jsonl")
    for case in ("complete", "blocked", "abstain", "reject"):
        req = next((e for e in requests if "CASE:" + case in e["payload"]["text"]), None)
        replies = [e for e in events if req and e["type"] == "request.responded" and e["payload"]["request"] == req["seq"]]
        resolutions = [e for e in events if req and e["type"] == "request.resolved" and e["payload"]["request"] == req["seq"]]
        checks[case + "_outcomes"] = [e["payload"]["outcome"] for e in replies] == (["blocked", "complete"] if case == "blocked" else [case])
        checks[case + "_resolution"] = len(resolutions) == 1 and resolutions[0]["payload"]["outcome"] == ("accepted" if case in ("complete", "blocked") else "cancelled")
        checks[case + "_exact_result"] = case not in ("complete", "blocked") or bool(replies and resolutions and resolutions[0]["payload"].get("response") == replies[-1]["seq"])
        relevant = [r for r in calls if req and req["payload"]["intent"] in r["argv"] and isinstance(r.get("result"), dict)]
        checks[case + "_wait_retained"] = any(r["result"].get("status") == "timeout" and r["result"].get("released") is False for r in relevant)
        reply_at = next((r["at"] for r in audit if replies and r.get("kind") == "append" and r["event"]["seq"] == replies[0]["seq"]), None)
        resolved_at = next((r["at"] for r in audit if resolutions and r.get("kind") == "append" and r["event"]["seq"] == resolutions[0]["seq"]), None)
        checks[case + "_arrival_not_acceptance"] = bool(reply_at and resolved_at) and any(reply_at <= r["at"] / 1000000 <= resolved_at and r["result"].get("status") == "needs_attention" and not r["result"]["released"] for r in relevant)
        checks[case + "_ready_after_resolution"] = bool(resolved_at) and any(r["at"] / 1000000 >= resolved_at and r["result"].get("released") is True for r in relevant)
        note = root / "requester" / ("decision-" + case + ".txt")
        checks[case + "_decision_note"] = note.exists() and "RESULT_" + case + "_" + suffix in note.read_text()
        if case == "blocked":
            complete_at = next((r["at"] for r in audit if len(replies) == 2 and r.get("kind") == "append" and r["event"]["seq"] == replies[1]["seq"]), None)
            checks["independent_native_work_while_blocked"] = bool(reply_at and complete_at) and any("blocked-fix.txt" in str(r.get("input")) and reply_at <= r["at"] / 1000000 <= complete_at for r in hooks)
    return checks


def run(args):
    root = args.output.absolute()
    root.mkdir(mode=0o700, parents=True)  # Existing roots are deliberately refused.
    before = h.fingerprints()
    witness = "PEER_" + uuid.uuid4().hex
    suffix = uuid.uuid4().hex[:12]
    h.write(root / "server-config.json", {"quiet": False, "role": "worker", "witness": witness})
    launcher = args.backend / "packages/maestro/node_modules/tsx/dist/cli.mjs"
    processes, server, files = {}, None, []
    try:
        author = previous.live_connection() if args.live else None
        if author:
            from cardinal_core import investigation_state_sync as sync
            inv = sync.create_investigation(author, "Intent paired CLI acceptance " + suffix, client="codex/0.39.0")["investigation_id"]
            meta = sync.get_investigation(author, inv, client="codex/0.39.0")
            if meta.get("capabilities", {}).get("coordination_intents") != {"enabled": True, "version": 1}:
                raise RuntimeError("Deploy coordination request protocol before live acceptance")
            owner = c.principal_key(meta["author"])
            if not owner.startswith("user:"):
                raise RuntimeError("This paired acceptance expects the existing user connection")
            endpoint = {"origin": author["origin"], "org": author["org"], "inv": inv, "owner": owner[5:]}
            h.write(root / "endpoint.json", endpoint)
            ie.append_event(author, inv, "cue.added", {"text": "Witness " + witness + ". Abandon the original action after handling.", "refs": []},
                session_id="peer-acceptance-" + suffix, idempotency_key="initial-" + suffix, client="codex/0.39.0")
        else:
            log = (root / "server.log").open("w"); files.append(log)
            server = subprocess.Popen(["node", str(launcher), str(OLD / "phase1-server.ts"), str(args.backend), str(root)], stdout=log, stderr=log)
            wait_for(lambda: (root / "endpoint.json").exists() or server.poll() is not None, 30)
            if server.poll() is not None:
                raise RuntimeError("real route fixture failed; inspect server.log")
            endpoint = json.loads((root / "endpoint.json").read_text())
        for role, runtime in (("responder", args.responder), ("requester", args.requester)):
            directory = root / role; directory.mkdir(mode=0o700)
            for command in (["init", "-q"], ["config", "user.name", "Coordination acceptance"], ["config", "user.email", "fixture@example.test"],
                            ["-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "baseline"]):
                subprocess.run(["git", *command], cwd=directory, check=True, capture_output=True)
            artifact = args.artifacts / runtime
            cli = artifact / ("scripts" if runtime == "codex" else "bin") / "cardinal-storyboard"
            h.write(directory / "cli.json", {"cli": str(cli)})
            hook = rt.hook_path(cli, runtime)
            h.write(directory / "hook-target.json", {"argv": [sys.executable, str(hook), *(["--event", "CoordinationGate"] if runtime == "codex" else [])]})
            prepare = h.command(sys.executable, Path(__file__), "--root", directory, "--helper", "prepare")
            hookcmd = h.command(sys.executable, OLD / "run.py", "--helper", "hook", "--root", directory)
            hooks = {"SessionStart": [{"hooks": [{"type": "command", "command": prepare, "timeout": 5}]}],
                "PreToolUse": [{"matcher": "Bash|apply_patch" if runtime == "codex" else "Bash|Write|Edit|NotebookEdit",
                    "hooks": [{"type": "command", "command": hookcmd, "timeout": 5}]}]}
            h.write(directory / "hooks.json", {"hooks": hooks})
            env = os.environ.copy()
            for name in ("CLAUDECODE", "CARDINAL_COORDINATION_MODE", "CARDINAL_INVESTIGATION_TOKEN", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE"):
                env.pop(name, None)
            env.update({rt.STATE_DIR: str(directory / "routes"), "CARDINAL_CONNECTION": "env",
                "CARDINAL_MCP_URL": endpoint["origin"] + "/api/orgs/" + endpoint["org"] + "/mcp",
                "CARDINAL_MCP_API_KEY": author["key"] if author else "phase3-fixture-key", "CARDINAL_OWNER_INPUT": "0",
                "CARDINAL_STORYBOARD_SESSION_START": "0", "CARDINAL_INVESTIGATION_POLLER": "0"})
            if runtime == "codex":
                argv = [shutil.which(runtime), "exec", "--ignore-user-config", "--skip-git-repo-check", "--ephemeral", "--sandbox", "danger-full-access",
                    "--add-dir", str(ie.sessions_dir(Path.home())), "--add-dir", str(root), "-c", "sandbox_workspace_write.network_access=true",
                    "-c", 'approval_policy="never"', "-c", "features.hooks=true", "--dangerously-bypass-hook-trust", "--enable", "code_mode", "--enable", "code_mode_host"]
                for event, groups in hooks.items():
                    value = "[" + ",".join('{matcher=' + json.dumps(g.get("matcher", ".*")) + ',hooks=[{type="command",command=' + json.dumps(g["hooks"][0]["command"]) + ',timeout=5}]}' for g in groups) + "]"
                    argv += ["-c", "hooks." + event + "=" + value]
                argv += ["--json", "-C", str(directory), "-"]
            else:
                argv = [shutil.which(runtime), "-p", "--setting-sources", "", "--settings", str(directory / "hooks.json"), "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--permission-mode", "dontAsk", "--allowedTools", "Bash,Write,Edit,Read,NotebookEdit", "--tools", "Bash,Write,Edit,Read,NotebookEdit",
                    "--disable-slash-commands", "--no-chrome", "--verbose", "--output-format", "stream-json", "--include-hook-events", "--no-session-persistence"]
            text = prompt(role, runtime, directory, cli, suffix)
            (directory / "prompt.txt").write_text(text)
            h.write(directory / "metadata.json", {"runtime": runtime, "version": subprocess.check_output([shutil.which(runtime), "--version"], text=True).strip(),
                "argv": argv, "mode_variable_present": "CARDINAL_COORDINATION_MODE" in env, "manual_enablement": False})
            out, err = (directory / "stdout.jsonl").open("w"), (directory / "stderr.log").open("w"); files += [out, err]
            proc = subprocess.Popen(argv, cwd=directory, env=env, stdin=subprocess.PIPE, stdout=out, stderr=err, text=True, start_new_session=True)
            processes[role] = proc
            proc.stdin.write(text); proc.stdin.close()
        end = time.monotonic() + args.timeout
        while any(p.poll() is None for p in processes.values()) and time.monotonic() < end:
            time.sleep(0.5)
        for proc in processes.values():
            stop(proc, group=True)
        h.clean_project_trust(root)
        audit = previous.live_audit(author, endpoint["inv"]) if author else h.jsonl(root / "server-audit.jsonl")
        if author:
            (root / "server-audit.jsonl").write_text("".join(json.dumps(row) + "\n" for row in audit))
        events = [x["event"] for x in audit if x.get("kind") == "append"]
        checks = check(root, processes, events, audit, suffix, witness, before)
        result = {"requester": args.requester, "responder": args.responder, "live": args.live,
            "investigation_id": endpoint["inv"], "checks": checks, "passed": all(checks.values())}
        h.write(root / "result.json", result)
        print(json.dumps(result, indent=2))
        return 0 if result["passed"] else 1
    finally:
        errors = []
        for proc in [*processes.values(), *([server] if server is not None else [])]:
            try: stop(proc, group=proc in processes.values())
            except Exception as error: errors.append(str(error))
        for role in processes:
            directory = root / role
            saved = directory / "session.json"
            try:
                if saved.exists():
                    meta = c.private_read(saved)
                    binding = ie.binding_path(Path.home(), meta["session"])
                    if str(binding) != meta["binding"]: raise ValueError("foreign binding cleanup refused")
                    if binding.exists():
                        if ie.read_binding(Path.home(), meta["session"])["investigation_id"] != json.loads((root / "endpoint.json").read_text())["inv"]:
                            raise ValueError("foreign Investigation cleanup refused")
                        shutil.copyfile(binding, directory / "binding-final.json"); binding.unlink()
                    outbox = ie.sessions_dir(Path.home()) / (meta["session"] + ".request-outbox")
                    if outbox.exists():
                        inv = json.loads((root / "endpoint.json").read_text())["inv"]
                        for record in outbox.glob("request-*.json"):
                            intent = c.private_read(record).get("intent", {})
                            if intent.get("session") != meta["session"] or intent.get("identity", {}).get("investigation_id") != inv:
                                raise ValueError("foreign request outbox cleanup refused")
                        shutil.copytree(outbox, directory / "request-outbox-final")
                        shutil.rmtree(outbox)
            except Exception as error: errors.append(str(error))
        try: h.clean_project_trust(root)
        except Exception as error: errors.append(str(error))
        for stream in files: stream.close()
        if errors: raise RuntimeError("Acceptance cleanup incomplete: " + "; ".join(errors))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--helper", choices=("prepare", "identity", "ready", "attempted", "fixed", "invoke"))
    p.add_argument("--root", type=Path)
    p.add_argument("--backend", type=Path)
    p.add_argument("--artifacts", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--requester", choices=("codex", "claude"))
    p.add_argument("--responder", choices=("codex", "claude"))
    p.add_argument("--timeout", type=float, default=1200)
    p.add_argument("--live", action="store_true")
    p.add_argument("arguments", nargs=argparse.REMAINDER)
    args = p.parse_args()
    if args.helper:
        sys.exit(helper(args.root, args.helper, args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments))
    sys.exit(run(args))
