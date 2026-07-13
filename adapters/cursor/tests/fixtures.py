"""Shared fixtures for the Cursor adapter's golden capture + parity test.

The SAME synthetic hook payloads, sandbox layout, and deterministic git
repo are used by:

  * capture_goldens.py — runs the pre-migration shipped hook script
    (cardinal-cursor-plugin v0.2.0) and freezes its normalized OTLP
    output + hook stdout into tests/goldens/*.json.
  * test_parity.py — runs the migrated adapter hook against the same
    fixtures and asserts byte-equal normalized output.

Payload spellings intentionally mix camelCase (conversationId, toolName,
toolInput, durationMs, modelId, modelParams, cursorVersion, …) and
snake_case (generation_id, trigger, context_tokens, status, task, …)
exactly where the shipped v0.2.0 hook accepts each form — the fixture is
the contract, and Cursor's payloads are the most camelCase-divergent of
the four agents.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

TESTS_DIR = Path(__file__).resolve().parent
ADAPTER_DIR = TESTS_DIR.parent
REPO_ROOT = ADAPTER_DIR.parent.parent
GOLDENS_DIR = TESTS_DIR / "goldens"

# core/tests/harness.py provides StubIngest + _normalize (read-only import).
sys.path.insert(0, str(REPO_ROOT / "core" / "tests"))
from harness import StubIngest, _normalize  # noqa: E402

CONV_ID = "conv-golden-0001"
FIXTURE_BRANCH = "feat/golden-fixture"
FIXTURE_REMOTE = "git@github.com:cardinalhq/golden-fixture.git"

# Cursor base fields present on every hook payload (Divergence L):
# stamped onto the OTLP resource as cursor.model / cursor.model_id /
# cursor.model_params / cursor.version.
BASE_FIELDS: dict[str, Any] = {
    "model": "claude-4.5-sonnet",
    "modelId": "anthropic/claude-4.5-sonnet",
    "modelParams": {"temperature": 0.2, "max_tokens": 4096},
    "cursorVersion": "1.7.29",
}

NOTIFY_MESSAGE = "You are at 60% of session budget."

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Golden Fixture",
    "GIT_AUTHOR_EMAIL": "golden@cardinalhq.io",
    "GIT_COMMITTER_NAME": "Golden Fixture",
    "GIT_COMMITTER_EMAIL": "golden@cardinalhq.io",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def build_sandbox(root: Path, ingest_endpoint: str) -> dict[str, Any]:
    """Prefabricated HOME with ~/.cursor connection state pointing at the
    stub ingest, plus a deterministic git repo (fixed dates + identity →
    stable HEAD sha across capture and parity runs)."""
    home = root
    cursor = home / ".cursor"
    cursor.mkdir(parents=True, exist_ok=True)
    (cursor / "cardinal.json").write_text(json.dumps({
        "schema_version": 1,
        "ingest_endpoint": ingest_endpoint,
        "deployment_environment": "prod",
        "user_email": "golden@cardinalhq.io",
        "org_slug": "cardinal-golden",
    }, indent=2) + "\n")
    (cursor / "cardinal-secrets.json").write_text(json.dumps({
        "ingest_api_key": "test-ingest-key",
        "ingest_api_header": "x-cardinalhq-api-key",
    }, indent=2) + "\n")

    repo = home / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **_GIT_ENV, "HOME": str(home)}

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                       capture_output=True, text=True)

    _git("init", "-q")
    _git("checkout", "-q", "-b", FIXTURE_BRANCH)
    (repo / "README.md").write_text("golden fixture\n")
    _git("add", "README.md")
    _git("commit", "-q", "-m", "golden fixture commit")
    _git("remote", "add", "origin", FIXTURE_REMOTE)

    return {"home": home, "cursor": cursor, "repo": repo}


def hook_env(home: Path) -> dict[str, str]:
    env = {**os.environ, "HOME": str(home), **_GIT_ENV}
    for var in ("CARDINAL_CURSOR_DEBUG_PAYLOADS", "CARDINAL_CURSOR_STRICT_WARN",
                "CURSOR_PROJECT_DIR"):
        env.pop(var, None)
    return env


def stage_notify(cursor_dir: Path, conv_id: str, message: str) -> None:
    """Prefabricate the Divergence-E staged notify file the way the
    beforeSubmitPrompt gate writes it, so postToolUse surfaces it."""
    limits = cursor_dir / "cardinal" / "limits"
    limits.mkdir(parents=True, exist_ok=True)
    (limits / f"{conv_id}.notify.json").write_text(
        json.dumps({"message": message, "band": 1, "staged_at": 0})
    )


def fixture_steps(repo: Path) -> list[dict[str, Any]]:
    """Ordered hook invocations. `pre` names a setup action the runner
    performs immediately before invoking the hook for that step."""
    repo_s = str(repo)
    return [
        {
            "name": "01-sessionStart",
            "event": "sessionStart",
            "payload": {
                "hook_event_name": "sessionStart",
                "conversationId": CONV_ID,
                "workspace_roots": [repo_s],
                **BASE_FIELDS,
            },
        },
        {
            "name": "02-beforeSubmitPrompt-plain",
            "event": "beforeSubmitPrompt",
            "payload": {
                "hook_event_name": "beforeSubmitPrompt",
                "conversationId": CONV_ID,
                "workspaceRoots": [repo_s],
                "prompt": "Refactor the ingest pipeline for retries",
                **BASE_FIELDS,
            },
        },
        {
            "name": "03-postToolUse-shell-compound",
            "event": "postToolUse",
            "payload": {
                "hook_event_name": "postToolUse",
                "conversationId": CONV_ID,
                "workspaceRoots": [repo_s],
                "generation_id": "gen-1",
                "toolName": "run_terminal_cmd",
                "toolInput": {"command": "tsc && pytest -k parity"},
                "toolOutput": {"exit_code": 0, "stdout": "2 passed"},
                **BASE_FIELDS,
            },
        },
        {
            "name": "04-postToolUse-mcp",
            "event": "postToolUse",
            "payload": {
                "hook_event_name": "postToolUse",
                "conversationId": CONV_ID,
                "workspaceRoots": [repo_s],
                "generation_id": "gen-1",
                "toolName": "mcp__cardinal__lakerunner__list_services",
                "toolInput": {"instance": "prod"},
                "toolOutput": {"text": "3 services"},
                **BASE_FIELDS,
            },
        },
        {
            "name": "05-postToolUse-read-file-notify",
            "event": "postToolUse",
            "pre": "stage_notify",
            "payload": {
                "hook_event_name": "postToolUse",
                "conversationId": CONV_ID,
                "workspaceRoots": [repo_s],
                "generation_id": "gen-2",
                "toolName": "read_file",
                "toolInput": {"path": "src/main.py"},
                "toolOutput": "def main() -> None: ...",
                **BASE_FIELDS,
            },
        },
        {
            "name": "06-beforeSubmitPrompt-command",
            "event": "beforeSubmitPrompt",
            "payload": {
                "hook_event_name": "beforeSubmitPrompt",
                "conversationId": CONV_ID,
                "workspaceRoots": [repo_s],
                "prompt": "/cardinal:status check the connection",
                **BASE_FIELDS,
            },
        },
        {
            "name": "07-subagentStop",
            "event": "subagentStop",
            "payload": {
                "hook_event_name": "subagentStop",
                "conversationId": CONV_ID,
                "workspaceRoots": [repo_s],
                "subagentType": "explore",
                "status": "completed",
                "task": "Find OTLP emit callsites",
                "description": "Search the repo for OTLP emit callsites",
                "summary": "Found 3 callsites",
                "durationMs": 15321,
                "messageCount": 12,
                "toolCallCount": 7,
                "loopCount": 2,
                "modified_files": [],
                "agent_transcript_path": "/tmp/subagent.jsonl",
                **BASE_FIELDS,
            },
        },
        {
            "name": "08-afterAgentThought",
            "event": "afterAgentThought",
            "payload": {
                "hook_event_name": "afterAgentThought",
                "conversationId": CONV_ID,
                "durationMs": 842,
                "text": "considering the parity constraints carefully",
                **BASE_FIELDS,
            },
        },
        {
            "name": "09-afterAgentResponse",
            "event": "afterAgentResponse",
            "payload": {
                "hook_event_name": "afterAgentResponse",
                "conversationId": CONV_ID,
                "text": "Here is the migration plan.",
                **BASE_FIELDS,
            },
        },
        {
            "name": "10-preCompact",
            "event": "preCompact",
            "payload": {
                "hook_event_name": "preCompact",
                "conversationId": CONV_ID,
                "workspaceRoots": [repo_s],
                "trigger": "auto",
                "context_usage_percent": 87,
                "context_tokens": 174000,
                "context_window_size": 200000,
                "message_count": 42,
                "messages_to_compact": 30,
                "is_first_compaction": True,
                **BASE_FIELDS,
            },
        },
    ]


def run_hook(script: Path, event: str, payload: dict[str, Any],
             home: Path) -> str:
    """Invoke a telemetry hook script the way Cursor does: payload on
    stdin, event via --event, sandboxed HOME. Returns raw stdout."""
    out = subprocess.run(
        [sys.executable, str(script), "--event", event],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=hook_env(home),
        timeout=30,
    )
    return out.stdout


def _scrub(node: Any, sandbox_root: str) -> Any:
    """Adapter-local normalization on TOP of core harness._normalize
    (which is read-only for us):

      * drop `cardinal.core_version` resource attributes entirely — the
        pre-migration plugin never emitted them, so pinning the value
        (what harness does) still leaves a key-presence diff;
      * pin `cardinal.plugin_version` values and OTel scope versions —
        the adapter's manifest version may drift from the shipped
        plugin's;
      * replace the sandbox path in every stringValue — the sandbox is a
        fresh tempdir on each run.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "attributes" and isinstance(v, list):
                kept = []
                for a in v:
                    if isinstance(a, dict) and a.get("key") == "cardinal.core_version":
                        continue
                    if isinstance(a, dict) and a.get("key") == "cardinal.plugin_version":
                        a = {**a, "value": {"stringValue": "<normalized>"}}
                    kept.append(_scrub(a, sandbox_root))
                out[k] = kept
            elif k == "scope" and isinstance(v, dict) and "version" in v:
                out[k] = {**_scrub(v, sandbox_root), "version": "<normalized>"}
            else:
                out[k] = _scrub(v, sandbox_root)
        return out
    if isinstance(node, list):
        return [_scrub(x, sandbox_root) for x in node]
    if isinstance(node, str):
        return node.replace(sandbox_root, "<SANDBOX>")
    return node


def normalize_batches(batches: list[dict[str, Any]], sandbox_root: Path) -> list[dict[str, Any]]:
    return [_scrub(_normalize(b), str(sandbox_root)) for b in batches]


def normalize_stdout(raw: str, sandbox_root: Path) -> Any:
    if not raw.strip():
        return None
    return _scrub(json.loads(raw), str(sandbox_root))


def run_all_steps(script: Path, stub: StubIngest, sandbox: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Run every fixture step against `script`; return
    {step_name: {"stdout": ..., "batches": [...]}} with normalized values."""
    results: dict[str, dict[str, Any]] = {}
    for step in fixture_steps(sandbox["repo"]):
        if step.get("pre") == "stage_notify":
            stage_notify(sandbox["cursor"], CONV_ID, NOTIFY_MESSAGE)
        before = len(stub.log_batches)
        raw_stdout = run_hook(script, step["event"], step["payload"], sandbox["home"])
        new_batches = stub.log_batches[before:]
        results[step["name"]] = {
            "stdout": normalize_stdout(raw_stdout, sandbox["home"]),
            "batches": normalize_batches(new_batches, sandbox["home"]),
        }
    return results
