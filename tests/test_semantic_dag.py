"""Cross-adapter contract tests for the shared Semantic DAG emitter."""
from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
EMITTERS = {
    "codex": ROOT / "adapters/codex/skills/semantic-dag/scripts/emit.py",
    "claude": ROOT / "adapters/claude/skills/semantic-dag/emit.py",
}
PROMPT_HOOKS = {
    "codex": ROOT / "adapters/codex/skills/semantic-dag/scripts/hooks/prompt_hook.py",
    "claude": ROOT / "adapters/claude/skills/semantic-dag/hooks/prompt_hook.py",
}
TOOL_HOOKS = {
    "codex": ROOT / "adapters/codex/skills/semantic-dag/scripts/hooks/tool_hook.py",
    "claude": ROOT / "adapters/claude/skills/semantic-dag/hooks/tool_hook.py",
}
CODEX_SESSION_BRIDGE = (
    ROOT / "adapters/codex/skills/semantic-dag/scripts/session_bridge.py"
)
SKILLS = {
    "codex": ROOT / "adapters/codex/skills/semantic-dag/SKILL.md",
    "claude": ROOT / "adapters/claude/skills/semantic-dag/SKILL.md",
}


def _without_volatile(value):
    if isinstance(value, dict):
        return {
            key: _without_volatile(item)
            for key, item in value.items()
            if key not in {"created", "updated", "ts", "session_started", "cwd", "runtime", "started", "ended"}
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

    def test_viewer_session_status_reflects_dag_state_and_recency(self) -> None:
        server_path = (
            ROOT
            / "adapters/codex/skills/semantic-dag/scripts/viewer/server.py"
        )
        spec = importlib.util.spec_from_file_location(
            "semantic_dag_viewer_server_test", server_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        active = {
            "nodes": {"work": {"status": "active"}},
            "active_by_agent": {"root": "work"},
        }
        self.assertEqual(module._session_status(active, 950, now=1000), "active")
        self.assertEqual(module._session_status(active, 800, now=1000), "stale")
        self.assertEqual(
            module._session_status(
                {"nodes": {"work": {"status": "paused"}}}, 950, now=1000
            ),
            "paused",
        )
        self.assertEqual(
            module._session_status(
                {"finished": True, "nodes": {"work": {"status": "error"}}},
                950,
                now=1000,
            ),
            "error",
        )
        self.assertEqual(
            module._session_status({"finished": True, "nodes": {}}, 950, now=1000),
            "completed",
        )
        self.assertEqual(
            module._session_status(
                {"nodes": {"work": {"status": "done"}}}, 950, now=1000
            ),
            "completed",
        )
        self.assertEqual(module._session_status({"nodes": {}}, 950, now=1000), "pending")

    def emit(self, runtime: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.update(
            {
                "SEMANTIC_DAG_STATE_DIR": str(self.root / runtime),
                "SEMANTIC_DAG_NO_SERVER": "1",
                "SEMANTIC_DAG_NO_OPEN": "1",
                "SEMANTIC_DAG_NO_SESSION_BRIDGE": "1",
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
            "start", "Inspect parity", "--agent", "scout", "--agent-label", "Parity Scout", "--parent", "goal",
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

    def test_reset_starts_a_new_turn_without_cross_turn_auto_chain(self) -> None:
        thread = ("--thread", "turns")
        self.emit("claude", "start", "first turn", *thread)
        self.emit("claude", "add", "goal1", "GOAL", "answer first question", *thread)
        self.emit("claude", "activate", "goal1", *thread)
        self.emit("claude", "add", "work1", "WORK", "investigate first thing", *thread)
        self.emit("claude", "activate", "work1", *thread)

        # Turn boundary — reset must NOT auto-chain the next add to work1 (turn 1).
        self.emit("claude", "reset", "second turn", *thread)
        self.emit("claude", "add", "hyp2", "HYPOTHESIS", "second turn hypothesis", *thread)

        dag = json.loads((self.root / "claude/threads/turns/dag.json").read_text())
        self.assertEqual(dag["turn"], 2)
        self.assertEqual([turn["n"] for turn in dag["turns"]], [1, 2])
        self.assertEqual(dag["turns"][0]["topic"], "first turn")
        self.assertIsNotNone(dag["turns"][0]["ended"])
        self.assertEqual(dag["turns"][1]["topic"], "second turn")
        self.assertIsNone(dag["turns"][1]["ended"])

        # First add in turn 2 must NOT chain to any turn-1 node.
        cross_turn = [
            edge for edge in dag["edges"]
            if dag["nodes"][edge["from"]]["turn"] != dag["nodes"][edge["to"]]["turn"]
        ]
        self.assertEqual(
            cross_turn, [], f"unexpected cross-turn edges: {cross_turn!r}"
        )
        self.assertEqual(dag["nodes"]["hyp2"]["turn"], 2)
        self.assertEqual(dag["nodes"]["work1"]["status"], "completed")

    def test_topic_rename_persists_on_session_and_current_turn(self) -> None:
        for runtime in EMITTERS:
            with self.subTest(runtime=runtime):
                self.emit(runtime, "start", "Original title", "--thread", "shared")
                self.emit(runtime, "topic", "Renamed session", "--thread", "shared")
                dag = self.dag(runtime)
                self.assertEqual(dag["topic"], "Renamed session")
                self.assertEqual(dag["turns"][-1]["topic"], "Renamed session")

    def test_load_dag_reconciles_turn_pointer_with_turns_list(self) -> None:
        """A stale/corrupt state file must not leave `turn` past `max(turns.n)`."""
        thread_dir = self.root / "claude" / "threads" / "recover"
        thread_dir.mkdir(parents=True)
        (thread_dir / "dag.json").write_text(json.dumps({
            "thread": "recover",
            "runtime": "claude",
            "turn": 5,
            "turns": [{"n": 1, "topic": "only turn", "started": 0, "ended": None, "outcome": ""}],
            "nodes": {},
            "edges": [],
            "agents": {"root": {"id": "root", "label": "Root", "status": "active"}},
        }))
        # An add should stamp with the reconciled current turn (1), not 5.
        self.emit(
            "claude", "add", "n1", "GOAL", "recover clamped turn",
            "--root", "--thread", "recover",
        )
        dag = json.loads((thread_dir / "dag.json").read_text())
        self.assertEqual(dag["turn"], 1)
        self.assertEqual(dag["nodes"]["n1"]["turn"], 1)

    def test_delete_thread_purges_state_bindings_and_pointer(self) -> None:
        """/t/<thread>/delete removes dag+events, session-id bindings that
        point at that thread, and any current-<cwd> pointer files."""
        state_dir = self.root / "delete"
        state_dir.mkdir(parents=True)
        self.emit("claude", "start", "one", "--thread", "keep")
        self.emit("claude", "start", "two", "--thread", "drop")
        self.emit("claude", "add", "n", "GOAL", "the drop turn goal",
                  "--root", "--thread", "drop")
        drop_dir = self.root / "claude" / "threads" / "drop"
        keep_dir = self.root / "claude" / "threads" / "keep"
        bindings_dir = self.root / "claude" / "bindings"
        bindings_dir.mkdir(parents=True, exist_ok=True)
        (bindings_dir / "sess-drop.json").write_text(json.dumps({"thread": "drop", "agent": "root"}))
        (bindings_dir / "sess-keep.json").write_text(json.dumps({"thread": "keep", "agent": "root"}))
        pointer = self.root / "claude" / "current-abc123"
        pointer.write_text("drop")

        server_path = ROOT / "adapters/claude/skills/semantic-dag/viewer/server.py"
        spec = importlib.util.spec_from_file_location("_dag_server", server_path)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {"SEMANTIC_DAG_STATE_DIR": str(self.root / "claude")}):
            spec.loader.exec_module(module)
            module._delete_thread("drop")

        self.assertTrue(keep_dir.is_dir(), "keep thread was collateral damage")
        self.assertFalse(drop_dir.exists(), "drop thread directory was not removed")
        self.assertFalse((bindings_dir / "sess-drop.json").exists(), "binding for drop remained")
        self.assertTrue((bindings_dir / "sess-keep.json").exists(), "binding for keep was removed")
        self.assertFalse(pointer.exists(), "current-cwd pointer was not cleared")

    def test_viewer_rename_validates_and_uses_the_event_emitter(self) -> None:
        server_path = (
            ROOT
            / "adapters/codex/skills/semantic-dag/scripts/viewer/server.py"
        )
        spec = importlib.util.spec_from_file_location(
            "semantic_dag_viewer_rename_test", server_path
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        completed = mock.Mock(returncode=0)
        with mock.patch.object(module.subprocess, "run", return_value=completed) as run:
            title = module._rename_thread("shared", "  Clear   session title  ")

        self.assertEqual(title, "Clear session title")
        run.assert_called_once_with(
            [
                sys.executable,
                str(module.EMIT),
                "topic",
                "Clear session title",
                "--thread",
                "shared",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        for value in ("", "   ", None, "x" * 161):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    module._rename_thread("shared", value)

    def test_finish_with_empty_summary_preserves_prior_outcome(self) -> None:
        thread = ("--thread", "outcome")
        self.emit("claude", "start", "task", *thread)
        self.emit("claude", "add", "a", "WORK", "do a", *thread)
        self.emit("claude", "activate", "a", *thread)
        self.emit("claude", "finish", "meaningful outcome", *thread)
        # A second finish with no summary must not blank the outcome.
        self.emit("claude", "finish", "", *thread)
        dag = json.loads((self.root / "claude/threads/outcome/dag.json").read_text())
        self.assertEqual(dag["turns"][-1]["outcome"], "meaningful outcome")

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
        self.assertEqual(codex["runtime"], "codex")
        self.assertEqual(claude["runtime"], "claude")
        self.assertEqual(codex["agents"]["scout"]["label"], "Parity Scout")
        self.assertEqual(codex["agents"]["scout"]["task"], "Inspect parity")
        self.assertEqual(codex["agents"]["scout"]["parent_agent"], "root")
        self.assertTrue(
            any(
                edge["from"] == "goal"
                and edge["to"] == "scout::proof"
                and edge["relationship"] == "decomposes_into"
                for edge in codex["edges"]
            ),
            f"expected goal→scout::proof edge in {codex['edges']!r}",
        )

    def test_entrypoints_are_thin_core_adapters(self) -> None:
        for runtime, emitter in EMITTERS.items():
            source = emitter.read_text()
            with self.subTest(runtime=runtime):
                self.assertIn("from cardinal_core.semantic_dag import RuntimeConfig, main", source)
                self.assertNotIn("def _apply(", source)
                self.assertNotIn("def emit(", source)

    def test_codex_and_claude_share_one_viewer_server(self) -> None:
        sources = {runtime: emitter.read_text() for runtime, emitter in EMITTERS.items()}
        for runtime, source in sources.items():
            with self.subTest(runtime=runtime):
                self.assertIn('default_state_dir="~/.cardinal/state/semantic-dag"', source)
                self.assertIn("default_port=8766", source)
                self.assertIn(
                    '~/.cardinal/state/semantic-dag',
                    PROMPT_HOOKS[runtime].read_text(),
                )

        codex_viewer = ROOT / "adapters/codex/skills/semantic-dag/scripts/viewer"
        claude_viewer = ROOT / "adapters/claude/skills/semantic-dag/viewer"
        self.assertEqual(
            (codex_viewer / "server.py").read_bytes(),
            (claude_viewer / "server.py").read_bytes(),
        )
        self.assertEqual(
            (codex_viewer / "index.html").read_bytes(),
            (claude_viewer / "index.html").read_bytes(),
        )
        self.assertEqual(
            (codex_viewer / "assets/cardinal-bird.png").read_bytes(),
            (claude_viewer / "assets/cardinal-bird.png").read_bytes(),
        )
        server = (codex_viewer / "server.py").read_text()
        viewer = (codex_viewer / "index.html").read_text()
        self.assertIn('path == "/sessions"', server)
        self.assertIn('suffix == "rename"', server)
        self.assertIn('path == "/assets/cardinal-bird.png"', server)
        self.assertIn('src="/assets/cardinal-bird.png"', viewer)
        self.assertIn('id="session-nav"', viewer)
        self.assertIn('id="workflow-tab"', viewer)
        self.assertIn('id="agents-tab"', viewer)
        self.assertIn('id="agent-workflows"', viewer)
        self.assertIn('id="agents-view"', viewer)
        self.assertIn("(node.agent||'root')===agentId", viewer)
        self.assertIn("agent.parent_agent", viewer)
        self.assertIn("function agentNarration", viewer)
        self.assertIn("workflow-agent-live", viewer)
        self.assertIn('id="topic-edit"', viewer)
        self.assertIn("session-rename", viewer)
        self.assertIn("function saveSessionRename", viewer)
        self.assertIn("runtime-crown", viewer)
        self.assertIn("turn-panel", viewer)
        self.assertIn("state.turns", viewer)

    def test_claude_viewer_restores_all_active_agents(self) -> None:
        source = (
            ROOT / "adapters/claude/skills/semantic-dag/viewer/index.html"
        ).read_text()
        self.assertIn("dag.active_by_agent", source)
        self.assertIn("Object.values(state.activeByAgent)", source)

    def test_structured_file_attribution_paths_are_installed(self) -> None:
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
        self.assertIn("SEMANTIC_SESSION_BRIDGE", codex_connect)

        codex_emitter = EMITTERS["codex"].read_text()
        self.assertIn("session_bridge.py", codex_emitter)
        self.assertIn("_ensure_session_bridge(arguments)", codex_emitter)

    def test_codex_emitter_restarts_bridge_on_later_turn_reset(self) -> None:
        spec = importlib.util.spec_from_file_location("codex_emitter_test", EMITTERS["codex"])
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with (
            mock.patch.dict(os.environ, {"CODEX_SESSION_ID": "session"}, clear=False),
            mock.patch.object(module, "_bridge_running", return_value=False),
            mock.patch.object(module.subprocess, "run") as run,
        ):
            module._ensure_session_bridge(["reset", "Later turn"])
        run.assert_called_once()
        self.assertIn("session_bridge.py", str(run.call_args.args[0]))

    def test_codex_session_bridge_materializes_completed_file_events(self) -> None:
        state = self.root / "bridge-state"
        transcript = self.root / "rollout-session.jsonl"
        read_path = ROOT / "README.md"
        updated_path = ROOT / "tests/test_semantic_dag.py"
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "status": "completed",
                        "cwd": ROOT.as_uri(),
                        "parsed_cmd": [
                            {"type": "read", "path": str(read_path)},
                            {"type": "search", "path": "README.md"},
                        ],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "CommandExecution",
                        "status": "failed",
                        "cwd": ROOT.as_uri(),
                        "parsed_cmd": [{"type": "read", "path": str(updated_path)}],
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "FileChange",
                        "cwd": ROOT.as_uri(),
                        "changes": {
                            str(updated_path): {
                                "type": "update",
                                "unified_diff": "@@",
                                "move_path": None,
                            }
                        },
                    },
                },
            },
        ]
        environment = os.environ.copy()
        environment.update(
            {
                "SEMANTIC_DAG_STATE_DIR": str(state),
                "SEMANTIC_DAG_NO_SERVER": "1",
                "SEMANTIC_DAG_NO_OPEN": "1",
                "SEMANTIC_DAG_NO_SESSION_BRIDGE": "1",
                "CODEX_SESSION_LOG": str(transcript),
            }
        )
        common = ["--thread", "session"]
        subprocess.run(
            [sys.executable, str(EMITTERS["codex"]), "start", "Bridge test", *common],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(EMITTERS["codex"]),
                "add",
                "first",
                "WORK",
                "Capture structured files",
                "--root",
                *common,
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        subprocess.run(
            [sys.executable, str(EMITTERS["codex"]), "activate", "first", *common],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        first_activation = json.loads(
            (state / "threads/session/events.jsonl").read_text().splitlines()[-1]
        )["ts"]
        subprocess.run(
            [
                sys.executable,
                str(EMITTERS["codex"]),
                "add",
                "second",
                "WORK",
                "Continue later work",
                "--root",
                *common,
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        subprocess.run(
            [sys.executable, str(EMITTERS["codex"]), "activate", "second", *common],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        second_activation = json.loads(
            (state / "threads/session/events.jsonl").read_text().splitlines()[-1]
        )["ts"]
        completed_at_ms = ((first_activation + second_activation) / 2) * 1000
        records[0]["payload"]["completed_at_ms"] = completed_at_ms
        records[2]["payload"]["completed_at_ms"] = completed_at_ms
        transcript.write_text("".join(json.dumps(item) + "\n" for item in records))
        binding = state / "bindings/session.json"
        binding.parent.mkdir(parents=True, exist_ok=True)
        binding.write_text(json.dumps({"thread": "session", "agent": "root"}))

        subprocess.run(
            [
                sys.executable,
                str(CODEX_SESSION_BRIDGE),
                "run",
                "--session",
                "session",
                "--once",
                "--from-start",
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        dag = json.loads((state / "threads/session/dag.json").read_text())
        self.assertEqual(dag["nodes"]["first"]["files"]["read"], ["README.md"])
        self.assertEqual(
            dag["nodes"]["first"]["files"]["updated"],
            ["tests/test_semantic_dag.py"],
        )
        self.assertEqual(
            dag["nodes"]["second"]["files"], {"read": [], "updated": []}
        )

    def test_codex_session_bridge_bootstraps_and_narrates_subagent_work(self) -> None:
        state = self.root / "subagent-bridge-state"
        transcript = self.root / "rollout-child.jsonl"
        commentary = "The live tail fix is implemented and focused tests are passing."
        records = [
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "id": "msg-progress-1",
                        "phase": "commentary",
                        "content": [{"type": "Text", "text": commentary}],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "msg-progress-1",
                    "role": "assistant",
                    "phase": "commentary",
                    "content": [{"type": "output_text", "text": commentary}],
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "AgentMessage",
                        "id": "msg-final-1",
                        "phase": "final",
                        "content": [{"type": "Text", "text": "Completed result."}],
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": "msg-final-1",
                    "role": "assistant",
                    "phase": "final",
                    "content": [{"type": "output_text", "text": "Completed result."}],
                },
            },
        ]
        transcript.write_text("".join(json.dumps(item) + "\n" for item in records))
        environment = os.environ.copy()
        environment.update(
            {
                "SEMANTIC_DAG_STATE_DIR": str(state),
                "SEMANTIC_DAG_NO_SERVER": "1",
                "SEMANTIC_DAG_NO_OPEN": "1",
                "SEMANTIC_DAG_NO_SESSION_BRIDGE": "1",
                "CODEX_SESSION_LOG": str(transcript),
            }
        )
        common = ["--thread", "session"]
        subprocess.run(
            [sys.executable, str(EMITTERS["codex"]), "start", "Bridge test", *common],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(EMITTERS["codex"]),
                "add",
                "logs-gap",
                "WORK",
                "Remove logs right gap",
                "--root",
                *common,
            ],
            cwd=ROOT,
            env=environment,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                str(EMITTERS["codex"]),
                "begin",
                "Fix logs right edge",
                "--agent",
                "logs_gap",
                "--agent-label",
                "Logs gap fix",
                "--parent",
                "logs-gap",
                *common,
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )
        binding = state / "bindings/child.json"
        binding.parent.mkdir(parents=True, exist_ok=True)
        binding.write_text(json.dumps({"thread": "session", "agent": "logs_gap"}))

        command = [
            sys.executable,
            str(CODEX_SESSION_BRIDGE),
            "run",
            "--session",
            "child",
            "--once",
            "--from-start",
        ]
        subprocess.run(command, cwd=ROOT, env=environment, check=True)
        # Replaying the same structured pair must not duplicate narration.
        subprocess.run(command, cwd=ROOT, env=environment, check=True)

        dag = json.loads((state / "threads/session/dag.json").read_text())
        node = dag["nodes"]["logs_gap::__activity"]
        self.assertTrue(node["provisional"])
        self.assertEqual(node["type"], "WORK")
        self.assertEqual(node["label"], "Fix logs right edge")
        self.assertEqual(node["status"], "completed")
        self.assertNotIn("logs_gap", dag["active_by_agent"])
        self.assertEqual(dag["agents"]["logs_gap"]["status"], "completed")
        self.assertEqual(dag["agents"]["logs_gap"]["summary"], "Completed result.")
        self.assertTrue(
            any(
                edge["from"] == "logs-gap"
                and edge["to"] == node["id"]
                and edge["relationship"] == "decomposes_into"
                for edge in dag["edges"]
            )
        )
        native_notes = [
            note for note in node["notes"] if note.get("source") == "codex-session"
        ]
        self.assertEqual(
            native_notes,
            [{
                "text": commentary,
                "ts": native_notes[0]["ts"],
                "source": "codex-session",
                "source_id": "msg-progress-1",
            }],
        )
        self.assertNotIn("Completed result.", [note["text"] for note in node["notes"]])

    def test_codex_session_bridge_suppresses_recent_explicit_note_duplicate(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "codex_session_bridge_dedup_test", CODEX_SESSION_BRIDGE
        )
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(
            os.environ,
            {"SEMANTIC_DAG_STATE_DIR": str(self.root / "dedup-state")},
            clear=False,
        ):
            spec.loader.exec_module(module)

        now = time.time()
        dag = {
            "nodes": {
                "child::work": {
                    "notes": [
                        {"text": "Same progress update.", "ts": now},
                        {
                            "text": "Native update.",
                            "ts": now,
                            "source_id": "msg-native-1",
                        },
                    ]
                }
            }
        }
        self.assertTrue(
            module._note_is_duplicate(
                dag, "child::work", None, "Same progress update."
            )
        )
        self.assertTrue(
            module._note_is_duplicate(
                dag, "child::work", "msg-native-1", "Native update."
            )
        )
        dag["nodes"]["child::work"]["notes"][0]["ts"] = now - 30
        self.assertFalse(
            module._note_is_duplicate(
                dag, "child::work", None, "Same progress update."
            )
        )

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
                self.assertNotIn("file metadata", context)
                self.assertIn("Consult the full skill", context)
                if runtime == "codex":
                    self.assertIn("mirrored onto the active node automatically", context)
                    self.assertNotIn("mirror the same sentence", context)
                self.assertLess(len(context.split()), 220)

                skill = SKILLS[runtime].read_text()
                self.assertIn(
                    "Populate the glossary on every substantive turn",
                    skill,
                )
                self.assertIn("do not add more than three new terms", skill)
                self.assertNotIn("python3 <emit> file ", skill)
                self.assertNotIn("Record every materially read", skill)


    def _seed_watch_state(self, runtime: str, prompt: str, dag_overrides: dict | None = None):
        state = self.root / f"turn-cover-{runtime}"
        thread_dir = state / "threads" / "shared"
        binding_dir = state / "bindings"
        thread_dir.mkdir(parents=True)
        binding_dir.mkdir(parents=True)
        (binding_dir / "session.json").write_text(json.dumps({"thread": "shared"}))
        dag = {
            "thread": "shared",
            "runtime": runtime,
            "topic": "Prior turn",
            "nodes": {},
            "edges": [],
            "active": None,
            "active_by_agent": {},
            "agents": {
                "root": {"id": "root", "label": "Root", "status": "active"}
            },
            "glossary": {},
            "watch_mode": True,
            "turn": 1,
            "turns": [{"n": 1, "topic": "Prior turn", "started": 0, "ended": None, "outcome": ""}],
        }
        if dag_overrides:
            dag.update(dag_overrides)
        (thread_dir / "dag.json").write_text(json.dumps(dag))
        environment = os.environ.copy()
        environment["SEMANTIC_DAG_STATE_DIR"] = str(state)
        environment["SEMANTIC_DAG_NO_SERVER"] = "1"
        environment["SEMANTIC_DAG_NO_OPEN"] = "1"
        subprocess.run(
            [sys.executable, str(PROMPT_HOOKS[runtime])],
            cwd=ROOT,
            env=environment,
            input=json.dumps({"session_id": "session", "prompt": prompt}),
            capture_output=True,
            text=True,
            check=True,
        )
        return state, environment

    def test_prompt_hook_seeds_active_goal_node_per_turn(self) -> None:
        for runtime in PROMPT_HOOKS:
            with self.subTest(runtime=runtime):
                state, _ = self._seed_watch_state(runtime, "diagnose latency spike in ingest")
                dag = json.loads((state / "threads" / "shared" / "dag.json").read_text())
                self.assertEqual(dag["turn"], 2)
                self.assertIn("turn-2-goal", dag["nodes"])
                goal = dag["nodes"]["turn-2-goal"]
                self.assertEqual(goal["type"], "GOAL")
                self.assertEqual(goal["status"], "active")
                self.assertEqual(goal["turn"], 2)
                self.assertEqual(dag["active_by_agent"].get("root"), "turn-2-goal")
                self.assertIn("diagnose", goal["label"].lower())

    def test_prompt_hook_goal_label_falls_back_when_prompt_is_thin(self) -> None:
        for runtime in PROMPT_HOOKS:
            with self.subTest(runtime=runtime):
                state, _ = self._seed_watch_state(runtime, "hi")
                dag = json.loads((state / "threads" / "shared" / "dag.json").read_text())
                self.assertIn("turn-2-goal", dag["nodes"])
                self.assertEqual(dag["nodes"]["turn-2-goal"]["label"], "Handle user request")

    def test_prompt_hook_goal_label_rejects_generic_phase(self) -> None:
        for runtime in PROMPT_HOOKS:
            with self.subTest(runtime=runtime):
                state, _ = self._seed_watch_state(runtime, "start phase 5 now")
                dag = json.loads((state / "threads" / "shared" / "dag.json").read_text())
                self.assertEqual(dag["nodes"]["turn-2-goal"]["label"], "Handle user request")

    def test_tool_hook_creates_work_node_and_attaches_tool_when_no_active(self) -> None:
        for runtime in TOOL_HOOKS:
            with self.subTest(runtime=runtime):
                state, environment = self._seed_watch_state(runtime, "look up recent invoice")
                # Simulate the model completing the goal so no node is active.
                dag_path = state / "threads" / "shared" / "dag.json"
                dag = json.loads(dag_path.read_text())
                dag["nodes"]["turn-2-goal"]["status"] = "completed"
                dag["active_by_agent"] = {}
                dag["active"] = None
                dag_path.write_text(json.dumps(dag))
                subprocess.run(
                    [sys.executable, str(TOOL_HOOKS[runtime])],
                    cwd=ROOT,
                    env=environment,
                    input=json.dumps({
                        "session_id": "session",
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "tool_input": {"cmd": "ls Downloads"},
                    }),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                # Allow the async tool spawn to settle.
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    dag = json.loads(dag_path.read_text())
                    node = dag["nodes"].get("turn-2-work")
                    if node and node.get("tool"):
                        break
                    time.sleep(0.05)
                self.assertIn("turn-2-work", dag["nodes"])
                work = dag["nodes"]["turn-2-work"]
                self.assertEqual(work["type"], "WORK")
                self.assertEqual(work["status"], "active")
                self.assertEqual(dag["active_by_agent"].get("root"), "turn-2-work")
                self.assertTrue(any(
                    edge["from"] == "turn-2-goal" and edge["to"] == "turn-2-work"
                    for edge in dag["edges"]
                ))
                self.assertEqual(work["tool"]["name"], "Bash")

    def test_tool_hook_preserves_subagent_parent_when_synthesizing_work(self) -> None:
        for runtime in TOOL_HOOKS:
            with self.subTest(runtime=runtime):
                state, environment = self._seed_watch_state(
                    runtime, "investigate delegated query gap"
                )
                dag_path = state / "threads" / "shared" / "dag.json"
                dag = json.loads(dag_path.read_text())
                dag["agents"]["query_scout"] = {
                    "id": "query_scout",
                    "label": "Query scout",
                    "status": "active",
                    "parent": "turn-2-goal",
                    "parent_agent": "root",
                }
                dag_path.write_text(json.dumps(dag))
                (state / "bindings" / "session.json").write_text(json.dumps({
                    "thread": "shared",
                    "agent": "query_scout",
                }))

                subprocess.run(
                    [sys.executable, str(TOOL_HOOKS[runtime])],
                    cwd=ROOT,
                    env=environment,
                    input=json.dumps({
                        "session_id": "session",
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Read",
                        "tool_input": {"file_path": "query.go"},
                    }),
                    capture_output=True,
                    text=True,
                    check=True,
                )

                work_id = "query_scout::turn-2-work"
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    dag = json.loads(dag_path.read_text())
                    node = dag["nodes"].get(work_id)
                    if node and node.get("tool"):
                        break
                    time.sleep(0.05)
                self.assertIn(work_id, dag["nodes"])
                self.assertEqual(dag["active_by_agent"]["query_scout"], work_id)
                self.assertTrue(any(
                    edge["from"] == "turn-2-goal"
                    and edge["to"] == work_id
                    and edge["relationship"] == "decomposes_into"
                    for edge in dag["edges"]
                ))
                self.assertEqual(dag["nodes"][work_id]["tool"]["name"], "Read")

    def test_tool_hook_skips_its_own_emit_bash_calls(self) -> None:
        for runtime in TOOL_HOOKS:
            with self.subTest(runtime=runtime):
                state, environment = self._seed_watch_state(runtime, "trivial")
                dag_path = state / "threads" / "shared" / "dag.json"
                dag = json.loads(dag_path.read_text())
                dag["nodes"]["turn-2-goal"]["status"] = "completed"
                dag["active_by_agent"] = {}
                dag["active"] = None
                dag_path.write_text(json.dumps(dag))
                subprocess.run(
                    [sys.executable, str(TOOL_HOOKS[runtime])],
                    cwd=ROOT,
                    env=environment,
                    input=json.dumps({
                        "session_id": "session",
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Bash",
                        "tool_input": {"cmd": "python3 semantic-dag/emit.py add x GOAL y"},
                    }),
                    capture_output=True,
                    text=True,
                    check=True,
                )
                dag = json.loads(dag_path.read_text())
                self.assertNotIn("turn-2-work", dag["nodes"])


if __name__ == "__main__":
    unittest.main()
