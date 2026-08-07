"""sentinel-lint Phase 1 — structural checks + R1–R6 via the shared ratification module.

Loaded by executor.py's `lint` subcommand. See spike/executor/README.md for
usage; see docs/sentinel-lint-plan.md for the plan this implements.

Structural checks (universal — all Sentinels, all deployment modes):

* Sentinel loads as YAML with the required top-level keys.
* `kind` is `Sentinel` (Variations refused with a plan-linked message).
* Every node has a recognized `kind` and every `dependsOn` target exists.
* DAG is acyclic (uses executor.topo_sort).
* Every ${nodes.<id>...} reference resolves to a declared node.
* Every ${inputs.<name>...} reference resolves to a declared input.
* Every `kind: function` node's `source` ends in `.py` (catches the compiler
  drift where nodejs-shaped `.mjs` files were emitted).
* Every referenced function source file exists next to sentinel.yaml.
* Every function file AST-parses (no import) and has a top-level `run(...)`.
* R1–R6 via `common.mechanize.ratification`.

If a Phase-1 lint FAILs, exit 1. WARN-only exits 0 with warnings printed. See
`LintFinding` for the finding shape.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# Executor lives next to this module.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from executor import DagValidationError, load_sentinel, topo_sort  # noqa: E402

# Ratification module lives at common/mechanize/ratification.py — two dirs up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))
from common.mechanize import ratification  # noqa: E402


Severity = Literal["FAIL", "WARN"]


@dataclass
class LintFinding:
    code: str
    severity: Severity
    file: str
    line: int | None
    message: str
    fix: str


@dataclass
class LintResult:
    findings: list[LintFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(f.severity == "FAIL" for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "findings": [asdict(f) for f in self.findings],
        }


# --------------------------------------------------------------------------- #
# Structural check                                                             #
# --------------------------------------------------------------------------- #

_KNOWN_NODE_KINDS = {"tool", "function", "condition", "llm", "emit", "ask_human"}
_SEVERITIES = ("info", "warning", "critical")

_INPUT_REF_RE = re.compile(r"\binputs\.([A-Za-z_][A-Za-z0-9_]*)")
_NODE_REF_RE = re.compile(r"\bnodes\.([A-Za-z_][A-Za-z0-9_-]*)")


def _walk_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def _ancestors(nodes: dict[str, Any], start: str) -> set[str]:
    """Every node that is guaranteed to run before `start` (transitive dependsOn)."""
    seen: set[str] = set()
    stack = list((nodes.get(start) or {}).get("dependsOn") or [])
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in nodes:
            continue
        seen.add(nid)
        stack.extend((nodes.get(nid) or {}).get("dependsOn") or [])
    return seen


def lint_structural(sentinel_dir: Path) -> LintResult:
    """Run all Phase 1 checks against `sentinel_dir/sentinel.yaml`.

    The Sentinel-directory convention: `sentinel.yaml` sits at the root;
    function files live under `functions/`; rationale (optional) is
    `rationale.md`. Anything else is ignored per Phase 1's scope.
    """
    result = LintResult()
    sentinel_dir = Path(sentinel_dir)
    sentinel_path = sentinel_dir / "sentinel.yaml"
    rel = str(sentinel_path)

    if not sentinel_path.exists():
        result.findings.append(
            LintFinding(
                code="STRUCT-MISSING",
                severity="FAIL",
                file=rel,
                line=None,
                message="sentinel.yaml not found in sentinel directory",
                fix=f"create {sentinel_path.name} at {sentinel_dir}",
            )
        )
        return result

    # 1. YAML parse.
    try:
        sentinel, _digest = load_sentinel(sentinel_path)
    except yaml.YAMLError as e:
        result.findings.append(
            LintFinding(
                code="STRUCT-YAML",
                severity="FAIL",
                file=rel,
                line=getattr(getattr(e, "problem_mark", None), "line", None),
                message=f"YAML parse error: {e}",
                fix="fix the YAML syntax",
            )
        )
        return result

    if not isinstance(sentinel, dict):
        result.findings.append(
            LintFinding(
                code="STRUCT-SHAPE",
                severity="FAIL",
                file=rel,
                line=None,
                message="sentinel.yaml top-level must be a mapping",
                fix="wrap the document in a top-level mapping",
            )
        )
        return result

    # 2. kind gate — Variations refused with the plan-linked message.
    kind = sentinel.get("kind")
    if kind == "Variation":
        result.findings.append(
            LintFinding(
                code="STRUCT-VARIATION",
                severity="FAIL",
                file=rel,
                line=None,
                message=(
                    "Variations not yet supported by sentinel-lint; "
                    "see runtime-comms-plan for the v1 overlay-bindings story."
                ),
                fix=(
                    "either land the overlay-bindings work first or convert this "
                    "artifact to a `kind: Sentinel` before checking it in"
                ),
            )
        )
        return result
    if kind != "Sentinel":
        result.findings.append(
            LintFinding(
                code="STRUCT-KIND",
                severity="FAIL",
                file=rel,
                line=None,
                message=f"top-level `kind` must be 'Sentinel' (got {kind!r})",
                fix="set `kind: Sentinel` in the manifest",
            )
        )
        return result

    # 3. required top-level keys.
    for req in ("apiVersion", "metadata", "spec"):
        if req not in sentinel:
            result.findings.append(
                LintFinding(
                    code="STRUCT-SHAPE",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=f"top-level key {req!r} missing",
                    fix=f"add `{req}:` at the top of the manifest",
                )
            )
    if any(f.code == "STRUCT-SHAPE" for f in result.findings):
        return result

    spec = sentinel["spec"] or {}
    nodes = spec.get("nodes") or {}
    if not isinstance(nodes, dict) or not nodes:
        result.findings.append(
            LintFinding(
                code="STRUCT-NODES",
                severity="FAIL",
                file=rel,
                line=None,
                message="spec.nodes must be a non-empty mapping",
                fix="declare at least one node under spec.nodes",
            )
        )
        return result

    # 4. per-node type/kind + dependsOn shape.
    for nid, node in nodes.items():
        if not isinstance(node, dict):
            result.findings.append(
                LintFinding(
                    code="STRUCT-NODE",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=f"node {nid!r} must be a mapping",
                    fix=f"declare `{nid}:` as a mapping with `kind:` etc.",
                )
            )
            continue
        nkind = node.get("kind")
        if nkind not in _KNOWN_NODE_KINDS:
            result.findings.append(
                LintFinding(
                    code="STRUCT-KIND",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=f"node {nid!r} has unknown kind {nkind!r}",
                    fix=f"set `kind:` to one of {sorted(_KNOWN_NODE_KINDS)}",
                )
            )
        deps = node.get("dependsOn") or []
        if not isinstance(deps, list):
            result.findings.append(
                LintFinding(
                    code="STRUCT-DEPS",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=f"node {nid!r} `dependsOn` must be a list",
                    fix="rewrite dependsOn as a YAML list of node ids",
                )
            )

    # 5. graph validity (topo_sort catches both unknown deps and cycles).
    try:
        topo_sort(nodes)
    except DagValidationError as e:
        result.findings.append(
            LintFinding(
                code="STRUCT-GRAPH",
                severity="FAIL",
                file=rel,
                line=None,
                message=str(e),
                fix="fix the dependency graph (cycle or unknown dependsOn target)",
            )
        )

    # 6. referential validity for ${inputs.*} and ${nodes.*}.
    declared_inputs = set((spec.get("inputs") or {}).keys())
    declared_nodes = set(nodes.keys())
    seen_bad_inputs: set[str] = set()
    seen_bad_nodes: set[str] = set()
    # Only scan spec — outputs and nodes contain the interpolations.
    for text in _walk_strings(spec):
        for m in _INPUT_REF_RE.finditer(text):
            name = m.group(1)
            if name not in declared_inputs and name not in seen_bad_inputs:
                seen_bad_inputs.add(name)
                result.findings.append(
                    LintFinding(
                        code="STRUCT-REF",
                        severity="FAIL",
                        file=rel,
                        line=None,
                        message=f"reference to inputs.{name} but spec.inputs has no such input",
                        fix=f"declare `{name}:` under spec.inputs, or fix the reference",
                    )
                )
        for m in _NODE_REF_RE.finditer(text):
            name = m.group(1)
            if name not in declared_nodes and name not in seen_bad_nodes:
                seen_bad_nodes.add(name)
                result.findings.append(
                    LintFinding(
                        code="STRUCT-REF",
                        severity="FAIL",
                        file=rel,
                        line=None,
                        message=f"reference to nodes.{name} but no such node in spec.nodes",
                        fix=f"add node `{name}:` under spec.nodes, or fix the reference",
                    )
                )

    # 7. function-node source: .py extension + file existence + AST-parses + `run` entrypoint,
    # plus runtime: python3.12 (v0 constraint per CORE.md Stage 4 "Function-node runtime").
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "function":
            continue
        config = node.get("config") or {}
        runtime = config.get("runtime")
        if runtime is not None and runtime != "python3.12":
            result.findings.append(
                LintFinding(
                    code="FUNC-RUNTIME",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=(
                        f"function node {nid!r} declares runtime={runtime!r}; "
                        f"v0 requires python3.12 (CORE.md Stage 4 rule)"
                    ),
                    fix=f"set runtime: python3.12 on {nid} (Node.js and other runtimes are a future concern)",
                )
            )
        source = config.get("source")
        if not isinstance(source, str) or not source:
            result.findings.append(
                LintFinding(
                    code="FUNC-SOURCE",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=f"function node {nid!r} has no `config.source`",
                    fix=f"set `source: functions/{nid}.py` on this node",
                )
            )
            continue
        if not source.endswith(".py"):
            result.findings.append(
                LintFinding(
                    code="FUNC-EXT",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=(
                        f"function node {nid!r} source {source!r} is not a .py file "
                        f"(v0 requires python3.12)"
                    ),
                    fix=f"rename to functions/{nid}.py and update the source: reference",
                )
            )
            # Continue anyway — we still want to check existence to surface both.

        # Resolve relative to sentinel dir. Compiler occasionally writes dashed names
        # even though the file is underscored — accept either.
        candidates = [
            sentinel_dir / source,
            sentinel_dir / source.replace("-", "_"),
        ]
        src_path = next((p for p in candidates if p.exists()), None)
        if src_path is None:
            result.findings.append(
                LintFinding(
                    code="FUNC-MISSING",
                    severity="FAIL",
                    file=str(sentinel_dir / source),
                    line=None,
                    message=(
                        f"function source {source!r} for node {nid!r} not found "
                        f"(looked in {sentinel_dir})"
                    ),
                    fix=f"create {sentinel_dir / source} with a top-level `def run(args):`",
                )
            )
            continue

        # AST parse only — no import — and look for a top-level `run(...)`.
        try:
            tree = ast.parse(src_path.read_text(), filename=str(src_path))
        except SyntaxError as e:
            result.findings.append(
                LintFinding(
                    code="FUNC-PARSE",
                    severity="FAIL",
                    file=str(src_path),
                    line=e.lineno,
                    message=f"function file failed to parse: {e.msg}",
                    fix="fix the Python syntax",
                )
            )
            continue
        has_run = any(
            isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run"
            for n in tree.body
        )
        if not has_run:
            result.findings.append(
                LintFinding(
                    code="FUNC-ENTRY",
                    severity="FAIL",
                    file=str(src_path),
                    line=None,
                    message=f"function {source!r} has no top-level `def run(args):`",
                    fix="add `def run(args):` at module scope as the executor entrypoint",
                )
            )

    # 8. emit-node evidence shape.
    #
    # The runtime resolves evidence as {nodeRef, field, optional} mappings —
    # the string form `${nodes.<id>.output}` cannot express `optional` or
    # field selection, so it is not accepted. It used to be skipped silently,
    # which shipped findings with empty evidence. executor._build_finding now
    # raises; this is the static catch so CI fails before a deploy does.
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "emit":
            continue
        finding = (node.get("config") or {}).get("finding") or {}

        # Severity: a literal out of range, or both spellings at once, silently
        # produced an `info` finding before the runtime learned to read
        # `severity:` at all. Catch both statically.
        severity = finding.get("severity")
        if severity is not None and severity not in _SEVERITIES:
            result.findings.append(
                LintFinding(
                    code="FINDING-SEVERITY",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=f"emit node {nid!r} severity {severity!r} is not one of {list(_SEVERITIES)}",
                    fix=f"set severity to one of {list(_SEVERITIES)}, or use severityExpression",
                )
            )
        if severity is not None and finding.get("severityExpression") is not None:
            result.findings.append(
                LintFinding(
                    code="FINDING-SEVERITY",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=(
                        f"emit node {nid!r} sets both `severity` and `severityExpression`; "
                        f"the runtime uses severityExpression and the literal is dead config"
                    ),
                    fix="keep whichever one the finding actually needs and delete the other",
                )
            )

        evidence = finding.get("evidence")
        if evidence is None:
            continue
        if not isinstance(evidence, list):
            result.findings.append(
                LintFinding(
                    code="EMIT-EVIDENCE",
                    severity="FAIL",
                    file=rel,
                    line=None,
                    message=f"emit node {nid!r} `evidence` must be a list (got {type(evidence).__name__})",
                    fix="rewrite evidence as a list of {nodeRef, field} mappings",
                )
            )
            continue
        for i, entry in enumerate(evidence):
            if not isinstance(entry, dict) or "nodeRef" not in entry:
                result.findings.append(
                    LintFinding(
                        code="EMIT-EVIDENCE",
                        severity="FAIL",
                        file=rel,
                        line=None,
                        message=(
                            f"emit node {nid!r} evidence[{i}] is {entry!r}; the runtime "
                            f"resolves only {{nodeRef, field, optional}} mappings and will "
                            f"fail this node"
                        ),
                        fix=(
                            "rewrite as `- nodeRef: <node-id>` + `field: output` "
                            "(add `optional: true` if the node may not produce)"
                        ),
                    )
                )
                continue
            ref = entry.get("nodeRef")
            if ref not in declared_nodes:
                result.findings.append(
                    LintFinding(
                        code="EMIT-EVIDENCE",
                        severity="FAIL",
                        file=rel,
                        line=None,
                        message=f"emit node {nid!r} evidence[{i}] references unknown node {ref!r}",
                        fix=f"point nodeRef at a node declared under spec.nodes, or remove the entry",
                    )
                )
            elif ref not in _ancestors(nodes, nid):
                # Transitive dependencies count: an emit node that depends on a
                # reducer which itself depends on the cited node is correctly
                # ordered. Only an evidence ref with no ordering guarantee at
                # all resolves to null non-deterministically.
                result.findings.append(
                    LintFinding(
                        code="EMIT-EVIDENCE",
                        severity="WARN",
                        file=rel,
                        line=None,
                        message=(
                            f"emit node {nid!r} cites evidence from {ref!r} but neither "
                            f"depends on it directly nor transitively; the value resolves "
                            f"to null unless {ref!r} happens to run first"
                        ),
                        fix=f"add {ref!r} to {nid}'s dependsOn",
                    )
                )

    # 9. R1–R6 via the shared ratification module.
    rationale_path = sentinel_dir / "rationale.md"
    rationale = rationale_path.read_text() if rationale_path.exists() else ""
    for r in ratification.run_all(sentinel, rationale):
        if r.verdict == "PASS":
            continue
        result.findings.append(
            LintFinding(
                code=r.rule,
                severity="FAIL",
                file=rel,
                line=None,
                message=r.detail,
                fix=_ratification_fix_hint(r.rule),
            )
        )

    return result


def _ratification_fix_hint(rule: str) -> str:
    hints = {
        "R1": "add the missing `path: /spec/inputs/<name>/default` entries to spec.variationPoints[]",
        "R2": "rewrite vendor-shaped capability ids to their abstract equivalent (observability.* / code.*)",
        "R3": "add a rationale.md paragraph naming the node and explaining why it isn't `kind: function` per §32",
        "R4": "either remove the hallucinated citation from rationale.md or add the node it refers to",
        "R5": "rewrite dedupeKey to only reference ${inputs.*} and ${nodes.*.output.*} — no execution.now, no uuid()",
        "R6": "align spec.capabilities.required[] with the toolRefs actually used (or remove the orphan)",
    }
    return hints.get(rule, "see CORE.md Stage 5.5 for the rule definition")


# --------------------------------------------------------------------------- #
# Formatting                                                                   #
# --------------------------------------------------------------------------- #

def format_text(result: LintResult, sentinel_dir: Path) -> str:
    lines: list[str] = []
    fails = 0
    warns = 0
    for f in result.findings:
        loc = f.file if f.line is None else f"{f.file}:{f.line}"
        lines.append(f"{loc} {f.code} {f.severity}: {f.message}")
        lines.append(f"    fix: {f.fix}")
        if f.severity == "FAIL":
            fails += 1
        else:
            warns += 1
    if not result.findings:
        lines.append(f"{sentinel_dir}: PASS (structural)")
    else:
        status = "PASS" if result.passed else "FAIL"
        lines.append(f"---")
        lines.append(f"{sentinel_dir}: {status} — {fails} fail, {warns} warn")
    return "\n".join(lines)


def format_json(result: LintResult) -> str:
    return json.dumps(result.to_dict(), indent=2, sort_keys=True)


# --------------------------------------------------------------------------- #
# CLI helper — invoked from executor.py                                        #
# --------------------------------------------------------------------------- #

def lint_all(
    sentinel_dir: Path,
    check_mode: str = "all",
    registry_path: Path | None = None,
    schema_path: Path | None = None,
) -> LintResult:
    """Run structural + remote-readiness checks per `check_mode`.

    `check_mode`:
      * ``structural`` — Phase 1 only (universal).
      * ``remote`` — Phase 2 only (no-op unless the sentinel declares
        `metadata.deployment.mode: remote`).
      * ``all`` (default) — both phases; the remote phase is still gated on
        the manifest's declared mode.

    Loading the sentinel twice is avoided: structural runs first and, if it
    passes far enough to parse the manifest, we hand the parsed dict to
    lint_remote via a re-load (lint_remote is designed to be self-contained
    so tests can drive it directly).
    """
    sentinel_dir = Path(sentinel_dir)
    result = LintResult()

    if check_mode in ("structural", "all"):
        structural = lint_structural(sentinel_dir)
        result.findings.extend(structural.findings)
        # If we couldn't even parse the manifest, remote checks are moot.
        blocking = {"STRUCT-MISSING", "STRUCT-YAML", "STRUCT-SHAPE",
                    "STRUCT-VARIATION", "STRUCT-KIND"}
        if any(f.code in blocking for f in structural.findings):
            return result

    if check_mode in ("remote", "all"):
        # Deferred import to keep the structural-only path light.
        from lint_remote import lint_remote  # noqa: E402

        sentinel_path = sentinel_dir / "sentinel.yaml"
        if not sentinel_path.exists():
            # Structural already reported STRUCT-MISSING if check_mode=all;
            # for check_mode=remote we surface a matching finding.
            if check_mode == "remote":
                result.findings.append(LintFinding(
                    code="STRUCT-MISSING",
                    severity="FAIL",
                    file=str(sentinel_path),
                    line=None,
                    message="sentinel.yaml not found in sentinel directory",
                    fix=f"create {sentinel_path.name} at {sentinel_dir}",
                ))
            return result
        try:
            with sentinel_path.open() as f:
                sentinel = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            result.findings.append(LintFinding(
                code="STRUCT-YAML",
                severity="FAIL",
                file=str(sentinel_path),
                line=getattr(getattr(e, "problem_mark", None), "line", None),
                message=f"YAML parse error: {e}",
                fix="fix the YAML syntax",
            ))
            return result
        if not isinstance(sentinel, dict):
            return result
        remote_result = lint_remote(
            sentinel_dir, sentinel,
            registry_path=registry_path, schema_path=schema_path,
        )
        result.findings.extend(remote_result.findings)

    return result


def run_cli(
    sentinel_dir: Path,
    output_format: str = "text",
    check_mode: str = "all",
    registry_path: Path | None = None,
    schema_path: Path | None = None,
) -> int:
    """Return 0 on PASS/warn-only, 1 on any FAIL."""
    result = lint_all(
        sentinel_dir,
        check_mode=check_mode,
        registry_path=registry_path,
        schema_path=schema_path,
    )
    if output_format == "json":
        print(format_json(result))
    else:
        print(format_text(result, sentinel_dir))
    return 0 if result.passed else 1
