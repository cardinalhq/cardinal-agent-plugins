#!/usr/bin/env python3
"""Installed CLIs + production adapters/engine + real Phase 1 router/reader.

Fixtures use loopback authentication/storage, not production credentials. Never
rewrite HOME, CODEX_HOME, normal hook/settings files or runtime authentication.
"""
import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "core"))
from cardinal_core import investigation_events as ie
from cardinal_core import investigation_consumer as c
from cardinal_core import investigation_gate_runtime as rt


def write(path, value):
    path.write_text(json.dumps(value, indent=2))


def command(*tokens):
    return shlex.join([str(x) for x in tokens])


def jsonl(path):
    out = []
    for line in path.read_text(errors="replace").splitlines() if path.exists() else []:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def fingerprints():
    paths = [Path.home() / ".codex/config.toml", Path.home() / ".codex/hooks.json", Path.home() / ".claude/settings.json"]
    out = {}
    for path in paths:
        if not path.exists():
            out[str(path)] = None
            continue
        raw = path.read_bytes()
        if path.name == "config.toml":
            value = tomllib.loads(raw.decode())
            for project in list(value.get("projects", {})):
                if Path(project).is_relative_to(HERE / "runs"):
                    value["projects"].pop(project)
            raw = json.dumps(value, sort_keys=True, default=str).encode()
        out[str(path)] = hashlib.sha256(raw).hexdigest()
    return out


def clean_project_trust(directory, path=None):
    """Remove only Codex-created tables naming this fixture's private roots.

    Preserve every other byte/setting, including concurrent unrelated changes.
    Never restore an old whole-file snapshot over the user's current config.
    """
    path = (path or Path.home() / ".codex/config.toml").resolve()
    if not path.exists():
        return 0
    for _ in range(3):
        original = path.read_text()
        lines, skip, removed = [], False, 0
        for line in original.splitlines(keepends=True):
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                skip = False
                if stripped.startswith('[projects."') and stripped.endswith('"]'):
                    project = json.loads(stripped[len("[projects."):-1])
                    if Path(project).is_relative_to(directory):
                        skip = True
                        removed += 1
            if not skip:
                lines.append(line)
        if not removed:
            return 0
        if path.read_text() != original:
            continue
        fd, temporary = tempfile.mkstemp(prefix=".coordination-config-", dir=path.parent)
        try:
            os.fchmod(fd, path.stat().st_mode & 0o777)
            with os.fdopen(fd, "w") as stream:
                stream.write("".join(lines))
                stream.flush()
                os.fsync(stream.fileno())
            if path.read_text() != original:
                continue
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return removed
        finally:
            Path(temporary).unlink(missing_ok=True)
    raise RuntimeError("config changed concurrently; fixture trust cleanup needs retry")


def helper(root, operation):
    if operation == "hook":
        payload = json.load(sys.stdin)
        meta = json.loads((root / "hook-target.json").read_text())
        result = subprocess.run(meta["argv"], input=json.dumps(payload), capture_output=True, text=True)
        try:
            decision = json.loads(result.stdout or "{}").get("hookSpecificOutput", {})
        except ValueError:
            decision = {}
        entry = {"tool": payload.get("tool_name"), "input": payload.get("tool_input"), "exit": result.returncode,
                 "stdout_bytes": len(result.stdout.encode()),
                 "decision": decision.get("permissionDecision"),
                 "reason_bytes": len(decision.get("permissionDecisionReason", "").encode()), "at": time.time_ns()}
        with (root / "hook-audit.jsonl").open("a") as stream:
            stream.write(json.dumps(entry) + "\n")
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        if result.returncode:
            sys.exit(result.returncode)
        return
    if operation == "prepare":
        payload = json.load(sys.stdin)
        sid = payload.get("session_id")
        if not ie.valid_session(sid):
            raise ValueError("runtime supplied no session")
        binding = ie.binding_path(Path.home(), sid)
        if binding.exists():
            raise ValueError("fixture will not replace an existing binding")
        endpoint = json.loads((root / "endpoint.json").read_text())
        config = json.loads((root / "server-config.json").read_text())
        bound_inv = "inv_" + "c" * 24 if config.get("role") == "supervisor" else endpoint["inv"]
        ie.write_binding(Path.home(), sid, {"investigation_id": bound_inv, "cursor": 0,
                         "org": endpoint["org"], "is_author": True, "question_status": "stated"})
        write(root / "session.json", {"session": sid, "binding": str(binding)})
        return
    if operation == "session":
        print((root / "session.json").read_text())
    elif operation == "replacement":
        (root / "replacement.marker").write_text("explicitly reconsidered")
    elif operation == "arm-error":
        (root / "read-error").touch()


def run(runtime, case, output, phase1, timeout, quiet_count=10):
    before = fingerprints()
    root = output / (runtime + "-" + case)
    root.mkdir(mode=0o700)
    endpoint_file = root / "endpoint.json"
    witness = "PEER_" + uuid.uuid4().hex
    role = "supervisor" if case == "verdict" else "worker"
    config = {"witness": witness, "quiet": case == "quiet", "role": role}
    if role == "supervisor":
        encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
        config["grant"] = "grt_" + "d" * 24
        config["token"] = encode({"typ": "CardinalInvestigation"}) + "." + encode({
            "org": "00000000-0000-0000-0000-00000000000a", "inv": "inv_0123456789abcdef01234567",
            "gid": config["grant"], "scopes": ["read", "advise", "supervise"], "exp": int(time.time()) + 3600}) + ".Zml4dHVyZQ"
    write(root / "server-config.json", config)
    launcher = phase1 / "packages/maestro/node_modules/tsx/dist/cli.mjs"
    server_log = (root / "server.log").open("w")
    server = subprocess.Popen(["node", str(launcher), str(HERE / "phase1-server.ts"), str(phase1), str(root)],
                              stdout=server_log, stderr=server_log)
    try:
        for _ in range(100):
            if endpoint_file.exists():
                break
            if server.poll() is not None:
                raise RuntimeError("Phase 1 server failed: " + (root / "server.log").read_text()[-2000:])
            time.sleep(0.1)
        endpoint = json.loads(endpoint_file.read_text())
        if role == "supervisor":
            c.private_write(root / "grant.json", {"token": config["token"], "origin": endpoint["origin"]})
        for args in (["init", "-q"], ["config", "user.name", "Fixture"], ["config", "user.email", "fixture@example.test"],
                     ["-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "baseline"]):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        cli = REPO / "adapters" / runtime / ("scripts" if runtime == "codex" else "bin") / "cardinal-storyboard"
        hook = rt.hook_path(cli, runtime)
        write(root / "hook-target.json", {"argv": [sys.executable, str(hook), *(["--event", "CoordinationGate"] if runtime == "codex" else [])]})
        hook_command = command(sys.executable, HERE / "run.py", "--helper", "hook", "--root", root)
        prepare = command(sys.executable, HERE / "run.py", "--helper", "prepare", "--root", root)
        helper_cmd = command(sys.executable, HERE / "run.py", "--helper")
        fake_gh = root / "gh"
        fake_gh.write_text("#!/usr/bin/env python3\nimport pathlib,sys\np=pathlib.Path(__file__).parent/'quiet-effects'\np.mkdir(exist_ok=True)\n(p/sys.argv[3]).write_text('effect')\nprint('QUIET_'+sys.argv[3])\n")
        fake_gh.chmod(0o700)
        policy = {"version": 1, "runtime": runtime, "role": role,
                  "peers": [{"principal": "user:" + endpoint["owner"], "session": "peer-session"}],
                  "cli_prefixes": [[str(cli)], [sys.executable, str(cli)]], "git": ["git"], "gh": [str(fake_gh)]}
        write(root / "policy.json", policy)
        write(root / "test.ipynb", {"nbformat": 4, "nbformat_minor": 5, "metadata": {}, "cells": [
            {"cell_type": "code", "id": "probe", "source": ["print('BEFORE')"], "metadata": {}, "outputs": [], "execution_count": None}]})
        (root / "edit-target.txt").write_text("BEFORE")
        hooks = {"SessionStart": [{"hooks": [{"type": "command", "command": prepare, "timeout": 5}]}],
                 "PreToolUse": [{"matcher": "Bash|apply_patch" if runtime == "codex" else "Bash|Write|Edit|NotebookEdit",
                                 "hooks": [{"type": "command", "command": hook_command, "timeout": 5}]}]}
        env = os.environ.copy()
        for name in ("CLAUDECODE", "CLAUDE_CODE_SAFE_MODE", "CLAUDE_CODE_SIMPLE"):
            env.pop(name, None)
        env.update({rt.MODE: "direct-cli", rt.STATE_DIR: str(root / "routes"), "CARDINAL_CONNECTION": "env",
                    "CARDINAL_MCP_URL": endpoint["origin"] + "/api/orgs/" + endpoint["org"] + "/mcp",
                    "CARDINAL_MCP_API_KEY": "phase3-fixture-key", "CARDINAL_OWNER_INPUT": "0",
                    "CARDINAL_STORYBOARD_SESSION_START": "0", "CARDINAL_INVESTIGATION_POLLER": "0"})
        cli_cmd = command(sys.executable, cli, "investigation")
        enable = cli_cmd + " gate-enable --session SID --investigation " + endpoint["inv"] + " --gate-file " + str(root / "gate.json") + " --policy " + str(root / "policy.json")
        if role == "supervisor":
            enable += " --credential-file " + str(root / "grant.json")
        setup = (f"First use Bash/shell to run {helper_cmd} session --root {root}; use the returned session id for SID everywhere. "
                 f"Then create {root / 'probe.txt'} with BEFORE using " + ("apply_patch" if runtime == "codex" else "Write") + ". ")
        if runtime == "claude":
            setup += f"Next use Edit to change BEFORE to AFTER in probe.txt; next use NotebookEdit to replace cell probe in {root / 'test.ipynb'} with print('AFTER'). "
        prompt = ("This is an isolated integration test. Do not inspect any fixture source, state, policy, audit or settings. Do not delegate. "
                  "If the runtime exposes tools only through code mode, use only a transparent uncaught await of the named shell/edit tool. Never catch or suppress a rejection, and never implement effects in JavaScript. "
                  "Use direct shell calls and the named native edit tools. Perform actions in order. " + setup +
                  "Now use one direct shell invocation to run (replace SID): " + enable + ". Stop and report if enablement fails. ")
        original = "ORIGINAL_" + uuid.uuid4().hex
        if case == "quiet":
            prompt += f"Now run these {quiet_count} commands sequentially, each as a separate direct shell call. No loops or compound commands:\n" + "\n".join(command(fake_gh, "pr", "merge", str(i), "--auto") for i in range(quiet_count))
        else:
            if case == "read_error":
                prompt += f"Now run {helper_cmd} arm-error --root {root}. "
            attempt = "git -C " + str(root) + " commit --allow-empty -m " + original
            if case == "verdict":
                attempt = cli_cmd + " verdict --session SID --credential-file " + str(root / "grant.json") + " 1 --unverifiable --scope fixture_validation --text 'Premature original verdict' --repo example.test/fixture/repo --commit " + "0" * 40
            replacement = helper_cmd + " replacement --root " + str(root)
            if case == "verdict":
                revised = attempt.replace("'Premature original verdict'", "'Reconsidered after reading worker finding'")
                replacement = revised + ". After that succeeds, separately run " + replacement
            if case == "patch":
                prompt += f"Now try to create {root / 'original.marker'} containing ORIGINAL using " + ("apply_patch" if runtime == "codex" else "Write") + ". "
            elif case == "edit":
                prompt += f"Now use Edit to change BEFORE to ORIGINAL in {root / 'edit-target.txt'}. "
            elif case == "notebook":
                prompt += f"Now use NotebookEdit to change cell probe in {root / 'test.ipynb'} to print('ORIGINAL'). "
            else:
                prompt += "Your next tool call must be this exact direct shell command: " + attempt + ". "
            if case == "read_error":
                prompt += "After a denial stop. Do not handle, retry, or create any replacement."
            else:
                prompt += ("After denial, abandon that original action. Use the installed CLI above: gate-read --session SID (one call per chunk) "
                           "to read ALL chunks, collecting their hashes and decoding data per record. Never combine reads into one shell command. "
                           + ("Do not acknowledge a worker semantic event. Then " if role == "supervisor" else
                              "Acknowledge cue #1 with ack --session SID 1 accepted --note 'I will abandon the original action' (the fixture binding selects this investigation). Then ") +
                           "gate-reviewed --session SID --batch B --chunk-hash H1 --chunk-hash H2 ... "
                           "with ALL chunk hashes in global order. Only after review succeeds run " + replacement +
                           ". Report the random peer witness from the contribution and your reconsideration. Do not disable the gate or retry the original action.")
        write(root / "hooks.json", {"hooks": hooks})
        (root / "prompt.txt").write_text(prompt)
        if runtime == "codex":
            options = ["exec", "--ignore-user-config", "--skip-git-repo-check", "--ephemeral", "--sandbox", "workspace-write",
                       "--add-dir", str(ie.sessions_dir(Path.home())), "-c", 'sandbox_workspace_write.network_access=true',
                       "-c", 'approval_policy="never"', "-c", 'features.hooks=true', "--dangerously-bypass-hook-trust"]
            options += ["--enable", "code_mode", "--enable", "code_mode_host"]
            options += ["-c", "projects." + json.dumps(str(root)) + '.trust_level="trusted"']
            for event_name, groups in hooks.items():
                value = "[" + ",".join('{matcher=' + json.dumps(group.get("matcher", ".*")) + ',hooks=[{type="command",command=' + json.dumps(group["hooks"][0]["command"]) + ',timeout=5}]}' for group in groups) + "]"
                options += ["-c", "hooks." + event_name + "=" + value]
            argv = [shutil.which(runtime), *options, "--json", "-C", str(root), "-"]
        else:
            argv = [shutil.which(runtime), "-p", "--setting-sources", "", "--settings", str(root / "hooks.json"),
                    "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "dontAsk",
                    "--allowedTools", "Bash,Write,Edit,Read,NotebookEdit", "--tools", "Bash,Write,Edit,Read,NotebookEdit",
                    "--disable-slash-commands", "--no-chrome", "--verbose", "--output-format", "stream-json",
                    "--include-hook-events", "--no-session-persistence"]
        write(root / "metadata.json", {"runtime": runtime, "case": case, "argv": argv,
              "configuration_before": before,
              "version": subprocess.run([shutil.which(runtime), "--version"], capture_output=True, text=True).stdout.strip(),
              "backend": "real Phase 1 router/reader; fixture auth and memory storage", "phase1": str(phase1),
              "hook_sha256": hashlib.sha256(hook.read_bytes()).hexdigest()})
        with (root / "stdout.jsonl").open("w") as stdout, (root / "stderr.log").open("w") as stderr:
            process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, text=True, start_new_session=True)
            try:
                process.communicate(prompt, timeout=timeout)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
        events = jsonl(root / "stdout.jsonl")
        model = []
        for item in events:
            if item.get("type") == "item.completed" and item.get("item", {}).get("type") == "agent_message":
                model.append(item["item"].get("text", ""))
            if item.get("type") == "assistant":
                model += [part.get("text", "") for part in item.get("message", {}).get("content", []) if part.get("type") == "text"]
        commits = subprocess.run(["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True).stdout
        state = c.private_read(root / "gate.json") if (root / "gate.json").exists() else None
        audit = jsonl(root / "server-audit.jsonl")
        hook_audit = jsonl(root / "hook-audit.jsonl")
        denials = [entry for entry in hook_audit if entry["decision"] == "deny"]
        acks = [e for e in audit if e.get("kind") == "append" and e["event"]["type"] == "acknowledged"]
        checks = {"enabled": bool(state and state.get("gate", {}).get("enabled")),
                  "normal_config_unchanged": fingerprints() == before,
                  "original_absent": original not in commits and not (root / "original.marker").exists()}
        if case == "quiet":
            quiet_calls = [e for e in hook_audit if isinstance(e.get("input"), dict) and
                           str(e["input"].get("command", "")).startswith(str(fake_gh) + " pr merge ")]
            checks.update(distinct_effects=len(list((root / "quiet-effects").glob("*"))) == quiet_count,
                          fresh_reads=len([r for r in audit if r.get("body", {}).get("snapshot")]) >= quiet_count + 1,
                          distinct_quiet_calls=len(quiet_calls) == quiet_count,
                          zero_quiet_stdout=all(e["stdout_bytes"] == 0 for e in quiet_calls),
                          no_denial=not denials)
        elif case == "read_error":
            checks.update(no_replacement=not (root / "replacement.marker").exists(), still_unhandled=bool(state and state["after"] == 0),
                          actual_denial=bool(denials))
        else:
            checks.update(acknowledged=not acks if role == "supervisor" else bool(acks),
                          reviewed=bool(state and state["after"] >= 1 and state["pending"] is None),
                          replacement=(root / "replacement.marker").exists(), witness_in_model=witness in "\n".join(model),
                          actual_denial=bool(denials))
            if case == "edit":
                checks["edit_absent"] = (root / "edit-target.txt").read_text() == "BEFORE"
            if case == "notebook":
                checks["notebook_absent"] = "ORIGINAL" not in (root / "test.ipynb").read_text()
            if case == "verdict":
                verdicts = [e["event"] for e in audit if e.get("kind") == "append" and e["event"]["type"] == "verification.recorded"]
                checks["verdict_reconsidered"] = len(verdicts) == 1 and verdicts[0]["payload"]["text"] == "Reconsidered after reading worker finding"
        if hook_audit:
            checks["feedback_bounded"] = all(entry["reason_bytes"] <= 1000 for entry in hook_audit)
        result = {"runtime": runtime, "case": case, "checks": checks, "passed": all(checks.values()), "exit_code": process.returncode,
                  "quiet_count": quiet_count if case == "quiet" else None,
                  "hook_calls": len(hook_audit), "denials": len(denials),
                  "configuration_after": fingerprints(),
                  "max_feedback_bytes": max([e["reason_bytes"] for e in hook_audit] or [0])}
        write(root / "result.json", result)
        return result
    finally:
        server.terminate()
        server.wait(timeout=10)
        server_log.close()
        # Only the binding this fixture created. Keep a copy beside the evidence.
        if (root / "session.json").exists():
            saved = json.loads((root / "session.json").read_text())
            binding = Path(saved["binding"])
            if binding.exists():
                shutil.copyfile(binding, root / "binding-final.json")
                binding.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", choices=["codex", "claude", "all"], default="all")
    parser.add_argument("--cases", nargs="+", choices=["reconsider", "patch", "edit", "notebook", "verdict", "quiet", "read_error"], default=["reconsider"])
    parser.add_argument("--phase1", type=Path, default=Path("/Users/ruchirj/workspace/conductor-coordination-snapshot"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--quiet-count", type=int, default=10)
    parser.add_argument("--clean-project-trust", action="store_true", help="remove only this testbed's disposable project trust tables")
    parser.add_argument("--helper", choices=["prepare", "session", "replacement", "arm-error", "hook"])
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    if args.clean_project_trust:
        print("Removed fixture project trust tables:", clean_project_trust(HERE / "runs"))
        return
    if not args.helper and args.runtime != "claude" and any(case in ("edit", "notebook") for case in args.cases):
        parser.error("edit/notebook probes require --runtime claude")
    if args.helper:
        helper(args.root, args.helper)
        return
    output = (args.output or HERE / "runs" / (time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6])).resolve()
    output.mkdir(parents=True)
    runtimes = ["codex", "claude"] if args.runtime == "all" else [args.runtime]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(run, runtime, case, output, args.phase1, args.timeout, args.quiet_count) for case in args.cases for runtime in runtimes]
            results = [future.result() for future in tasks]
    finally:
        removed = clean_project_trust(output)
    write(output / "results.json", results)
    print(json.dumps({"output": str(output), "results": results, "fixture_trust_tables_removed": removed}, indent=2))
    if not all(result["passed"] for result in results):
        sys.exit(1)


if __name__ == "__main__":
    main()
