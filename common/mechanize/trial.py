"""Stage 10 (trial execution) + Stage 11 (conclusion comparison) for the mechanize compiler.

The compiler used to stop at Stage 9 — graph validation — and hand the operator
a candidate Sentinel that nobody had ever run. Every contract disagreement
between the compiler and the runtime therefore surfaced in production, as a
pod log. `sentinels.md` §29 always specified otherwise: stage 10 is trial
execution and stage 11 is conclusion comparison, and §49 says in as many words
that "a Sentinel is not considered complete after schema validation alone."
This module is those two stages.

**What it does.** Runs the freshly-compiled Sentinel against the fixtures the
compiler captured from the source session, twice, through the same code path
production runs (`executor.py serve`), then checks that the run reached the
conclusion the original investigation reached. Nine checks, T1–T9, in three
groups:

    preflight   T1 trial-inputs        inputs.json satisfies every required input
                T2 fixture-coverage    every tool node has a captured fixture
                T3 executable-bodies   no stub functions, no unrunnable node kinds
    execution   T4 execution-completes no node FAILED or CANCELLED
                T5 determinism         two runs produce identical findings
    findings    T6 evidence-populated  every finding carries resolved evidence
                T7 attributes-resolved no attribute rendered to the literal "null"
    conclusion  T8 conclusion-reached  the expected emit nodes fired, and only those
                T9 conclusion-matches  type and severity match the original

T6 is the check that would have caught the bug this module was written for: an
emit node whose declared evidence silently resolved to `[]`, producing a
`critical` finding backed by nothing. It passed graph validation, passed
ratification, deployed clean, and was wrong.

**Hermetic by construction.** The trial synthesizes its own deployment binding
every capability — both the declared ones and any a node's `toolRef` reaches
for — to the `fixture` provider. A trial must never touch the network: a
compile-time check that quietly queried production would be worse than no
check. Any capability without a fixture fails T2 rather than falling through
to a live binding.

Usage:

    python3 common/mechanize/trial.py <SENTINEL_DIR>
    python3 common/mechanize/trial.py <SENTINEL_DIR> --json
    python3 common/mechanize/trial.py <SENTINEL_DIR> --allow-missing-expectation

Prints the Stage 10/11 verdict block on stdout and writes
`<SENTINEL_DIR>/trial/report.json`. Exit 0 on `TRIAL: PASSED`, 1 on
`TRIAL: FAILED`, 2 on a harness error (missing sentinel.yaml, unreadable
fixture, and so on — a broken harness must not read as a passing trial).

Inputs the compiler must write next to `sentinel.yaml` for a trial to run:

    inputs.json           the input bindings the source investigation used
    fixtures/<node>.json  the captured tool_result each tool node derived from
    expectation.json      Stage 11's ground truth (see EXPECTATION_SHAPE below)
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import io
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

# The executor lives at spike/executor/ — same repo-root convention
# spike/executor/lint.py uses in the other direction to reach this package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXECUTOR_DIR = _REPO_ROOT / "spike" / "executor"


EXPECTATION_SHAPE = """\
{
  "sourceSession": "<short session id the Sentinel was compiled from>",
  "conclusion": "<one line: what the original investigation concluded>",
  "expectFindings": [
    {"emitNode": "<emit node id>",
     "type": "<finding type>",            // optional; checked when present
     "severity": "info|warning|critical", // optional; checked when present
     "evidenceNodes": ["<node id>", ...]  // optional; checked when present
    }
  ],
  "expectNoFindings": ["<emit node id that must NOT fire>", ...]
}"""


Verdict = Literal["PASS", "FAIL", "SKIP"]


class TrialHarnessError(RuntimeError):
    """The trial could not be run at all (as distinct from being run and failing)."""


@dataclass
class TrialCheck:
    id: str
    name: str
    verdict: Verdict
    detail: str


@dataclass
class RunOutcome:
    run_id: str
    exit_code: int
    node_states: dict[str, str]
    findings: list[dict[str, Any]]          # finding bodies, in emission order
    finding_nodes: dict[str, str]           # dedupeHash -> emit node id
    failures: dict[str, str]                # node id -> error detail
    stdout: str


# --------------------------------------------------------------------------- #
# Loading                                                                      #
# --------------------------------------------------------------------------- #


def _import_executor_modules():
    """Import the executor package, adding its directory to sys.path once."""
    if str(_EXECUTOR_DIR) not in sys.path:
        sys.path.insert(0, str(_EXECUTOR_DIR))
    import runtime_serve  # noqa: PLC0415
    import state as state_mod  # noqa: PLC0415

    return runtime_serve, state_mod


def load_sentinel_dir(sentinel_dir: Path) -> dict:
    import yaml  # noqa: PLC0415

    path = sentinel_dir / "sentinel.yaml"
    if not path.exists():
        raise TrialHarnessError(f"no sentinel.yaml under {sentinel_dir}")
    doc = yaml.safe_load(path.read_text())
    if not isinstance(doc, dict):
        raise TrialHarnessError(f"{path} does not parse to a mapping")
    return doc


def _nodes(sentinel: dict) -> dict[str, dict]:
    return (sentinel.get("spec") or {}).get("nodes") or {}


def _nodes_of_kind(sentinel: dict, kind: str) -> dict[str, dict]:
    return {
        nid: n for nid, n in _nodes(sentinel).items()
        if isinstance(n, dict) and n.get("kind") == kind
    }


def _declared_capabilities(sentinel: dict) -> set[str]:
    """Every capability id the Sentinel could possibly reach for.

    Union of `spec.capabilities.required[].id` and every node's
    `config.toolRef`. Binding the union — not just the declared list — is
    what keeps a Sentinel with a dangling toolRef (R6's failure mode) from
    silently falling through to a live capability binding mid-trial.
    """
    spec = sentinel.get("spec") or {}
    caps: set[str] = set()
    for entry in (spec.get("capabilities") or {}).get("required") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            caps.add(entry["id"])
    for node in _nodes(sentinel).values():
        ref = (node.get("config") or {}).get("toolRef") if isinstance(node, dict) else None
        if isinstance(ref, str):
            caps.add(ref)
    return caps


# --------------------------------------------------------------------------- #
# Preflight — T1, T2, T3                                                       #
# --------------------------------------------------------------------------- #


def check_t1_inputs(sentinel: dict, sentinel_dir: Path) -> tuple[TrialCheck, dict | None]:
    """T1 — inputs.json exists and satisfies every required input."""
    inputs_path = sentinel_dir / "inputs.json"
    if not inputs_path.exists():
        return TrialCheck(
            "T1", "trial-inputs", "FAIL",
            f"no inputs.json at {inputs_path}; the compiler must write the input "
            f"bindings the source investigation used so the Sentinel can be run",
        ), None
    try:
        raw = json.loads(inputs_path.read_text())
    except ValueError as e:
        return TrialCheck("T1", "trial-inputs", "FAIL", f"inputs.json is not valid JSON: {e}"), None
    if not isinstance(raw, dict):
        return TrialCheck("T1", "trial-inputs", "FAIL", "inputs.json must be a JSON object"), None

    declared = (sentinel.get("spec") or {}).get("inputs") or {}
    missing = [
        name for name, decl in declared.items()
        if isinstance(decl, dict) and decl.get("required")
        and raw.get(name) is None and "default" not in decl
    ]
    if missing:
        return TrialCheck(
            "T1", "trial-inputs", "FAIL",
            f"required inputs unbound in inputs.json: {sorted(missing)}",
        ), raw
    return TrialCheck(
        "T1", "trial-inputs", "PASS",
        f"inputs.json binds {len(raw)} input(s); {len(declared)} declared, none required-and-unbound",
    ), raw


def check_t2_fixtures(sentinel: dict, sentinel_dir: Path) -> TrialCheck:
    """T2 — every tool node resolves to a captured fixture."""
    tool_nodes = _nodes_of_kind(sentinel, "tool")
    if not tool_nodes:
        return TrialCheck("T2", "fixture-coverage", "PASS", "no tool nodes to cover")

    fixtures_dir = sentinel_dir / "fixtures"
    uncovered: list[str] = []
    unreadable: list[str] = []
    covered = 0
    for nid, node in tool_nodes.items():
        cap = (node.get("config") or {}).get("toolRef") or ""
        # Same lookup order capabilities._fixture_impl uses.
        candidates = [
            fixtures_dir / f"{nid}.json",
            fixtures_dir / f"{cap}.json",
            fixtures_dir / (cap.replace(".", "_") + ".json"),
        ]
        found = next((p for p in candidates if p.exists()), None)
        if found is None:
            uncovered.append(nid)
            continue
        try:
            json.loads(found.read_text())
        except ValueError as e:
            unreadable.append(f"{nid} ({found.name}: {e})")
            continue
        covered += 1

    problems = []
    if uncovered:
        problems.append(f"tool nodes with no fixture: {sorted(uncovered)}")
    if unreadable:
        problems.append(f"fixtures that are not valid JSON: {sorted(unreadable)}")
    if problems:
        return TrialCheck(
            "T2", "fixture-coverage", "FAIL",
            "; ".join(problems)
            + f" — write the captured tool_result verbatim to {fixtures_dir}/<node-id>.json "
            f"(never a synthesized one; see CORE.md 'Do NOT invent tool outputs')",
        )
    return TrialCheck(
        "T2", "fixture-coverage", "PASS",
        f"{covered}/{len(tool_nodes)} tool nodes have a captured fixture",
    )


_UNRUNNABLE_KINDS = {
    "llm": "the trial runtime has no LLM provider",
    "ask_human": "the trial runtime cannot solicit an operator answer",
}


def check_t3_bodies(sentinel: dict, sentinel_dir: Path) -> TrialCheck:
    """T3 — no stub function bodies, no node kinds this runtime cannot execute."""
    problems: list[str] = []

    for nid, node in _nodes_of_kind(sentinel, "function").items():
        source = (node.get("config") or {}).get("source")
        if not isinstance(source, str):
            problems.append(f"{nid}: no config.source")
            continue
        candidates = [sentinel_dir / source, sentinel_dir / source.replace("-", "_")]
        path = next((p for p in candidates if p.exists()), None)
        if path is None:
            problems.append(f"{nid}: function source {source!r} not found")
            continue
        try:
            tree = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as e:
            problems.append(f"{nid}: {path.name} does not parse ({e.msg})")
            continue
        if _raises_not_implemented(tree):
            problems.append(
                f"{nid}: {path.name} is a STUB (raises NotImplementedError) — "
                f"an unimplemented function node cannot be trial-executed"
            )

    for kind, why in _UNRUNNABLE_KINDS.items():
        for nid, node in _nodes_of_kind(sentinel, kind).items():
            if node.get("when") is not None:
                # A gated node may legitimately skip; T4 reports the skip.
                continue
            problems.append(f"{nid}: kind={kind} and ungated — {why}")

    if problems:
        return TrialCheck("T3", "executable-bodies", "FAIL", "; ".join(problems))
    return TrialCheck("T3", "executable-bodies", "PASS", "all node bodies are executable")


def _raises_not_implemented(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        exc = node.exc
        name = exc.func if isinstance(exc, ast.Call) else exc
        if isinstance(name, ast.Name) and name.id == "NotImplementedError":
            return True
    return False


# --------------------------------------------------------------------------- #
# Execution                                                                    #
# --------------------------------------------------------------------------- #


TRIAL_DEPLOYMENT_HEADER = """\
# GENERATED by common/mechanize/trial.py — Stage 10 trial execution.
#
# Every capability binds to `fixture`, so the trial is hermetic: it reads the
# tool responses the compiler captured from the source session and reaches the
# network for nothing. Do not deploy this file; the compiler writes a real
# deployment.yaml separately.
"""


def write_trial_deployment(sentinel: dict, dest: Path) -> Path:
    """Synthesize a fixture-only deployment for the trial run."""
    import yaml  # noqa: PLC0415

    doc = {
        "schemaVersion": "mechanize.dev/v1alpha1",
        "kind": "SentinelDeployment",
        "runtime": "manual",
        "execution": {"allowFixtures": True},
        "capabilityBindings": {
            cap: {"provider": "fixture", "side_effect_class": "read-only"}
            for cap in sorted(_declared_capabilities(sentinel))
        },
        "findingsRouting": [{"match": {"*": True}, "sink": "stdout"}],
        "functions": {
            nid: {"network": "disabled", "filesystem": "none"}
            for nid in sorted(_nodes_of_kind(sentinel, "function"))
        },
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(TRIAL_DEPLOYMENT_HEADER + yaml.safe_dump(doc, sort_keys=False))
    return dest


def run_once(sentinel_dir: Path, deployment_path: Path, run_dir: Path, run_id: str) -> RunOutcome:
    """Execute the Sentinel once through the production `serve` path."""
    runtime_serve, state_mod = _import_executor_modules()

    run_dir.mkdir(parents=True, exist_ok=True)
    state_path = run_dir / "state.sqlite"
    if state_path.exists():
        state_path.unlink()

    buf = io.StringIO()
    try:
        # `serve` mirrors every audit event and finding to stdout for pod logs.
        # Capture it so the verdict block stays readable; the buffer is kept
        # in the report and printed when a run fails.
        with contextlib.redirect_stdout(buf):
            exit_code = runtime_serve.run_serve(
                sentinel_dir=sentinel_dir,
                deployment_path=deployment_path,
                inputs_path=sentinel_dir / "inputs.json",
                state_path=state_path,
                run_id_override=run_id,
            )
    except Exception as e:  # noqa: BLE001 — surfaced as a T4 failure, not a crash
        return RunOutcome(
            run_id=run_id, exit_code=99, node_states={}, findings=[],
            finding_nodes={}, failures={"<harness>": f"{type(e).__name__}: {e}"},
            stdout=buf.getvalue(),
        )

    node_states: dict[str, str] = {}
    failures: dict[str, str] = {}
    finding_nodes: dict[str, str] = {}
    findings: list[dict[str, Any]] = []

    with state_mod.StateStore.open(state_path) as state:
        for row in state.list_audit(run_id):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError):
                continue
            event = row["event_type"]
            if event == "execution.completed":
                node_states.update(payload.get("nodeStates") or {})
            elif event == "node.failed":
                failures[payload.get("node") or row["node_id"] or "?"] = str(payload.get("error"))
            elif event in ("finding.delivered", "finding.unrouted", "finding.sink_failed"):
                h = payload.get("dedupe_hash")
                if h:
                    finding_nodes[h] = payload.get("emit_node") or ""
        for row in state.list_findings(run_id):
            if row.get("finding"):
                findings.append(row["finding"])

    return RunOutcome(
        run_id=run_id, exit_code=exit_code, node_states=node_states,
        findings=findings, finding_nodes=finding_nodes, failures=failures,
        stdout=buf.getvalue(),
    )


def check_t4_execution(outcome: RunOutcome) -> TrialCheck:
    """T4 — no node FAILED or CANCELLED."""
    if outcome.failures.get("<harness>"):
        return TrialCheck(
            "T4", "execution-completes", "FAIL",
            f"the run raised before completing: {outcome.failures['<harness>']}",
        )
    bad = {nid: st for nid, st in outcome.node_states.items() if st in ("FAILED", "CANCELLED")}
    if bad:
        detail = "; ".join(
            f"{nid} {st}" + (f" ({outcome.failures[nid]})" if nid in outcome.failures else "")
            for nid, st in sorted(bad.items())
        )
        return TrialCheck("T4", "execution-completes", "FAIL", detail)
    skipped = sorted(nid for nid, st in outcome.node_states.items() if st == "SKIPPED")
    succeeded = sum(1 for st in outcome.node_states.values() if st == "SUCCEEDED")
    detail = f"{succeeded} node(s) SUCCEEDED"
    if skipped:
        detail += f"; {len(skipped)} SKIPPED by `when:` gate: {skipped}"
    return TrialCheck("T4", "execution-completes", "PASS", detail)


# Fields that legitimately differ between two runs of the same Sentinel.
_NONDETERMINISTIC_FINDING_FIELDS = ("observedAt",)


def _stable_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for f in findings:
        copy = {k: v for k, v in f.items() if k not in _NONDETERMINISTIC_FINDING_FIELDS}
        out.append(copy)
    return out


def check_t5_determinism(first: RunOutcome, second: RunOutcome) -> TrialCheck:
    """T5 — two runs against the same fixtures produce the same findings."""
    if first.node_states != second.node_states:
        differing = sorted(
            nid for nid in set(first.node_states) | set(second.node_states)
            if first.node_states.get(nid) != second.node_states.get(nid)
        )
        return TrialCheck(
            "T5", "determinism", "FAIL",
            f"node states differ between runs for: {differing}",
        )
    a, b = _stable_findings(first.findings), _stable_findings(second.findings)
    if a != b:
        a_keys = [f.get("dedupeHash") for f in a]
        b_keys = [f.get("dedupeHash") for f in b]
        if a_keys != b_keys:
            return TrialCheck(
                "T5", "determinism", "FAIL",
                f"dedupeHash differs between runs ({a_keys} vs {b_keys}) — the "
                f"dedupeKey or an upstream output is time-varying; a finding that "
                f"cannot dedupe against itself will re-alert on every execution",
            )
        return TrialCheck(
            "T5", "determinism", "FAIL",
            "finding bodies differ between two runs against identical fixtures "
            "(same dedupeHash) — a function node or expression is non-deterministic",
        )
    return TrialCheck(
        "T5", "determinism", "PASS",
        f"two runs produced identical node states and {len(a)} identical finding(s)",
    )


# --------------------------------------------------------------------------- #
# Findings well-formedness — T6, T7                                            #
# --------------------------------------------------------------------------- #


def check_t6_evidence(sentinel: dict, outcome: RunOutcome) -> TrialCheck:
    """T6 — every finding carries the evidence its emit node declared, resolved.

    The failure this exists for: an emit node declares three evidence refs, the
    runtime cannot read the shape the compiler wrote, and the finding ships
    with `evidence: []`. Graph validation passes. Ratification passes. The
    finding is `critical` and backed by nothing.
    """
    if not outcome.findings:
        return TrialCheck("T6", "evidence-populated", "SKIP", "no findings emitted; nothing to check")

    emit_nodes = _nodes_of_kind(sentinel, "emit")
    problems: list[str] = []
    checked = 0
    for finding in outcome.findings:
        node_id = outcome.finding_nodes.get(finding.get("dedupeHash", ""))
        if node_id not in emit_nodes:
            # The audit did not correlate this finding to an emit node, so we
            # cannot compare it against a declaration. Check what we can.
            if not finding.get("evidence"):
                problems.append(
                    f"<uncorrelated finding {finding.get('type')!r}>: emitted with no evidence"
                )
            checked += 1
            continue
        declared = ((emit_nodes.get(node_id) or {}).get("config") or {}).get("finding") or {}
        declared_evidence = declared.get("evidence") or []
        evidence = finding.get("evidence") or []

        if declared_evidence and not evidence:
            problems.append(
                f"{node_id}: declares {len(declared_evidence)} evidence ref(s) but the "
                f"finding carries none"
            )
            continue
        if not declared_evidence and not evidence:
            problems.append(
                f"{node_id}: emitted a {finding.get('severity', '?')} finding with no "
                f"evidence at all — declare `evidence:` refs to the nodes that justify it"
            )
            continue
        if len(evidence) != len(declared_evidence):
            problems.append(
                f"{node_id}: declared {len(declared_evidence)} evidence ref(s), "
                f"finding carries {len(evidence)}"
            )
        for entry in evidence:
            if not isinstance(entry, dict):
                problems.append(f"{node_id}: evidence entry is not a mapping: {entry!r}")
                continue
            if entry.get("optional"):
                continue
            # `resolved` is set by executor._build_finding; absent on older runs.
            if entry.get("resolved") is False or (
                entry.get("resolved") is None and entry.get("value") is None
            ):
                problems.append(
                    f"{node_id}: required evidence {entry.get('nodeRef')}"
                    f".{entry.get('field')} resolved to null"
                )
        checked += 1

    if problems:
        return TrialCheck("T6", "evidence-populated", "FAIL", "; ".join(problems))
    return TrialCheck(
        "T6", "evidence-populated", "PASS",
        f"{checked} finding(s) carry fully-resolved evidence",
    )


_INTERPOLATION_RE = re.compile(r"\$\{[^}]+\}")


def _title_has_unresolved_reference(declared_title: Any, rendered_title: Any) -> bool:
    """True when "null" in the rendered title came from an interpolation.

    A title may legitimately contain the word null ("null pointer exception"),
    so the token alone proves nothing. Split the declared template on its
    `${...}` slots: anything in the rendered string that is not accounted for
    by a literal segment came from a reference that did not resolve.
    """
    if not isinstance(declared_title, str) or not isinstance(rendered_title, str):
        return False
    if "null" not in rendered_title.split():
        return False
    literals = _INTERPOLATION_RE.split(declared_title)
    return not any("null" in seg.split() for seg in literals)


def check_t7_attributes(sentinel: dict, outcome: RunOutcome) -> TrialCheck:
    """T7 — no rendered attribute or title collapsed to the literal string "null".

    `render_template` stringifies an unresolved reference as "null". Downstream
    that is indistinguishable from a real value, so it has to fail here.
    """
    if not outcome.findings:
        return TrialCheck("T7", "attributes-resolved", "SKIP", "no findings emitted; nothing to check")
    emit_nodes = _nodes_of_kind(sentinel, "emit")
    problems: list[str] = []
    for finding in outcome.findings:
        node_id = outcome.finding_nodes.get(finding.get("dedupeHash", "")) or "<unknown emit node>"
        for key, value in (finding.get("attributes") or {}).items():
            if isinstance(value, str) and value.strip() == "null":
                problems.append(f"{node_id}: attribute {key!r} rendered to \"null\"")
            elif value is None:
                problems.append(f"{node_id}: attribute {key!r} is null")
        declared = ((emit_nodes.get(node_id) or {}).get("config") or {}).get("finding") or {}
        if _title_has_unresolved_reference(declared.get("title"), finding.get("title")):
            problems.append(
                f"{node_id}: title contains an unresolved reference: {finding.get('title')!r}"
            )
    if problems:
        return TrialCheck("T7", "attributes-resolved", "FAIL", "; ".join(problems))
    return TrialCheck("T7", "attributes-resolved", "PASS", "all rendered attributes resolved")


# --------------------------------------------------------------------------- #
# Stage 11 — conclusion comparison, T8 + T9                                    #
# --------------------------------------------------------------------------- #


def load_expectation(sentinel_dir: Path) -> dict | None:
    path = sentinel_dir / "expectation.json"
    if not path.exists():
        return None
    try:
        doc = json.loads(path.read_text())
    except ValueError as e:
        raise TrialHarnessError(f"expectation.json is not valid JSON: {e}") from e
    if not isinstance(doc, dict):
        raise TrialHarnessError("expectation.json must be a JSON object")
    return doc


def _missing_expectation_checks(allow_missing: bool) -> list[TrialCheck]:
    if allow_missing:
        detail = "no expectation.json; Stage 11 not run (--allow-missing-expectation)"
        return [
            TrialCheck("T8", "conclusion-reached", "SKIP", detail),
            TrialCheck("T9", "conclusion-matches", "SKIP", detail),
        ]
    detail = (
        "no expectation.json — Stage 11 cannot compare the run against the original "
        "investigation. The compiler must write it; shape:\n" + EXPECTATION_SHAPE
    )
    return [
        TrialCheck("T8", "conclusion-reached", "FAIL", detail),
        TrialCheck("T9", "conclusion-matches", "FAIL", "no expectation.json (see T8)"),
    ]


def check_t8_t9_conclusion(
    sentinel: dict, outcome: RunOutcome, expectation: dict
) -> list[TrialCheck]:
    """T8/T9 — did the DAG reach the conclusion the investigation reached?"""
    expect_findings = expectation.get("expectFindings") or []
    expect_none = set(expectation.get("expectNoFindings") or [])

    emit_nodes = set(_nodes_of_kind(sentinel, "emit"))
    by_node: dict[str, dict[str, Any]] = {}
    for finding in outcome.findings:
        node_id = outcome.finding_nodes.get(finding.get("dedupeHash", ""))
        if node_id:
            by_node[node_id] = finding

    # --- T8: the right emit nodes fired, and only those.
    problems: list[str] = []
    unknown = [
        e.get("emitNode") for e in expect_findings
        if isinstance(e, dict) and e.get("emitNode") not in emit_nodes
    ]
    if unknown:
        problems.append(
            f"expectation names emit node(s) absent from sentinel.yaml: {sorted(filter(None, unknown))}"
        )
    for entry in expect_findings:
        if not isinstance(entry, dict):
            continue
        nid = entry.get("emitNode")
        if nid in emit_nodes and nid not in by_node:
            problems.append(
                f"{nid}: the original investigation concluded a finding here, the trial "
                f"produced none"
            )
    for nid in sorted(expect_none & set(by_node)):
        problems.append(
            f"{nid}: the original investigation did NOT conclude a finding here, the "
            f"trial produced one"
        )
    if not expect_findings and not expect_none:
        problems.append(
            "expectation.json declares neither expectFindings nor expectNoFindings — "
            "Stage 11 has no ground truth to compare against"
        )

    if problems:
        t8 = TrialCheck("T8", "conclusion-reached", "FAIL", "; ".join(problems))
    else:
        t8 = TrialCheck(
            "T8", "conclusion-reached", "PASS",
            f"{len(expect_findings)} expected finding(s) produced; "
            f"{len(expect_none)} correctly absent",
        )

    # --- T9: the findings that fired say what the investigation said.
    mismatches: list[str] = []
    compared = 0
    for entry in expect_findings:
        if not isinstance(entry, dict):
            continue
        nid = entry.get("emitNode")
        finding = by_node.get(nid)
        if finding is None:
            continue  # already reported by T8
        compared += 1
        want_type = entry.get("type")
        if want_type and finding.get("type") != want_type:
            mismatches.append(f"{nid}: type {finding.get('type')!r} != expected {want_type!r}")
        want_sev = entry.get("severity")
        if want_sev and finding.get("severity") != want_sev:
            mismatches.append(
                f"{nid}: severity {finding.get('severity')!r} != expected {want_sev!r}"
            )
        want_nodes = entry.get("evidenceNodes")
        if want_nodes:
            got = {e.get("nodeRef") for e in finding.get("evidence") or [] if isinstance(e, dict)}
            missing = sorted(set(want_nodes) - got)
            if missing:
                mismatches.append(f"{nid}: evidence missing node(s) {missing}")

    if mismatches:
        t9 = TrialCheck("T9", "conclusion-matches", "FAIL", "; ".join(mismatches))
    elif compared == 0:
        t9 = TrialCheck("T9", "conclusion-matches", "SKIP", "no findings available to compare")
    else:
        t9 = TrialCheck(
            "T9", "conclusion-matches", "PASS",
            f"{compared} finding(s) match the original conclusion on type, severity, and evidence",
        )
    return [t8, t9]


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #


def run_trial(sentinel_dir: Path, allow_missing_expectation: bool = False) -> dict[str, Any]:
    """Run Stages 10 and 11. Returns the report dict; never raises on a FAIL."""
    sentinel_dir = Path(sentinel_dir).resolve()
    sentinel = load_sentinel_dir(sentinel_dir)
    trial_dir = sentinel_dir / "trial"
    if trial_dir.exists():
        shutil.rmtree(trial_dir)
    trial_dir.mkdir(parents=True)

    checks: list[TrialCheck] = []
    t1, _inputs = check_t1_inputs(sentinel, sentinel_dir)
    checks.append(t1)
    checks.append(check_t2_fixtures(sentinel, sentinel_dir))
    checks.append(check_t3_bodies(sentinel, sentinel_dir))

    report: dict[str, Any] = {
        "sentinelDir": str(sentinel_dir),
        "sentinelName": (sentinel.get("metadata") or {}).get("name"),
        "stage": "10+11",
    }

    if any(c.verdict == "FAIL" for c in checks):
        # Preflight failed: running the DAG anyway would produce a second,
        # noisier failure that says less than the first one.
        for cid, name in (("T4", "execution-completes"), ("T5", "determinism"),
                          ("T6", "evidence-populated"), ("T7", "attributes-resolved"),
                          ("T8", "conclusion-reached"), ("T9", "conclusion-matches")):
            checks.append(TrialCheck(cid, name, "SKIP", "preflight failed; trial not run"))
        return _finalize(report, checks, trial_dir, runs=[])

    deployment_path = write_trial_deployment(sentinel, trial_dir / "deployment.trial.yaml")
    first = run_once(sentinel_dir, deployment_path, trial_dir / "run-1", "trial-run-1")
    second = run_once(sentinel_dir, deployment_path, trial_dir / "run-2", "trial-run-2")

    checks.append(check_t4_execution(first))
    checks.append(check_t5_determinism(first, second))
    checks.append(check_t6_evidence(sentinel, first))
    checks.append(check_t7_attributes(sentinel, first))

    try:
        expectation = load_expectation(sentinel_dir)
    except TrialHarnessError as e:
        expectation = None
        checks.append(TrialCheck("T8", "conclusion-reached", "FAIL", str(e)))
        checks.append(TrialCheck("T9", "conclusion-matches", "FAIL", "expectation.json unreadable (see T8)"))
    else:
        if expectation is None:
            checks.extend(_missing_expectation_checks(allow_missing_expectation))
        else:
            report["conclusion"] = expectation.get("conclusion")
            report["sourceSession"] = expectation.get("sourceSession")
            checks.extend(check_t8_t9_conclusion(sentinel, first, expectation))

    return _finalize(report, checks, trial_dir, runs=[first, second])


def _finalize(
    report: dict[str, Any], checks: list[TrialCheck], trial_dir: Path, runs: list[RunOutcome]
) -> dict[str, Any]:
    passed = not any(c.verdict == "FAIL" for c in checks)
    report["passed"] = passed
    report["checks"] = [asdict(c) for c in checks]
    report["runs"] = [
        {
            "runId": r.run_id,
            "exitCode": r.exit_code,
            "nodeStates": r.node_states,
            "findings": r.findings,
            "findingNodes": r.finding_nodes,
            "failures": r.failures,
        }
        for r in runs
    ]
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    if runs:
        (trial_dir / "run-1" / "stdout.log").write_text(runs[0].stdout)
    return report


def format_verdict_block(report: dict[str, Any]) -> str:
    """Stage 10/11 verdict block, shaped like Stage 5.5's."""
    lines = [f"TRIAL: {'PASSED' if report['passed'] else 'FAILED'}", ""]
    for c in report["checks"]:
        lines.append(f"{c['id']} [{c['verdict']}] {c['name']}: {c['detail']}")
    if not report["passed"]:
        lines.append("")
        lines.append("Fix list (one entry per FAIL):")
        for c in report["checks"]:
            if c["verdict"] == "FAIL":
                lines.append(f"  - {c['id']} {c['name']}: {c['detail']}")
        lines.append("")
        lines.append(
            "A failed trial is a compile that did not finish. Iterate the DAG (CORE.md "
            "Stage 6) and re-run, or emit with `metadata.trial.status: failed` and say so."
        )
    return "\n".join(lines)


def _main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="trial.py",
        description="Stage 10 (trial execution) + Stage 11 (conclusion comparison).",
    )
    parser.add_argument("sentinel_dir", type=Path, help="directory holding sentinel.yaml")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument(
        "--allow-missing-expectation",
        action="store_true",
        help="downgrade T8/T9 to SKIP when expectation.json is absent (CI over "
             "checked-in Sentinels; NOT for a fresh compile)",
    )
    args = parser.parse_args(argv)

    try:
        report = run_trial(args.sentinel_dir, allow_missing_expectation=args.allow_missing_expectation)
    except TrialHarnessError as e:
        print(f"trial harness error: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_verdict_block(report))
    return 0 if report["passed"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
