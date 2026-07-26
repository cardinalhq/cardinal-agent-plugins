"""Unit tests for cardinal_core.envelope — schema-v1 roundtrip and validation.

Run:
    cd core && python3 -m unittest tests.test_envelope -v
"""

from __future__ import annotations

import unittest

from cardinal_core import envelope as env


def _node_kwargs(**overrides):
    base = dict(
        execution_key="ek-1",
        node_key="nk-1",
        node_kind=env.NodeKind.INVOCATION,
        node_name="Edit",
        identity_source=env.IdentitySource.NATIVE,
        parent_source=env.ParentSource.NATIVE,
        timing_source=env.TimingSource.NATIVE,
        model_source=env.ModelSource.EXPLICIT,
        toolkit_source=env.ToolkitSource.NATIVE,
        usage_source=env.UsageSource.NATIVE,
        invocation_kind=env.InvocationKind.TOOL,
        tool_kind=env.ToolKind.BUILTIN,
    )
    base.update(overrides)
    return base


def _envelope(record_type: env.RecordType, payload, **overrides) -> env.Envelope:
    base = dict(
        schema_version=env.SCHEMA_VERSION,
        org_id="org-1",
        adapter="claude",
        session_id="sess-1",
        record_id="rec-1",
        record_type=record_type,
        observed_ns=1_000,
        effective_ns=1_000,
        payload=payload,
    )
    base.update(overrides)
    return env.Envelope(**base)


class RoundtripTests(unittest.TestCase):
    def test_node_observed_roundtrip(self) -> None:
        payload = env.NodeObserved(**_node_kwargs())
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        env.validate(e)
        self.assertEqual(env.from_json(env.to_json(e)), e)

    def test_node_updated_roundtrip(self) -> None:
        payload = env.NodeUpdated(**_node_kwargs(end_ns=2_000))
        e = _envelope(env.RecordType.NODE_UPDATED, payload)
        env.validate(e)
        self.assertEqual(env.from_json(env.to_json(e)), e)

    def test_edge_observed_roundtrip(self) -> None:
        payload = env.EdgeObserved(
            execution_key="ek-1",
            source_node_key="nk-1",
            target_node_key="nk-2",
            edge_kind=env.EdgeKind.PARENT_OF,
            attributes={"note": "turn-to-call"},
        )
        e = _envelope(env.RecordType.EDGE_OBSERVED, payload)
        env.validate(e)
        self.assertEqual(env.from_json(env.to_json(e)), e)

    def test_execution_event_roundtrip(self) -> None:
        payload = env.ExecutionEvent(
            execution_key="ek-1",
            event_kind=env.EventKind.FILE_MUTATION,
            event_ns=5_000,
            related_node_key="nk-1",
            attributes={"path": "foo.py"},
        )
        e = _envelope(env.RecordType.EXECUTION_EVENT, payload)
        env.validate(e)
        self.assertEqual(env.from_json(env.to_json(e)), e)

    def test_usage_observed_roundtrip(self) -> None:
        payload = env.UsageObserved(
            execution_key="ek-1",
            node_key="nk-1",
            input_tokens=100,
            output_tokens=50,
            usage_source=env.UsageSource.NATIVE,
            cached_tokens=10,
            cost_usd=0.0123,
        )
        e = _envelope(env.RecordType.USAGE_OBSERVED, payload)
        env.validate(e)
        self.assertEqual(env.from_json(env.to_json(e)), e)

    def test_artifact_link_observed_roundtrip(self) -> None:
        payload = env.ArtifactLinkObserved(
            execution_key="ek-1",
            node_key="nk-1",
            artifact_kind="file",
            artifact_ref="src/foo.py",
        )
        e = _envelope(env.RecordType.ARTIFACT_LINK_OBSERVED, payload)
        env.validate(e)
        self.assertEqual(env.from_json(env.to_json(e)), e)


class ValidationTests(unittest.TestCase):
    def test_invocation_kind_on_non_invocation_node_rejected(self) -> None:
        payload = env.NodeObserved(**_node_kwargs(
            node_kind=env.NodeKind.TURN,
            invocation_kind=env.InvocationKind.TOOL,
            tool_kind=None,
        ))
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        with self.assertRaises(env.EnvelopeValidationError):
            env.validate(e)

    def test_tool_kind_on_non_tool_invocation_rejected(self) -> None:
        payload = env.NodeObserved(**_node_kwargs(
            invocation_kind=env.InvocationKind.SKILL,
            tool_kind=env.ToolKind.BUILTIN,
        ))
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        with self.assertRaises(env.EnvelopeValidationError):
            env.validate(e)

    def test_invocation_node_without_invocation_kind_rejected(self) -> None:
        payload = env.NodeObserved(**_node_kwargs(
            node_kind=env.NodeKind.INVOCATION,
            invocation_kind=None,
            tool_kind=None,
        ))
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        with self.assertRaises(env.EnvelopeValidationError):
            env.validate(e)

    def test_tool_invocation_without_tool_kind_rejected(self) -> None:
        payload = env.NodeObserved(**_node_kwargs(
            node_kind=env.NodeKind.INVOCATION,
            invocation_kind=env.InvocationKind.TOOL,
            tool_kind=None,
        ))
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        with self.assertRaises(env.EnvelopeValidationError):
            env.validate(e)

    def test_missing_provenance_axis_on_node_payload_rejected(self) -> None:
        payload = env.NodeObserved(**_node_kwargs(usage_source=None))
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        with self.assertRaises(env.EnvelopeValidationError):
            env.validate(e)

    def test_unknown_enum_value_rejected(self) -> None:
        payload = env.NodeObserved(**_node_kwargs(identity_source="bogus"))
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        with self.assertRaises(env.EnvelopeValidationError):
            env.validate(e)

    def test_schema_version_mismatch_rejected(self) -> None:
        payload = env.NodeObserved(**_node_kwargs())
        e = _envelope(env.RecordType.NODE_OBSERVED, payload, schema_version=2)
        with self.assertRaises(env.EnvelopeValidationError):
            env.validate(e)

    def test_record_type_payload_mismatch_rejected(self) -> None:
        payload = env.NodeObserved(**_node_kwargs())
        e = _envelope(env.RecordType.EDGE_OBSERVED, payload)
        with self.assertRaises(env.EnvelopeValidationError):
            env.validate(e)

    def test_valid_node_passes(self) -> None:
        payload = env.NodeObserved(**_node_kwargs())
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        env.validate(e)  # no raise

    def test_from_json_rejects_unknown_enum_value(self) -> None:
        payload = env.NodeObserved(**_node_kwargs())
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        data = env.to_json(e)
        data["payload"]["node_kind"] = "not-a-real-kind"
        with self.assertRaises(env.EnvelopeValidationError):
            env.from_json(data)

    def test_from_json_rejects_unknown_record_type(self) -> None:
        payload = env.NodeObserved(**_node_kwargs())
        e = _envelope(env.RecordType.NODE_OBSERVED, payload)
        data = env.to_json(e)
        data["record_type"] = "not-a-real-record-type"
        with self.assertRaises(env.EnvelopeValidationError):
            env.from_json(data)


class RecordIdTests(unittest.TestCase):
    def test_deterministic_same_input(self) -> None:
        payload = {"a": 1, "b": "x"}
        self.assertEqual(env.record_id_for(payload), env.record_id_for(dict(payload)))

    def test_key_order_does_not_matter(self) -> None:
        self.assertEqual(
            env.record_id_for({"a": 1, "b": 2}),
            env.record_id_for({"b": 2, "a": 1}),
        )

    def test_different_input_different_hash(self) -> None:
        self.assertNotEqual(
            env.record_id_for({"a": 1}),
            env.record_id_for({"a": 2}),
        )

    def test_returns_hex_sha256(self) -> None:
        digest = env.record_id_for({"a": 1})
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises if not hex


class EnumValueSpotCheckTests(unittest.TestCase):
    """Enum values must match docs/local-notes/plans/agent-execution-graph.md
    and docs/canonical-model.md verbatim — these are spot checks, not an
    exhaustive contract test."""

    def test_record_type_values(self) -> None:
        self.assertEqual(env.RecordType.NODE_OBSERVED.value, "node_observed")
        self.assertEqual(env.RecordType.ARTIFACT_LINK_OBSERVED.value, "artifact_link_observed")

    def test_node_kind_values(self) -> None:
        self.assertEqual(
            {k.value for k in env.NodeKind},
            {"turn", "llm_call", "invocation", "artifact", "event"},
        )

    def test_edge_kind_values(self) -> None:
        self.assertEqual(
            {k.value for k in env.EdgeKind},
            {
                "parent_of", "invoked", "delegated_to", "continued_as",
                "used_toolkit", "produced_artifact", "contributed_to",
                "linked_to_outcome",
            },
        )

    def test_provenance_axis_values(self) -> None:
        self.assertEqual(
            {v.value for v in env.IdentitySource}, {"native", "derived", "synthetic"}
        )
        self.assertEqual(
            {v.value for v in env.ParentSource},
            {"native", "transcript", "temporal", "inferred", "unknown"},
        )
        self.assertEqual(
            {v.value for v in env.TimingSource},
            {"native", "reconstructed", "estimated", "marker", "unknown"},
        )
        self.assertEqual(
            {v.value for v in env.ModelSource},
            {"explicit", "inherited", "session_default", "unknown"},
        )
        self.assertEqual(
            {v.value for v in env.ToolkitSource},
            {"native", "command_parse", "prompt_inference", "unknown"},
        )
        self.assertEqual(
            {v.value for v in env.UsageSource},
            {"native", "allocated", "estimated", "unknown"},
        )

    def test_skill_lifecycle_values(self) -> None:
        self.assertEqual(
            {v.value for v in env.SkillLifecycleState},
            {"requested", "resolved", "executed"},
        )


if __name__ == "__main__":
    unittest.main()
