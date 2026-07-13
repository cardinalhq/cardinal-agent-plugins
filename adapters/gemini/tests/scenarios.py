"""Shared synthetic Gemini hook-payload scenarios.

Used twice, with the SAME definitions:

  1. capture_goldens.py runs the pre-migration hook script (from the shipped
     cardinal-gemini-plugin repo) over these scenarios and freezes the
     normalized OTLP batches + hook stdout as goldens.
  2. test_parity.py runs the migrated adapter hook over the same scenarios
     and asserts normalized output == goldens.

Every scenario runs in an isolated sandbox: a fresh HOME with prefabricated
~/.gemini/cardinal.json + cardinal-secrets.json pointing at a StubIngest,
plus (where needed) a deterministic fixture git repo (fixed commit
identity + dates so the head SHA is stable across runs).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parents[2]  # monorepo root
sys.path.insert(0, str(_ROOT / "core" / "tests"))
from harness import StubIngest  # noqa: E402

HOOK_TIMEOUT = 15

# Deterministic git identity/dates → deterministic fixture-repo head SHA.
_FIXED_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_AUTHOR_NAME": "Cardinal Fixture",
    "GIT_AUTHOR_EMAIL": "fixture@cardinalhq.io",
    "GIT_COMMITTER_NAME": "Cardinal Fixture",
    "GIT_COMMITTER_EMAIL": "fixture@cardinalhq.io",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00 +0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00 +0000",
}

FIXTURE_BRANCH = "feat/outcomes-observability"
FIXTURE_REMOTE = "git@github.com:cardinalhq/fixture-repo.git"

# Long prompt for the subagent_description 160-char truncation case.
LONG_PROMPT = (
    "Investigate every flaky integration test in the payments suite, "
    "bisect the responsible commits, propose fixes with minimal diffs, "
    "and write a summary of root causes ranked by blast radius."
)


def make_fixture_repo(root: Path) -> Path:
    repo = root / "fixture-repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **_FIXED_GIT_ENV}

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                       capture_output=True, timeout=10)

    _git("init", "-q", "-b", FIXTURE_BRANCH)
    _git("commit", "--allow-empty", "-q", "-m", "fixture: initial commit")
    _git("remote", "add", "origin", FIXTURE_REMOTE)
    return repo


def write_connected_state(home: Path, endpoint: str) -> None:
    gemini = home / ".gemini"
    gemini.mkdir(parents=True, exist_ok=True)
    (gemini / "cardinal.json").write_text(json.dumps({
        "schema_version": 1,
        "host": "https://app.cardinalhq.io",
        "mode": "telemetry-and-mcp",
        "org_slug": "fixture-org",
        "user_email": "fixture@cardinalhq.io",
        "deployment_environment": "prod",
        "ingest_endpoint": endpoint,
        "telemetry": {"enabled": True},
    }, indent=2) + "\n")
    (gemini / "cardinal-secrets.json").write_text(json.dumps({
        "schema_version": 1,
        "ingest_api_key": "fixture-ingest-key",
        "ingest_api_header": "x-cardinalhq-api-key",
    }, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Scenario setup callables (run against the sandbox HOME at execution time)
# ---------------------------------------------------------------------------

def _setup_plan_stamp(home: Path) -> None:
    tel = home / ".gemini" / "cardinal" / "telemetry"
    tel.mkdir(parents=True, exist_ok=True)
    (tel / "plan.json").write_text(json.dumps({
        "plan_type": "gemini-code-assist-standard",
        "rate_limit_tier": "tier-2",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }) + "\n")


def _setup_limits_verdicts(home: Path) -> None:
    limits = home / ".gemini" / "cardinal" / "limits"
    limits.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (limits / "s-lim.verdict.json").write_text(json.dumps({
        "decision": "warn",
        "band": 2,
        "agent_context": "Cardinal budget standing: 80% of the weekly "
                         "engineer budget is spent. Work economically.",
        "user_message": "You are at 80% of your weekly Cardinal budget.",
        "fetched_at": now,
    }))
    (limits / "s-lim-block.verdict.json").write_text(json.dumps({
        "decision": "block",
        "band": 3,
        "block_reason": "The weekly spend limit for this initiative is exhausted.",
        "fetched_at": now,
    }))


# ---------------------------------------------------------------------------
# Scenario table
# ---------------------------------------------------------------------------

def scenarios() -> list[dict[str, Any]]:
    return [
        {
            "name": "session-start",
            "needs_repo": True,
            "steps": [
                ("SessionStart", {"session_id": "s-start", "cwd": "__REPO__"}),
                ("SessionStart", {"session_id": "s-start", "cwd": "__PLAIN__"}),
            ],
        },
        {
            "name": "before-agent",
            "needs_repo": True,
            "steps": [
                ("BeforeAgent", {"session_id": "s-agent", "cwd": "__REPO__",
                                 "prompt": "please fix the login bug"}),
                ("BeforeAgent", {"session_id": "s-agent", "cwd": "__REPO__",
                                 "prompt": "/code-review --fix"}),
                ("BeforeAgent", {"session_id": "s-agent", "cwd": "__REPO__",
                                 "prompt": "ran <command-name>/cardinal:status</command-name> already"}),
                ("BeforeAgent", {"session_id": "s-agent", "cwd": "__PLAIN__",
                                 "prompt": "now outside the repo"}),
            ],
        },
        {
            "name": "after-model",
            "needs_repo": False,
            "steps": [
                ("BeforeAgent", {"session_id": "s-model", "cwd": "__PLAIN__",
                                 "prompt": "hi"}),
                # `usage` spelling, priced model, thought tokens bill as output.
                ("AfterModel", {"session_id": "s-model",
                                "model": "gemini-2.0-flash",
                                "usage": {"input_tokens": 1200,
                                          "output_tokens": 340,
                                          "thought_tokens": 64,
                                          "cached_input_tokens": 200}}),
                # `usageMetadata` spelling, dated SKU → longest-prefix pricing.
                ("AfterModel", {"session_id": "s-model",
                                "modelId": "gemini-2.0-pro-2026-03-01",
                                "usageMetadata": {"promptTokenCount": 5000,
                                                  "candidatesTokenCount": 800,
                                                  "thoughtsTokenCount": 1200,
                                                  "cachedContentTokenCount": 2500,
                                                  "toolUsePromptTokenCount": 300}}),
                # Unpriced model → no cost_usd attribute.
                ("AfterModel", {"session_id": "s-model",
                                "model": "imagen-4-ultra",
                                "usage": {"input_tokens": 10, "output_tokens": 5}}),
                # Plan facts surface → plan stamp + cardinal.plan_state.
                ("AfterModel", {"session_id": "s-model",
                                "model": "gemini-1.5-flash",
                                "planType": "gemini-code-assist-standard",
                                "rateLimitTier": "tier-2",
                                "usage": {"input_tokens": 700, "output_tokens": 90}}),
                # Same plan facts again → stamped records, NO new plan_state.
                ("AfterModel", {"session_id": "s-model",
                                "model": "gemini-1.5-flash",
                                "usage": {"input_tokens": 800, "output_tokens": 100}}),
                # All-zero usage → suppressed entirely.
                ("AfterModel", {"session_id": "s-model",
                                "model": "gemini-2.0-flash",
                                "usage": {"input_tokens": 0, "output_tokens": 0}}),
            ],
        },
        {
            "name": "after-tool",
            "needs_repo": False,
            "steps": [
                ("BeforeAgent", {"session_id": "s-tool", "cwd": "__PLAIN__",
                                 "prompt": "run the checks"}),
                # Compound bash → write-risk class wins, bash_multi set.
                ("AfterTool", {"session_id": "s-tool",
                               "tool_name": "run_shell_command",
                               "tool_input": {"command": "ls -la && rm -rf build"},
                               "success": True}),
                # git read + exit_code fallback for success.
                ("AfterTool", {"session_id": "s-tool",
                               "tool_name": "run_shell_command",
                               "tool_input": {"command": "git status"},
                               "exit_code": 0}),
                # Qualified MCP tool, arguments as a JSON string, status fallback.
                ("AfterTool", {"session_id": "s-tool",
                               "tool_name": "mcp__lakerunner__list_services",
                               "arguments": "{\"instance\": \"prod\"}",
                               "status": "ok"}),
                # read_file with a path target, explicit failure.
                ("AfterTool", {"session_id": "s-tool",
                               "tool_name": "read_file",
                               "tool_input": {"path": "src/main.py"},
                               "success": False}),
                # Claude-style passthrough name with file_path target.
                ("AfterTool", {"session_id": "s-tool",
                               "tool_name": "Read",
                               "toolInput": {"file_path": "/etc/hosts"},
                               "status": "completed"}),
                # write_file, exitCode!=0 → success false.
                ("AfterTool", {"session_id": "s-tool",
                               "tool_name": "write_file",
                               "tool_input": {"file_path": "out.txt"},
                               "exitCode": 1}),
            ],
        },
        {
            "name": "after-agent",
            "needs_repo": False,
            "setup": _setup_plan_stamp,
            "steps": [
                ("AfterAgent", {"session_id": "s-sub",
                                "subagent_type": "code-reviewer",
                                "model": "gemini-2.0-pro",
                                "usage": {"total_tokens": 4200}}),
                ("AfterAgent", {"session_id": "s-sub",
                                "description": "review the diff for bugs"}),
                ("AfterAgent", {"session_id": "s-sub",
                                "agentId": "a-123", "durationMs": 5400,
                                "status": "completed"}),
                ("AfterAgent", {"session_id": "s-sub",
                                "usageMetadata": {"totalTokenCount": 991}}),
                ("AfterAgent", {"session_id": "s-sub",
                                "tool_input": {"description": "scan dependencies"}}),
                ("AfterAgent", {"session_id": "s-sub", "prompt": LONG_PROMPT}),
                # No identifying facet → suppressed.
                ("AfterAgent", {"session_id": "s-sub", "status": "done"}),
            ],
        },
        {
            "name": "pre-compress",
            "needs_repo": False,
            "steps": [
                ("PreCompress", {"session_id": "s-comp",
                                 "context_tokens": 150000,
                                 "context_window_size": 200000,
                                 "context_usage_percent": 75.0,
                                 "trigger": "auto",
                                 "messages_to_compact": 42,
                                 "is_first_compaction": True}),
                ("PreCompress", {"session_id": "s-comp",
                                 "contextTokens": 10,
                                 "contextWindowSize": 100,
                                 "contextUsagePercent": 10.5,
                                 "isFirstCompaction": False}),
            ],
        },
        {
            "name": "session-end",
            "needs_repo": False,
            "steps": [
                ("SessionEnd", {"session_id": "s-end", "reason": "exit"}),
            ],
        },
        {
            "name": "limits-gate",
            "needs_repo": False,
            "setup": _setup_limits_verdicts,
            "steps": [
                # warn verdict: additionalContext + systemMessage, once.
                ("BeforeAgent", {"session_id": "s-lim", "cwd": "__PLAIN__",
                                 "prompt": "keep going"}),
                # band hysteresis: same band → silent.
                ("BeforeAgent", {"session_id": "s-lim", "cwd": "__PLAIN__",
                                 "prompt": "still going"}),
                # block verdict: enforced on every turn.
                ("BeforeAgent", {"session_id": "s-lim-block", "cwd": "__PLAIN__",
                                 "prompt": "spend more"}),
                ("BeforeAgent", {"session_id": "s-lim-block", "cwd": "__PLAIN__",
                                 "prompt": "spend even more"}),
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Runner + normalization
# ---------------------------------------------------------------------------

def _substitute(payload: dict[str, Any], repo: Path | None, plain: Path) -> dict[str, Any]:
    out = dict(payload)
    cwd = out.get("cwd")
    if cwd == "__REPO__":
        out["cwd"] = str(repo)
    elif cwd == "__PLAIN__":
        out["cwd"] = str(plain)
    return out


def run_scenario(hook_script: Path, scenario: dict[str, Any], workdir: Path) -> dict[str, Any]:
    """Execute one scenario's event steps against `hook_script` inside a
    sandbox rooted at `workdir`. Returns {"steps": [...], "batches": [...]}
    with volatile fields normalized."""
    home = workdir / "home"
    plain = workdir / "plain"
    (home / ".gemini").mkdir(parents=True, exist_ok=True)
    plain.mkdir(parents=True, exist_ok=True)
    repo = make_fixture_repo(workdir) if scenario.get("needs_repo") else None

    stub = StubIngest().start()
    try:
        write_connected_state(home, stub.endpoint)
        setup: Callable[[Path], None] | None = scenario.get("setup")
        if setup:
            setup(home)

        env = {k: v for k, v in os.environ.items()
               if k not in ("GEMINI_SESSION_ID", "CARDINAL_GEMINI_DEBUG_PAYLOADS")}
        env["HOME"] = str(home)
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_SYSTEM"] = "/dev/null"

        steps_out = []
        for event, payload in scenario["steps"]:
            proc = subprocess.run(
                [sys.executable, str(hook_script), "--event", event],
                input=json.dumps(_substitute(payload, repo, plain)).encode(),
                capture_output=True,
                timeout=HOOK_TIMEOUT,
                env=env,
                cwd=str(workdir),
            )
            steps_out.append({
                "event": event,
                "returncode": proc.returncode,
                "stdout": proc.stdout.decode("utf-8", "replace"),
            })
        batches = stub.normalized_batches()
    finally:
        stub.stop()

    return {"steps": steps_out, "batches": normalize_extra(batches)}


_NORMALIZED_ATTR_VALUES = {"cardinal_cwd", "cardinal.plugin_version"}
_DROPPED_ATTRS = {"cardinal.core_version"}


def normalize_extra(node: Any) -> Any:
    """Adapter-local normalization on top of harness.normalized_batches():

    - `cardinal.plugin_version` (resource attr) and OTel scope `version`
      pinned: the monorepo adapter versions independently of the source repo.
    - `cardinal_cwd` pinned: sandbox temp paths differ per run.
    - `cardinal.core_version` resource attr DROPPED: it exists only in
      core-emitting (post-migration) output by design — documented in
      REPORT.md; every other attribute must match byte-for-byte.
    """
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "attributes" and isinstance(v, list):
                kept = []
                for a in v:
                    if isinstance(a, dict) and a.get("key") in _DROPPED_ATTRS:
                        continue
                    if isinstance(a, dict) and a.get("key") in _NORMALIZED_ATTR_VALUES:
                        kept.append({**a, "value": {"stringValue": "<normalized>"}})
                    else:
                        kept.append(normalize_extra(a))
                out[k] = kept
            elif k == "scope" and isinstance(v, dict):
                out[k] = {**v, "version": "<normalized>"}
            else:
                out[k] = normalize_extra(v)
        return out
    if isinstance(node, list):
        return [normalize_extra(x) for x in node]
    return node
