"""Tests for hooks/trace_emit.py — Phase 1 Claude vertical slice
(transcript -> Envelope records against the schema-v1 canonical
execution graph contract).

Two groups:
  - Real-fixture roundtrip: adapters/claude/tests/fixtures/
    real_transcript_p1.jsonl (+ its subagents/ sibling), a redacted
    copy of an ended real Claude Code session. See the module docstring
    in hooks/trace_emit.py and the P1 report for how it was captured
    and what was redacted.
  - Synthetic edge cases: minimal hand-built transcripts exercising
    empty input, subagent delegation, skill detection (both the real
    `Skill` tool_use shape and the `SlashCommand` forward-compat alias),
    parallel tool_use blocks, and provenance honesty.

Run with: python3 -m unittest tests.test_trace_emit -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import trace_emit as te  # noqa: E402
from cardinal_core import envelope as env  # noqa: E402
from cardinal_core.pricing import ANTHROPIC_PRICING_USD_PER_M  # noqa: E402
from cardinal_core.redaction import KNOWN_SECRET_PATTERNS  # noqa: E402

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
REAL_FIXTURE = FIXTURES_DIR / "real_transcript_p1.jsonl"

ORG_ID = "org-test"
SESSION_ID = "sess-test-0001"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _user(text: str, ts: str) -> dict:
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def _tool_result_user(pairs: list[tuple[str, str]], ts: str) -> dict:
    return {
        "type": "user",
        "timestamp": ts,
        "message": {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": tuid, "content": content}
                for tuid, content in pairs
            ],
        },
    }


def _assistant(
    ts: str,
    *,
    model: str = "claude-sonnet-5",
    msg_id: str | None = "msg_001",
    usage: dict | None = None,
    content: list | None = None,
) -> dict:
    msg: dict = {"role": "assistant", "model": model, "content": content or []}
    if msg_id is not None:
        msg["id"] = msg_id
    if usage is not None:
        msg["usage"] = usage
    return {"type": "assistant", "timestamp": ts, "message": msg}


def _usage(i=10, o=20, cc=0, cr=0) -> dict:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
    }


class RealFixtureRoundtripTests(unittest.TestCase):
    """Real, redacted transcript captured from an ended session —
    see hooks/trace_emit.py module docstring for the empirical basis."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.envelopes = list(
            te.emit_from_transcript(
                REAL_FIXTURE, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude"
            )
        )

    def test_fixture_exists_and_is_nonempty(self) -> None:
        self.assertTrue(REAL_FIXTURE.is_file())
        self.assertGreater(len(self.envelopes), 0)

    def test_at_least_one_turn_node(self) -> None:
        turns = [
            e for e in self.envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.node_kind == env.NodeKind.TURN
        ]
        self.assertGreaterEqual(len(turns), 1)

    def test_at_least_one_llm_call_with_known_model(self) -> None:
        llm_calls = [
            e for e in self.envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.node_kind == env.NodeKind.LLM_CALL
        ]
        self.assertGreaterEqual(len(llm_calls), 1)
        models = {e.payload.request_model for e in llm_calls if e.payload.request_model}
        self.assertTrue(models, "expected at least one llm_call with a request_model set")
        self.assertTrue(
            models & set(ANTHROPIC_PRICING_USD_PER_M),
            f"none of {models} matched a known Anthropic model SKU",
        )

    def test_at_least_one_tool_invocation(self) -> None:
        tools = [
            e for e in self.envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.node_kind == env.NodeKind.INVOCATION
            and e.payload.invocation_kind == env.InvocationKind.TOOL
        ]
        self.assertGreaterEqual(len(tools), 1)

    def test_subagent_recursion_present(self) -> None:
        """The real fixture's subagents/ sibling should have been walked:
        a subagent invocation node, a delegated_to edge, and at least one
        nested llm_call from the subagent's own transcript."""
        subagents = [
            e for e in self.envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.invocation_kind == env.InvocationKind.SUBAGENT
        ]
        self.assertGreaterEqual(len(subagents), 1)
        delegated = [
            e for e in self.envelopes
            if e.record_type == env.RecordType.EDGE_OBSERVED
            and e.payload.edge_kind == env.EdgeKind.DELEGATED_TO
        ]
        self.assertGreaterEqual(len(delegated), 1)
        turn_nodes = [
            e for e in self.envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.node_kind == env.NodeKind.TURN
        ]
        self.assertGreaterEqual(len(turn_nodes), 2, "expected main turn + subagent child turn")

    def test_all_six_provenance_axes_set_on_every_node(self) -> None:
        nodes = [
            e for e in self.envelopes
            if e.record_type in (env.RecordType.NODE_OBSERVED, env.RecordType.NODE_UPDATED)
        ]
        self.assertGreater(len(nodes), 0)
        for e in nodes:
            p = e.payload
            for field_name in (
                "identity_source", "parent_source", "timing_source",
                "model_source", "toolkit_source", "usage_source",
            ):
                self.assertIsNotNone(
                    getattr(p, field_name),
                    f"{p.node_kind}/{p.node_name} missing {field_name}",
                )

    def test_record_id_stable_across_runs(self) -> None:
        again = list(
            te.emit_from_transcript(
                REAL_FIXTURE, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude"
            )
        )
        self.assertEqual(len(again), len(self.envelopes))
        self.assertEqual(
            [e.record_id for e in self.envelopes],
            [e.record_id for e in again],
        )

    def test_every_envelope_validates(self) -> None:
        for e in self.envelopes:
            env.validate(e)  # raises on failure

    def test_no_secret_patterns_in_any_attributes(self) -> None:
        import re

        compiled = [(name, re.compile(pat)) for name, pat in KNOWN_SECRET_PATTERNS]
        for e in self.envelopes:
            attrs = getattr(e.payload, "attributes", None)
            if not attrs:
                continue
            blob = json.dumps(attrs, default=str)
            for name, pattern in compiled:
                self.assertIsNone(
                    pattern.search(blob),
                    f"secret pattern {name} found in attributes of "
                    f"{e.record_type.value}: {blob[:200]}",
                )

    def test_no_parent_tool_use_id_so_invocation_parent_source_never_native(self) -> None:
        """Empirical finding (see module docstring): parent_tool_use_id
        is absent from every captured transcript. Provenance must not
        lie about it — every invocation node's parent_source must be
        something other than NATIVE."""
        invocations = [
            e for e in self.envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.node_kind == env.NodeKind.INVOCATION
        ]
        self.assertGreater(len(invocations), 0)
        for e in invocations:
            self.assertNotEqual(e.payload.parent_source, env.ParentSource.NATIVE)


class SyntheticEdgeCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_empty_transcript_yields_nothing_and_does_not_crash(self) -> None:
        path = self.tmp / "empty.jsonl"
        path.write_text("")
        result = list(te.emit_from_transcript(path, session_id="s", org_id="o", adapter="claude"))
        self.assertEqual(result, [])

    def test_missing_transcript_yields_nothing_and_does_not_crash(self) -> None:
        path = self.tmp / "does-not-exist.jsonl"
        result = list(te.emit_from_transcript(path, session_id="s", org_id="o", adapter="claude"))
        self.assertEqual(result, [])

    def test_subagent_task_call_orchestrator_model_inherited(self) -> None:
        transcript = self.tmp / f"{SESSION_ID}.jsonl"
        records = [
            _user("please investigate", "2026-01-01T00:00:00.000Z"),
            _assistant(
                "2026-01-01T00:00:05.000Z",
                model="claude-opus-4-7",
                msg_id="msg_a1",
                usage=_usage(),
                content=[
                    {
                        "type": "tool_use", "id": "toolu_task1", "name": "Task",
                        "input": {"subagent_type": "Explore", "description": "scan repo"},
                    },
                ],
            ),
            _tool_result_user([("toolu_task1", "done")], "2026-01-01T00:00:20.000Z"),
        ]
        _write_jsonl(transcript, records)

        sub_dir = self.tmp / SESSION_ID / "subagents"
        sub_dir.mkdir(parents=True)
        (sub_dir / "agent-x1.meta.json").write_text(json.dumps({
            "agentType": "Explore", "description": "scan repo",
            "toolUseId": "toolu_task1", "spawnDepth": 1,
        }))
        _write_jsonl(sub_dir / "agent-x1.jsonl", [
            {"type": "user", "timestamp": "2026-01-01T00:00:06.000Z",
             "message": {"role": "user", "content": "scan the repo"}, "agentId": "x1"},
            _assistant(
                "2026-01-01T00:00:10.000Z", model="claude-haiku-4-5", msg_id="msg_sub1",
                usage=_usage(5, 5),
                content=[{"type": "tool_use", "id": "toolu_sub_read", "name": "Read",
                          "input": {"file_path": "/repo/a.py"}}],
            ),
        ])

        envelopes = list(te.emit_from_transcript(
            transcript, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude"))
        for e in envelopes:
            env.validate(e)

        subagent_nodes = [
            e for e in envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.invocation_kind == env.InvocationKind.SUBAGENT
        ]
        self.assertEqual(len(subagent_nodes), 1)
        node = subagent_nodes[0].payload
        self.assertEqual(node.orchestrator_model, "claude-opus-4-7")
        self.assertEqual(node.model_source, env.ModelSource.INHERITED)
        self.assertEqual(node.identity_source, env.IdentitySource.NATIVE)
        self.assertEqual(node.parent_source, env.ParentSource.TRANSCRIPT)

        # Nested llm_call from the subagent's own transcript should also
        # be present, proving recursion actually walked the sub-file.
        nested_models = {
            e.payload.request_model
            for e in envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.node_kind == env.NodeKind.LLM_CALL
        }
        self.assertIn("claude-haiku-4-5", nested_models)

        delegated = [
            e for e in envelopes
            if e.record_type == env.RecordType.EDGE_OBSERVED
            and e.payload.edge_kind == env.EdgeKind.DELEGATED_TO
        ]
        self.assertEqual(len(delegated), 1)

    def test_skill_tool_use_executed_lifecycle(self) -> None:
        """The REAL Claude Code shape (empirically verified): a `Skill`
        tool_use with input={"skill": <name>, "args": ...} — NOT
        SlashCommand. See hooks/trace_emit.py module docstring."""
        transcript = self.tmp / f"{SESSION_ID}.jsonl"
        records = [
            _user("help me price this", "2026-01-01T00:00:00.000Z"),
            _assistant(
                "2026-01-01T00:00:05.000Z", model="claude-opus-4-7", msg_id="msg_s1",
                usage=_usage(),
                content=[{
                    "type": "tool_use", "id": "toolu_skill1", "name": "Skill",
                    "input": {"skill": "claude-api", "args": "pricing lookup"},
                }],
            ),
            _tool_result_user([("toolu_skill1", "pricing info")], "2026-01-01T00:00:08.000Z"),
        ]
        _write_jsonl(transcript, records)

        envelopes = list(te.emit_from_transcript(
            transcript, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude"))
        for e in envelopes:
            env.validate(e)

        skills = [
            e for e in envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.invocation_kind == env.InvocationKind.SKILL
        ]
        self.assertEqual(len(skills), 1)
        node = skills[0].payload
        self.assertEqual(node.node_name, "claude-api")
        self.assertEqual(node.toolkit_source, env.ToolkitSource.NATIVE)
        self.assertEqual(node.attributes["lifecycle_state"], env.SkillLifecycleState.EXECUTED.value)

        resolutions = [
            e for e in envelopes
            if e.record_type == env.RecordType.EXECUTION_EVENT
            and e.payload.event_kind == env.EventKind.SKILL_RESOLUTION
        ]
        self.assertGreaterEqual(len(resolutions), 1)

    def test_slashcommand_alias_executed_lifecycle(self) -> None:
        """Forward/defensive alias per the task brief — unverified
        against any real transcript (none exercise it; see report)."""
        transcript = self.tmp / f"{SESSION_ID}.jsonl"
        records = [
            _user("/code-review please", "2026-01-01T00:00:00.000Z"),
            _assistant(
                "2026-01-01T00:00:05.000Z", model="claude-opus-4-7", msg_id="msg_sc1",
                usage=_usage(),
                content=[{
                    "type": "tool_use", "id": "toolu_sc1", "name": "SlashCommand",
                    "input": {"command": "code-review"},
                }],
            ),
        ]
        _write_jsonl(transcript, records)

        envelopes = list(te.emit_from_transcript(
            transcript, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude"))
        for e in envelopes:
            env.validate(e)

        skills = [
            e for e in envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.invocation_kind == env.InvocationKind.SKILL
        ]
        names = {s.payload.node_name for s in skills}
        self.assertIn("code-review", names)

        # command-parse detection also fires from the raw "/code-review"
        # prompt (independent signal) -- requested lifecycle_state must
        # appear too, with toolkit_source=command_parse.
        requested = [
            s for s in skills
            if s.payload.attributes.get("lifecycle_state") == env.SkillLifecycleState.REQUESTED.value
        ]
        self.assertGreaterEqual(len(requested), 1)
        self.assertEqual(requested[0].payload.toolkit_source, env.ToolkitSource.COMMAND_PARSE)
        self.assertEqual(requested[0].payload.identity_source, env.IdentitySource.SYNTHETIC)
        self.assertEqual(requested[0].payload.parent_source, env.ParentSource.INFERRED)

    def test_parallel_tool_use_blocks_get_distinct_node_keys(self) -> None:
        transcript = self.tmp / f"{SESSION_ID}.jsonl"
        records = [
            _user("run these", "2026-01-01T00:00:00.000Z"),
            _assistant(
                "2026-01-01T00:00:05.000Z", model="claude-opus-4-7", msg_id="msg_p1",
                usage=_usage(),
                content=[
                    {"type": "tool_use", "id": "toolu_p1", "name": "Read",
                     "input": {"file_path": "/repo/a.py"}},
                    {"type": "tool_use", "id": "toolu_p2", "name": "Read",
                     "input": {"file_path": "/repo/b.py"}},
                    {"type": "tool_use", "id": "toolu_p3", "name": "Bash",
                     "input": {"command": "ls -la"}},
                ],
            ),
        ]
        _write_jsonl(transcript, records)

        envelopes = list(te.emit_from_transcript(
            transcript, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude"))
        for e in envelopes:
            env.validate(e)

        tool_nodes = [
            e for e in envelopes
            if e.record_type == env.RecordType.NODE_OBSERVED
            and e.payload.node_kind == env.NodeKind.INVOCATION
        ]
        self.assertEqual(len(tool_nodes), 3)
        keys = {e.payload.node_key for e in tool_nodes}
        self.assertEqual(len(keys), 3, "parallel tool_use blocks must not collide on node_key")

    def test_no_secret_patterns_leak_via_bash_command_attributes(self) -> None:
        """Belt-and-suspenders: even if a synthetic transcript carries a
        raw secret in a Bash command, redact_tool_args must keep it out
        of the emitted attributes (hash/classify only, never verbatim)."""
        transcript = self.tmp / f"{SESSION_ID}.jsonl"
        records = [
            _user("run this", "2026-01-01T00:00:00.000Z"),
            _assistant(
                "2026-01-01T00:00:05.000Z", model="claude-opus-4-7", msg_id="msg_sec1",
                usage=_usage(),
                content=[{
                    "type": "tool_use", "id": "toolu_sec1", "name": "Bash",
                    "input": {"command": "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE && ls"},
                }],
            ),
        ]
        _write_jsonl(transcript, records)

        envelopes = list(te.emit_from_transcript(
            transcript, session_id=SESSION_ID, org_id=ORG_ID, adapter="claude"))
        for e in envelopes:
            env.validate(e)

        import re
        compiled = [(name, re.compile(pat)) for name, pat in KNOWN_SECRET_PATTERNS]
        for e in envelopes:
            attrs = getattr(e.payload, "attributes", None)
            if not attrs:
                continue
            blob = json.dumps(attrs, default=str)
            for name, pattern in compiled:
                self.assertIsNone(pattern.search(blob), f"{name} leaked into attributes: {blob}")


if __name__ == "__main__":
    unittest.main()
