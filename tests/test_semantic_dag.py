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
PROMPT_HOOKS = {
    "codex": ROOT / "adapters/codex/skills/semantic-dag/scripts/hooks/prompt_hook.py",
    "claude": ROOT / "adapters/claude/skills/semantic-dag/hooks/prompt_hook.py",
}
SKILLS = {
    "codex": ROOT / "adapters/codex/skills/semantic-dag/SKILL.md",
    "claude": ROOT / "adapters/claude/skills/semantic-dag/SKILL.md",
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
        self.assertEqual(
            codex["glossary"],
            {"adapter": "A thin runtime-specific entrypoint."},
        )
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

    def test_both_adapters_register_post_tool_file_attribution(self) -> None:
        claude_hooks = json.loads(
            (ROOT / "adapters/claude/hooks/hooks.json").read_text()
        )["hooks"]
        post_commands = [
            hook["command"]
            for group in claude_hooks["PostToolUse"]
            for hook in group.get("hooks", [])
            if isinstance(hook, dict) and "command" in hook
        ]
        self.assertTrue(any("semantic-dag/hooks/tool_hook.py" in item for item in post_commands))

        codex_connect = (ROOT / "adapters/codex/scripts/cardinal-connect").read_text()
        self.assertIn('hooks.setdefault("PostToolUse", [])', codex_connect)
        self.assertIn('managed_semantic_hook_group("tool", "*"', codex_connect)

    def test_watch_prompts_use_compact_bounded_protocol(self) -> None:
        for runtime, prompt_hook in PROMPT_HOOKS.items():
            with self.subTest(runtime=runtime):
                state = self.root / f"watch-state-{runtime}"
                thread_dir = state / "threads" / "shared"
                binding_dir = state / "bindings"
                thread_dir.mkdir(parents=True)
                binding_dir.mkdir(parents=True)
                (binding_dir / "session.json").write_text(
                    json.dumps({"thread": "shared"})
                )
                (thread_dir / "dag.json").write_text(json.dumps({
                    "thread": "shared",
                    "topic": "Previous turn",
                    "nodes": {},
                    "edges": [],
                    "active": None,
                    "active_by_agent": {},
                    "agents": {},
                    "glossary": {},
                    "watch_mode": True,
                }))
                environment = os.environ.copy()
                environment["SEMANTIC_DAG_STATE_DIR"] = str(state)
                result = subprocess.run(
                    [sys.executable, str(prompt_hook)],
                    cwd=ROOT,
                    env=environment,
                    input=json.dumps({
                        "session_id": "session",
                        "prompt": "Continue analysis",
                    }),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                payload = json.loads(result.stdout)
                context = payload["hookSpecificOutput"]["additionalContext"]
                self.assertNotIn("Read and follow the semantic-dag skill", context)
                self.assertIn("1–3 important non-obvious terms", context)
                self.assertIn("Consult the full skill", context)
                self.assertLess(len(context.split()), 220)

                skill = SKILLS[runtime].read_text()
                self.assertIn(
                    "Populate the glossary on every substantive turn",
                    skill,
                )
                self.assertIn("do not add more than three new terms", skill)


if __name__ == "__main__":
    unittest.main()
