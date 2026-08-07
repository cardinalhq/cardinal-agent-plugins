"""Shared ratification checklist (R1–R6) for the mechanize skill and sentinel-lint.

Single source of truth for the six semantic rules the compiler's Stage 5.5
cold judge enforces (see CORE.md §"Stage 5.5 — Cold ratification"). The
sentinel-lint CLI (spike/executor/lint.py) imports the same functions so
compiler and lint never drift.

**Framework-free by design.** Pure functions, no I/O beyond what's passed in.
Callers (compiler skill or CLI) load YAML themselves and hand a parsed dict.

Each check returns a RatificationResult(rule, verdict, detail). `run_all`
returns the six results in order. When invoked as `python3 ratification.py
<sentinel.yaml> [rationale.md]` prints the Stage 5.5 verdict block on stdout.

Rules:
- R1  Variation-point completeness  — every templated `${inputs.<name>}` with a
      default is declared in spec.variationPoints[].
- R2  Capability-ID abstraction     — every capability id uses an abstract
      prefix from the known registry (observability.*, code.*).
- R3  Function-vs-LLM discipline    — every `kind: llm` node has a rationale
      paragraph explaining why it isn't `kind: function` per §32.
- R4  Node existence                — every node id cited in rationale.md
      exists in sentinel.yaml.
- R5  Emit dedupeKey decomposability — no `${execution.now}`, no `${uuid()}`,
      no free text; only ${inputs.*}, ${nodes.*.output.*}, and literals.
- R6  toolRef ↔ capability referential integrity — no orphan capabilities,
      no dangling toolRefs.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from typing import Any, Literal

Verdict = Literal["PASS", "FAIL"]


# Fallback only. The authority is common/capabilities-registry.yaml, which
# callers parse and pass in as `registry` — see check_r2. These prefixes are
# what R2 falls back to when no registry is reachable (ratification is
# framework-free and must stay usable on a bare dict), and they are strictly
# weaker: a prefix match proves a naming convention was followed, not that the
# capability is one any provider implements. That gap is not hypothetical — a
# Sentinel declaring `observability.fetch-status-summary` passed prefix-only R2
# and then failed at runtime with UnknownProviderError, because no such
# capability was ever registered.
KNOWN_CAPABILITY_PREFIXES: tuple[str, ...] = (
    "observability.",
    "code.",
    "web.",
)


@dataclass(frozen=True)
class RatificationResult:
    rule: str
    verdict: Verdict
    detail: str


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

_INTERPOLATION_RE = re.compile(r"\$\{([^}]+)\}")
_INPUT_REF_RE = re.compile(r"\binputs\.([A-Za-z_][A-Za-z0-9_]*)")
_NODE_REF_RE = re.compile(r"\bnodes\.([A-Za-z_][A-Za-z0-9_-]*)")


def _walk_strings(value: Any):
    """Yield every string embedded anywhere inside a nested dict/list."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def _collect_input_refs(value: Any) -> set[str]:
    """Input names referenced from inside `${...}` interpolations.

    Scoped to interpolations on purpose. Matching bare `inputs.foo` anywhere in
    any string also matched prose — an `llm` node's `task:` or an `ask_human`
    node's `question:` that merely *names* an input in a sentence would make R1
    demand a variation point for it.
    """
    refs: set[str] = set()
    for s in _walk_strings(value):
        for interp in _INTERPOLATION_RE.finditer(s):
            for m in _INPUT_REF_RE.finditer(interp.group(1)):
                refs.add(m.group(1))
    return refs


def _collect_node_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    for s in _walk_strings(value):
        for m in _NODE_REF_RE.finditer(s):
            refs.add(m.group(1))
    return refs


def _get_spec(sentinel: dict) -> dict:
    return sentinel.get("spec") or {}


def _get_nodes(sentinel: dict) -> dict:
    return _get_spec(sentinel).get("nodes") or {}


# --------------------------------------------------------------------------- #
# Individual rules                                                             #
# --------------------------------------------------------------------------- #

def check_r1(sentinel: dict) -> RatificationResult:
    """R1 — variation-point completeness for defaulted-and-referenced inputs."""
    spec = _get_spec(sentinel)
    inputs = spec.get("inputs") or {}
    variation_points = spec.get("variationPoints") or []

    # Which inputs have a default AND are actually templated anywhere?
    referenced = _collect_input_refs(spec.get("nodes") or {})
    referenced |= _collect_input_refs(spec.get("outputs") or {})

    # Collect declared variation-point paths and operations.
    vp_ops: dict[str, list] = {}
    for entry in variation_points:
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if isinstance(p, str):
            vp_ops[p] = entry.get("operations") or []

    missing: list[str] = []
    for name, decl in inputs.items():
        if not isinstance(decl, dict):
            continue
        if "default" not in decl:
            continue
        if name not in referenced:
            continue
        wanted = f"/spec/inputs/{name}/default"
        ops = vp_ops.get(wanted)
        if not ops:
            missing.append(name)

    if missing:
        return RatificationResult(
            rule="R1",
            verdict="FAIL",
            detail=(
                f"inputs with defaults but no /spec/inputs/<name>/default "
                f"variation point (non-empty operations): {sorted(missing)}"
            ),
        )
    return RatificationResult("R1", "PASS", "all defaulted+referenced inputs declared as variation points")


def check_r2(sentinel: dict, registry: dict | None = None) -> RatificationResult:
    """R2 — capability IDs must be registered in the shared capabilities registry.

    `registry` is the parsed common/capabilities-registry.yaml. When supplied,
    R2 checks *membership* — the only question that predicts whether the
    capability will actually bind at runtime. When it is absent (a caller
    holding nothing but a parsed Sentinel), R2 degrades to the prefix check and
    says so in the detail, because a silent downgrade would read as a stronger
    guarantee than it is.
    """
    spec = _get_spec(sentinel)
    caps = (spec.get("capabilities") or {}).get("required") or []
    declared_ids = [
        e.get("id") for e in caps if isinstance(e, dict) and isinstance(e.get("id"), str)
    ]

    known = set((registry or {}).get("capabilities") or {})
    if known:
        unregistered = [cid for cid in declared_ids if cid not in known]
        if unregistered:
            return RatificationResult(
                rule="R2",
                verdict="FAIL",
                detail=(
                    f"capability ids absent from the capabilities registry: "
                    f"{sorted(unregistered)} — add them to "
                    f"common/capabilities-registry.yaml with their providers, or "
                    f"map to an existing id"
                ),
            )
        return RatificationResult(
            "R2", "PASS", f"all {len(declared_ids)} capability id(s) are registered"
        )

    bad = [
        cid for cid in declared_ids
        if not any(cid.startswith(p) for p in KNOWN_CAPABILITY_PREFIXES)
    ]
    if bad:
        return RatificationResult(
            rule="R2",
            verdict="FAIL",
            detail=(
                f"vendor-shaped capability ids (no registry supplied; allowed "
                f"prefixes: {list(KNOWN_CAPABILITY_PREFIXES)}): {bad}"
            ),
        )
    return RatificationResult(
        "R2", "PASS",
        "all capability ids use abstract prefixes (no registry supplied — "
        "prefix check only, membership unverified)",
    )


def check_r3(sentinel: dict, rationale: str = "") -> RatificationResult:
    """R3 — every `kind: llm` node has a §32 function-vs-llm justification in rationale."""
    nodes = _get_nodes(sentinel)
    llm_ids = [nid for nid, n in nodes.items() if isinstance(n, dict) and n.get("kind") == "llm"]
    if not llm_ids:
        return RatificationResult("R3", "PASS", "no llm nodes present")
    if not rationale:
        return RatificationResult(
            rule="R3",
            verdict="FAIL",
            detail=(
                f"llm nodes present ({llm_ids}) but no rationale supplied "
                f"to justify why each isn't `kind: function` per §32"
            ),
        )
    missing: list[str] = []
    lower = rationale.lower()
    for nid in llm_ids:
        # Node id is mentioned, and one of {function, §32, deterministic}
        # appears within ~400 chars of SOME mention. Checking every occurrence
        # rather than only the first matters: node ids routinely appear first
        # in a classification table and are justified in a later paragraph, so
        # a first-occurrence-only check failed nodes that were properly argued.
        needle = nid.lower()
        found_any = False
        justified = False
        start = lower.find(needle)
        while start >= 0:
            found_any = True
            window = lower[max(0, start - 200) : start + 400]
            if any(kw in window for kw in ("function", "§32", "deterministic")):
                justified = True
                break
            start = lower.find(needle, start + 1)
        if not (found_any and justified):
            missing.append(nid)
    if missing:
        return RatificationResult(
            rule="R3",
            verdict="FAIL",
            detail=f"llm nodes lacking function-vs-llm justification in rationale: {missing}",
        )
    return RatificationResult("R3", "PASS", f"all llm nodes justified in rationale: {llm_ids}")


def _declared_non_node_identifiers(sentinel: dict) -> set[str]:
    """Kebab-shaped identifiers the Sentinel itself declares that are NOT node ids.

    R4 guesses which backticked tokens in prose are node citations. Without
    this exclusion set it flagged a finding *type* (`service-health-status`)
    as a hallucinated node, because the type is kebab-case and the surrounding
    sentence mentioned nodes. Every name here is one the Sentinel declares
    elsewhere, so citing it in the rationale is correct, not a hallucination.
    """
    out: set[str] = set()
    spec = _get_spec(sentinel)
    out.update(str(k) for k in (spec.get("inputs") or {}))
    for entry in (spec.get("capabilities") or {}).get("required") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            out.add(entry["id"])
            # Capability ids carry a dotted prefix; the tail alone is what
            # tends to appear in prose.
            out.add(entry["id"].split(".", 1)[-1])
    for node in _get_nodes(sentinel).values():
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") or {}
        if isinstance(cfg.get("toolRef"), str):
            out.add(cfg["toolRef"])
            out.add(cfg["toolRef"].split(".", 1)[-1])
        finding = cfg.get("finding") or {}
        if isinstance(finding, dict) and isinstance(finding.get("type"), str):
            out.add(finding["type"])
    purpose = spec.get("purpose") or {}
    if isinstance(purpose, dict) and isinstance(purpose.get("conclusionType"), str):
        out.add(purpose["conclusionType"])
    return out


def check_r4(sentinel: dict, rationale: str = "") -> RatificationResult:
    """R4 — every node id cited in rationale.md exists in sentinel.yaml."""
    if not rationale:
        return RatificationResult("R4", "PASS", "no rationale supplied; nothing to cross-check")
    nodes = set(_get_nodes(sentinel).keys())
    not_nodes = _declared_non_node_identifiers(sentinel)
    # Extract candidate node-id-shaped tokens from rationale: dashed identifiers
    # inside backticks (`foo-bar`). Only flag when the citation appears near
    # the word "node" to reduce false positives against arbitrary backticked
    # code/paths.
    hallucinated: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"`([A-Za-z][A-Za-z0-9_-]{2,})`", rationale):
        cand = m.group(1)
        if cand in seen:
            continue
        if cand in nodes:
            continue
        # Names the Sentinel declares elsewhere (finding types, capability ids,
        # input names) are legitimate citations, not hallucinated node ids.
        if cand in not_nodes:
            continue
        # Skip tokens that couldn't be node ids: containing dots/slashes, upper-case,
        # or lacking a dash (Sentinel node ids are kebab-case).
        if "." in cand or "/" in cand or cand != cand.lower() or "-" not in cand:
            continue
        if len(cand) < 4:
            continue
        start = m.start()
        window = rationale[max(0, start - 60) : start + 60].lower()
        if "node" in window:
            hallucinated.append(cand)
            seen.add(cand)
    if hallucinated:
        return RatificationResult(
            rule="R4",
            verdict="FAIL",
            detail=f"node ids cited in rationale but absent from sentinel.yaml: {hallucinated}",
        )
    return RatificationResult("R4", "PASS", "all cited node ids resolve")


_ALLOWED_DEDUPE_REF_RE = re.compile(
    r"^(?:inputs\.[A-Za-z_][A-Za-z0-9_.-]*|nodes\.[A-Za-z_][A-Za-z0-9_-]*\.output(?:\.[A-Za-z_][A-Za-z0-9_.-]*)?)$"
)


def check_r5(sentinel: dict) -> RatificationResult:
    """R5 — emit dedupeKey must be stable: only ${inputs.*} / ${nodes.*.output.*} + literals."""
    nodes = _get_nodes(sentinel)
    bad: list[str] = []
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "emit":
            continue
        finding = (node.get("config") or {}).get("finding") or {}
        dedupe = finding.get("dedupeKey")
        if not isinstance(dedupe, str):
            bad.append(f"{nid}: dedupeKey missing")
            continue
        # Every ${...} must match the allowed shape.
        for m in _INTERPOLATION_RE.finditer(dedupe):
            inner = m.group(1).strip()
            if not _ALLOWED_DEDUPE_REF_RE.match(inner):
                bad.append(f"{nid}: {inner!r} not allowed in dedupeKey")
        # Explicit forbid on execution.now / uuid() even if not wrapped.
        if "execution.now" in dedupe or "uuid(" in dedupe:
            bad.append(f"{nid}: dedupeKey uses time-varying token")
    if bad:
        return RatificationResult(
            rule="R5",
            verdict="FAIL",
            detail="; ".join(bad),
        )
    return RatificationResult("R5", "PASS", "all emit dedupeKeys are stable")


def check_r6(sentinel: dict) -> RatificationResult:
    """R6 — toolRef ↔ capability referential integrity (no orphans, no dangling)."""
    spec = _get_spec(sentinel)
    declared = set()
    for entry in (spec.get("capabilities") or {}).get("required") or []:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            declared.add(entry["id"])
    used = set()
    for nid, node in _get_nodes(sentinel).items():
        if not isinstance(node, dict):
            continue
        ref = (node.get("config") or {}).get("toolRef")
        if isinstance(ref, str):
            used.add(ref)
    dangling = sorted(used - declared)
    orphan = sorted(declared - used)
    problems = []
    if dangling:
        problems.append(f"dangling toolRefs (referenced but undeclared): {dangling}")
    if orphan:
        problems.append(f"orphan capabilities (declared but unused): {orphan}")
    if problems:
        return RatificationResult("R6", "FAIL", "; ".join(problems))
    return RatificationResult("R6", "PASS", "capabilities and toolRefs align")


# --------------------------------------------------------------------------- #
# Aggregate                                                                    #
# --------------------------------------------------------------------------- #

def run_all(
    sentinel: dict, rationale: str = "", registry: dict | None = None
) -> list[RatificationResult]:
    """Run R1–R6 in order and return the results list.

    `registry` is the parsed common/capabilities-registry.yaml. Pass it whenever
    it is reachable: without it R2 can only check naming convention, which does
    not predict whether a capability binds at runtime.
    """
    return [
        check_r1(sentinel),
        check_r2(sentinel, registry),
        check_r3(sentinel, rationale),
        check_r4(sentinel, rationale),
        check_r5(sentinel),
        check_r6(sentinel),
    ]


def format_verdict_block(results: list[RatificationResult]) -> str:
    """Format results in the Stage 5.5 verdict-block shape CORE.md documents."""
    passed_all = all(r.verdict == "PASS" for r in results)
    lines = [f"VERDICT: {'RATIFIED' if passed_all else 'REVISE'}", ""]
    for r in results:
        lines.append(f"{r.rule} [{r.verdict}]: {r.detail}")
    if not passed_all:
        lines.append("")
        lines.append("If REVISE — fix list (mandatory, one entry per FAIL rule):")
        for r in results:
            if r.verdict == "FAIL":
                lines.append(f"  - {r.rule}: {r.detail}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI entrypoint — the cold Stage 5.5 subagent invokes this                    #
# --------------------------------------------------------------------------- #

def find_registry_path(start: "Path") -> "Path | None":
    """Walk up from `start` looking for common/capabilities-registry.yaml.

    Mirrors lint_remote.default_registry_path so the compiler's Stage 5.5 and
    sentinel-lint resolve the same registry from the same starting point.
    """
    from pathlib import Path as _Path

    cur = _Path(start).resolve()
    for _ in range(10):
        candidate = cur / "common" / "capabilities-registry.yaml"
        if candidate.exists():
            return candidate
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(
            "Usage: python3 ratification.py <sentinel.yaml> [rationale.md]\n"
            "Prints the Stage 5.5 verdict block. Exit 0 on RATIFIED, 1 on REVISE.",
            file=sys.stderr,
        )
        return 0 if argv else 2
    try:
        import yaml
    except ImportError:
        print("ratification.py CLI requires PyYAML; install it or import ratification from Python.", file=sys.stderr)
        return 2
    from pathlib import Path

    sentinel_path = Path(argv[0])
    rationale = ""
    if len(argv) > 1:
        rationale_path = Path(argv[1])
        if rationale_path.exists():
            rationale = rationale_path.read_text()
    doc = yaml.safe_load(sentinel_path.read_text())
    registry = None
    registry_path = find_registry_path(sentinel_path.parent)
    if registry_path is not None:
        registry = yaml.safe_load(registry_path.read_text())
    results = run_all(doc or {}, rationale, registry)
    print(format_verdict_block(results))
    return 0 if all(r.verdict == "PASS" for r in results) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_main(sys.argv[1:]))
