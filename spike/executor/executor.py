"""Spike executor for the mechanize.dev Sentinel DAG.

Scope: minimum viable runtime to prove that a compiled Sentinel DAG can be
executed reliably against a live target. Not a production engine.

Design choices worth stating:

* No LLM runtime. LLM nodes are supported only via the ``when:`` gate — if
  a node's ``when`` evaluates false the node is SKIPPED; a required LLM
  node would raise LlmUnavailableError. v2's single LLM node is gated by
  ``inputs.codeRepoPath != null``, so leaving that input unset skips it.
* Tool nodes call capability bindings defined in ``capabilities.py``. The
  ``observability.query-metrics`` binding reads pre-populated cache files,
  written by a driver that runs the Cardinal MCP tool. See capabilities.py.
* Expressions use a small AST-restricted evaluator supporting:
  attribute access on inputs/nodes/execution, comparisons, boolean ops,
  ternary, arithmetic on numbers and datetimes, null checks, and the
  ``join(list, sep)`` helper the spec's v2 skill notes as a "subset B"
  addition to §13. The wider spec ambiguity is flagged in RELIABILITY.md.
* Findings are deduped per §14: ``sha256(sentinel_yaml)`` +
  ``variation_digest (empty)`` + ``rendered_dedupeKey``.

Usage:

    python executor.py plan   --sentinel <path> --inputs <path> --run <path>
    python executor.py execute --sentinel <path> --inputs <path> --run <path>

The ``plan`` phase resolves all root tool-node arguments and writes them to
``<run>/pending-queries/<node-id>.json``. A driver populates
``<run>/tool-cache/<node-id>.json`` with the corresponding MCP responses.
``execute`` reads the cache and runs the full DAG.
"""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import operator
import re
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from importlib import util as importlib_util
from pathlib import Path
from typing import Any, Callable

import yaml
from jsonschema import Draft202012Validator

# Local imports
sys.path.insert(0, str(Path(__file__).resolve().parent))
import capabilities  # noqa: E402


# --------------------------------------------------------------------------- #
# Expression evaluator (restricted AST walk)                                  #
# --------------------------------------------------------------------------- #

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)\s*$")
_INTERPOLATION_RE = re.compile(r"\$\{([^}]+)\}")


def parse_duration(spec: str | int | float | timedelta) -> timedelta:
    if isinstance(spec, timedelta):
        return spec
    if isinstance(spec, (int, float)):
        return timedelta(seconds=float(spec))
    m = _DURATION_RE.match(str(spec))
    if not m:
        raise ValueError(f"unrecognized duration: {spec!r}")
    n = float(m.group(1))
    unit = m.group(2)
    return {
        "ms": timedelta(milliseconds=n),
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
    }[unit]


class ExpressionError(Exception):
    pass


class _Env:
    def __init__(self, inputs: dict, nodes: dict, execution: dict):
        self.inputs = inputs
        self.nodes = nodes
        self.execution = execution


_BIN_OPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_CMP_OPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
}


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _resolve_attr(target: Any, name: str) -> Any:
    if isinstance(target, dict):
        return target.get(name)
    return getattr(target, name, None)


def _eval_ast(node: ast.AST, env: _Env) -> Any:
    if isinstance(node, ast.Expression):
        return _eval_ast(node.body, env)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "inputs":
            return env.inputs
        if node.id == "nodes":
            return env.nodes
        if node.id == "execution":
            return env.execution
        if node.id in ("true", "True"):
            return True
        if node.id in ("false", "False"):
            return False
        if node.id in ("null", "None"):
            return None
        raise ExpressionError(f"unknown name: {node.id}")
    if isinstance(node, ast.Attribute):
        base = _eval_ast(node.value, env)
        if base is None:
            return None
        return _resolve_attr(base, node.attr)
    if isinstance(node, ast.Subscript):
        base = _eval_ast(node.value, env)
        key = _eval_ast(node.slice, env)
        if base is None:
            return None
        if isinstance(base, dict):
            return base.get(key)
        if isinstance(base, (list, tuple)):
            try:
                return base[int(key)]
            except (IndexError, ValueError):
                return None
        return None
    if isinstance(node, ast.BinOp):
        left = _eval_ast(node.left, env)
        right = _eval_ast(node.right, env)
        op_t = type(node.op)
        if op_t is ast.Sub and isinstance(left, datetime) and isinstance(right, timedelta):
            return left - right
        if op_t is ast.Add and isinstance(left, datetime) and isinstance(right, timedelta):
            return left + right
        if op_t is ast.Sub and isinstance(left, datetime) and isinstance(right, datetime):
            return left - right
        # Duration strings interpreted lazily when combined with datetimes.
        if isinstance(left, datetime) and isinstance(right, str):
            try:
                right = parse_duration(right)
            except ValueError:
                pass
        if isinstance(right, datetime) and isinstance(left, str):
            try:
                left = parse_duration(left)
            except ValueError:
                pass
        try:
            return _BIN_OPS[op_t](left, right)
        except KeyError as e:
            raise ExpressionError(f"unsupported binary op {op_t.__name__}") from e
    if isinstance(node, ast.UnaryOp):
        val = _eval_ast(node.operand, env)
        if isinstance(node.op, ast.Not):
            return not val
        if isinstance(node.op, ast.USub):
            return -val if val is not None else None
        if isinstance(node.op, ast.UAdd):
            return +val if val is not None else None
        raise ExpressionError(f"unsupported unary op {type(node.op).__name__}")
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval_ast(v, env)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            for v in node.values:
                r = _eval_ast(v, env)
                if r:
                    return r
            return _eval_ast(node.values[-1], env)
    if isinstance(node, ast.Compare):
        left = _eval_ast(node.left, env)
        result = True
        for op_node, comparator in zip(node.ops, node.comparators, strict=False):
            right = _eval_ast(comparator, env)
            op_t = type(op_node)
            if op_t in _CMP_OPS:
                try:
                    result = result and _CMP_OPS[op_t](left, right)
                except TypeError:
                    # e.g. comparing None with number: treat as False.
                    return False
            else:
                raise ExpressionError(f"unsupported comparator {op_t.__name__}")
            left = right
        return result
    if isinstance(node, ast.IfExp):
        cond = _eval_ast(node.test, env)
        return _eval_ast(node.body if cond else node.orelse, env)
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ExpressionError("only named function calls are supported")
        name = node.func.id
        args = [_eval_ast(a, env) for a in node.args]
        return _call_helper(name, args)
    raise ExpressionError(f"unsupported AST node {type(node).__name__}")


def _call_helper(name: str, args: list[Any]) -> Any:
    if name == "join":
        if len(args) != 2:
            raise ExpressionError("join(array, sep) takes exactly 2 args")
        arr, sep = args
        if arr is None:
            return ""
        return str(sep).join(str(x) for x in arr)
    if name == "abs":
        return abs(args[0])
    if name == "min":
        return min(*args) if len(args) > 1 else min(args[0])
    if name == "max":
        return max(*args) if len(args) > 1 else max(args[0])
    if name == "len":
        if args[0] is None:
            return 0
        return len(args[0])
    if name == "contains":
        haystack, needle = args
        if haystack is None:
            return False
        return needle in haystack
    raise ExpressionError(f"unknown helper function {name!r}")


_DASH_ID_RE = re.compile(r"\b(nodes|inputs|execution)\.([A-Za-z_][A-Za-z0-9_-]*)")


def _rewrite_dashed_ids(src: str) -> str:
    """Convert ``nodes.name-with-dash.attr`` → ``nodes["name-with-dash"].attr``.

    Sentinel node IDs are dash-separated by convention (§9). Python's parser
    reads ``foo-bar`` as ``foo - bar``. We rewrite the first path segment
    after ``nodes.`` / ``inputs.`` / ``execution.`` when it contains a dash.
    Subsequent attribute segments are ordinary identifiers.
    """
    def _sub(m: re.Match[str]) -> str:
        root, name = m.group(1), m.group(2)
        if "-" in name:
            return f'{root}["{name}"]'
        return m.group(0)

    return _DASH_ID_RE.sub(_sub, src)


def eval_expr(expr: str, env: _Env) -> Any:
    """Evaluate a bare expression (no ${...} wrapping)."""
    src = expr.strip()
    # Dashed node IDs → subscript form before we touch anything else.
    src = _rewrite_dashed_ids(src)
    # Rewrite `?:` ternary → Python `X if C else Y` since Python has no C-ternary.
    src = _rewrite_ternary(src)
    # Rewrite `&&`, `||`, `!` to Python operators.
    src = re.sub(r"&&", " and ", src)
    src = re.sub(r"\|\|", " or ", src)
    # Convert `!X` to `not X` but not `!=`.
    src = re.sub(r"(?<![=!<>])!(?!=)", " not ", src)
    src = re.sub(r"\bnull\b", "None", src)
    src = re.sub(r"\btrue\b", "True", src)
    src = re.sub(r"\bfalse\b", "False", src)
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ExpressionError(f"parse error {e!r} in {expr!r}") from e
    return _eval_ast(tree, env)


def _rewrite_ternary(src: str) -> str:
    """Rewrite `cond ? a : b` (recursively) to `a if cond else b`.

    Handles the paren-wrapped and one-line forms actually used in v2's YAML.
    Approach: find the outermost top-level `?` and its matching `:` skipping
    nested `?:` in string/paren context.
    """
    depth = 0
    q_idx = -1
    for i, ch in enumerate(src):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "?" and depth == 0:
            q_idx = i
            break
    if q_idx == -1:
        return src
    # Find matching top-level `:` skipping nested `?:`.
    depth = 0
    inner = 0
    c_idx = -1
    for i in range(q_idx + 1, len(src)):
        ch = src[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "?" and depth == 0:
            inner += 1
        elif ch == ":" and depth == 0:
            if inner == 0:
                c_idx = i
                break
            inner -= 1
    if c_idx == -1:
        return src
    cond = src[:q_idx].strip()
    then_part = _rewrite_ternary(src[q_idx + 1 : c_idx].strip())
    else_part = _rewrite_ternary(src[c_idx + 1 :].strip())
    return f"(({then_part}) if ({cond}) else ({else_part}))"


def render_template(text: str, env: _Env) -> Any:
    """Render a string with ``${...}`` interpolation.

    If the string is exactly ``${expr}`` (nothing else), return the raw
    evaluated value (preserves types). Otherwise stringify each match and
    substitute in place.

    Multiple ``${...}`` occurrences in a single string ARE supported —
    each is matched independently and evaluated in place. This covers the
    "nested interpolation" case the mechanize SKILL's expression-language
    section describes (e.g. a LogQL query embedding both a service-name
    reference and a bucket-duration reference)::

        sum by (level) (count_over_time({service_name="${nodes.X.output.name}"}[${inputs.bucket}]))

    Truly-nested syntax (``${outer${inner}}``) is NOT supported; the SKILL
    does not emit it and the executor treats the outer ``${...}`` as one
    interpolation that fails to parse. If a future compilation needs it,
    substitute inner-first at the compiler side.
    """
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    exact = _INTERPOLATION_RE.fullmatch(stripped)
    if exact:
        return eval_expr(exact.group(1), env)

    def _sub(m: re.Match[str]) -> str:
        val = eval_expr(m.group(1), env)
        if val is None:
            return "null"
        if isinstance(val, datetime):
            return _rfc3339(val)
        if isinstance(val, timedelta):
            return f"{int(val.total_seconds())}s"
        if isinstance(val, (list, tuple, dict)):
            return json.dumps(val)
        return str(val)

    return _INTERPOLATION_RE.sub(_sub, text)


def render_deep(value: Any, env: _Env) -> Any:
    if isinstance(value, str):
        return render_template(value, env)
    if isinstance(value, dict):
        return {k: render_deep(v, env) for k, v in value.items()}
    if isinstance(value, list):
        return [render_deep(v, env) for v in value]
    return value


# --------------------------------------------------------------------------- #
# DAG loader + validator                                                      #
# --------------------------------------------------------------------------- #

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "SKIPPED", "CANCELLED", "CACHED"}


class DagValidationError(Exception):
    pass


class LlmUnavailableError(Exception):
    pass


def load_sentinel(path: Path) -> tuple[dict, str]:
    text = path.read_text()
    doc = yaml.safe_load(text)
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return doc, digest


def topo_sort(nodes: dict[str, dict]) -> list[str]:
    remaining = {nid: set(spec.get("dependsOn") or []) for nid, spec in nodes.items()}
    for nid, deps in remaining.items():
        for d in deps:
            if d not in nodes:
                raise DagValidationError(f"node {nid!r} depends on unknown node {d!r}")
    order: list[str] = []
    while remaining:
        ready = sorted(nid for nid, deps in remaining.items() if not deps)
        if not ready:
            raise DagValidationError(f"cycle detected: remaining={list(remaining)}")
        for nid in ready:
            order.append(nid)
            del remaining[nid]
        for deps in remaining.values():
            for nid in ready:
                deps.discard(nid)
    return order


def coerce_inputs(spec: dict, raw_inputs: dict) -> dict:
    resolved: dict[str, Any] = {}
    for name, decl in (spec.get("inputs") or {}).items():
        provided = raw_inputs.get(name)
        if provided is None and "default" in decl:
            provided = decl["default"]
        if provided is None and decl.get("required"):
            raise DagValidationError(f"required input {name!r} not provided")
        t = decl.get("type")
        if t == "duration" and provided is not None and not isinstance(provided, timedelta):
            provided = parse_duration(provided)
        if t == "integer" and provided is not None:
            provided = int(provided)
        if t == "number" and provided is not None:
            provided = float(provided)
        if t == "array" and provided is not None and not isinstance(provided, list):
            raise DagValidationError(f"input {name!r} must be array")
        resolved[name] = provided
    # Preserve any extra caller-supplied fields (for forward compatibility).
    for k, v in raw_inputs.items():
        resolved.setdefault(k, v)
    return resolved


# --------------------------------------------------------------------------- #
# Function-node loader                                                         #
# --------------------------------------------------------------------------- #


def load_function(source: str, sentinel_dir: Path) -> Callable[[dict], dict]:
    """Load a function-node body relative to the Sentinel's directory.

    A Sentinel ships as a directory: `sentinel.yaml` plus `functions/<id>.py`
    files that its nodes reference via `source: functions/<id>.py`. The Sentinel
    is self-contained on disk. The executor resolves `source` under sentinel_dir
    (falling back to its own directory only for legacy/spike Sentinels whose
    functions live next to executor.py).
    """
    # Preferred: sibling to the Sentinel's YAML.
    candidates: list[Path] = [
        (sentinel_dir / source.replace("-", "_")).resolve(),
        (sentinel_dir / source).resolve(),
    ]
    # Legacy: functions bundled with the executor (pre-self-contained Sentinels).
    executor_dir = Path(__file__).parent
    candidates.extend([
        (executor_dir / source.replace("-", "_")).resolve(),
        (executor_dir / source).resolve(),
    ])
    src_path = next((p for p in candidates if p.exists()), None)
    if src_path is None:
        raise DagValidationError(
            f"function source not found: {source} "
            f"(looked in {sentinel_dir} and {executor_dir})"
        )
    spec = importlib_util.spec_from_file_location(src_path.stem, src_path)
    assert spec and spec.loader
    module = importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)
    entry = getattr(module, "run", None)
    if entry is None:
        raise DagValidationError(f"function {source} has no `run` entrypoint")
    return entry


# --------------------------------------------------------------------------- #
# Execution                                                                    #
# --------------------------------------------------------------------------- #


def _write_event(events_path: Path, payload: dict) -> None:
    line = json.dumps(payload, default=_json_default)
    with events_path.open("a") as f:
        f.write(line + "\n")
    # Mirror to stdout so `kubectl logs` shows the DAG play out node-by-node,
    # not just the file that stays inside the pod. Prefixed for easy grep.
    print(f"[dag] {line}", flush=True)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return _rfc3339(o)
    if isinstance(o, timedelta):
        return f"{int(o.total_seconds())}s"
    if isinstance(o, set):
        return sorted(o)
    return str(o)


def _validate_output(node_id: str, node_spec: dict, output: Any) -> tuple[bool, str | None]:
    schema_wrap = (node_spec.get("output") or {}).get("schema")
    if not schema_wrap:
        return True, None
    # Some sub-schemas use unofficial `itemType`; translate to array items.
    schema = _sanitize_schema(schema_wrap)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(output), key=lambda e: e.path)
    if not errors:
        return True, None
    msg = "; ".join(f"{list(e.path)}: {e.message}" for e in errors[:5])
    return False, msg


def _sanitize_schema(schema: Any) -> Any:
    """Translate the compiler's `itemType: X` into standard `items: {type: X}`.

    v2's YAML uses `itemType` under array-typed properties which is not part
    of Draft 2020-12. We normalize on read so the validator accepts it.
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k == "itemType" and isinstance(v, str):
                out.setdefault("items", {"type": v})
            else:
                out[k] = _sanitize_schema(v)
        return out
    if isinstance(schema, list):
        return [_sanitize_schema(v) for v in schema]
    return schema


def execute(
    sentinel_path: Path,
    inputs_path: Path,
    run_dir: Path,
    findings_path: Path,
    plan_only: bool = False,
) -> int:
    sentinel, sentinel_digest = load_sentinel(sentinel_path)
    spec = sentinel["spec"]
    node_specs = spec["nodes"]

    raw_inputs = json.loads(inputs_path.read_text())
    inputs = coerce_inputs(spec, raw_inputs)

    now = datetime.now(timezone.utc)
    run_id = raw_inputs.get("_runId") or f"run-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    execution = {
        "runId": run_id,
        "now": now,
        "sentinelDigest": sentinel_digest,
        "variationDigest": "",
        "startedAt": now,
    }

    run_dir.mkdir(parents=True, exist_ok=True)
    events_path = run_dir / "events.jsonl"
    audit_path = run_dir / "audit.jsonl"
    if events_path.exists():
        events_path.unlink()
    if audit_path.exists():
        audit_path.unlink()

    _write_event(events_path, {
        "type": "execution.started",
        "timestamp": _rfc3339(now),
        "runId": run_id,
        "sentinelDigest": sentinel_digest,
        "inputs": {k: (v if not isinstance(v, timedelta) else f"{int(v.total_seconds())}s") for k, v in inputs.items()},
        "planOnly": plan_only,
    })

    node_states: dict[str, str] = {nid: "PENDING" for nid in node_specs}
    node_outputs: dict[str, dict] = {}
    node_errors: dict[str, str] = {}
    exit_code = 0
    findings_emitted: list[dict] = []

    try:
        order = topo_sort(node_specs)
    except DagValidationError as e:
        _write_event(events_path, {"type": "execution.failed", "error": str(e), "phase": "topo-sort"})
        return 2

    for node_id in order:
        node = node_specs[node_id]
        env = _Env(inputs=inputs, nodes={k: {"output": v} for k, v in node_outputs.items()}, execution=execution)

        # Check dependencies: any FAILED upstream cancels this node.
        deps = node.get("dependsOn") or []
        dep_failed = [d for d in deps if node_states.get(d) == "FAILED"]
        if dep_failed:
            node_states[node_id] = "CANCELLED"
            _write_event(events_path, {"type": "node.cancelled", "node": node_id, "reason": f"upstream failed: {dep_failed}"})
            _write_audit(audit_path, node_id, node, "CANCELLED", None, None, f"upstream failed: {dep_failed}")
            continue

        # Check `when:` gate.
        when_expr = node.get("when")
        if when_expr is not None:
            try:
                gate = render_template(when_expr, env)
            except ExpressionError as e:
                node_states[node_id] = "FAILED"
                node_errors[node_id] = f"when-expression: {e}"
                _write_event(events_path, {"type": "node.failed", "node": node_id, "error": str(e), "phase": "when-eval"})
                _write_audit(audit_path, node_id, node, "FAILED", None, None, f"when-expression: {e}")
                exit_code = exit_code or 4
                continue
            if not gate:
                node_states[node_id] = "SKIPPED"
                _write_event(events_path, {"type": "node.skipped", "node": node_id, "reason": "when-gate=false"})
                _write_audit(audit_path, node_id, node, "SKIPPED", None, None, "when-gate=false")
                continue

        node_states[node_id] = "RUNNING"
        started = datetime.now(timezone.utc)
        _write_event(events_path, {
            "type": "node.started",
            "node": node_id,
            "kind": node.get("kind"),
            "timestamp": _rfc3339(started),
        })

        try:
            output = _run_node(node_id, node, env, run_dir, plan_only=plan_only, sentinel_dir=sentinel_path.parent)
        except capabilities.MissingCacheError as e:
            node_states[node_id] = "FAILED"
            node_errors[node_id] = f"missing tool cache: {e.cache_path}"
            _write_event(events_path, {
                "type": "node.failed",
                "node": node_id,
                "error": "missing tool cache",
                "cachePath": str(e.cache_path),
                "resolvedArgs": e.args,
                "phase": "tool-invoke",
            })
            _write_audit(audit_path, node_id, node, "FAILED", None, None, f"missing tool cache: {e.cache_path}")
            exit_code = exit_code or 4
            continue
        except LlmUnavailableError as e:
            node_states[node_id] = "FAILED"
            node_errors[node_id] = f"llm unavailable: {e}"
            _write_event(events_path, {"type": "node.failed", "node": node_id, "error": str(e)})
            _write_audit(audit_path, node_id, node, "FAILED", None, None, f"llm unavailable: {e}")
            exit_code = exit_code or 4
            continue
        except Exception as e:
            node_states[node_id] = "FAILED"
            node_errors[node_id] = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()
            _write_event(events_path, {"type": "node.failed", "node": node_id, "error": str(e), "type_": type(e).__name__, "traceback": tb})
            _write_audit(audit_path, node_id, node, "FAILED", None, None, f"{type(e).__name__}: {e}")
            exit_code = exit_code or 4
            continue

        finished = datetime.now(timezone.utc)
        # Validate the node's output against its declared schema.
        ok, err = _validate_output(node_id, node, output)
        if not ok:
            node_states[node_id] = "FAILED"
            node_errors[node_id] = f"schema-validation: {err}"
            _write_event(events_path, {"type": "node.failed", "node": node_id, "error": err, "phase": "schema-validate", "output": output})
            _write_audit(audit_path, node_id, node, "FAILED", output, None, f"schema-validation: {err}")
            exit_code = exit_code or 6
            continue

        node_outputs[node_id] = output
        node_states[node_id] = "SUCCEEDED"
        _write_event(events_path, {
            "type": "node.succeeded",
            "node": node_id,
            "kind": node.get("kind"),
            "startedAt": _rfc3339(started),
            "endedAt": _rfc3339(finished),
            "durationMs": int((finished - started).total_seconds() * 1000),
            "output": output,
        })
        _write_audit(audit_path, node_id, node, "SUCCEEDED", output, {
            "startedAt": _rfc3339(started),
            "endedAt": _rfc3339(finished),
        }, None)

        # If this was an emit node that produced a finding, add to findings.
        if node.get("kind") == "emit" and output:
            findings_emitted.append(output)
            _emit_finding(findings_path, output)

    completed = datetime.now(timezone.utc)
    _write_event(events_path, {
        "type": "execution.completed",
        "runId": run_id,
        "endedAt": _rfc3339(completed),
        "durationMs": int((completed - now).total_seconds() * 1000),
        "nodeStates": node_states,
        "exitCode": exit_code,
        "findings": findings_emitted,
    })

    # Write a summary.json for reliability inspection.
    summary = {
        "runId": run_id,
        "sentinelDigest": sentinel_digest,
        "startedAt": _rfc3339(now),
        "endedAt": _rfc3339(completed),
        "durationMs": int((completed - now).total_seconds() * 1000),
        "nodeStates": node_states,
        "nodeErrors": node_errors,
        "exitCode": exit_code,
        "findings": findings_emitted,
    }
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=_json_default))
    return exit_code


def _write_audit(audit_path: Path, node_id: str, node_spec: dict, state: str, output: Any, timing: dict | None, error: str | None) -> None:
    """§47-shaped audit record per node.

    We record the node id, kind, dependsOn, when-gate presence, state,
    error (if any), and a snapshot of the emitted output so a reviewer
    can trace decisions after the run.
    """
    rec = {
        "node": node_id,
        "kind": node_spec.get("kind"),
        "dependsOn": node_spec.get("dependsOn") or [],
        "when": node_spec.get("when"),
        "state": state,
        "error": error,
        "timing": timing,
        "output": output,
    }
    with audit_path.open("a") as f:
        f.write(json.dumps(rec, default=_json_default) + "\n")


def _run_node(node_id: str, node: dict, env: _Env, run_dir: Path, plan_only: bool, sentinel_dir: Path) -> Any:
    kind = node.get("kind")
    config = node.get("config") or {}
    if kind == "tool":
        args = render_deep(config.get("arguments") or {}, env)
        tool_ref = config["toolRef"]
        if plan_only:
            # Emit the resolved args and return a stub with the recognized shape.
            pending = run_dir / "pending-queries"
            pending.mkdir(parents=True, exist_ok=True)
            (pending / f"{node_id}.json").write_text(json.dumps({"toolRef": tool_ref, "arguments": args}, indent=2, default=_json_default))
            # Return an empty-but-schema-satisfying stub. Not used downstream.
            return _stub_for_tool(node)
        binding = capabilities.resolve(tool_ref)
        return binding(node_id, args, run_dir)
    if kind == "function":
        args = render_deep(config.get("arguments") or {}, env)
        entry = load_function(config["source"], sentinel_dir)
        return entry(args)
    if kind == "condition":
        expr = config.get("expression") or ""
        return bool(eval_expr(expr, env))
    if kind == "llm":
        raise LlmUnavailableError(f"no LLM runtime for node {node_id!r}")
    if kind == "emit":
        return _build_finding(node_id, node, env, run_dir)
    if kind == "ask_human":
        raise LlmUnavailableError(f"ask_human node {node_id!r} not supported in spike")
    raise DagValidationError(f"unknown node kind: {kind}")


def _stub_for_tool(node: dict) -> dict:
    # Return a shape that plausibly satisfies the declared schema.
    schema = (node.get("output") or {}).get("schema") or {}
    return _stub_from_schema(schema)


def _stub_from_schema(schema: dict) -> Any:
    t = schema.get("type")
    if t == "object":
        out: dict[str, Any] = {}
        for k, sub in (schema.get("properties") or {}).items():
            out[k] = _stub_from_schema(sub if isinstance(sub, dict) else {})
        for k in schema.get("required") or []:
            out.setdefault(k, _stub_from_schema({}))
        return out
    if t == "array":
        return []
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    if t == "string":
        return ""
    return None


def _build_finding(node_id: str, node: dict, env: _Env, run_dir: Path) -> dict:
    finding_spec = (node.get("config") or {}).get("finding") or {}
    finding_type = finding_spec.get("type")
    title = render_template(finding_spec.get("title", ""), env) if finding_spec.get("title") else ""
    severity_expr = finding_spec.get("severityExpression") or "\"info\""
    try:
        severity = eval_expr(severity_expr.strip(), env)
    except ExpressionError:
        severity = "info"
    dedupe_key_tpl = finding_spec.get("dedupeKey") or ""
    dedupe_key = render_template(dedupe_key_tpl, env) if dedupe_key_tpl else ""
    if isinstance(dedupe_key, str):
        dedupe_key = re.sub(r"\s+", " ", dedupe_key).strip()

    # Compute the §14 dedupe hash: sentinel_digest + variation_digest + dedupeKey.
    digest_input = f"{env.execution['sentinelDigest']}|{env.execution['variationDigest']}|{dedupe_key}".encode()
    dedupe_hash = hashlib.sha256(digest_input).hexdigest()

    # Evidence refs.
    evidence: list[dict[str, Any]] = []
    for ref in finding_spec.get("evidence") or []:
        if not isinstance(ref, dict):
            continue
        node_ref = ref.get("nodeRef")
        field = ref.get("field")
        optional = bool(ref.get("optional"))
        upstream = env.nodes.get(node_ref)
        if upstream is None:
            if optional:
                evidence.append({"nodeRef": node_ref, "field": field, "value": None, "optional": True, "reason": "upstream-not-produced"})
                continue
            raise DagValidationError(f"emit {node_id!r} evidence references missing node {node_ref!r}")
        upstream_out = upstream.get("output") if isinstance(upstream, dict) else None
        if field == "output" or field is None:
            value = upstream_out
        else:
            value = upstream_out.get(field) if isinstance(upstream_out, dict) else None
        evidence.append({"nodeRef": node_ref, "field": field, "value": value, "optional": optional})

    attributes = render_deep(finding_spec.get("attributes") or {}, env)

    finding = {
        "type": finding_type,
        "title": title.strip() if isinstance(title, str) else title,
        "severity": severity if severity in ("info", "warning", "critical") else "info",
        "dedupeKey": dedupe_key,
        "dedupeHash": dedupe_hash,
        "observedAt": _rfc3339(env.execution["now"]),
        "sentinelDigest": env.execution["sentinelDigest"],
        "variationDigest": env.execution["variationDigest"],
        "evidence": evidence,
        "attributes": attributes,
    }
    return finding


def _emit_finding(findings_path: Path, finding: dict) -> None:
    findings_path.parent.mkdir(parents=True, exist_ok=True)
    with findings_path.open("a") as f:
        f.write(json.dumps(finding, default=_json_default) + "\n")
    # Also to stdout as required.
    print("FINDING " + json.dumps({
        "type": finding.get("type"),
        "severity": finding.get("severity"),
        "title": finding.get("title"),
        "dedupeKey": finding.get("dedupeKey"),
        "dedupeHash": finding.get("dedupeHash"),
    }, default=_json_default))


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sentinel spike executor")
    sub = parser.add_subparsers(dest="phase", required=True)

    # Legacy positional form kept intact: `executor.py plan|execute --sentinel ...`.
    for phase in ("plan", "execute"):
        p = sub.add_parser(phase, help=f"{phase} a Sentinel against tool-cache inputs")
        p.add_argument("--sentinel", required=True, type=Path)
        p.add_argument("--inputs", required=True, type=Path)
        p.add_argument("--run", required=True, type=Path, help="Per-run directory")
        p.add_argument("--findings", type=Path, default=None)

    lint_p = sub.add_parser("lint", help="Structural + remote-readiness lint over a Sentinel directory")
    lint_p.add_argument("sentinel_dir", type=Path, help="directory containing sentinel.yaml")
    lint_p.add_argument(
        "--format",
        dest="output_format",
        choices=("text", "json"),
        default="text",
    )
    lint_p.add_argument(
        "--check",
        dest="check_mode",
        choices=("structural", "remote", "all"),
        default="all",
        help=(
            "'structural' = Phase 1 only; 'remote' = Phase 2 only (no-op unless "
            "metadata.deployment.mode=remote); 'all' (default) = both."
        ),
    )
    lint_p.add_argument(
        "--registry",
        dest="registry_path",
        type=Path,
        default=None,
        help="Path to capabilities-registry.yaml (defaults to <repo>/common/capabilities-registry.yaml).",
    )
    lint_p.add_argument(
        "--schema",
        dest="schema_path",
        type=Path,
        default=None,
        help="Path to deployment-schema.yaml (defaults to <repo>/common/deployment-schema.yaml).",
    )

    # `serve` — Phase 1 runtime subcommand.
    ps = sub.add_parser(
        "serve",
        help="Run a Sentinel using deployment.yaml bindings (Phase 1 runtime).",
    )
    ps.add_argument("--sentinel", required=True, type=Path, help="Sentinel directory (holds sentinel.yaml)")
    ps.add_argument("--deployment", required=True, type=Path, help="Path to deployment.yaml")
    ps.add_argument("--inputs", required=True, type=Path)
    ps.add_argument("--state", type=Path, default=None, help="sqlite state DB path")
    ps.add_argument("--run-id", type=str, default=None)

    args = parser.parse_args(argv)

    if args.phase == "lint":
        # Local import so `python3 executor.py execute ...` doesn't force lint's
        # dependency graph.
        from lint import run_cli as _lint_cli
        return _lint_cli(
            args.sentinel_dir,
            args.output_format,
            check_mode=args.check_mode,
            registry_path=args.registry_path,
            schema_path=args.schema_path,
        )

    if args.phase == "serve":
        # Deferred import — pulls in state/askhuman/channels only when
        # the runtime subcommand fires, keeping the `execute` path
        # dependency-light.
        import runtime_serve as serve_mod  # noqa: E402
        return serve_mod.run_serve(
            sentinel_dir=args.sentinel,
            deployment_path=args.deployment,
            inputs_path=args.inputs,
            state_path=args.state,
            run_id_override=args.run_id,
        )

    findings = args.findings or (args.run.parent / "findings.jsonl")
    try:
        return execute(args.sentinel, args.inputs, args.run, findings, plan_only=(args.phase == "plan"))
    except Exception as e:
        traceback.print_exc()
        print(f"internal error: {type(e).__name__}: {e}", file=sys.stderr)
        return 7


if __name__ == "__main__":
    raise SystemExit(main())
