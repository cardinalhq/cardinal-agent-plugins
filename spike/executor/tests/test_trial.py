"""Tests for common/mechanize/trial.py — Stage 10 + Stage 11.

Each check exists because a specific class of broken compile got through to
production. A test that only proves the happy path passes would let any of
them regress silently, so every T-check here has a negative case that pins
what it catches.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

# conftest.py puts spike/executor/ on sys.path; trial.py lives in common/mechanize/.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "common" / "mechanize") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "common" / "mechanize"))

import trial as trial_mod  # noqa: E402


# --------------------------------------------------------------------------- #
# A minimal but complete Sentinel directory                                   #
# --------------------------------------------------------------------------- #


SUMMARIZE_BODY = """\
def run(args):
    errors = args["errors"]
    return {"total": sum(errors), "degraded": sum(errors) > 100}
"""


def _sentinel_doc() -> dict[str, Any]:
    return {
        "apiVersion": "mechanize.dev/v1alpha1",
        "kind": "Sentinel",
        "metadata": {"name": "trial-fixture", "version": "0.1.0"},
        "spec": {
            "purpose": {"summary": "test", "reusableQuestion": "test", "conclusionType": "test"},
            "inputs": {
                "service": {"type": "string", "required": True},
                "window": {"type": "duration", "default": "1h"},
            },
            "capabilities": {
                "required": [{"id": "observability.error-overview", "capabilityType": "tool"}]
            },
            "variationPoints": [{"path": "/spec/inputs/window/default", "operations": ["replace"]}],
            "nodes": {
                "query-errors": {
                    "kind": "tool",
                    "dependsOn": [],
                    "config": {
                        "toolRef": "observability.error-overview",
                        "arguments": {"service": "${inputs.service}", "window": "${inputs.window}"},
                    },
                },
                "summarize-errors": {
                    "kind": "function",
                    "dependsOn": ["query-errors"],
                    "config": {
                        "runtime": "python3.12",
                        "source": "functions/summarize-errors.py",
                        "arguments": {"errors": "${nodes.query-errors.output.counts}"},
                    },
                },
                "degraded-condition": {
                    "kind": "condition",
                    "dependsOn": ["summarize-errors"],
                    "config": {"expression": "nodes.summarize-errors.output.degraded == true"},
                },
                "emit-degradation-finding": {
                    "kind": "emit",
                    "dependsOn": ["summarize-errors", "degraded-condition"],
                    "when": "${nodes.degraded-condition.output == true}",
                    "config": {
                        "finding": {
                            "type": "error-degradation",
                            "title": "Errors elevated for ${inputs.service}",
                            "severity": "warning",
                            "dedupeKey": "${inputs.service}:degradation",
                            "evidence": [{"nodeRef": "summarize-errors", "field": "output"}],
                            "attributes": {"total": "${nodes.summarize-errors.output.total}"},
                        }
                    },
                },
            },
        },
    }


def _write_sentinel_dir(tmp_path: Path, *, counts=(200, 150), expectation: dict | None = None) -> Path:
    sdir = tmp_path / "sentinel"
    sdir.mkdir()
    (sdir / "sentinel.yaml").write_text(yaml.safe_dump(_sentinel_doc(), sort_keys=False))

    (sdir / "functions").mkdir()
    (sdir / "functions" / "summarize-errors.py").write_text(SUMMARIZE_BODY)

    (sdir / "fixtures").mkdir()
    (sdir / "fixtures" / "query-errors.json").write_text(json.dumps({"counts": list(counts)}))

    (sdir / "inputs.json").write_text(json.dumps({"service": "checkout-api"}))

    if expectation is None:
        expectation = {
            "sourceSession": "test0001",
            "conclusion": "checkout-api errors are elevated",
            "expectFindings": [
                {
                    "emitNode": "emit-degradation-finding",
                    "type": "error-degradation",
                    "severity": "warning",
                    "evidenceNodes": ["summarize-errors"],
                }
            ],
            "expectNoFindings": [],
        }
    (sdir / "expectation.json").write_text(json.dumps(expectation))
    return sdir


def _patch(sdir: Path, mutate) -> None:
    path = sdir / "sentinel.yaml"
    doc = yaml.safe_load(path.read_text())
    mutate(doc)
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


def _check(report: dict, cid: str) -> dict:
    return next(c for c in report["checks"] if c["id"] == cid)


# --------------------------------------------------------------------------- #
# Happy path                                                                  #
# --------------------------------------------------------------------------- #


def test_a_complete_sentinel_passes_every_check(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)

    report = trial_mod.run_trial(sdir)

    assert report["passed"], trial_mod.format_verdict_block(report)
    assert [c["verdict"] for c in report["checks"]] == ["PASS"] * 9
    assert report["conclusion"] == "checkout-api errors are elevated"


def test_the_trial_writes_an_inspectable_report(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)

    trial_mod.run_trial(sdir)

    report = json.loads((sdir / "trial" / "report.json").read_text())
    assert report["passed"] is True
    assert len(report["runs"]) == 2
    assert report["runs"][0]["nodeStates"]["emit-degradation-finding"] == "SUCCEEDED"
    assert (sdir / "trial" / "run-1" / "stdout.log").exists()


def test_the_trial_deployment_is_fixture_only(tmp_path: Path):
    """A trial that could reach the network would be worse than no trial."""
    sdir = _write_sentinel_dir(tmp_path)

    trial_mod.run_trial(sdir)

    doc = yaml.safe_load((sdir / "trial" / "deployment.trial.yaml").read_text())
    bindings = doc["capabilityBindings"]
    assert bindings, "every capability must be bound"
    assert {b["provider"] for b in bindings.values()} == {"fixture"}


def test_toolrefs_outside_declared_capabilities_are_still_bound_to_fixtures(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    _patch(sdir, lambda d: d["spec"]["capabilities"].__setitem__("required", []))

    doc = yaml.safe_load((sdir / "sentinel.yaml").read_text())
    dest = trial_mod.write_trial_deployment(doc, tmp_path / "d.yaml")

    bindings = yaml.safe_load(dest.read_text())["capabilityBindings"]
    assert "observability.error-overview" in bindings


# --------------------------------------------------------------------------- #
# Preflight — T1, T2, T3                                                      #
# --------------------------------------------------------------------------- #


def test_t1_fails_without_inputs_json(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    (sdir / "inputs.json").unlink()

    report = trial_mod.run_trial(sdir)

    assert not report["passed"]
    assert _check(report, "T1")["verdict"] == "FAIL"
    # Preflight failure must not be followed by a noisier execution failure.
    assert _check(report, "T4")["verdict"] == "SKIP"


def test_t1_fails_when_a_required_input_is_unbound(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    (sdir / "inputs.json").write_text(json.dumps({"window": "2h"}))

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T1")["verdict"] == "FAIL"
    assert "service" in _check(report, "T1")["detail"]


def test_t2_fails_when_a_tool_node_has_no_fixture(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    (sdir / "fixtures" / "query-errors.json").unlink()

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T2")["verdict"] == "FAIL"
    assert "query-errors" in _check(report, "T2")["detail"]


def test_t3_fails_on_a_stub_function_body(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    (sdir / "functions" / "summarize-errors.py").write_text(
        "def run(args):\n"
        "    raise NotImplementedError('fill in summarize-errors: total the error counts')\n"
    )

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T3")["verdict"] == "FAIL"
    assert "STUB" in _check(report, "T3")["detail"]


def test_t3_fails_on_an_ungated_llm_node(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)

    def add_llm(doc):
        doc["spec"]["nodes"]["interpret-semantics"] = {
            "kind": "llm",
            "dependsOn": ["query-errors"],
            "config": {"task": "what do these errors mean?"},
        }

    _patch(sdir, add_llm)

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T3")["verdict"] == "FAIL"
    assert "interpret-semantics" in _check(report, "T3")["detail"]


# --------------------------------------------------------------------------- #
# Execution — T4, T5                                                          #
# --------------------------------------------------------------------------- #


def test_t4_fails_when_a_node_errors(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    (sdir / "functions" / "summarize-errors.py").write_text(
        "def run(args):\n    return {'total': sum(args['nope'])}\n"
    )

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T4")["verdict"] == "FAIL"
    assert "summarize-errors" in _check(report, "T4")["detail"]


def test_t4_reports_gated_skips_without_failing(tmp_path: Path):
    # counts below the threshold: the condition is false, emit is gated off.
    sdir = _write_sentinel_dir(
        tmp_path, counts=(1, 2),
        expectation={
            "sourceSession": "test0001",
            "conclusion": "checkout-api errors are within normal range",
            "expectFindings": [],
            "expectNoFindings": ["emit-degradation-finding"],
        },
    )

    report = trial_mod.run_trial(sdir)

    assert report["passed"], trial_mod.format_verdict_block(report)
    assert "SKIPPED" in _check(report, "T4")["detail"]
    assert _check(report, "T8")["verdict"] == "PASS"


def test_t5_fails_on_a_nondeterministic_function(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    (sdir / "functions" / "summarize-errors.py").write_text(
        "import uuid\n"
        "def run(args):\n"
        "    return {'total': sum(args['errors']), 'degraded': True, 'nonce': uuid.uuid4().hex}\n"
    )

    def cite_nonce(doc):
        finding = doc["spec"]["nodes"]["emit-degradation-finding"]["config"]["finding"]
        finding["attributes"]["nonce"] = "${nodes.summarize-errors.output.nonce}"

    _patch(sdir, cite_nonce)

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T5")["verdict"] == "FAIL"


def test_t5_fails_when_the_dedupekey_varies_between_runs(tmp_path: Path):
    # `${execution.runId}` stands in for any per-run token — `${execution.now}`
    # is the shape seen in the wild but two runs land in the same second here.
    sdir = _write_sentinel_dir(tmp_path)

    def vary_dedupe(doc):
        finding = doc["spec"]["nodes"]["emit-degradation-finding"]["config"]["finding"]
        finding["dedupeKey"] = "${inputs.service}:${execution.runId}"

    _patch(sdir, vary_dedupe)

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T5")["verdict"] == "FAIL"
    assert "dedupeHash" in _check(report, "T5")["detail"]


# --------------------------------------------------------------------------- #
# Findings well-formedness — T6, T7                                           #
# --------------------------------------------------------------------------- #


def test_t6_fails_on_a_finding_with_no_declared_evidence(tmp_path: Path):
    """The production bug: a severity-bearing finding backed by nothing."""
    sdir = _write_sentinel_dir(tmp_path)

    def strip_evidence(doc):
        doc["spec"]["nodes"]["emit-degradation-finding"]["config"]["finding"].pop("evidence")

    _patch(sdir, strip_evidence)

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T6")["verdict"] == "FAIL"
    assert "no evidence" in _check(report, "T6")["detail"]


def test_t6_fails_when_required_evidence_resolves_to_null(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)

    def point_at_absent_field(doc):
        finding = doc["spec"]["nodes"]["emit-degradation-finding"]["config"]["finding"]
        finding["evidence"] = [{"nodeRef": "summarize-errors", "field": "notAField"}]

    _patch(sdir, point_at_absent_field)

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T6")["verdict"] == "FAIL"
    assert "resolved to null" in _check(report, "T6")["detail"]


def test_t7_does_not_flag_a_title_that_legitimately_says_null(tmp_path: Path):
    """"null pointer exception" is a plausible finding title, not a bug."""
    sdir = _write_sentinel_dir(tmp_path)

    def literal_null_in_title(doc):
        finding = doc["spec"]["nodes"]["emit-degradation-finding"]["config"]["finding"]
        finding["title"] = "null pointer exceptions elevated for ${inputs.service}"

    _patch(sdir, literal_null_in_title)

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T7")["verdict"] == "PASS", _check(report, "T7")["detail"]


def test_t7_fails_when_an_interpolated_title_resolves_to_null(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)

    def unresolvable_title(doc):
        finding = doc["spec"]["nodes"]["emit-degradation-finding"]["config"]["finding"]
        finding["title"] = "Errors elevated for ${nodes.summarize-errors.output.absentField}"

    _patch(sdir, unresolvable_title)

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T7")["verdict"] == "FAIL"
    assert "unresolved reference" in _check(report, "T7")["detail"]


def test_t7_fails_on_an_attribute_that_rendered_to_null(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)

    def unresolvable_attribute(doc):
        finding = doc["spec"]["nodes"]["emit-degradation-finding"]["config"]["finding"]
        finding["attributes"]["ratio"] = "${nodes.summarize-errors.output.absentField}"

    _patch(sdir, unresolvable_attribute)

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T7")["verdict"] == "FAIL"
    assert "ratio" in _check(report, "T7")["detail"]


# --------------------------------------------------------------------------- #
# Stage 11 — conclusion comparison, T8 + T9                                   #
# --------------------------------------------------------------------------- #


def test_t8_fails_when_the_expected_finding_never_fires(tmp_path: Path):
    # Counts below threshold, but the expectation says a finding was concluded.
    sdir = _write_sentinel_dir(tmp_path, counts=(1, 2))

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T8")["verdict"] == "FAIL"
    assert "produced none" in _check(report, "T8")["detail"]


def test_t8_fails_when_a_finding_fires_that_should_not(tmp_path: Path):
    sdir = _write_sentinel_dir(
        tmp_path,
        expectation={
            "sourceSession": "test0001",
            "conclusion": "checkout-api is healthy",
            "expectFindings": [],
            "expectNoFindings": ["emit-degradation-finding"],
        },
    )

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T8")["verdict"] == "FAIL"
    assert "did NOT conclude" in _check(report, "T8")["detail"]


def test_t8_fails_on_an_expectation_naming_a_nonexistent_node(tmp_path: Path):
    sdir = _write_sentinel_dir(
        tmp_path,
        expectation={
            "sourceSession": "test0001",
            "conclusion": "x",
            "expectFindings": [{"emitNode": "emit-something-else"}],
            "expectNoFindings": [],
        },
    )

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T8")["verdict"] == "FAIL"
    assert "emit-something-else" in _check(report, "T8")["detail"]


def test_t9_fails_when_severity_diverges_from_the_original_conclusion(tmp_path: Path):
    sdir = _write_sentinel_dir(
        tmp_path,
        expectation={
            "sourceSession": "test0001",
            "conclusion": "checkout-api errors are critically elevated",
            "expectFindings": [
                {"emitNode": "emit-degradation-finding", "severity": "critical"}
            ],
            "expectNoFindings": [],
        },
    )

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T8")["verdict"] == "PASS"
    assert _check(report, "T9")["verdict"] == "FAIL"
    assert "severity" in _check(report, "T9")["detail"]


def test_t9_fails_when_the_evidence_does_not_include_the_expected_node(tmp_path: Path):
    sdir = _write_sentinel_dir(
        tmp_path,
        expectation={
            "sourceSession": "test0001",
            "conclusion": "x",
            "expectFindings": [
                {"emitNode": "emit-degradation-finding", "evidenceNodes": ["query-errors"]}
            ],
            "expectNoFindings": [],
        },
    )

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T9")["verdict"] == "FAIL"
    assert "query-errors" in _check(report, "T9")["detail"]


def test_missing_expectation_fails_stage_11_by_default(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    (sdir / "expectation.json").unlink()

    report = trial_mod.run_trial(sdir)

    assert not report["passed"]
    assert _check(report, "T8")["verdict"] == "FAIL"
    # Stage 10 still ran and passed — the failure is scoped to Stage 11.
    assert _check(report, "T4")["verdict"] == "PASS"


def test_missing_expectation_can_be_downgraded_for_ci(tmp_path: Path):
    sdir = _write_sentinel_dir(tmp_path)
    (sdir / "expectation.json").unlink()

    report = trial_mod.run_trial(sdir, allow_missing_expectation=True)

    assert report["passed"]
    assert _check(report, "T8")["verdict"] == "SKIP"


def test_an_empty_expectation_is_not_a_pass(tmp_path: Path):
    """Ground truth that asserts nothing must not read as a satisfied Stage 11."""
    sdir = _write_sentinel_dir(
        tmp_path,
        expectation={"sourceSession": "test0001", "conclusion": "x",
                     "expectFindings": [], "expectNoFindings": []},
    )

    report = trial_mod.run_trial(sdir)

    assert _check(report, "T8")["verdict"] == "FAIL"
    assert "no ground truth" in _check(report, "T8")["detail"]


# --------------------------------------------------------------------------- #
# Harness behaviour                                                           #
# --------------------------------------------------------------------------- #


def test_a_missing_sentinel_is_a_harness_error_not_a_failed_trial(tmp_path: Path):
    with pytest.raises(trial_mod.TrialHarnessError):
        trial_mod.run_trial(tmp_path)


def test_cli_exit_codes(tmp_path: Path, capsys):
    sdir = _write_sentinel_dir(tmp_path)
    assert trial_mod._main([str(sdir)]) == 0
    assert "TRIAL: PASSED" in capsys.readouterr().out

    (sdir / "expectation.json").unlink()
    assert trial_mod._main([str(sdir)]) == 1
    assert "TRIAL: FAILED" in capsys.readouterr().out

    assert trial_mod._main([str(tmp_path / "nope")]) == 2
