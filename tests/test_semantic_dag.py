"""Cross-adapter contract tests for the shared Semantic DAG emitter."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
EMITTERS = {
    "codex": ROOT / "adapters/codex/skills/semantic-dag/scripts/emit.py",
    "claude": ROOT / "adapters/claude/skills/semantic-dag/emit.py",
}


def _without_volatile(value):
    if isinstance(value, dict):
        return {
            key: _without_volatile(item)
            for key, item in value.items()
            if key not in {"created", "updated", "ts", "session_started", "cwd"}
        }
    if isinstance(value, list):
        return [_without_volatile(item) for item in value]
    return value


class SemanticDagAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def emit(self, runtime: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "SEMANTIC_DAG_STATE_DIR": str(self.root / runtime),
                "SEMANTIC_DAG_NO_SERVER": "1",
                "SEMANTIC_DAG_NO_OPEN": "1",
            }
        )
        return subprocess.run(
            [sys.executable, str(EMITTERS[runtime]), *arguments],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )

    def dag(self, runtime: str) -> dict:
        path = self.root / runtime / "threads/shared/dag.json"
        return json.loads(path.read_text())

    def exercise(self, runtime: str) -> dict:
        common = ("--thread", "shared")
        self.emit(runtime, "start", "Shared semantic memory", *common)
        self.emit(
            runtime,
            "add", "goal", "GOAL", "Unify semantic emission", "--root",
            "--description", "Keep graph semantics identical across runtimes.",
            *common,
        )
        self.emit(runtime, "activate", "goal", *common)
        self.emit(runtime, "note", "goal", "The shared engine is active.", *common)
        self.emit(runtime, "file", "goal", "read", "README.md", *common)
        self.emit(
            runtime,
            "concept", "goal", "adapter", "A thin runtime-specific entrypoint.",
            *common,
        )
        self.emit(
            runtime,
            "start", "Inspect parity", "--agent", "scout", "--parent", "goal",
            *common,
        )
        self.emit(
            runtime,
            "add", "proof", "EVIDENCE", "Confirm adapter parity",
            "--description", "Exercise namespaced subagent state.",
            "--agent", "scout", *common,
        )
        self.emit(runtime, "activate", "proof", "--agent", "scout", *common)
        return self.dag(runtime)

    def test_codex_and_claude_materialize_the_same_graph(self) -> None:
        codex = self.exercise("codex")
        claude = self.exercise("claude")
        self.assertEqual(_without_volatile(codex), _without_volatile(claude))
        self.assertEqual(codex["active_by_agent"], {"root": "goal", "scout": "scout::proof"})
        self.assertEqual(codex["nodes"]["scout::proof"]["agent"], "scout")
        self.assertIn(
            {"from": "goal", "to": "scout::proof", "relationship": "decomposes_into"},
            codex["edges"],
        )

    def test_entrypoints_are_thin_core_adapters(self) -> None:
        for runtime, emitter in EMITTERS.items():
            source = emitter.read_text()
            with self.subTest(runtime=runtime):
                self.assertIn("from cardinal_core.semantic_dag import RuntimeConfig, main", source)
                self.assertNotIn("def _apply(", source)
                self.assertNotIn("def emit(", source)

    def test_claude_viewer_restores_all_active_agents(self) -> None:
        source = (
            ROOT / "adapters/claude/skills/semantic-dag/viewer/index.html"
        ).read_text()
        self.assertIn("d.active_by_agent", source)
        self.assertIn("Object.values(activeByAgent)", source)


if __name__ == "__main__":
    unittest.main()
