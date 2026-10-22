#!/usr/bin/env python3
"""Capture the Copilot adapter's golden OTLP fixtures.

Unlike the migrated adapters (codex / cursor / gemini / claude), Copilot has
no pre-migration plugin to compare against: these goldens are captured from
THIS adapter's hook, against the payload shapes in Copilot CLI's hooks
reference, and frozen so tests/test_goldens.py catches unreviewed output
changes and tests/test_contract.py (repo root) checks the event contract.

Usage (from the repo root, after `python3 build/vendor.py copilot`):
    python3 adapters/copilot/tests/capture_goldens.py

Review the diff of tests/goldens/ before committing a re-capture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402
from harness import _normalize  # noqa: E402  (core/tests, put on sys.path by fixtures)

GOLDENS_DIR = fixtures.TESTS_DIR / "goldens"
# Attribute values that differ per run (temp paths, a fresh commit, clocks).
VOLATILE_KEYS = {"cardinal_head_sha", "ts"}


def steps(repo: Path) -> list[tuple[str, str, dict]]:
    base = {"sessionId": fixtures.SID, "timestamp": 1760000000000, "cwd": str(repo)}

    def tool(name: str, args: Any, text: str, result_type: str = "success") -> dict:
        return {**base, "toolName": name, "toolArgs": args,
                "toolResult": {"resultType": result_type, "textResultForLlm": text}}

    return [
        ("01-userPromptSubmitted-command", "userPromptSubmitted", {**base, "prompt": "/review the change"}),
        ("02-postToolUse-bash", "postToolUse",
         tool("bash", json.dumps({"command": "git status && ls"}), "On branch\n<exited with exit code 0>")),
        ("03-postToolUse-mcp", "postToolUse",
         tool("cardinal-lakerunner__list_services", {"instance": "prod"}, '{"services": []}')),
        ("04-postToolUse-view-failed", "postToolUse",
         tool("view", {"path": "README.md"}, "no such file", result_type="failure")),
        ("05-subagentStop", "subagentStop",
         {**base, "agentName": "explore", "agentType": "explore", "agentDisplayName": "Explore the hooks",
          "agentId": "agent-1", "stopReason": "end_turn", "transcriptPath": "/dev/null"}),
        ("06-preCompact", "preCompact", {**base, "trigger": "auto", "customInstructions": ""}),
    ]


def _scrub(node: Any, tmp: str) -> Any:
    if isinstance(node, dict):
        if isinstance(node.get("key"), str) and node["key"] in VOLATILE_KEYS and isinstance(node.get("value"), dict):
            return {**node, "value": {"stringValue": "<normalized>"}}
        return {k: _scrub(v, tmp) for k, v in node.items()}
    if isinstance(node, list):
        return [_scrub(x, tmp) for x in node]
    if isinstance(node, str) and tmp in node:
        return node.replace(tmp, "<tmp>")
    return node


def capture() -> dict[str, dict]:
    """{golden name: {"stdout": hook stdout or None, "batches": normalized OTLP}}."""
    stub = fixtures.StubIngest().start()
    out: dict[str, dict] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="cardinal-copilot-golden-") as raw_tmp:
            tmp = os.path.realpath(raw_tmp)
            base = Path(tmp)
            home = base / "home"
            (home / ".copilot").mkdir(parents=True)
            (home / ".copilot" / "cardinal.json").write_text(json.dumps({
                "schema_version": 1, "ingest_endpoint": stub.endpoint, "deployment_environment": "prod",
                "user_email": "golden@cardinalhq.io", "org_slug": "cardinal-golden"}))
            (home / ".copilot" / "cardinal-secrets.json").write_text(json.dumps({"ingest_api_key": "golden-key"}))
            repo = fixtures.make_repo(base / "repo", branch="feat/golden-fixture",
                                      remote="git@github.com:cardinalhq/golden-fixture.git")
            bin_dir = base / "bin"
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text("#!/bin/sh\nexit 1\n")
            gh.chmod(0o755)
            env = fixtures.clean_env(home, PATH=f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
                                     CARDINAL_COPILOT_BACKGROUND_INLINE="1", CARDINAL_INVESTIGATION_POLLER="0",
                                     CARDINAL_EVIDENCE_CAPTURE="0")
            for name, event, payload in steps(repo):
                before = len(stub.log_batches)
                res = subprocess.run([sys.executable, str(fixtures.HOOK), "--event", event],
                                     input=json.dumps(payload), capture_output=True, text=True, env=env,
                                     cwd=str(repo), timeout=60)
                if res.returncode != 0:
                    raise RuntimeError(f"{name}: hook exited {res.returncode}: {res.stderr}")
                batches = list(stub.log_batches)[before:]
                out[name] = _scrub({"stdout": res.stdout or None, "batches": _normalize(batches)}, tmp)
    finally:
        stub.stop()
    return out


def main() -> int:
    GOLDENS_DIR.mkdir(parents=True, exist_ok=True)
    for name, blob in capture().items():
        path = GOLDENS_DIR / f"{name}.json"
        path.write_text(json.dumps(blob, indent=2) + "\n")
        print(f"wrote {path.name}: {len(blob['batches'])} batch(es)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
