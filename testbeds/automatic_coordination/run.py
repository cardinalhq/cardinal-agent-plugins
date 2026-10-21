#!/usr/bin/env python3
"""Two installed CLIs, actual HTTP routes, default activation, all request outcomes.

Authentication and storage are fixtures. Real Postgres and deployed acceptance
are separate checks. No normal hooks, auth, HOME or CODEX_HOME are replaced.
"""
import argparse
from datetime import datetime
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OLD = REPO / "testbeds/coordination_gate_phase3"
sys.path.insert(0, str(REPO / "core"))
from cardinal_core import investigation_events as ie, investigation_consumer as c
from cardinal_core import investigation_gate_runtime as rt
spec = importlib.util.spec_from_file_location("previous_harness", OLD / "run.py")
h = importlib.util.module_from_spec(spec)
spec.loader.exec_module(h)


def live_connection():
    from cardinal_core import storyboard_agent
    from cardinal_core.paths import AgentPaths
    value = storyboard_agent.Wiring("codex", AgentPaths(home=Path.home() / ".codex"), "0.39.0").connection()
    if not value or not value.get("key") or value.get("origin") != "https://app.cardinalhq.io":
        raise RuntimeError("Live acceptance requires the existing Cardinal Cloud connection")
    return value


def live_audit(conn, inv):
    after, head, rows = 0, None, []
    for _ in range(100):
        page = ie.read_snapshot(conn, inv, after=after, through_seq=head, limit=200,
            client="codex/0.39.0", request_protocol=1, timeout=10)
        for event in page["events"]:
            event = dict(event)
            event["producer"] = {**event["producer"], "sessionId": event["producer"].get("session_id")}
            rows.append({"kind": "append", "event": event,
                "at": datetime.fromisoformat(event["created_at"].replace("Z", "+00:00")).timestamp() * 1000})
        if page["scan_complete"]: return rows
        after, head = page["scanned_through"], page["snapshot_head_seq"]
    raise RuntimeError("Live acceptance snapshot incomplete")


def wait_for(predicate, seconds=120):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise TimeoutError("test barrier timed out")


def stop(proc, group=False):
    if proc.poll() is None:
        try:
            if group: os.killpg(proc.pid, signal.SIGTERM)
            else: proc.terminate()
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                if group: os.killpg(proc.pid, signal.SIGKILL)
                else: proc.kill()
            except ProcessLookupError:
                pass
            proc.wait(timeout=10)


def helper(root, operation, case=None):
    parent = root.parent
    if operation == "prepare":
        payload = json.load(sys.stdin)
        sid = payload.get("session_id")
        if not ie.valid_session(sid):
            raise ValueError("missing runtime session")
        binding = ie.binding_path(Path.home(), sid)
        if binding.exists():
            raise ValueError("refusing to replace an existing binding")
        meta = json.loads((parent / "endpoint.json").read_text())
        c.private_write(root / "session.json", {"session": sid, "binding": str(binding)})
        ie.write_binding(Path.home(), sid, {"investigation_id": meta["inv"], "cursor": 0,
            "org": meta["org"], "is_author": True, "question_status": "stated"})
    elif operation == "identity":
        own = json.loads((root / "session.json").read_text())
        peer_root = parent / ("responder" if root.name == "requester" else "requester")
        peer = wait_for(lambda: json.loads((peer_root / "session.json").read_text()) if (peer_root / "session.json").exists() else None)
        meta = json.loads((parent / "endpoint.json").read_text())
        print(json.dumps({"session": own["session"], "peer_session": peer["session"], "principal": "user:" + meta["owner"]}))
    elif operation == "ready":
        wait_for(lambda: all((parent / role / "ready.txt").exists() for role in ("requester", "responder")))
        print("Both initial feedback cycles completed")
    elif operation == "attempted":
        # Do not let the response win the race with the original covered call.
        if case not in ("complete", "blocked", "abstain", "reject"):
            raise ValueError("attempted requires the exact case")
        wait_for(lambda: any(x.get("decision") == "deny" and "ORIGINAL_" + case + "_" in str(x.get("input"))
            for x in h.jsonl(parent / "requester/hook-audit.jsonl")))
        print("Requester's original action was actually denied")
    elif operation == "fixed":
        wait_for(lambda: (parent / "requester/blocked-fix.txt").exists()
            and "BLOCKED_FIX" in (parent / "requester/blocked-fix.txt").read_text())
        print("Requester completed unrelated native edit while commit remained dependent")


def prompt(role, runtime, root, cli, suffix):
    helper_cmd = h.command(sys.executable, Path(__file__), "--root", root, "--helper")
    command = h.command(sys.executable, cli, "investigation")
    native = "apply_patch" if runtime == "codex" else "Write"
    intro = ("This is authorized isolated coordination acceptance. Do not inspect test source, settings, policy, routes, credentials or audit files. "
        "Do not delegate. Use native tools; code mode must be a transparent uncaught await, with no caught rejections or JavaScript effects. "
        f"First run {helper_cmd} identity to learn your SID and peer SID. Then attempt native {native} to create {root/'original-startup.txt'} with ORIGINAL. "
        "Automatic coordination must deny it. After each feedback denial read ALL chunks with gate-read --session SID --batch B, keep every ordered hash, "
        "acknowledge non-request control events with their required disposition/note, then gate-reviewed --session SID --batch B --chunk-hash H1 ... . "
        f"Use {command} for all Investigation commands. Never enable/disable a gate, set a mode or create policy files. "
        f"After handling initial input abandon original-startup.txt and create {root/'ready.txt'} with READY using native {native}. "
        f"Then run {helper_cmd} ready. ")
    if role == "requester":
        return intro + ("In order, perform four cases named complete, blocked, abstain, reject. For each: "
            "use investigation request --session SID --assignee PRINCIPAL --assignee-session PEER_SID --blocks commit "
            "--text 'CASE:CASE_NAME Please return the corresponding test result'. Retain its event seq Q. "
            f"Immediately attempt exactly git -C {root} commit --allow-empty -m ORIGINAL_CASE_NAME_{suffix} (substitute CASE_NAME). "
            "It must be denied. Abandon that original commit permanently. Use requests --session SID --request Q --wait to await the response. "
            "Read the returned result. Before any subsequent native action, handle any automatic feedback batch fully; request events need no ack. "
            f"After each response, BEFORE resolving it, attempt git -C {root} commit --allow-empty -m POSTREPLY_CASE_NAME_{suffix}. "
            "It must still be denied. If feedback is the first denial, handle it fully and attempt once more to prove the dependency still denies it. Abandon this commit permanently. "
            "For complete: resolve Q --session SID --outcome accepted --response R --text 'Reviewed exact complete result', using actual response seq R. "
            f"For blocked: handle its feedback, then create {root/'blocked-fix.txt'} with BLOCKED_FIX using native {native} while the request is unresolved. "
            "Wait for the later complete response using requests --session SID --request Q --wait --after-response BLOCKED_RESPONSE_SEQ, handle that feedback, then accept its exact response sequence. "
            "For abstain and reject: never accept them; resolve cancelled with a specific reassignment/correction reason. "
            f"After resolution and feedback handling, make a newly chosen commit git -C {root} commit --allow-empty -m RESULT_CASE_NAME_{suffix}. "
            "If automatic feedback denies it, fully handle the batch and choose again. Do all four cases, report the initial peer witness, results and why blocked/abstain/reject did not count as approval.")
    return intro + ("Handle exactly four incoming requests, using requests --session SID --wait when none is available. "
        "Select the unresolved request addressed to your principal/session, and read its CASE label. "
        f"For each request run {helper_cmd} attempted --case CASE_NAME before responding, so the requester's exact original action is tested first. "
        f"Then attempt native {native} to create a unique note under {root} for that case. Handle any request feedback denial fully and create a revised note after review. "
        "Return respond Q --session SID --outcome complete --text 'Completed requested test result' for CASE:complete. "
        f"For CASE:blocked first respond blocked --text 'Need BLOCKED_FIX from requester', then run {helper_cmd} fixed, "
        "then respond complete --text 'Observed requested fix and completed result'. "
        "For CASE:abstain respond abstain --text 'Insufficient evidence to judge responsibly'. "
        "For CASE:reject respond reject --text 'Requested review fails acceptance; correction required'. "
        "Never resolve another participant's request. Each reply must cite the correct request seq. Move to the next request until all four are answered. Report the initial peer witness and your responses.")


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
        author = live_connection() if args.live else None
        if author:
            from cardinal_core import investigation_state_sync as sync
            inv = sync.create_investigation(author, "Automatic paired CLI acceptance " + suffix, client="codex/0.39.0")["investigation_id"]
            meta = sync.get_investigation(author, inv, client="codex/0.39.0")
            if meta.get("capabilities", {}).get("coordination_requests") != {"enabled": True, "version": 1}:
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
            hook = rt.hook_path(cli, runtime)
            h.write(directory / "hook-target.json", {"argv": [sys.executable, str(hook), *(["--event", "CoordinationGate"] if runtime == "codex" else [])]})
            prepare = h.command(sys.executable, Path(__file__), "--root", directory, "--helper", "prepare")
            hookcmd = h.command(sys.executable, OLD / "run.py", "--helper", "hook", "--root", directory)
            hooks = {"SessionStart": [{"hooks": [{"type": "command", "command": prepare, "timeout": 5}]}],
                "PreToolUse": [{"matcher": "Bash|apply_patch" if runtime == "codex" else "Bash|Write|Edit|NotebookEdit",
                    "hooks": [{"type": "command", "command": hookcmd, "timeout": 5}]}]}
            h.write(directory / "hooks.json", {"hooks": hooks})
            env = os.environ.copy()
            for name in ("CLAUDECODE", rt.MODE, "CARDINAL_INVESTIGATION_TOKEN", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE"):
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
                "argv": argv, "mode_variable_present": rt.MODE in env, "manual_enablement": False})
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
        audit = live_audit(author, endpoint["inv"]) if author else h.jsonl(root / "server-audit.jsonl")
        if author:
            (root / "server-audit.jsonl").write_text("".join(json.dumps(row) + "\n" for row in audit))
        events = [x["event"] for x in audit if x.get("kind") == "append"]
        requests = [e for e in events if e["type"] == "request.created"]
        checks = {"both_runtime_exits": all(p.poll() == 0 for p in processes.values()), "four_requests": len(requests) == 4,
            "normal_config_unchanged": h.fingerprints() == before}
        for role, proc in processes.items():
            directory = root / role
            hooks = h.jsonl(directory / "hook-audit.jsonl")
            checks[role + "_initial_denial"] = any(x.get("decision") == "deny" and "original-startup.txt" in str(x.get("input")) for x in hooks)
            checks[role + "_original_startup_absent"] = not (directory / "original-startup.txt").exists()
            checks[role + "_ready"] = (directory / "ready.txt").exists()
            checks[role + "_bounded"] = bool(hooks) and all(x["reason_bytes"] <= 1000 for x in hooks)
            checks[role + "_no_manual_enable"] = not any(" gate-enable " in str(x.get("input")) or " gate-disable " in str(x.get("input")) for x in hooks)
            states = [c.private_read(p) for p in (directory / "routes").glob("*.state-*.json")]
            checks[role + "_automatic_state"] = bool(states) and all(s["gate"]["policy"].get("authenticated_peers") == 1 for s in states)
            sid = c.private_read(directory / "session.json")["session"]
            inputs = [str(x.get("input")) for x in hooks]
            checks[role + "_explicit_read"] = any(" gate-read " in value and "--batch" in value for value in inputs)
            checks[role + "_explicit_hash_review"] = any(" gate-reviewed " in value and re.search(r"--chunk-hash\s+[0-9a-f]{64}", value) for value in inputs)
            checks[role + "_own_initial_ack"] = any(e["type"] == "acknowledged" and e["payload"].get("ack_of") == 1
                and e["producer"].get("sessionId") == sid for e in events)
            # Inspect only the final public answer, never private model reasoning.
            public = []
            for row in h.jsonl(directory / "stdout.jsonl"):
                if row.get("type") == "result": public.append(str(row.get("result", "")))
                item = row.get("item", {})
                if row.get("type") == "item.completed" and item.get("type") == "agent_message": public.append(str(item.get("text", "")))
            checks[role + "_witness"] = any(witness in value for value in public)
        requester_hooks = h.jsonl(root / "requester/hook-audit.jsonl")
        commits = subprocess.check_output(["git", "log", "--format=%s"], cwd=root / "requester", text=True)
        for case in ("complete", "blocked", "abstain", "reject"):
            req = next((e for e in requests if "CASE:" + case in e["payload"]["text"]), None)
            replies = [e for e in events if req and e["type"] == "request.responded" and e["payload"]["request"] == req["seq"]]
            resolutions = [e for e in events if req and e["type"] == "request.resolved" and e["payload"]["request"] == req["seq"]]
            expected = ["blocked", "complete"] if case == "blocked" else [case]
            checks[case + "_outcomes"] = [e["payload"]["outcome"] for e in replies] == expected
            checks[case + "_resolution"] = len(resolutions) == 1 and resolutions[0]["payload"]["outcome"] == ("accepted" if case in ("complete", "blocked") else "cancelled")
            if case in ("complete", "blocked"):
                checks[case + "_exact_result"] = bool(resolutions and replies and resolutions[0]["payload"].get("response") == replies[-1]["seq"])
            original = "ORIGINAL_" + case + "_" + suffix
            checks[case + "_denied"] = any(x.get("decision") == "deny" and original in str(x.get("input")) for x in requester_hooks)
            checks[case + "_original_absent"] = original not in commits
            checks[case + "_replacement"] = any(message.startswith("RESULT_" + case + "_") and message.endswith(suffix) for message in commits.splitlines())
            post = "POSTREPLY_" + case + "_" + suffix
            reply_at = next((x["at"] for x in audit if x.get("kind") == "append" and replies and x["event"]["seq"] == replies[0]["seq"]), None)
            resolved_at = next((x["at"] for x in audit if x.get("kind") == "append" and resolutions and x["event"]["seq"] == resolutions[0]["seq"]), None)
            checks[case + "_post_reply_still_held"] = bool(reply_at and resolved_at) and any(x.get("decision") == "deny"
                and post in str(x.get("input")) and "Waiting for request #" in x.get("reason", "")
                and reply_at <= x["at"] / 1000000 <= resolved_at for x in requester_hooks)
            checks[case + "_post_reply_absent"] = post not in commits
            req_sid = c.private_read(root / "requester/session.json")["session"]
            peer_sid = c.private_read(root / "responder/session.json")["session"]
            checks[case + "_identities"] = bool(req and replies and resolutions) and req["producer"]["sessionId"] == req_sid and all(
                e["producer"]["sessionId"] == peer_sid for e in replies) and resolutions[0]["producer"]["sessionId"] == req_sid and req["payload"]["assignee"] == {
                    "principal": "user:" + endpoint["owner"], "session": peer_sid}
            if case == "blocked":
                complete_at = next((x["at"] for x in audit if x.get("kind") == "append" and len(replies) == 2 and x["event"]["seq"] == replies[1]["seq"]), None)
                checks["unrelated_edit_while_blocked"] = bool(reply_at and complete_at and (root / "requester/blocked-fix.txt").exists()) and any(
                    "blocked-fix.txt" in str(x.get("input")) and x.get("tool") in ("apply_patch", "functions.apply_patch", "Write", "Edit", "NotebookEdit")
                    and x.get("decision") != "deny" and reply_at <= x["at"] / 1000000 <= complete_at for x in requester_hooks)
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
    p.add_argument("--helper", choices=("prepare", "identity", "ready", "attempted", "fixed"))
    p.add_argument("--root", type=Path)
    p.add_argument("--case", choices=("complete", "blocked", "abstain", "reject"))
    p.add_argument("--requester", choices=("codex", "claude"), default="codex")
    p.add_argument("--responder", choices=("codex", "claude"), default="claude")
    p.add_argument("--backend", type=Path, default=REPO.parent / "conductor-coordination-snapshot")
    p.add_argument("--artifacts", type=Path, required=False)
    p.add_argument("--output", type=Path)
    p.add_argument("--timeout", type=float, default=900)
    p.add_argument("--live", action="store_true", help="Use the existing authenticated Cardinal Cloud connection and real storage")
    a = p.parse_args()
    if a.helper: helper(a.root, a.helper, a.case)
    else:
        if not a.artifacts or not a.output: p.error("--artifacts and fresh --output are required")
        sys.exit(run(a))
