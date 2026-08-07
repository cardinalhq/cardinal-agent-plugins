"""Round-trip tests for the emit-node evidence contract.

The compiler writes `evidence:` and the runtime resolves it. When those two
disagreed, the runtime silently dropped every entry it did not recognize and
shipped a finding with `evidence: []` — a `critical` conclusion backed by
nothing, which passed graph validation and deployed clean.

Canonical form is the mapping `{nodeRef, field, optional}`: the string form
`${nodes.<id>.output}` cannot express `optional` or field selection, so it is
refused rather than silently normalized. These tests pin both halves of the
contract — what the runtime accepts, and what the linter catches before a run
ever happens.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

# conftest.py already put spike/executor/ on sys.path.
import executor as executor_mod  # noqa: E402
import lint as lint_mod  # noqa: E402


def _env(node_outputs: dict) -> executor_mod._Env:
    return executor_mod._Env(
        inputs={"service": "checkout-api"},
        nodes={k: {"output": v} for k, v in node_outputs.items()},
        execution={
            "runId": "run-test",
            "now": datetime(2026, 8, 6, tzinfo=timezone.utc),
            "sentinelDigest": "sha256:test",
            "variationDigest": "",
        },
    )


def _emit_node(evidence, depends_on=("summarize",)):
    return {
        "kind": "emit",
        "dependsOn": list(depends_on),
        "config": {
            "finding": {
                "type": "test-finding",
                "title": "test",
                "dedupeKey": "${inputs.service}:test",
                "evidence": evidence,
            }
        },
    }


# --------------------------------------------------------------------------- #
# Runtime resolution                                                          #
# --------------------------------------------------------------------------- #


def test_mapping_form_resolves_values(tmp_path: Path):
    node = _emit_node([{"nodeRef": "summarize", "field": "output"}])
    env = _env({"summarize": {"overall": "degraded", "errors": 321}})

    finding = executor_mod._build_finding("emit-test", node, env, tmp_path)

    assert len(finding["evidence"]) == 1
    entry = finding["evidence"][0]
    assert entry["nodeRef"] == "summarize"
    assert entry["value"] == {"overall": "degraded", "errors": 321}
    assert entry["resolved"] is True


def test_mapping_form_selects_a_single_field(tmp_path: Path):
    node = _emit_node([{"nodeRef": "summarize", "field": "errors"}])
    env = _env({"summarize": {"overall": "degraded", "errors": 321}})

    finding = executor_mod._build_finding("emit-test", node, env, tmp_path)

    assert finding["evidence"][0]["value"] == 321
    assert finding["evidence"][0]["resolved"] is True


def test_string_form_raises_instead_of_emitting_empty_evidence(tmp_path: Path):
    """The regression this file exists for: silently-dropped evidence."""
    node = _emit_node(["${nodes.summarize.output}"])
    env = _env({"summarize": {"overall": "degraded"}})

    with pytest.raises(executor_mod.DagValidationError) as exc:
        executor_mod._build_finding("emit-test", node, env, tmp_path)

    message = str(exc.value)
    assert "emit-test" in message
    assert "evidence[0]" in message
    # The error must name the accepted shape, not just report a rejection.
    assert "nodeRef" in message


def test_mapping_without_noderef_raises(tmp_path: Path):
    node = _emit_node([{"field": "output"}])
    env = _env({"summarize": {"overall": "degraded"}})

    with pytest.raises(executor_mod.DagValidationError):
        executor_mod._build_finding("emit-test", node, env, tmp_path)


def test_missing_field_is_flagged_unresolved_not_silently_null(tmp_path: Path):
    node = _emit_node([{"nodeRef": "summarize", "field": "absent"}])
    env = _env({"summarize": {"overall": "degraded"}})

    finding = executor_mod._build_finding("emit-test", node, env, tmp_path)

    entry = finding["evidence"][0]
    assert entry["value"] is None
    assert entry["resolved"] is False


def test_optional_evidence_from_a_skipped_node_resolves_false_without_raising(tmp_path: Path):
    node = _emit_node([{"nodeRef": "never-ran", "field": "output", "optional": True}])
    env = _env({"summarize": {"overall": "degraded"}})

    finding = executor_mod._build_finding("emit-test", node, env, tmp_path)

    entry = finding["evidence"][0]
    assert entry["optional"] is True
    assert entry["resolved"] is False
    assert entry["reason"] == "upstream-not-produced"


def test_required_evidence_from_a_missing_node_raises(tmp_path: Path):
    node = _emit_node([{"nodeRef": "never-ran", "field": "output"}])
    env = _env({"summarize": {"overall": "degraded"}})

    with pytest.raises(executor_mod.DagValidationError):
        executor_mod._build_finding("emit-test", node, env, tmp_path)


# --------------------------------------------------------------------------- #
# Static catch — lint sees it before anything runs                            #
# --------------------------------------------------------------------------- #


def _write_sentinel(tmp_path: Path, evidence, depends_on=("summarize",)) -> Path:
    sdir = tmp_path / "sentinel"
    sdir.mkdir()
    doc = {
        "apiVersion": "mechanize.dev/v1alpha1",
        "kind": "Sentinel",
        "metadata": {"name": "evidence-test", "version": "0.1.0"},
        "spec": {
            "inputs": {"service": {"type": "string", "required": True}},
            "capabilities": {"required": []},
            "nodes": {
                "summarize": {
                    "kind": "function",
                    "dependsOn": [],
                    "config": {
                        "runtime": "python3.12",
                        "source": "functions/summarize.py",
                        "arguments": {"service": "${inputs.service}"},
                    },
                },
                "emit-test": _emit_node(evidence, depends_on),
            },
        },
    }
    (sdir / "sentinel.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    (sdir / "functions").mkdir()
    (sdir / "functions" / "summarize.py").write_text("def run(args):\n    return {'ok': True}\n")
    return sdir


def _codes(result, code: str, severity: str | None = None) -> list:
    return [
        f for f in result.findings
        if f.code == code and (severity is None or f.severity == severity)
    ]


def test_lint_fails_the_string_form(tmp_path: Path):
    sdir = _write_sentinel(tmp_path, ["${nodes.summarize.output}"])

    result = lint_mod.lint_structural(sdir)

    fails = _codes(result, "EMIT-EVIDENCE", "FAIL")
    assert len(fails) == 1
    assert "nodeRef" in fails[0].fix
    assert not result.passed


def test_lint_passes_the_mapping_form(tmp_path: Path):
    sdir = _write_sentinel(tmp_path, [{"nodeRef": "summarize", "field": "output"}])

    result = lint_mod.lint_structural(sdir)

    assert _codes(result, "EMIT-EVIDENCE") == []


def test_lint_fails_evidence_pointing_at_an_undeclared_node(tmp_path: Path):
    sdir = _write_sentinel(tmp_path, [{"nodeRef": "no-such-node", "field": "output"}])

    result = lint_mod.lint_structural(sdir)

    fails = _codes(result, "EMIT-EVIDENCE", "FAIL")
    assert len(fails) == 1
    assert "no-such-node" in fails[0].message


def test_lint_warns_when_evidence_has_no_ordering_guarantee(tmp_path: Path):
    # emit-test cites `summarize` but depends on nothing — nothing orders them.
    sdir = _write_sentinel(tmp_path, [{"nodeRef": "summarize", "field": "output"}], depends_on=())

    result = lint_mod.lint_structural(sdir)

    warns = _codes(result, "EMIT-EVIDENCE", "WARN")
    assert len(warns) == 1
    assert "summarize" in warns[0].message
    # A WARN must not fail the lint.
    assert result.passed


def test_lint_accepts_transitive_ordering(tmp_path: Path):
    """An emit that reaches its evidence through a reducer is correctly ordered."""
    sdir = _write_sentinel(tmp_path, [{"nodeRef": "summarize", "field": "output"}], depends_on=("reduce",))
    path = sdir / "sentinel.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["spec"]["nodes"]["reduce"] = {
        "kind": "function",
        "dependsOn": ["summarize"],
        "config": {"runtime": "python3.12", "source": "functions/reduce.py", "arguments": {}},
    }
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    (sdir / "functions" / "reduce.py").write_text("def run(args):\n    return {'ok': True}\n")

    result = lint_mod.lint_structural(sdir)

    assert _codes(result, "EMIT-EVIDENCE") == []
