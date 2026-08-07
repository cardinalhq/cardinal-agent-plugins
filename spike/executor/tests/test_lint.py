"""Phase-1 sentinel-lint coverage.

Run: `spike/executor/.venv/bin/pytest spike/executor/tests/test_lint.py -v`.

Each test builds a minimal well-formed Sentinel in a tmp_path directory, mutates
exactly one thing, and asserts the correct code fires. This keeps failures
independent — a regression in one rule doesn't blast every assertion.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SPIKE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SPIKE_DIR))

from lint import lint_structural, format_json  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures — a canonical well-formed Sentinel + its supporting files          #
# --------------------------------------------------------------------------- #

BASELINE_SENTINEL = {
    "apiVersion": "mechanize.dev/v1alpha1",
    "kind": "Sentinel",
    "metadata": {"name": "unit-test-sentinel", "version": "0.1.0"},
    "spec": {
        "inputs": {
            "service": {"type": "string", "required": True},
            "window": {"type": "duration", "default": "6h"},
        },
        "capabilities": {
            "required": [
                {"id": "observability.query-metrics", "capabilityType": "tool"},
            ],
        },
        "variationPoints": [
            {"path": "/spec/inputs/service", "operations": ["bind"]},
            {"path": "/spec/inputs/window/default", "operations": ["replace"]},
        ],
        "nodes": {
            "query-metric": {
                "kind": "tool",
                "dependsOn": [],
                "config": {
                    "toolRef": "observability.query-metrics",
                    "arguments": {
                        "service": "${inputs.service}",
                        "window": "${inputs.window}",
                    },
                },
            },
            "summarize-metric": {
                "kind": "function",
                "dependsOn": ["query-metric"],
                "config": {
                    "runtime": "python3.12",
                    "source": "functions/summarize-metric.py",
                    "entrypoint": "run",
                    "arguments": {"series": "${nodes.query-metric.output}"},
                },
            },
            "emit-finding": {
                "kind": "emit",
                "dependsOn": ["summarize-metric"],
                "config": {
                    "finding": {
                        "type": "metric-anomaly",
                        "title": "Anomaly for ${inputs.service}",
                        "dedupeKey": "${inputs.service}:anomaly",
                    },
                },
            },
        },
    },
}

FUNCTION_BODY_OK = """def run(args):
    return {"summary": "ok"}
"""


def _write_baseline(tmp_path: Path, sentinel: dict | None = None) -> Path:
    d = tmp_path / "sentinel-dir"
    d.mkdir()
    doc = copy.deepcopy(sentinel or BASELINE_SENTINEL)
    (d / "sentinel.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    (d / "functions").mkdir()
    (d / "functions" / "summarize-metric.py").write_text(FUNCTION_BODY_OK)
    return d


def _codes(result) -> list[str]:
    return [f.code for f in result.findings]


# --------------------------------------------------------------------------- #
# Core structural cases                                                        #
# --------------------------------------------------------------------------- #

def test_well_formed_sentinel_passes(tmp_path):
    d = _write_baseline(tmp_path)
    result = lint_structural(d)
    assert result.passed, f"expected PASS, got: {[(f.code, f.message) for f in result.findings]}"


def test_missing_function_file_fails(tmp_path):
    d = _write_baseline(tmp_path)
    (d / "functions" / "summarize-metric.py").unlink()
    result = lint_structural(d)
    assert not result.passed
    assert "FUNC-MISSING" in _codes(result)


def test_mjs_extension_fails(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["nodes"]["summarize-metric"]["config"]["source"] = "functions/summarize-metric.mjs"
    d = _write_baseline(tmp_path, doc)
    # Also drop the .py file so the fixture doesn't accidentally satisfy FUNC-MISSING.
    (d / "functions" / "summarize-metric.py").unlink()
    (d / "functions" / "summarize-metric.mjs").write_text("export function run() {}")
    result = lint_structural(d)
    assert not result.passed
    assert "FUNC-EXT" in _codes(result), _codes(result)


def test_nodejs_runtime_fails(tmp_path):
    """CORE.md Stage 4 rule: v0 function nodes MUST declare runtime: python3.12."""
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["nodes"]["summarize-metric"]["config"]["runtime"] = "nodejs22"
    d = _write_baseline(tmp_path, doc)
    result = lint_structural(d)
    assert not result.passed
    codes = _codes(result)
    assert "FUNC-RUNTIME" in codes, codes


def test_missing_run_entrypoint_fails(tmp_path):
    d = _write_baseline(tmp_path)
    (d / "functions" / "summarize-metric.py").write_text("def not_run(args):\n    return {}\n")
    result = lint_structural(d)
    assert not result.passed
    assert "FUNC-ENTRY" in _codes(result)


def test_variation_kind_fails(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["kind"] = "Variation"
    d = _write_baseline(tmp_path, doc)
    result = lint_structural(d)
    assert not result.passed
    codes = _codes(result)
    assert "STRUCT-VARIATION" in codes
    msg = next(f.message for f in result.findings if f.code == "STRUCT-VARIATION")
    assert "Variations not yet supported" in msg
    assert "overlay-bindings" in msg


# --------------------------------------------------------------------------- #
# R1–R6 — one focused failing case each                                        #
# --------------------------------------------------------------------------- #

def test_r1_fails_when_defaulted_input_missing_variation_point(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    # Remove the /spec/inputs/window/default variation point; `window` still
    # referenced by the query-metric tool arg.
    doc["spec"]["variationPoints"] = [
        vp for vp in doc["spec"]["variationPoints"]
        if vp["path"] != "/spec/inputs/window/default"
    ]
    d = _write_baseline(tmp_path, doc)
    result = lint_structural(d)
    assert not result.passed
    assert "R1" in _codes(result)


def test_r2_fails_on_duplicate_capability_id(tmp_path):
    # Vendor-shaped ids are fine now — inventories are transcript-derived, so
    # there is no vocabulary to police. What R2 still owns is internal
    # consistency: the same id declared twice is a compile error.
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["capabilities"]["required"].append(
        dict(doc["spec"]["capabilities"]["required"][0])
    )
    d = _write_baseline(tmp_path, doc)
    result = lint_structural(d)
    assert not result.passed
    assert "R2" in _codes(result)


def test_r3_fails_on_llm_node_without_rationale(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["nodes"]["classify"] = {
        "kind": "llm",
        "dependsOn": ["summarize-metric"],
        "config": {
            "modelClass": "analytical-small",
            "task": "classify",
        },
    }
    doc["spec"]["nodes"]["emit-finding"]["dependsOn"].append("classify")
    d = _write_baseline(tmp_path, doc)
    # No rationale.md — R3 must FAIL.
    result = lint_structural(d)
    assert not result.passed
    assert "R3" in _codes(result), _codes(result)


def test_r4_fails_on_hallucinated_node_citation(tmp_path):
    d = _write_baseline(tmp_path)
    (d / "rationale.md").write_text(
        "## Nodes\n\n"
        "The node `phantom-node` produces evidence used by `emit-finding`.\n"
    )
    result = lint_structural(d)
    assert not result.passed
    assert "R4" in _codes(result), _codes(result)


def test_r5_fails_on_execution_now_in_dedupe_key(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["nodes"]["emit-finding"]["config"]["finding"]["dedupeKey"] = (
        "${inputs.service}:${execution.now}"
    )
    d = _write_baseline(tmp_path, doc)
    result = lint_structural(d)
    assert not result.passed
    assert "R5" in _codes(result)


def test_r6_fails_on_orphan_capability(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["capabilities"]["required"].append(
        {"id": "observability.query-logs", "capabilityType": "tool"},
    )
    d = _write_baseline(tmp_path, doc)
    result = lint_structural(d)
    assert not result.passed
    assert "R6" in _codes(result)


def test_r6_fails_on_dangling_toolref(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["nodes"]["query-metric"]["config"]["toolRef"] = "observability.list-services"
    d = _write_baseline(tmp_path, doc)
    result = lint_structural(d)
    assert not result.passed
    assert "R6" in _codes(result)


# --------------------------------------------------------------------------- #
# CLI + output formatting                                                      #
# --------------------------------------------------------------------------- #

def test_json_output_is_parseable(tmp_path):
    d = _write_baseline(tmp_path)
    # Introduce one failure so findings is non-empty and passed is False.
    (d / "functions" / "summarize-metric.py").unlink()
    result = lint_structural(d)
    js = format_json(result)
    parsed = json.loads(js)
    assert parsed["passed"] is False
    assert isinstance(parsed["findings"], list)
    assert parsed["findings"], "expected at least one finding"
    for f in parsed["findings"]:
        for key in ("code", "severity", "file", "line", "message", "fix"):
            assert key in f, f


def test_executor_lint_subcommand_exit_code_pass(tmp_path):
    d = _write_baseline(tmp_path)
    env_python = SPIKE_DIR / ".venv" / "bin" / "python"
    python = str(env_python) if env_python.exists() else sys.executable
    proc = subprocess.run(
        [python, str(SPIKE_DIR / "executor.py"), "lint", str(d), "--format=json"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed["passed"] is True


def test_executor_lint_subcommand_exit_code_fail(tmp_path):
    d = _write_baseline(tmp_path)
    (d / "functions" / "summarize-metric.py").unlink()
    env_python = SPIKE_DIR / ".venv" / "bin" / "python"
    python = str(env_python) if env_python.exists() else sys.executable
    proc = subprocess.run(
        [python, str(SPIKE_DIR / "executor.py"), "lint", str(d)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "FUNC-MISSING" in proc.stdout
