"""End-to-end tests for the Phase 1 runtime.

Uses the test.mock channel to drive ask_human replies deterministically.
Covers plan v0.2 edge cases 1, 2, 4, 8, 10, 11 (partial), 20, 21, 22.
See the file-level 'covered edge cases' comments for the mapping.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml


# conftest.py already put spike/executor/ on sys.path.
import askhuman  # noqa: E402
import channels as channels_mod  # noqa: E402
from channels.test_mock import TestMockChannel  # noqa: E402
import deployment as deployment_mod  # noqa: E402
import normalizer as normalizer_mod  # noqa: E402
import runtime_serve  # noqa: E402
import secrets as secrets_mod  # noqa: E402
import state as state_mod  # noqa: E402


# -------------------------------------------------------------------------
# Fixture: synthesize a Sentinel + deployment on disk
# -------------------------------------------------------------------------


def _write_sentinel_dir(
    tmp_path: Path,
    reply_normalization: str = "structured",
    with_escalation: bool = False,
    identity_policy: dict[str, Any] | None = None,
    parser_model: str | None = None,
    emit_after_ask: bool = True,
) -> Path:
    sdir = tmp_path / "sentinel"
    sdir.mkdir()
    if identity_policy is None:
        identity_policy = {"allowedUsers": ["alice@example.com"]}

    nodes: dict[str, Any] = {
        "confirm-env": {
            "kind": "ask_human",
            "config": {
                "question": "Which env should we investigate?",
                "answerSchema": {
                    "type": "object",
                    "required": ["environment"],
                    "properties": {
                        "environment": {"type": "string"},
                        "skipInvestigation": {"type": "boolean"},
                    },
                },
                "timeout": {"mode": "block-until-answered", "maxWait": "5m"},
                "evidence": {},
            },
        }
    }
    if emit_after_ask:
        nodes["emit-env-picked"] = {
            "kind": "emit",
            "dependsOn": ["confirm-env"],
            "config": {
                "finding": {
                    "type": "env-picked",
                    "title": "Operator picked ${nodes.confirm-env.output.environment}",
                    "severityExpression": '"info"',
                    "dedupeKey": "env-${nodes.confirm-env.output.environment}",
                    "attributes": {"environment": "${nodes.confirm-env.output.environment}"},
                }
            },
        }

    sentinel_doc = {
        "schemaVersion": "mechanize.dev/v1alpha1",
        "kind": "Sentinel",
        "metadata": {"name": "t"},
        "spec": {
            "inputs": {},
            "nodes": nodes,
        },
    }
    (sdir / "sentinel.yaml").write_text(yaml.safe_dump(sentinel_doc, sort_keys=False))

    binding: dict[str, Any] = {
        "channel_ref": "test.mock",
        "channel_params": {},
        "identity_policy": identity_policy,
        "reply_normalization": reply_normalization,
    }
    if parser_model:
        binding["parserModel"] = parser_model
    if with_escalation:
        binding["escalation"] = {
            "afterMinutes": 0.001,  # ~60ms, exercisable in tests
            "channel_ref": "test.mock",
            "channel_params": {"kind": "escalation"},
        }

    deployment_doc = {
        "schemaVersion": "mechanize.dev/v1alpha1",
        "kind": "SentinelDeployment",
        "runtime": "manual",
        "askHumanBindings": {"confirm-env": binding},
        "findingsRouting": [
            {"match": {"emitNode": "emit-env-picked"}, "sink": "stdout"}
        ],
        "execution": {
            "timeout": "5m",
            "sinkRetry": {"attempts": 2, "onExhausted": "drop-and-audit"},
        },
    }
    (sdir / "deployment.yaml").write_text(yaml.safe_dump(deployment_doc, sort_keys=False))
    (sdir / "inputs.json").write_text(json.dumps({}))
    return sdir


def _serve(sdir: Path, state_path: Path, **kwargs) -> int:
    return runtime_serve.run_serve(
        sentinel_dir=sdir,
        deployment_path=sdir / "deployment.yaml",
        inputs_path=sdir / "inputs.json",
        state_path=state_path,
        poll_interval=0.02,
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    TestMockChannel.reset()
    yield
    TestMockChannel.reset()


# -------------------------------------------------------------------------
# Happy path — structured reply flows through the DAG
# -------------------------------------------------------------------------


def test_happy_path_structured(tmp_path: Path) -> None:
    """Edge case 21-adjacent: full ask_human flow works locally in one process."""
    sdir = _write_sentinel_dir(tmp_path)
    TestMockChannel.INBOX["confirm-env"] = json.dumps(
        {"environment": "staging", "skipInvestigation": False}
    )
    TestMockChannel.IDENTITY["confirm-env"] = "alice@example.com"

    state_path = tmp_path / "state.db"
    rc = _serve(sdir, state_path)
    assert rc == 0

    # State recorded the normalized answer.
    with state_mod.StateStore.open(state_path) as st:
        asks = st.list_pending_asks()
        assert len(asks) == 1
        ask = asks[0]
        assert ask["state"] == state_mod.ASK_STATE_NORMALIZED
        assert ask["operator_id"] == "alice@example.com"
        assert ask["raw_reply"] is not None
        norm = json.loads(ask["normalized_json"])
        assert norm == {"environment": "staging", "skipInvestigation": False}
        events = [a["event_type"] for a in st.list_audit(run_id=ask["run_id"])]
        # State-machine coverage: waiting → received → parsing → normalized
        assert "ask_human.published" in events
        assert "ask_human.received" in events
        assert "ask_human.normalized" in events
        # Finding got routed & delivered (or at least attempted).
        assert "finding.delivered" in events or "finding.sink_failed" in events


# -------------------------------------------------------------------------
# Identity mismatch — treated as no-reply; audit trail records mismatch
# -------------------------------------------------------------------------


def test_identity_mismatch_treated_as_no_reply(tmp_path: Path) -> None:
    """Plan §Edge cases: 'identity mismatch → treat as no-reply, audit'."""
    sdir = _write_sentinel_dir(
        tmp_path, identity_policy={"allowedUsers": ["alice@example.com"]}
    )
    # Wrong operator replies; no follow-up. Runtime should record mismatch
    # and time out on the overall wait.
    TestMockChannel.INBOX["confirm-env"] = json.dumps({"environment": "prod"})
    TestMockChannel.IDENTITY["confirm-env"] = "eve@example.com"

    state_path = tmp_path / "state.db"
    rc = _serve(sdir, state_path, max_wait_override=timedelta(milliseconds=250))
    assert rc == 0  # DAG stays green; ask resolves inconclusive → downstream skipped

    with state_mod.StateStore.open(state_path) as st:
        asks = st.list_pending_asks()
        ask = asks[0]
        assert ask["state"] == state_mod.ASK_STATE_INCONCLUSIVE
        events = [a["event_type"] for a in st.list_audit(run_id=ask["run_id"])]
        assert "ask_human.identity_mismatch" in events
        assert "ask_human.timeout" in events


# -------------------------------------------------------------------------
# Timeout with no reply at all
# -------------------------------------------------------------------------


def test_timeout_no_reply(tmp_path: Path) -> None:
    """Edge case 1-ish: runtime respects maxWait and lands in inconclusive."""
    sdir = _write_sentinel_dir(tmp_path)
    # No INBOX entry at all → wait_for_reply returns None each poll.
    state_path = tmp_path / "state.db"
    rc = _serve(sdir, state_path, max_wait_override=timedelta(milliseconds=200))
    assert rc == 0

    with state_mod.StateStore.open(state_path) as st:
        ask = st.list_pending_asks()[0]
        assert ask["state"] == state_mod.ASK_STATE_INCONCLUSIVE
        assert "timeout" in (ask["inconclusive_reason"] or "")


# -------------------------------------------------------------------------
# Escalation — fires after breach; first-reply-wins on original still works
# -------------------------------------------------------------------------


def test_escalation_fires_and_original_still_wins(tmp_path: Path) -> None:
    """Plan §Edge cases: 'escalation.channel_ref honored + original channel first-reply-wins'."""
    sdir = _write_sentinel_dir(tmp_path, with_escalation=True)
    # Delay the reply long enough for escalation to fire first.
    TestMockChannel.REPLY_DELAY_S["confirm-env"] = 0.2
    TestMockChannel.INBOX["confirm-env"] = json.dumps({"environment": "prod"})
    TestMockChannel.IDENTITY["confirm-env"] = "alice@example.com"

    state_path = tmp_path / "state.db"
    rc = _serve(sdir, state_path, max_wait_override=timedelta(seconds=2))
    assert rc == 0

    with state_mod.StateStore.open(state_path) as st:
        ask = st.list_pending_asks()[0]
        assert ask["state"] == state_mod.ASK_STATE_NORMALIZED
        events = [a["event_type"] for a in st.list_audit(run_id=ask["run_id"])]
        assert "ask_human.escalated" in events, events

    # Escalation should have shown up as a second publish on the mock channel.
    escalation_publishes = [p for p in TestMockChannel.PUBLISHED if "#escalation" in p["node_id"]]
    assert escalation_publishes, TestMockChannel.PUBLISHED


# -------------------------------------------------------------------------
# Hedged reply routes to defer (edge case 20)
# -------------------------------------------------------------------------


def test_hedged_reply_routes_to_defer(tmp_path: Path) -> None:
    """Plan §Edge cases 20: '_defer: true routes to inconclusive/deferred'."""
    sdir = _write_sentinel_dir(
        tmp_path,
        reply_normalization="prose-llm-parse",
        parser_model="test.echo",  # hedge-phrase detector inside the parser
        with_escalation=True,
    )
    TestMockChannel.INBOX["confirm-env"] = "hold off — checking with security first"
    TestMockChannel.IDENTITY["confirm-env"] = "alice@example.com"

    state_path = tmp_path / "state.db"
    rc = _serve(sdir, state_path, max_wait_override=timedelta(seconds=1))
    assert rc == 0

    with state_mod.StateStore.open(state_path) as st:
        ask = st.list_pending_asks()[0]
        assert ask["state"] == state_mod.ASK_STATE_DEFERRED
        assert ask["defer_reason"]
        events = [a["event_type"] for a in st.list_audit(run_id=ask["run_id"])]
        assert "ask_human.deferred" in events
        # Escalation must be notified per the plan's routing rule.
        assert "ask_human.deferred_escalated" in events


# -------------------------------------------------------------------------
# Explicit state-machine transition test (waiting → received → parsing → normalized)
# -------------------------------------------------------------------------


def test_state_machine_transitions(tmp_path: Path) -> None:
    """Direct StateStore test: illegal transitions raise; legal ones stick."""
    state_path = tmp_path / "state.db"
    with state_mod.StateStore.open(state_path) as st:
        st.start_run("r1", "sha256:xyz", {})
        ask_id = st.create_pending_ask(
            run_id="r1",
            node_id="n",
            question="q",
            evidence={},
            channel_id="test.mock",
            handle_reference="n",
        )
        assert st.get_pending_ask(ask_id)["state"] == state_mod.ASK_STATE_WAITING

        # Illegal: waiting → parsing (must go through received first).
        with pytest.raises(state_mod.InvalidTransitionError):
            st.transition(ask_id, state_mod.ASK_STATE_PARSING)

        st.transition(ask_id, state_mod.ASK_STATE_RECEIVED, raw_reply="hi")
        st.transition(ask_id, state_mod.ASK_STATE_PARSING)
        st.transition(ask_id, state_mod.ASK_STATE_NORMALIZED, normalized_json={"ok": True})

        # Illegal: normalized → waiting.
        with pytest.raises(state_mod.InvalidTransitionError):
            st.transition(ask_id, state_mod.ASK_STATE_WAITING)


# -------------------------------------------------------------------------
# Flock refusal on concurrent serve (edge case 22)
# -------------------------------------------------------------------------


def test_flock_refuses_second_serve(tmp_path: Path) -> None:
    """Plan §Edge cases 22: 'two serve invocations against same sqlite state'."""
    state_path = tmp_path / "state.db"
    holder = state_mod.StateStore.open(state_path)
    try:
        with pytest.raises(state_mod.StateLockError) as excinfo:
            state_mod.StateStore.open(state_path)
        assert str(os.getpid()) in str(excinfo.value)
    finally:
        holder.close()

    # After close, a fresh open works.
    with state_mod.StateStore.open(state_path):
        pass


# -------------------------------------------------------------------------
# Secret resolver — env:// resolves, Phase 2 schemes refused
# -------------------------------------------------------------------------


def test_secret_env_scheme_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MECH_TEST_SECRET", "s3cr3t")
    assert secrets_mod.resolve("env://MECH_TEST_SECRET") == "s3cr3t"

    with pytest.raises(secrets_mod.UnsupportedSchemeError):
        secrets_mod.resolve("k8s-secret://foo/bar")
    with pytest.raises(secrets_mod.UnsupportedSchemeError):
        secrets_mod.resolve("vault://kv/foo")
    with pytest.raises(secrets_mod.UnsupportedSchemeError):
        secrets_mod.resolve("aws-sm://foo")


# -------------------------------------------------------------------------
# Deployment schema rejects malformed docs
# -------------------------------------------------------------------------


def test_deployment_schema_rejects_missing_runtime(tmp_path: Path) -> None:
    bad = tmp_path / "d.yaml"
    bad.write_text(yaml.safe_dump({"schemaVersion": "mechanize.dev/v1alpha1", "kind": "SentinelDeployment"}))
    with pytest.raises(deployment_mod.DeploymentValidationError):
        deployment_mod.load_deployment(bad)


def test_deployment_schema_accepts_min_shape(tmp_path: Path) -> None:
    ok = tmp_path / "d.yaml"
    ok.write_text(yaml.safe_dump({
        "schemaVersion": "mechanize.dev/v1alpha1",
        "kind": "SentinelDeployment",
        "runtime": "manual",
    }))
    d = deployment_mod.load_deployment(ok)
    assert d.runtime == "manual"
    assert d.default_parser_model is None


# -------------------------------------------------------------------------
# Normalizer sanity: structured mode + prose-llm-parse mode
# -------------------------------------------------------------------------


def test_normalizer_structured_ok() -> None:
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    out = normalizer_mod.normalize(
        raw_reply=json.dumps({"x": 3}),
        answer_schema=schema,
        reply_normalization="structured",
        parser_model=None,
    )
    assert out.status == "normalized"
    assert out.normalized_value == {"x": 3}


def test_normalizer_structured_bad_json_is_inconclusive() -> None:
    out = normalizer_mod.normalize(
        raw_reply="not json",
        answer_schema={},
        reply_normalization="structured",
        parser_model=None,
    )
    assert out.status == "inconclusive"


def test_normalizer_prose_defers_on_hedge() -> None:
    schema = {"type": "object", "required": ["x"], "properties": {"x": {"type": "integer"}}}
    out = normalizer_mod.normalize(
        raw_reply="i guess so?",
        answer_schema=schema,
        reply_normalization="prose-llm-parse",
        parser_model="test.echo",
    )
    assert out.status == "deferred"
    assert out.defer_reason
