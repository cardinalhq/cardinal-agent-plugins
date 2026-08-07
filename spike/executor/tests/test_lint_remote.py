"""Phase-2 sentinel-lint coverage (remote-readiness R7-R20).

Run: `spike/executor/.venv/bin/pytest spike/executor/tests/test_lint_remote.py -v`.

Baseline pattern (mirrors test_lint.py): each test builds a tiny well-formed
remote Sentinel + deployment.yaml, mutates exactly one thing, asserts the
correct R-code fires. Registry + schema are re-used from repo-root
`common/*.yaml`.
"""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

SPIKE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SPIKE_DIR.parent.parent
sys.path.insert(0, str(SPIKE_DIR))

from lint import lint_all  # noqa: E402
from lint_remote import lint_remote  # noqa: E402


REGISTRY_PATH = REPO_ROOT / "common" / "integrations.yaml"
SCHEMA_PATH = REPO_ROOT / "common" / "deployment-schema.yaml"


# --------------------------------------------------------------------------- #
# Baseline fixtures — a passing remote sentinel + deployment                   #
# --------------------------------------------------------------------------- #

BASELINE_SENTINEL: dict[str, Any] = {
    "apiVersion": "mechanize.dev/v1alpha1",
    "kind": "Sentinel",
    "metadata": {
        "name": "unit-test-remote-sentinel",
        "version": "0.1.0",
        "deployment": {"mode": "remote"},
    },
    "spec": {
        "inputs": {
            "service": {"type": "string", "required": True},
        },
        "capabilities": {
            "required": [
                {"id": "observability.query-metrics", "capabilityType": "tool"},
            ],
        },
        "variationPoints": [
            {"path": "/spec/inputs/service", "operations": ["bind"]},
        ],
        "nodes": {
            "query-metric": {
                "kind": "tool",
                "dependsOn": [],
                "config": {
                    "toolRef": "observability.query-metrics",
                    "arguments": {"service": "${inputs.service}"},
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
            "classify": {
                "kind": "llm",
                "dependsOn": ["summarize-metric"],
                "config": {
                    "modelClass": "analytical-small",
                    "task": "classify",
                },
            },
            "confirm-env": {
                "kind": "ask_human",
                "dependsOn": ["classify"],
                "config": {
                    "question": "Which env?",
                    "answerSchema": {
                        "type": "object",
                        "required": ["environment"],
                        "properties": {"environment": {"type": "string"}},
                    },
                    "timeout": {"mode": "fall-through-default", "maxWait": "5m"},
                    "evidence": {"summary": "${nodes.summarize-metric.output}"},
                },
            },
            "emit-finding": {
                "kind": "emit",
                "dependsOn": ["confirm-env"],
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

# Rationale is required by R3 to justify the `llm` node.
BASELINE_RATIONALE = (
    "## Nodes\n\n"
    "The node `classify` is `kind: llm` because the classification is a "
    "prose-shaped judgement over evidence, not a deterministic computation.\n"
    "It is not `kind: function` per §32 because the output depends on "
    "narrative summarization the DAG cannot express symbolically.\n"
    "Function node `summarize-metric` reduces the metric series to a "
    "structured summary before the llm sees it.\n"
    "Ask-human `confirm-env` gates the emit `emit-finding`.\n"
)

BASELINE_DEPLOYMENT: dict[str, Any] = {
    "schemaVersion": "mechanize.dev/v1alpha1",
    "kind": "SentinelDeployment",
    "runtime": "k8s-controller",
    "askHumanBindings": {
        "confirm-env": {
            "channel_ref": "slack.socket-mode",
            "channel_params": {
                "token_ref": "env://SLACK_BOT_TOKEN",
                "channel_id": "C0123ABC",
            },
            "identity_policy": {"allowedGroups": ["oncall-observability"]},
            "reply_normalization": "structured",
        },
    },
    "capabilityBindings": {
        "observability.query-metrics": {
            # `mcp` is the one implemented tool provider; the old baseline
            # bound `lakerunner`, a provider that never existed but that the
            # (now-deleted) capability registry happily allowed.
            "provider": "mcp",
            "credential_ref": "k8s-secret://lakerunner-token",
            "side_effect_class": "read-only",
        },
    },
    "inputBindings": {
        "service": {"source": "webhook.service"},
    },
    "llmBindings": {
        "classify": {"model": "claude-haiku-4-5", "tokenBudget": 4096},
    },
    "findingsRouting": [
        {"match": {"emitNode": "emit-finding"}, "sink": "outcomes-dashboard"},
    ],
    "functions": {
        "summarize-metric": {"network": "disabled", "filesystem": "none"},
    },
    "execution": {
        "timeout": "15m",
        "sinkRetry": {"attempts": 3, "onExhausted": "spool-to-state"},
    },
}

FUNCTION_BODY_OK = """def run(args):
    return {\"summary\": \"ok\"}
"""


def _write_baseline(
    tmp_path: Path,
    sentinel: dict | None = None,
    deployment: dict | None = None,
    include_deployment: bool = True,
    function_body: str = FUNCTION_BODY_OK,
    rationale: str | None = BASELINE_RATIONALE,
) -> Path:
    d = tmp_path / "sentinel-dir"
    d.mkdir()
    s_doc = copy.deepcopy(sentinel or BASELINE_SENTINEL)
    (d / "sentinel.yaml").write_text(yaml.safe_dump(s_doc, sort_keys=False))
    if include_deployment:
        dep_doc = copy.deepcopy(deployment or BASELINE_DEPLOYMENT)
        (d / "deployment.yaml").write_text(yaml.safe_dump(dep_doc, sort_keys=False))
    (d / "functions").mkdir()
    (d / "functions" / "summarize-metric.py").write_text(function_body)
    if rationale is not None:
        (d / "rationale.md").write_text(rationale)
    return d


def _run(sentinel_dir: Path, check_mode: str = "all"):
    return lint_all(
        sentinel_dir,
        check_mode=check_mode,
        registry_path=REGISTRY_PATH,
        schema_path=SCHEMA_PATH,
    )


def _codes(result) -> list[str]:
    return [f.code for f in result.findings]


# --------------------------------------------------------------------------- #
# Baseline sanity                                                              #
# --------------------------------------------------------------------------- #

def test_baseline_remote_passes(tmp_path):
    d = _write_baseline(tmp_path)
    result = _run(d)
    assert result.passed, [(f.code, f.message) for f in result.findings]


def test_local_mode_skips_phase2(tmp_path):
    """metadata.deployment.mode=local (or absent) → Phase 2 no-op."""
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["metadata"]["deployment"] = {"mode": "local"}
    d = _write_baseline(tmp_path, doc, include_deployment=False)
    result = _run(d)
    # No DEPLOY-MISSING even though deployment.yaml is absent.
    assert result.passed, [(f.code, f.message) for f in result.findings]
    assert "DEPLOY-MISSING" not in _codes(result)


def test_absent_deployment_mode_defaults_local(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["metadata"].pop("deployment", None)
    d = _write_baseline(tmp_path, doc, include_deployment=False)
    result = _run(d)
    assert result.passed


def test_deploy_missing_remote(tmp_path):
    d = _write_baseline(tmp_path, include_deployment=False)
    result = _run(d)
    assert not result.passed
    assert "DEPLOY-MISSING" in _codes(result)


def test_cli_structural_only_skips_r_codes(tmp_path):
    """--check=structural must skip R7-R20 even when mode=remote."""
    # Deliberately break R10 (unknown provider) so 'all' would fail.
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["capabilityBindings"]["observability.query-metrics"]["provider"] = "typo"
    d = _write_baseline(tmp_path, deployment=dep)
    all_result = _run(d, check_mode="all")
    assert "R10" in _codes(all_result)
    structural = _run(d, check_mode="structural")
    assert "R10" not in _codes(structural)
    assert structural.passed


# --------------------------------------------------------------------------- #
# R7 — bash.* not remote-deployable                                            #
# --------------------------------------------------------------------------- #

def test_r7_fails_on_bash_capability(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["capabilities"]["required"].append(
        {"id": "bash.kubectl", "capabilityType": "tool"}
    )
    # Wire it up so R6 doesn't also flag it as orphan.
    doc["spec"]["nodes"]["query-metric-2"] = {
        "kind": "tool",
        "dependsOn": [],
        "config": {"toolRef": "bash.kubectl"},
    }
    d = _write_baseline(tmp_path, doc)
    result = _run(d)
    assert not result.passed
    assert "R7" in _codes(result)


# --------------------------------------------------------------------------- #
# R8 — direct-import safety                                                    #
# --------------------------------------------------------------------------- #

def test_r8_fails_on_subprocess_import(tmp_path):
    body = "import subprocess\n\ndef run(args):\n    return {}\n"
    d = _write_baseline(tmp_path, function_body=body)
    result = _run(d)
    assert not result.passed
    assert "R8" in _codes(result)


def test_r8_allow_annotation_suppresses(tmp_path):
    body = (
        "import subprocess  # lint-allow: subprocess # only used in run(), guarded\n"
        "def run(args):\n"
        "    return {}\n"
    )
    d = _write_baseline(tmp_path, function_body=body)
    result = _run(d)
    assert result.passed, [(f.code, f.message) for f in result.findings]


# --------------------------------------------------------------------------- #
# R9 — ask_human coverage                                                      #
# --------------------------------------------------------------------------- #

def test_r9_fails_when_ask_human_binding_missing(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["askHumanBindings"].pop("confirm-env")
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R9" in _codes(result)


# --------------------------------------------------------------------------- #
# R10 — capability ↔ registry                                                  #
# --------------------------------------------------------------------------- #

def test_r10_fails_on_unregistered_provider(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["capabilityBindings"]["observability.query-metrics"]["provider"] = "typo"
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R10" in _codes(result)


def test_r10_fixture_provider_requires_allow_fixtures(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["capabilityBindings"]["observability.query-metrics"]["provider"] = "fixture"
    d = _write_baseline(tmp_path, deployment=dep)
    # Without allowFixtures → FAIL.
    result = _run(d)
    assert not result.passed
    r10s = [f for f in result.findings if f.code == "R10"]
    assert r10s, _codes(result)
    assert any("allowFixtures" in f.message for f in r10s)


def test_r10_fixture_provider_allowed_when_flag_true(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["capabilityBindings"]["observability.query-metrics"]["provider"] = "fixture"
    dep["execution"]["allowFixtures"] = True
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert result.passed, [(f.code, f.message) for f in result.findings]


# --------------------------------------------------------------------------- #
# R11 — input bindings + attachment caps                                       #
# --------------------------------------------------------------------------- #

def test_r11_fails_when_input_has_no_binding(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["inputBindings"].pop("service")
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R11" in _codes(result)


def test_r11_fails_when_attachment_has_no_size_cap(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["inputs"]["chart"] = {"type": "image", "required": True}
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["inputBindings"]["chart"] = {"source": "webhook.chart"}  # no maxSizeBytes/mimeTypes
    d = _write_baseline(tmp_path, doc, deployment=dep)
    result = _run(d)
    assert not result.passed
    r11s = [f for f in result.findings if f.code == "R11"]
    assert any("maxSizeBytes" in f.message for f in r11s), _codes(result)


# --------------------------------------------------------------------------- #
# R12 — findingsRouting coverage + shadowing                                   #
# --------------------------------------------------------------------------- #

def test_r12_fails_when_emit_uncovered(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["findingsRouting"] = [
        {"match": {"emitNode": "no-such-emit"}, "sink": "outcomes-dashboard"},
    ]
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R12" in _codes(result)


def test_r12_catch_all_covers_everything(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["findingsRouting"] = [
        {"match": {"*": True}, "sink": "outcomes-dashboard"},
    ]
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert result.passed, [(f.code, f.message) for f in result.findings]


def test_r12_warns_on_shadowed_rule_after_catch_all(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["findingsRouting"] = [
        {"match": {"*": True}, "sink": "outcomes-dashboard"},
        {"match": {"emitNode": "emit-finding"}, "sink": "slack"},
    ]
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    warns = [f for f in result.findings if f.code == "R12" and f.severity == "WARN"]
    assert warns, [(f.code, f.severity, f.message) for f in result.findings]
    # WARN alone must not fail the overall lint.
    assert result.passed


# --------------------------------------------------------------------------- #
# R13 — llm model registry                                                     #
# --------------------------------------------------------------------------- #

def test_r13_fails_on_unregistered_model(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["llmBindings"]["classify"]["model"] = "claude-typo-5"
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R13" in _codes(result)


def test_r13_fails_when_llm_binding_missing(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["llmBindings"].pop("classify")
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R13" in _codes(result)


# --------------------------------------------------------------------------- #
# R14 — literal-secret detection                                               #
# --------------------------------------------------------------------------- #

def test_r14_fails_on_literal_slack_token(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["askHumanBindings"]["confirm-env"]["channel_params"]["token"] = (
        "xoxb-1234567890-ABCDEFGHIJ"
    )
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R14" in _codes(result)


def test_r14_fails_on_aws_access_key(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["capabilityBindings"]["observability.query-metrics"]["params"] = {
        "aws_key": "AKIAIOSFODNN7EXAMPLE",
    }
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R14" in _codes(result)


# --------------------------------------------------------------------------- #
# R15 — runtime timeout compatibility                                          #
# --------------------------------------------------------------------------- #

def test_r15_fails_ci_plugin_with_block_until_answered(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["nodes"]["confirm-env"]["config"]["timeout"] = {
        "mode": "block-until-answered", "maxWait": "24h"
    }
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["runtime"] = "ci-plugin"
    d = _write_baseline(tmp_path, doc, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R15" in _codes(result)


def test_r15_passes_k8s_controller_with_block_until_answered(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["spec"]["nodes"]["confirm-env"]["config"]["timeout"] = {
        "mode": "block-until-answered", "maxWait": "24h"
    }
    d = _write_baseline(tmp_path, doc)
    result = _run(d)
    assert result.passed, [(f.code, f.message) for f in result.findings]


# --------------------------------------------------------------------------- #
# R17 — ratification.status: revise blocks remote                              #
# --------------------------------------------------------------------------- #

def test_r17_fails_remote_when_ratification_revise(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["metadata"]["ratification"] = {
        "status": "revise",
        "unresolved": [{"R2": "vendor-shape leak in capability id"}],
    }
    d = _write_baseline(tmp_path, doc)
    result = _run(d)
    assert not result.passed
    r17 = [f for f in result.findings if f.code == "R17"]
    assert r17
    assert r17[0].severity == "FAIL"


def test_r17_warns_local_when_ratification_revise(tmp_path):
    doc = copy.deepcopy(BASELINE_SENTINEL)
    doc["metadata"]["deployment"] = {"mode": "local"}
    doc["metadata"]["ratification"] = {
        "status": "revise",
        "unresolved": [{"R2": "vendor-shape leak"}],
    }
    d = _write_baseline(tmp_path, doc, include_deployment=False)
    result = _run(d)
    r17 = [f for f in result.findings if f.code == "R17"]
    assert r17
    assert r17[0].severity == "WARN"
    assert result.passed  # WARN alone must not fail overall


# --------------------------------------------------------------------------- #
# R18 — function-node runtime capabilities                                     #
# --------------------------------------------------------------------------- #

def test_r18_fails_when_function_block_missing(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["functions"].pop("summarize-metric")
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R18" in _codes(result)


def test_r18_fails_when_network_enabled_but_no_source_annotation(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["functions"]["summarize-metric"] = {
        "network": "enabled", "filesystem": "none"
    }
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    r18 = [f for f in result.findings if f.code == "R18"]
    assert any("runtime-cap: network=enabled" in f.message for f in r18), _codes(result)


def test_r18_passes_when_annotation_matches_deployment(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["functions"]["summarize-metric"] = {
        "network": "enabled", "filesystem": "none"
    }
    body = (
        "# runtime-cap: network=enabled # needed to poll upstream stats API\n"
        "def run(args):\n"
        "    return {}\n"
    )
    d = _write_baseline(tmp_path, deployment=dep, function_body=body)
    result = _run(d)
    assert result.passed, [(f.code, f.message) for f in result.findings]


# --------------------------------------------------------------------------- #
# R19 — parserModel resolves in registry                                       #
# --------------------------------------------------------------------------- #

def test_r19_fails_on_unregistered_parser_model(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["askHumanBindings"]["confirm-env"]["parserModel"] = "unknown-model"
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R19" in _codes(result)


def test_r19_fails_on_unregistered_default_parser_model(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["defaultParserModel"] = "unknown-model"
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R19" in _codes(result)


# --------------------------------------------------------------------------- #
# R20 — prose-llm-parse requires a parserModel                                 #
# --------------------------------------------------------------------------- #

def test_r20_fails_prose_without_parser_model(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["askHumanBindings"]["confirm-env"]["reply_normalization"] = "prose-llm-parse"
    # No parserModel and no defaultParserModel.
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert not result.passed
    assert "R20" in _codes(result)


def test_r20_passes_with_default_parser_model(tmp_path):
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["askHumanBindings"]["confirm-env"]["reply_normalization"] = "prose-llm-parse"
    dep["defaultParserModel"] = "claude-haiku-4-5"
    d = _write_baseline(tmp_path, deployment=dep)
    result = _run(d)
    assert result.passed, [(f.code, f.message) for f in result.findings]


# --------------------------------------------------------------------------- #
# CLI end-to-end                                                               #
# --------------------------------------------------------------------------- #

def test_executor_lint_remote_pass_exit_code(tmp_path):
    d = _write_baseline(tmp_path)
    env_python = SPIKE_DIR / ".venv" / "bin" / "python"
    python = str(env_python) if env_python.exists() else sys.executable
    proc = subprocess.run(
        [
            python, str(SPIKE_DIR / "executor.py"), "lint", str(d),
            "--format=json",
            "--registry", str(REGISTRY_PATH),
            "--schema", str(SCHEMA_PATH),
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    parsed = json.loads(proc.stdout)
    assert parsed["passed"] is True


def test_executor_lint_check_structural_flag(tmp_path):
    # Break R10 so 'all' fails but 'structural' passes.
    dep = copy.deepcopy(BASELINE_DEPLOYMENT)
    dep["capabilityBindings"]["observability.query-metrics"]["provider"] = "typo"
    d = _write_baseline(tmp_path, deployment=dep)
    env_python = SPIKE_DIR / ".venv" / "bin" / "python"
    python = str(env_python) if env_python.exists() else sys.executable
    proc = subprocess.run(
        [
            python, str(SPIKE_DIR / "executor.py"), "lint", str(d),
            "--check=structural",
            "--registry", str(REGISTRY_PATH),
            "--schema", str(SCHEMA_PATH),
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
