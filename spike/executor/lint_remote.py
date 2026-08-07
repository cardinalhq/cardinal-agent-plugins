"""sentinel-lint Phase 2 — remote-readiness checks (R7-R20).

Only fires when `metadata.deployment.mode: remote` in sentinel.yaml. Loads the
sibling deployment.yaml + the shared capabilities registry, JSON-Schema-
validates deployment.yaml against common/deployment-schema.yaml, then applies
R7-R20 per the sentinel-lint plan v0.4.

The `lint_remote` entry-point returns a `LintResult` (imported from lint.py)
whose findings each carry code, severity, file, line (usually None), message
and fix — same shape as Phase 1.

The policy path defaults to `<repo-root>/common/integrations.yaml`
resolved by walking up from the sentinel_dir; a caller can override via the
`registry_path` argument (surfaced as `--registry` on the CLI).
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

# Executor sits next to us; lint.py exports the shared LintFinding/LintResult.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import capabilities as capabilities_mod  # noqa: E402 — R10 provider resolvability
from lint import LintFinding, LintResult  # noqa: E402


# --------------------------------------------------------------------------- #
# Constants / small helpers                                                    #
# --------------------------------------------------------------------------- #

# R14 secret patterns — literal-looking secrets that must be *_ref instead.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-key", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("slack-bot-token", re.compile(r"xoxb-[0-9a-zA-Z-]+")),
    ("github-personal-token", re.compile(r"ghp_[a-zA-Z0-9]{20,}")),
    ("pem-private-key", re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)

# R8 disallowed direct imports in remote-mode function bodies.
_R8_DISALLOWED_IMPORTS = frozenset(
    {"subprocess", "socket", "urllib.request", "http.client", "importlib"}
)
# Call-shape targets that trigger R8 in addition to bare imports.
# `os.system`, `exec`, `eval`, and dynamic `open(...)` are the sensitive verbs.
_R8_DISALLOWED_CALLS_ATTR = frozenset({("os", "system")})
_R8_DISALLOWED_CALLS_NAME = frozenset({"exec", "eval"})

# Attachment input types that require size/MIME caps under R11.
_ATTACHMENT_TYPES = frozenset({"image", "pdf", "binary"})


# --------------------------------------------------------------------------- #
# Registry / schema location helpers                                           #
# --------------------------------------------------------------------------- #

def _find_repo_root(start: Path) -> Path | None:
    """Walk up from `start` looking for common/integrations.yaml.

    Returns the parent dir (i.e. repo root) or None if not found.
    """
    cur = start.resolve()
    for _ in range(10):
        if (cur / "common" / "integrations.yaml").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def default_registry_path(sentinel_dir: Path) -> Path | None:
    """Default path of the integrations POLICY file (common/integrations.yaml).

    The parameter keeps its historical `registry` name through the call chain,
    but there is no capability registry anymore — capability inventories are
    transcript-derived (CORE.md Stage 2.1). What this file carries is policy:
    approved LLM models, runtime/channel/sink integrations.
    """
    root = _find_repo_root(Path(sentinel_dir))
    if root is None:
        return None
    return root / "common" / "integrations.yaml"


def default_schema_path(sentinel_dir: Path) -> Path | None:
    root = _find_repo_root(Path(sentinel_dir))
    if root is None:
        return None
    return root / "common" / "deployment-schema.yaml"


def _flatten_llm_providers(registry: dict) -> set[str]:
    """The approved-model allowlist: `llmModels:` in common/integrations.yaml."""
    return {m for m in registry.get("llmModels") or [] if isinstance(m, str)}


def _runtime_flag(registry: dict, runtime_id: str, flag: str) -> Any:
    for r in (registry.get("integrations") or {}).get("runtime") or []:
        if isinstance(r, dict) and r.get("id") == runtime_id:
            return r.get(flag)
    return None


# --------------------------------------------------------------------------- #
# Deployment-mode gate                                                         #
# --------------------------------------------------------------------------- #

def sentinel_deployment_mode(sentinel: dict) -> str:
    """`metadata.deployment.mode` or `local` when absent (plan v0.4 default)."""
    meta = sentinel.get("metadata") or {}
    dep = meta.get("deployment") or {}
    return dep.get("mode") or "local"


# --------------------------------------------------------------------------- #
# Value walkers                                                                #
# --------------------------------------------------------------------------- #

def _walk_strings_with_path(value: Any, path: str = ""):
    """Yield (dotted-path, string) tuples for every string leaf."""
    if isinstance(value, str):
        yield (path, value)
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from _walk_strings_with_path(v, f"{path}.{k}" if path else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk_strings_with_path(v, f"{path}[{i}]")


# --------------------------------------------------------------------------- #
# Individual checks                                                            #
# --------------------------------------------------------------------------- #

def _check_r7(sentinel: dict) -> list[LintFinding]:
    """R7 — no bash.* capabilities in remote-mode Sentinels."""
    findings: list[LintFinding] = []
    caps = ((sentinel.get("spec") or {}).get("capabilities") or {}).get("required") or []
    for c in caps:
        if not isinstance(c, dict):
            continue
        cid = c.get("id") or ""
        if isinstance(cid, str) and cid.startswith("bash."):
            findings.append(LintFinding(
                code="R7",
                severity="FAIL",
                file="sentinel.yaml",
                line=None,
                message=(
                    f"capability {cid!r} not remote-deployable "
                    f"(shell exec in unattended runtime)"
                ),
                fix=(
                    "rewrite to an abstract capability (observability.* / code.*) "
                    "or set metadata.deployment.mode = local"
                ),
            ))
    return findings


def _check_r8(sentinel_dir: Path, sentinel: dict) -> list[LintFinding]:
    """R8 — syntactic safety of function-node source files (direct imports only)."""
    findings: list[LintFinding] = []
    nodes = ((sentinel.get("spec") or {}).get("nodes") or {})
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "function":
            continue
        source = (node.get("config") or {}).get("source")
        if not isinstance(source, str) or not source.endswith(".py"):
            continue
        candidates = [sentinel_dir / source, sentinel_dir / source.replace("-", "_")]
        src_path = next((p for p in candidates if p.exists()), None)
        if src_path is None:
            continue  # FUNC-MISSING already fires in Phase 1
        try:
            source_text = src_path.read_text()
            tree = ast.parse(source_text, filename=str(src_path))
        except SyntaxError:
            continue  # FUNC-PARSE already fires in Phase 1
        # Collect line-level `# lint-allow: <symbol> # <rationale>` opt-outs.
        allowed: dict[int, set[str]] = {}
        for i, line in enumerate(source_text.splitlines(), start=1):
            m = re.search(r"#\s*lint-allow:\s*([A-Za-z0-9_.]+)\s*#\s*.+", line)
            if m:
                allowed.setdefault(i, set()).add(m.group(1))

        def _allowed(symbol: str, lineno: int) -> bool:
            return symbol in allowed.get(lineno, set())

        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for alias in n.names:
                    name = alias.name
                    for banned in _R8_DISALLOWED_IMPORTS:
                        if name == banned or name.startswith(banned + "."):
                            if not _allowed(banned, n.lineno):
                                findings.append(_r8_finding(src_path, n.lineno, banned))
            elif isinstance(n, ast.ImportFrom):
                mod = n.module or ""
                for banned in _R8_DISALLOWED_IMPORTS:
                    if mod == banned or mod.startswith(banned + "."):
                        if not _allowed(banned, n.lineno):
                            findings.append(_r8_finding(src_path, n.lineno, banned))
            elif isinstance(n, ast.Call):
                func = n.func
                if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                    key = (func.value.id, func.attr)
                    if key in _R8_DISALLOWED_CALLS_ATTR:
                        sym = f"{key[0]}.{key[1]}"
                        if not _allowed(sym, n.lineno):
                            findings.append(_r8_finding(src_path, n.lineno, sym))
                elif isinstance(func, ast.Name):
                    if func.id in _R8_DISALLOWED_CALLS_NAME:
                        if not _allowed(func.id, n.lineno):
                            findings.append(_r8_finding(src_path, n.lineno, func.id))
                    elif func.id == "open":
                        # Dynamic open — reject when the first arg is not a
                        # plain string literal (constant paths are fine).
                        first = n.args[0] if n.args else None
                        if first is not None and not (
                            isinstance(first, ast.Constant)
                            and isinstance(first.value, str)
                        ):
                            if not _allowed("open", n.lineno):
                                findings.append(_r8_finding(src_path, n.lineno, "open"))
    return findings


def _r8_finding(src: Path, line: int, symbol: str) -> LintFinding:
    return LintFinding(
        code="R8",
        severity="FAIL",
        file=str(src),
        line=line,
        message=(
            f"{src}:{line} uses {symbol!r} (disallowed in remote runtime)"
        ),
        fix=(
            f"rewrite to a hosted capability, or annotate this line "
            f"'# lint-allow: {symbol} # <why>' with rationale"
        ),
    )


def _check_r9(sentinel: dict, deployment: dict) -> list[LintFinding]:
    """R9 — every ask_human has an answerSchema + matching deployment binding."""
    findings: list[LintFinding] = []
    nodes = ((sentinel.get("spec") or {}).get("nodes") or {})
    bindings = deployment.get("askHumanBindings") or {}
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "ask_human":
            continue
        cfg = node.get("config") or {}
        if not cfg.get("answerSchema"):
            findings.append(LintFinding(
                code="R9",
                severity="FAIL",
                file="sentinel.yaml",
                line=None,
                message=f"ask_human node {nid!r} has no answerSchema",
                fix=f"declare `answerSchema:` under spec.nodes.{nid}.config",
            ))
        if nid not in bindings:
            findings.append(LintFinding(
                code="R9",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=f"ask_human node {nid!r} has no binding in deployment.yaml",
                fix=(
                    f"add askHumanBindings.{nid} with channel_ref, "
                    f"identity_policy, reply_normalization"
                ),
            ))
    return findings


def _check_r10(
    sentinel: dict, deployment: dict, registry: dict
) -> list[LintFinding]:
    """R10 — capability ↔ binding ↔ registry coherence + fixture gating."""
    findings: list[LintFinding] = []
    required = ((sentinel.get("spec") or {}).get("capabilities") or {}).get("required") or []
    declared_caps = [c.get("id") for c in required if isinstance(c, dict)]
    bindings = deployment.get("capabilityBindings") or {}
    allow_fixtures = bool((deployment.get("execution") or {}).get("allowFixtures"))

    # LLM capabilities are bound via llmBindings (R13) rather than
    # capabilityBindings, so we do NOT demand a capabilityBindings entry
    # for `llm.*` here. R13 covers those.
    for cap_id in declared_caps:
        if not isinstance(cap_id, str):
            continue
        if cap_id.startswith("llm."):
            continue
        if cap_id not in bindings:
            findings.append(LintFinding(
                code="R10",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=(
                    f"capability {cap_id!r} declared in spec.capabilities.required "
                    f"but has no deployment.yaml capabilityBindings entry"
                ),
                fix=f"add capabilityBindings.{cap_id} with provider/credential_ref",
            ))

    # Every binding must reference a declared capability + registered provider.
    for cap_id, binding in bindings.items():
        if not isinstance(binding, dict):
            continue
        if cap_id not in declared_caps:
            findings.append(LintFinding(
                code="R10",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=(
                    f"capabilityBindings.{cap_id} has no matching entry "
                    f"in spec.capabilities.required"
                ),
                fix=(
                    f"declare {cap_id!r} under spec.capabilities.required "
                    f"or drop the orphan binding"
                ),
            ))
            continue
        provider = binding.get("provider")
        if provider == "fixture":
            if not allow_fixtures:
                findings.append(LintFinding(
                    code="R10",
                    severity="FAIL",
                    file="deployment.yaml",
                    line=None,
                    message=(
                        f"capability {cap_id!r} provider 'fixture' used in remote-mode "
                        f"without execution.allowFixtures: true"
                    ),
                    fix=(
                        "set execution.allowFixtures: true for pinned-fixture staging, "
                        "or bind to a real provider"
                    ),
                ))
            continue
        # There is no capability registry to consult — the question that
        # predicts a runtime failure is whether the runtime can resolve this
        # (capability, provider) pair, and the provider registrations in
        # capabilities.py are that answer. They cannot drift from the
        # implementations because they ARE the implementations.
        if not isinstance(provider, str) or not capabilities_mod.provider_is_resolvable(
            cap_id, provider
        ):
            registered = sorted(
                {p for _, p in capabilities_mod.registered_providers()}
                | set(capabilities_mod._UNIVERSAL_PROVIDERS)
            )
            findings.append(LintFinding(
                code="R10",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=(
                    f"capability {cap_id!r} provider {provider!r} is not "
                    f"resolvable by the runtime (registered providers: "
                    f"{registered})"
                ),
                fix=(
                    f"bind to a provider the runtime implements ({registered}), "
                    f"or implement {provider!r} under spike/executor/providers/"
                ),
            ))
    return findings


def _check_r11(sentinel: dict, deployment: dict) -> list[LintFinding]:
    """R11 — every input has a source binding; attachment-type inputs need caps."""
    findings: list[LintFinding] = []
    inputs = ((sentinel.get("spec") or {}).get("inputs") or {})
    bindings = deployment.get("inputBindings") or {}
    for name, spec in inputs.items():
        if not isinstance(spec, dict):
            continue
        binding = bindings.get(name)
        if binding is None:
            findings.append(LintFinding(
                code="R11",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=(
                    f"input {name!r} declared in spec.inputs has no "
                    f"inputBindings entry"
                ),
                fix=(
                    f"add inputBindings.{name} with `source: webhook.<path>` | "
                    f"`cron.<literal>` | `dispatch`"
                ),
            ))
            continue
        source = binding.get("source")
        itype = spec.get("type")
        if itype in _ATTACHMENT_TYPES:
            if not (isinstance(source, str) and source.startswith("webhook.")):
                findings.append(LintFinding(
                    code="R11",
                    severity="FAIL",
                    file="deployment.yaml",
                    line=None,
                    message=(
                        f"input {name!r} type={itype} must be sourced from "
                        f"webhook.<path> (got source={source!r})"
                    ),
                    fix=f"set inputBindings.{name}.source: webhook.<field-path>",
                ))
            if not isinstance(binding.get("maxSizeBytes"), int):
                findings.append(LintFinding(
                    code="R11",
                    severity="FAIL",
                    file="deployment.yaml",
                    line=None,
                    message=(
                        f"input {name!r} has type={itype} but no maxSizeBytes cap"
                    ),
                    fix=f"set inputBindings.{name}.maxSizeBytes: <bytes>",
                ))
            mimes = binding.get("mimeTypes")
            if not (isinstance(mimes, list) and mimes):
                findings.append(LintFinding(
                    code="R11",
                    severity="FAIL",
                    file="deployment.yaml",
                    line=None,
                    message=(
                        f"input {name!r} has type={itype} but no non-empty mimeTypes"
                    ),
                    fix=f"set inputBindings.{name}.mimeTypes: [mime/type, ...]",
                ))
    return findings


def _check_r12(sentinel: dict, deployment: dict) -> list[LintFinding]:
    """R12 — every emit node covered by findingsRouting; warn on shadowed rules."""
    findings: list[LintFinding] = []
    nodes = ((sentinel.get("spec") or {}).get("nodes") or {})
    emit_ids = [
        nid for nid, n in nodes.items()
        if isinstance(n, dict) and n.get("kind") == "emit"
    ]
    routing = deployment.get("findingsRouting") or []
    covered: set[str] = set()
    have_catch_all = False
    catch_all_index: int | None = None
    for i, rule in enumerate(routing):
        match = (rule or {}).get("match") or {}
        if match.get("*") is True:
            if have_catch_all:
                findings.append(LintFinding(
                    code="R12",
                    severity="WARN",
                    file="deployment.yaml",
                    line=None,
                    message=(
                        f"findingsRouting[{i}] is a redundant catch-all; "
                        f"earlier catch-all at index {catch_all_index} already matches everything"
                    ),
                    fix="remove the duplicate catch-all rule",
                ))
            have_catch_all = True
            catch_all_index = i
            covered.update(emit_ids)
            continue
        # A rule appearing AFTER a catch-all is shadowed dead code.
        if have_catch_all and "emitNode" in match:
            findings.append(LintFinding(
                code="R12",
                severity="WARN",
                file="deployment.yaml",
                line=None,
                message=(
                    f"findingsRouting[{i}] is shadowed — earlier catch-all "
                    f"at index {catch_all_index} always matches first"
                ),
                fix=f"move this rule above the catch-all, or drop it",
            ))
        eid = match.get("emitNode")
        if eid is not None:
            covered.add(eid)
    for eid in emit_ids:
        if eid not in covered:
            findings.append(LintFinding(
                code="R12",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=f"emit node {eid!r} matches no findingsRouting rule",
                fix=(
                    f"add a findingsRouting entry with match.emitNode: {eid} "
                    f"or a catch-all match: '*': true"
                ),
            ))
    return findings


def _check_r13(sentinel: dict, deployment: dict, registry: dict) -> list[LintFinding]:
    """R13 — every llm node has an llmBindings.model resolving to registry."""
    findings: list[LintFinding] = []
    nodes = ((sentinel.get("spec") or {}).get("nodes") or {})
    bindings = deployment.get("llmBindings") or {}
    llm_providers = _flatten_llm_providers(registry)
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "llm":
            continue
        binding = bindings.get(nid)
        if binding is None:
            findings.append(LintFinding(
                code="R13",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=f"llm node {nid!r} has no llmBindings entry",
                fix=f"add llmBindings.{nid}.model: <registered llm provider>",
            ))
            continue
        model = binding.get("model")
        if model not in llm_providers:
            model_class = (node.get("config") or {}).get("modelClass")
            hint_cap = f"llm.{model_class}" if model_class else "llm.*"
            findings.append(LintFinding(
                code="R13",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=(
                    f"llm node {nid!r} model {model!r} not in "
                    f"integrations.yaml llmModels {hint_cap}"
                ),
                fix=(
                    f"pick a provider registered under capabilities.{hint_cap} "
                    f"or add {model!r} to the registry"
                ),
            ))
    return findings


def _check_r14(deployment: dict) -> list[LintFinding]:
    """R14 — no literal secrets in deployment.yaml string values."""
    findings: list[LintFinding] = []
    for path, s in _walk_strings_with_path(deployment):
        for label, pat in _SECRET_PATTERNS:
            if pat.search(s):
                findings.append(LintFinding(
                    code="R14",
                    severity="FAIL",
                    file="deployment.yaml",
                    line=None,
                    message=(
                        f"deployment.yaml {path} looks like a literal {label}; "
                        f"use a *_ref: env://... | k8s-secret://... | vault://..."
                    ),
                    fix=(
                        f"replace the literal at {path} with an indirection "
                        f"(e.g. token_ref: env://SLACK_BOT_TOKEN)"
                    ),
                ))
                break
    return findings


def _check_r15(sentinel: dict, deployment: dict, registry: dict) -> list[LintFinding]:
    """R15 — runtime timeout compatibility with ask_human nodes."""
    findings: list[LintFinding] = []
    runtime_id = deployment.get("runtime")
    allows = _runtime_flag(registry, runtime_id, "allowsBlockUntilAnswered")
    if allows is None:
        findings.append(LintFinding(
            code="R15",
            severity="FAIL",
            file="deployment.yaml",
            line=None,
            message=(
                f"runtime {runtime_id!r} not present in "
                f"integrations.yaml integrations.runtime[]"
            ),
            fix=(
                f"pick a registered runtime id "
                f"(k8s-controller | ci-plugin | daemon | manual) "
                f"or add {runtime_id!r} to the registry"
            ),
        ))
        return findings
    if allows:
        return findings  # runtime allows all modes; nothing to check.
    nodes = ((sentinel.get("spec") or {}).get("nodes") or {})
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "ask_human":
            continue
        timeout = (node.get("config") or {}).get("timeout") or {}
        mode = timeout.get("mode") if isinstance(timeout, dict) else None
        if mode == "block-until-answered":
            findings.append(LintFinding(
                code="R15",
                severity="FAIL",
                file="sentinel.yaml",
                line=None,
                message=(
                    f"ask_human {nid!r} has timeout.mode=block-until-answered "
                    f"but runtime={runtime_id} cannot suspend indefinitely"
                ),
                fix=(
                    "set timeout.mode: fall-through-default with a bounded "
                    "maxWait, or switch runtime to k8s-controller/daemon"
                ),
            ))
    return findings


def _check_r17(sentinel: dict, mode: str) -> list[LintFinding]:
    """R17 — compiler-declared ratification.status: revise blocks remote deploy.

    Severity: FAIL for remote, WARN for local (per plan v0.2).
    """
    ratification = ((sentinel.get("metadata") or {}).get("ratification") or {})
    if ratification.get("status") != "revise":
        return []
    unresolved = ratification.get("unresolved") or []
    severity = "FAIL" if mode == "remote" else "WARN"
    return [LintFinding(
        code="R17",
        severity=severity,
        file="sentinel.yaml",
        line=None,
        message=(
            f"sentinel carries compiler-declared ratification=revise with "
            f"unresolved: {unresolved}; block remote deploy"
        ),
        fix=(
            "re-run mechanize Stage 5.5 with cold ratification enabled or "
            "hand-fix the flagged items and clear metadata.ratification"
        ),
    )]


def _check_r18(sentinel_dir: Path, sentinel: dict, deployment: dict) -> list[LintFinding]:
    """R18 — function-node runtime capabilities match source annotations."""
    findings: list[LintFinding] = []
    nodes = ((sentinel.get("spec") or {}).get("nodes") or {})
    functions_block = deployment.get("functions") or {}

    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "function":
            continue
        fn = functions_block.get(nid)
        if not isinstance(fn, dict):
            findings.append(LintFinding(
                code="R18",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=(
                    f"function node {nid!r} has no functions.{nid} block "
                    f"declaring network/filesystem"
                ),
                fix=(
                    f"add functions.{nid}: {{network: disabled, filesystem: none}} "
                    f"(defaults) or elevate with explicit values"
                ),
            ))
            continue
        for req in ("network", "filesystem"):
            if req not in fn:
                findings.append(LintFinding(
                    code="R18",
                    severity="FAIL",
                    file="deployment.yaml",
                    line=None,
                    message=(
                        f"function node {nid!r} functions.{nid} block "
                        f"missing required key {req!r}"
                    ),
                    fix=f"add functions.{nid}.{req}: <value>",
                ))

        network = fn.get("network", "disabled")
        filesystem = fn.get("filesystem", "none")

        source = (node.get("config") or {}).get("source")
        if not isinstance(source, str):
            continue
        candidates = [sentinel_dir / source, sentinel_dir / source.replace("-", "_")]
        src_path = next((p for p in candidates if p.exists()), None)
        if src_path is None:
            continue  # FUNC-MISSING fires in Phase 1
        source_text = src_path.read_text()

        # Grammar: `# runtime-cap: <key>=<value> # <rationale>`
        declared: dict[str, str] = {}
        for line in source_text.splitlines():
            m = re.search(
                r"#\s*runtime-cap:\s*([A-Za-z_][A-Za-z0-9_]*)=([A-Za-z0-9_-]+)\s*#\s*.+",
                line,
            )
            if m:
                declared[m.group(1)] = m.group(2)

        # Non-default declarations in deployment.yaml require matching source
        # annotations. Defaults require nothing.
        checks: list[tuple[str, str, str]] = [
            ("network", network, "disabled"),
            ("filesystem", filesystem, "none"),
        ]
        for key, deployed, default_val in checks:
            if deployed == default_val:
                if key in declared and declared[key] != default_val:
                    findings.append(LintFinding(
                        code="R18",
                        severity="FAIL",
                        file=str(src_path),
                        line=None,
                        message=(
                            f"{src_path} declares '# runtime-cap: {key}={declared[key]}' "
                            f"but deployment.yaml functions.{nid}.{key}={deployed}"
                        ),
                        fix=(
                            f"either raise functions.{nid}.{key} to {declared[key]!r} "
                            f"or remove the source annotation"
                        ),
                    ))
                continue
            if declared.get(key) != deployed:
                findings.append(LintFinding(
                    code="R18",
                    severity="FAIL",
                    file=str(src_path),
                    line=None,
                    message=(
                        f"{src_path} does not declare "
                        f"'# runtime-cap: {key}={deployed}' but deployment.yaml "
                        f"functions.{nid}.{key}={deployed}"
                    ),
                    fix=(
                        f"add '# runtime-cap: {key}={deployed} # <rationale>' "
                        f"to {source} (or drop the elevation from deployment.yaml)"
                    ),
                ))
    return findings


def _check_r19(deployment: dict, registry: dict) -> list[LintFinding]:
    """R19 — every parserModel + defaultParserModel resolves to capabilities.llm.*"""
    findings: list[LintFinding] = []
    llm_providers = _flatten_llm_providers(registry)
    default = deployment.get("defaultParserModel")
    if default is not None and default not in llm_providers:
        findings.append(LintFinding(
            code="R19",
            severity="FAIL",
            file="deployment.yaml",
            line=None,
            message=(
                f"runtime.defaultParserModel {default!r} not in "
                f"integrations.yaml llmModels"
            ),
            fix=(
                f"pick a provider registered under capabilities.llm.* "
                f"or add {default!r} to the registry"
            ),
        ))
    for nid, binding in (deployment.get("askHumanBindings") or {}).items():
        if not isinstance(binding, dict):
            continue
        pm = binding.get("parserModel")
        if pm is None:
            continue
        if pm not in llm_providers:
            findings.append(LintFinding(
                code="R19",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=(
                    f"askHumanBindings.{nid}.parserModel {pm!r} not in "
                    f"integrations.yaml llmModels"
                ),
                fix=(
                    f"pick a provider registered under capabilities.llm.* "
                    f"or add {pm!r} to the registry"
                ),
            ))
    return findings


def _check_r20(sentinel: dict, deployment: dict) -> list[LintFinding]:
    """R20 — prose-llm-parse ask_humans must have a parserModel resolvable."""
    findings: list[LintFinding] = []
    nodes = ((sentinel.get("spec") or {}).get("nodes") or {})
    bindings = deployment.get("askHumanBindings") or {}
    default = deployment.get("defaultParserModel")
    for nid, node in nodes.items():
        if not isinstance(node, dict) or node.get("kind") != "ask_human":
            continue
        binding = bindings.get(nid) or {}
        rn = binding.get("reply_normalization")
        if rn != "prose-llm-parse":
            continue
        per_node = binding.get("parserModel")
        if not per_node and not default:
            findings.append(LintFinding(
                code="R20",
                severity="FAIL",
                file="deployment.yaml",
                line=None,
                message=(
                    f"ask_human {nid!r} has reply_normalization=prose-llm-parse "
                    f"but no parserModel binding (and no runtime.defaultParserModel)"
                ),
                fix=(
                    f"set askHumanBindings.{nid}.parserModel: <registered llm provider> "
                    f"or set top-level defaultParserModel"
                ),
            ))
    return findings


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def lint_remote(
    sentinel_dir: Path,
    sentinel: dict,
    *,
    registry_path: Path | None = None,
    schema_path: Path | None = None,
    force: bool = False,
) -> LintResult:
    """Run Phase 2 remote-readiness checks.

    Gated on `metadata.deployment.mode: remote` (unless `force=True`, useful
    for tests that want to exercise the check bodies without setting the
    mode). Missing deployment.yaml when gated → DEPLOY-MISSING FAIL. Missing
    policy file → POLICY-MISSING FAIL. Deployment schema violations →
    DEPLOY-SCHEMA FAIL. Otherwise emits R7-R20 findings.
    """
    result = LintResult()
    sentinel_dir = Path(sentinel_dir)

    mode = sentinel_deployment_mode(sentinel)
    # R17 also fires in local mode as WARN — handle before the gate.
    result.findings.extend(_check_r17(sentinel, mode))
    if mode != "remote" and not force:
        return result

    dep_path = sentinel_dir / "deployment.yaml"
    if not dep_path.exists():
        result.findings.append(LintFinding(
            code="DEPLOY-MISSING",
            severity="FAIL",
            file=str(dep_path),
            line=None,
            message=(
                f"metadata.deployment.mode=remote but no deployment.yaml "
                f"found at {dep_path}"
            ),
            fix=(
                "add deployment.yaml with capability/channel/sink bindings "
                "(see common/deployment-schema.yaml for the shape)"
            ),
        ))
        return result

    reg_path = Path(registry_path) if registry_path else default_registry_path(sentinel_dir)
    sch_path = Path(schema_path) if schema_path else default_schema_path(sentinel_dir)
    if reg_path is None or not reg_path.exists():
        result.findings.append(LintFinding(
            code="POLICY-MISSING",
            severity="FAIL",
            file=str(reg_path) if reg_path else "common/integrations.yaml",
            line=None,
            message=(
                "integrations.yaml not found; pass --registry <path> "
                "or run lint from inside the repo"
            ),
            fix=(
                "supply --registry pointing at common/integrations.yaml, "
                "or invoke lint from the repo checkout"
            ),
        ))
        return result
    if sch_path is None or not sch_path.exists():
        result.findings.append(LintFinding(
            code="DEPLOY-SCHEMA-MISSING",
            severity="FAIL",
            file=str(sch_path) if sch_path else "common/deployment-schema.yaml",
            line=None,
            message="deployment-schema.yaml not found",
            fix="restore common/deployment-schema.yaml or pass --schema <path>",
        ))
        return result

    try:
        with reg_path.open() as f:
            registry = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        result.findings.append(LintFinding(
            code="POLICY-PARSE",
            severity="FAIL",
            file=str(reg_path),
            line=None,
            message=f"failed to parse integrations.yaml: {e}",
            fix="fix the YAML syntax in the registry",
        ))
        return result

    try:
        with dep_path.open() as f:
            deployment = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        result.findings.append(LintFinding(
            code="DEPLOY-PARSE",
            severity="FAIL",
            file=str(dep_path),
            line=getattr(getattr(e, "problem_mark", None), "line", None),
            message=f"failed to parse deployment.yaml: {e}",
            fix="fix the YAML syntax in deployment.yaml",
        ))
        return result

    try:
        with sch_path.open() as f:
            schema = yaml.safe_load(f)
    except yaml.YAMLError as e:
        result.findings.append(LintFinding(
            code="DEPLOY-SCHEMA-PARSE",
            severity="FAIL",
            file=str(sch_path),
            line=None,
            message=f"failed to parse deployment-schema.yaml: {e}",
            fix="fix the JSON Schema file",
        ))
        return result

    # JSON-Schema-validate deployment.yaml against the shared shape.
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(deployment), key=lambda e: list(e.path))
    for err in errors:
        loc = ".".join(str(p) for p in err.absolute_path) or "<root>"
        result.findings.append(LintFinding(
            code="DEPLOY-SCHEMA",
            severity="FAIL",
            file=str(dep_path),
            line=None,
            message=f"deployment.yaml schema violation at {loc}: {err.message}",
            fix=(
                "align deployment.yaml with common/deployment-schema.yaml; "
                "see the property description for the expected shape"
            ),
        ))
    if errors:
        # Bail early — subsequent checks assume a validly-shaped document.
        return result

    result.findings.extend(_check_r7(sentinel))
    result.findings.extend(_check_r8(sentinel_dir, sentinel))
    result.findings.extend(_check_r9(sentinel, deployment))
    result.findings.extend(_check_r10(sentinel, deployment, registry))
    result.findings.extend(_check_r11(sentinel, deployment))
    result.findings.extend(_check_r12(sentinel, deployment))
    result.findings.extend(_check_r13(sentinel, deployment, registry))
    result.findings.extend(_check_r14(deployment))
    result.findings.extend(_check_r15(sentinel, deployment, registry))
    result.findings.extend(_check_r18(sentinel_dir, sentinel, deployment))
    result.findings.extend(_check_r19(deployment, registry))
    result.findings.extend(_check_r20(sentinel, deployment))

    return result


__all__ = [
    "lint_remote",
    "sentinel_deployment_mode",
    "default_registry_path",
    "default_schema_path",
]
