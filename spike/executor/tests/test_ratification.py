"""First test coverage for the shared R1-R6 ratification module.

`common/mechanize/ratification.py` is the single source of truth for the six
semantic rules, imported by sentinel-lint, baked into the executor image, and
cited as normative by all four adapter SKILL copies — and it had no tests. The
cases below pin both directions of each rule, and every FALSE-VERDICT case is
one observed in the wild rather than invented.
"""
from __future__ import annotations

import pytest

from common.mechanize import ratification as rat


def _sentinel(**spec) -> dict:
    return {"apiVersion": "mechanize.dev/v1alpha1", "kind": "Sentinel", "spec": spec}




# --------------------------------------------------------------------------- #
# R1 — variation-point completeness                                            #
# --------------------------------------------------------------------------- #


def test_r1_flags_defaulted_input_referenced_without_a_variation_point():
    s = _sentinel(
        inputs={"window": {"type": "string", "default": "1h"}},
        nodes={"q": {"config": {"arguments": {"w": "${inputs.window}"}}}},
    )
    assert rat.check_r1(s).verdict == "FAIL"


def test_r1_passes_when_the_variation_point_is_declared():
    s = _sentinel(
        inputs={"window": {"type": "string", "default": "1h"}},
        nodes={"q": {"config": {"arguments": {"w": "${inputs.window}"}}}},
        variationPoints=[{"path": "/spec/inputs/window/default", "operations": ["replace"]}],
    )
    assert rat.check_r1(s).verdict == "PASS"


def test_r1_ignores_an_input_named_only_in_prose():
    """Regression: bare `inputs.x` in an llm task read as a template reference.

    Matching outside `${...}` meant an ask_human question that merely mentions
    an input by name demanded a variation point for it.
    """
    s = _sentinel(
        inputs={"window": {"type": "string", "default": "1h"}},
        nodes={"ask": {"kind": "ask_human",
                       "config": {"question": "Is inputs.window the right range?"}}},
    )
    assert rat.check_r1(s).verdict == "PASS"


def test_r1_ignores_inputs_without_a_default():
    s = _sentinel(
        inputs={"service": {"type": "string", "required": True}},
        nodes={"q": {"config": {"arguments": {"s": "${inputs.service}"}}}},
    )
    assert rat.check_r1(s).verdict == "PASS"


# --------------------------------------------------------------------------- #
# R2 — capability well-formedness (no registry exists; transcript-derived)     #
# --------------------------------------------------------------------------- #


def _with_cap(cid) -> dict:
    return _sentinel(capabilities={"required": [{"id": cid, "capabilityType": "tool"}]})


def test_r2_passes_any_id_the_compiler_recorded_from_the_session():
    """Transcript-derived ids are not checked against any vocabulary.

    The old registry/prefix checks were wrong in both directions at once:
    they admitted `observability.fetch-status-summary` (prefix-shaped, but
    implemented by nothing) and rejected honest observed identities. The
    inventory now IS the session's tool usage; there is nothing to match
    it against at compile time.
    """
    for cid in ("lakerunner__execute_logs_query", "observability.query-logs", "anything-at-all"):
        assert rat.check_r2(_with_cap(cid)).verdict == "PASS"


def test_r2_fails_a_duplicate_capability_id():
    s = _sentinel(capabilities={"required": [{"id": "code.grep"}, {"id": "code.grep"}]})
    r = rat.check_r2(s)
    assert r.verdict == "FAIL" and "duplicate" in r.detail


def test_r2_fails_an_entry_with_no_id():
    s = _sentinel(capabilities={"required": [{"capabilityType": "tool"}]})
    assert rat.check_r2(s).verdict == "FAIL"


def test_r2_fails_a_non_mapping_entry():
    s = _sentinel(capabilities={"required": ["code.grep"]})
    assert rat.check_r2(s).verdict == "FAIL"


def test_r2_passes_an_empty_inventory():
    """A Sentinel whose evidence steps all compiled to functions has no capabilities."""
    assert rat.check_r2(_sentinel(capabilities={"required": []})).verdict == "PASS"


# --------------------------------------------------------------------------- #
# R3 — function-vs-llm justification                                           #
# --------------------------------------------------------------------------- #


_LLM = _sentinel(nodes={"judge-severity": {"kind": "llm"}})


def test_r3_fails_an_llm_node_with_no_rationale():
    assert rat.check_r3(_LLM, "").verdict == "FAIL"


def test_r3_fails_an_llm_node_mentioned_without_justification():
    assert rat.check_r3(_LLM, "The judge-severity node looks at the data.").verdict == "FAIL"


def test_r3_accepts_justification_at_a_later_mention():
    """Regression: only the FIRST mention was inspected.

    Node ids routinely appear first in a classification table and are argued
    in a later paragraph, so first-occurrence-only failed properly-argued nodes.
    """
    rationale = (
        "| judge-severity | llm | retained |\n"
        + ("filler. " * 200)
        + "We chose judge-severity as llm because no deterministic function "
          "could rank these qualitatively; see §32."
    )
    assert rat.check_r3(_LLM, rationale).verdict == "PASS"


def test_r3_passes_when_no_llm_nodes_exist():
    assert rat.check_r3(_sentinel(nodes={"f": {"kind": "function"}}), "").verdict == "PASS"


# --------------------------------------------------------------------------- #
# R4 — node existence                                                          #
# --------------------------------------------------------------------------- #


def test_r4_flags_a_hallucinated_node_id():
    s = _sentinel(nodes={"fetch-status": {"kind": "tool"}})
    r = rat.check_r4(s, "The `compute-health-score` node derives the result.")
    assert r.verdict == "FAIL"
    assert "compute-health-score" in r.detail


def test_r4_does_not_flag_a_finding_type_as_a_node():
    """Regression: a kebab-case finding type near the word 'node' was flagged.

    `service-health-status` is declared as the emit node's finding type, so
    citing it in the rationale is correct, not a hallucination.
    """
    s = _sentinel(
        nodes={"emit-health": {"kind": "emit",
                               "config": {"finding": {"type": "service-health-status"}}}}
    )
    rationale = "All three nodes ran; the emit produced `service-health-status`."
    assert rat.check_r4(s, rationale).verdict == "PASS"


def test_r4_does_not_flag_an_input_name_or_capability_id():
    s = _sentinel(
        inputs={"status-endpoint": {"type": "string"}},
        capabilities={"required": [{"id": "web.fetch-with-summary"}]},
        nodes={"fetch": {"kind": "tool", "config": {"toolRef": "web.fetch-with-summary"}}},
    )
    rationale = "The node reads `status-endpoint` via `fetch-with-summary`."
    assert rat.check_r4(s, rationale).verdict == "PASS"


def test_r4_passes_when_every_citation_resolves():
    s = _sentinel(nodes={"fetch-status": {"kind": "tool"}})
    assert rat.check_r4(s, "The `fetch-status` node runs first.").verdict == "PASS"


# --------------------------------------------------------------------------- #
# R5 — dedupeKey stability                                                     #
# --------------------------------------------------------------------------- #


def _emit(dedupe: str) -> dict:
    return _sentinel(nodes={"emit-x": {"kind": "emit", "config": {"finding": {"dedupeKey": dedupe}}}})


def test_r5_rejects_a_time_varying_dedupe_key():
    assert rat.check_r5(_emit("${inputs.service}:${execution.now}")).verdict == "FAIL"


def test_r5_accepts_inputs_and_node_output_references():
    assert rat.check_r5(_emit("${inputs.service}:${nodes.classify.output.indicator}")).verdict == "PASS"


def test_r5_reports_a_missing_dedupe_key():
    s = _sentinel(nodes={"emit-x": {"kind": "emit", "config": {"finding": {}}}})
    assert rat.check_r5(s).verdict == "FAIL"


# --------------------------------------------------------------------------- #
# R6 — toolRef <-> capability integrity                                        #
# --------------------------------------------------------------------------- #


def test_r6_flags_a_dangling_tool_ref():
    s = _sentinel(capabilities={"required": []},
                  nodes={"q": {"kind": "tool", "config": {"toolRef": "code.grep"}}})
    r = rat.check_r6(s)
    assert r.verdict == "FAIL" and "dangling" in r.detail


def test_r6_flags_an_orphan_capability():
    s = _sentinel(capabilities={"required": [{"id": "code.grep"}]}, nodes={})
    r = rat.check_r6(s)
    assert r.verdict == "FAIL" and "orphan" in r.detail


def test_r6_passes_when_aligned():
    s = _sentinel(capabilities={"required": [{"id": "code.grep"}]},
                  nodes={"q": {"kind": "tool", "config": {"toolRef": "code.grep"}}})
    assert rat.check_r6(s).verdict == "PASS"


# --------------------------------------------------------------------------- #
# Aggregate + registry discovery                                               #
# --------------------------------------------------------------------------- #


def test_run_all_returns_six_results_in_order():
    results = rat.run_all(_sentinel(nodes={}), "")
    assert [r.rule for r in results] == ["R1", "R2", "R3", "R4", "R5", "R6"]


def test_verdict_block_is_revise_when_any_rule_fails():
    results = rat.run_all(
        _sentinel(capabilities={"required": []},
                  nodes={"q": {"kind": "tool", "config": {"toolRef": "code.grep"}}}),
        "",
    )
    block = rat.format_verdict_block(results)
    assert block.startswith("VERDICT: REVISE")
    assert "fix list" in block


def test_every_checked_in_sentinel_satisfies_r2():
    """End-to-end over the real artifacts under mechanize-out/."""
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[3]
    sentinels = sorted((root / "mechanize-out").glob("*/sentinel.yaml"))
    if not sentinels:
        pytest.skip("no checked-in Sentinels")
    for path in sentinels:
        doc = yaml.safe_load(path.read_text())
        r = rat.check_r2(doc)
        assert r.verdict == "PASS", f"{path.parent.name}: {r.detail}"
