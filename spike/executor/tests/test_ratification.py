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


REGISTRY = {"capabilities": {"observability.query-logs": {"providers": ["mcp"]},
                             "web.fetch-with-summary": {"providers": ["http-get"]}}}


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
# R2 — capability registration                                                 #
# --------------------------------------------------------------------------- #


def _with_cap(cid: str) -> dict:
    return _sentinel(capabilities={"required": [{"id": cid, "capabilityType": "tool"}]})


def test_r2_fails_an_unregistered_id_even_with_an_abstract_prefix():
    """The exact false PASS that shipped: prefix-shaped but in no registry.

    `observability.fetch-status-summary` passed prefix-only R2, then died at
    runtime with UnknownProviderError because nothing implemented it.
    """
    r = rat.check_r2(_with_cap("observability.fetch-status-summary"), REGISTRY)
    assert r.verdict == "FAIL"
    assert "registry" in r.detail


def test_r2_passes_a_registered_id_outside_the_fallback_prefixes():
    assert rat.check_r2(_with_cap("web.fetch-with-summary"), REGISTRY).verdict == "PASS"


def test_r2_fails_a_vendor_shaped_id_against_the_registry():
    assert rat.check_r2(_with_cap("lakerunner.query"), REGISTRY).verdict == "FAIL"


def test_r2_without_a_registry_degrades_to_prefix_and_says_so():
    """A silent downgrade would read as a stronger guarantee than it is."""
    r = rat.check_r2(_with_cap("observability.anything-at-all"), None)
    assert r.verdict == "PASS"
    assert "membership unverified" in r.detail


def test_r2_without_a_registry_still_rejects_vendor_shapes():
    assert rat.check_r2(_with_cap("datadog.query"), None).verdict == "FAIL"


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
    results = rat.run_all(_sentinel(nodes={}), "", REGISTRY)
    assert [r.rule for r in results] == ["R1", "R2", "R3", "R4", "R5", "R6"]


def test_verdict_block_is_revise_when_any_rule_fails():
    results = rat.run_all(
        _sentinel(capabilities={"required": []},
                  nodes={"q": {"kind": "tool", "config": {"toolRef": "code.grep"}}}),
        "", REGISTRY,
    )
    block = rat.format_verdict_block(results)
    assert block.startswith("VERDICT: REVISE")
    assert "fix list" in block


def test_find_registry_path_locates_the_real_registry():
    """The compiler's Stage 5.5 and lint must resolve the same registry file."""
    from pathlib import Path

    found = rat.find_registry_path(Path(__file__).parent)
    assert found is not None and found.name == "capabilities-registry.yaml"


def test_the_shipped_registry_satisfies_r2_for_every_checked_in_sentinel():
    """End-to-end: real registry, real Sentinels under mechanize-out/."""
    from pathlib import Path

    import yaml

    registry_path = rat.find_registry_path(Path(__file__).parent)
    registry = yaml.safe_load(registry_path.read_text())
    root = registry_path.parent.parent
    sentinels = sorted((root / "mechanize-out").glob("*/sentinel.yaml"))
    if not sentinels:
        pytest.skip("no checked-in Sentinels")
    for path in sentinels:
        doc = yaml.safe_load(path.read_text())
        r = rat.check_r2(doc, registry)
        assert r.verdict == "PASS", f"{path.parent.name}: {r.detail}"
