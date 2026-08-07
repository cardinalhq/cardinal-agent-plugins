"""`executor.py serve` entry point.

Reads sentinel.yaml + deployment.yaml + inputs.json and drives the DAG
using deployment bindings. Dispatch responsibility:

* capability providers → capabilities.resolve_provider(cap, provider_id)
* channel drivers      → channels.resolve_channel(channel_ref)
* parser models        → normalizer.resolve_parser(model_id)
* findings sinks       → sinks.resolve_sink(sink_id)
* secret refs          → secrets.resolve(<scheme>://...)

All ask_human state lives in the sqlite state store; the store's flock
guarantees a single writer.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sys
import traceback
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Match executor's sys.path convention so subordinate modules resolve
# cleanly whether we're invoked via `python executor.py serve` or
# imported directly by tests.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import askhuman
import capabilities as capabilities_mod
import sandbox
import channels as channels_mod
import deployment as deployment_mod
import executor as executor_mod
import normalizer as normalizer_mod
import secrets as secrets_mod
import sinks as sinks_mod
import state as state_mod
import yaml


def _load_sentinel_from_dir(sentinel_dir: Path) -> tuple[dict, str, Path]:
    path = sentinel_dir / "sentinel.yaml"
    if not path.exists():
        raise FileNotFoundError(f"no sentinel.yaml under {sentinel_dir}")
    text = path.read_text()
    doc = yaml.safe_load(text)
    digest = "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return doc, digest, path


def run_serve(
    sentinel_dir: Path,
    deployment_path: Path,
    inputs_path: Path,
    state_path: Path | None = None,
    run_id_override: str | None = None,
    max_wait_override: timedelta | None = None,
    poll_interval: float = 0.05,
) -> int:
    """Serve one Sentinel run to completion. Returns process exit code."""
    sentinel_dir = Path(sentinel_dir)
    sentinel_doc, sentinel_digest, sentinel_yaml_path = _load_sentinel_from_dir(sentinel_dir)
    deployment = deployment_mod.load_deployment(deployment_path)
    inputs_raw = json.loads(Path(inputs_path).read_text())

    spec = sentinel_doc["spec"]
    node_specs = spec["nodes"]
    resolved_inputs = executor_mod.coerce_inputs(spec, inputs_raw)

    now = datetime.now(timezone.utc)
    run_id = (
        run_id_override
        or inputs_raw.get("_runId")
        or f"run-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    )

    with state_mod.StateStore.open(state_path) as state:
        state.start_run(run_id, sentinel_digest, inputs_raw)
        exit_code = _run(
            sentinel_doc=sentinel_doc,
            sentinel_digest=sentinel_digest,
            sentinel_dir=sentinel_dir,
            node_specs=node_specs,
            inputs=resolved_inputs,
            deployment=deployment,
            state=state,
            run_id=run_id,
            now=now,
            max_wait_override=max_wait_override,
            poll_interval=poll_interval,
        )
        state.complete_run(run_id, status="succeeded" if exit_code == 0 else "failed")
    return exit_code


def _run(
    sentinel_doc: dict,
    sentinel_digest: str,
    sentinel_dir: Path,
    node_specs: dict[str, dict],
    inputs: dict[str, Any],
    deployment: deployment_mod.Deployment,
    state: state_mod.StateStore,
    run_id: str,
    now: datetime,
    max_wait_override: timedelta | None,
    poll_interval: float,
) -> int:
    execution = {
        "runId": run_id,
        "now": now,
        "sentinelDigest": sentinel_digest,
        "variationDigest": "",
        "startedAt": now,
    }
    node_states: dict[str, str] = {nid: "PENDING" for nid in node_specs}
    node_outputs: dict[str, Any] = {}
    exit_code = 0

    try:
        order = executor_mod.topo_sort(node_specs)
    except executor_mod.DagValidationError as e:
        state.audit("execution.failed", {"error": str(e), "phase": "topo-sort"}, run_id=run_id)
        return 2

    for node_id in order:
        node = node_specs[node_id]
        env = executor_mod._Env(
            inputs=inputs,
            nodes={k: {"output": v} for k, v in node_outputs.items()},
            execution=execution,
        )

        deps = node.get("dependsOn") or []
        dep_failed = [d for d in deps if node_states.get(d) == "FAILED"]
        if dep_failed:
            node_states[node_id] = "CANCELLED"
            state.audit(
                "node.cancelled",
                {"node": node_id, "reason": f"upstream failed: {dep_failed}"},
                run_id=run_id,
                node_id=node_id,
            )
            continue

        when_expr = node.get("when")
        if when_expr is not None:
            try:
                gate = executor_mod.render_template(when_expr, env)
            except executor_mod.ExpressionError as e:
                node_states[node_id] = "FAILED"
                state.audit(
                    "node.failed",
                    {"node": node_id, "error": str(e), "phase": "when-eval"},
                    run_id=run_id,
                    node_id=node_id,
                )
                exit_code = exit_code or 4
                continue
            if not gate:
                node_states[node_id] = "SKIPPED"
                state.audit(
                    "node.skipped",
                    {"node": node_id, "reason": "when-gate=false"},
                    run_id=run_id,
                    node_id=node_id,
                )
                continue

        state.audit("node.started", {"node": node_id, "kind": node.get("kind")}, run_id=run_id, node_id=node_id)
        try:
            output, status = _run_node(
                node_id=node_id,
                node=node,
                env=env,
                sentinel_dir=sentinel_dir,
                deployment=deployment,
                state=state,
                run_id=run_id,
                max_wait_override=max_wait_override,
                poll_interval=poll_interval,
            )
        except Exception as e:
            node_states[node_id] = "FAILED"
            state.audit(
                "node.failed",
                {"node": node_id, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()},
                run_id=run_id,
                node_id=node_id,
            )
            exit_code = exit_code or 4
            continue

        if status == "SKIPPED":
            node_states[node_id] = "SKIPPED"
            continue
        if status == "INCONCLUSIVE":
            # Downstream nodes typically depend on this node's answer;
            # they'll cancel due to upstream not-succeeded. For Phase 1
            # we mark it as SKIPPED (no answer produced) so the DAG loop
            # continues without treating this as a hard failure.
            node_states[node_id] = "SKIPPED"
            continue

        # Declared-output-schema validation, matching `executor.execute`.
        # Without this the two run paths disagree: a node whose output drifted
        # from its declared schema fails under `execute` and sails through
        # `serve` — which is the path production actually runs.
        ok, schema_err = executor_mod._validate_output(node_id, node, output)
        if not ok:
            node_states[node_id] = "FAILED"
            state.audit(
                "node.failed",
                {"node": node_id, "error": schema_err, "phase": "schema-validate"},
                run_id=run_id,
                node_id=node_id,
            )
            exit_code = exit_code or 6
            continue

        node_outputs[node_id] = output
        node_states[node_id] = "SUCCEEDED"
        state.audit(
            "node.succeeded",
            {"node": node_id, "kind": node.get("kind")},
            run_id=run_id,
            node_id=node_id,
        )

        if node.get("kind") == "emit" and output:
            _route_finding(node_id, output, deployment, state, run_id)

    state.audit(
        "execution.completed",
        {"runId": run_id, "nodeStates": node_states, "exitCode": exit_code},
        run_id=run_id,
    )
    return exit_code


def _run_node(
    node_id: str,
    node: dict,
    env: executor_mod._Env,
    sentinel_dir: Path,
    deployment: deployment_mod.Deployment,
    state: state_mod.StateStore,
    run_id: str,
    max_wait_override: timedelta | None,
    poll_interval: float,
) -> tuple[Any, str]:
    kind = node.get("kind")
    config = node.get("config") or {}
    if kind == "ask_human":
        binding = deployment.ask_human_bindings.get(node_id)
        if binding is None:
            raise KeyError(f"ask_human node {node_id!r} has no deployment binding")
        parser_model = deployment.parser_model_for(node_id)
        evidence = executor_mod.render_deep(config.get("evidence") or {}, env)
        timeout_cfg = config.get("timeout") or {}
        # Respect the caller-supplied max_wait_override (tests use this)
        # or fall back to the node's maxWait.
        if max_wait_override is not None:
            max_wait = max_wait_override
        else:
            raw = timeout_cfg.get("maxWait") or "5m"
            max_wait = executor_mod.parse_duration(raw)
        outcome = askhuman.handle_ask_human(
            node_id=node_id,
            node_spec=node,
            binding=binding,
            evidence=evidence,
            parser_model=parser_model,
            state=state,
            run_id=run_id,
            resolve_secret=secrets_mod.resolve,
            max_wait=max_wait,
            poll_interval=poll_interval,
        )
        if outcome.status == "normalized":
            return outcome.answer, "SUCCEEDED"
        # inconclusive | deferred | timeout: no schema-conforming answer.
        return None, "INCONCLUSIVE"

    if kind == "tool":
        args = executor_mod.render_deep(config.get("arguments") or {}, env)
        tool_ref = config["toolRef"]
        binding = deployment.capability_bindings.get(tool_ref)
        if binding is None:
            # Fall back to the pre-registered spike bindings for smoothness
            # against Sentinels that predate deployment.yaml.
            fn = capabilities_mod.resolve(tool_ref)
            return fn(node_id, args, sentinel_dir), "SUCCEEDED"
        provider_id = binding["provider"]
        fn = capabilities_mod.resolve_provider(tool_ref, provider_id)
        ctx = {
            "run_dir": sentinel_dir,
            "sentinel_dir": sentinel_dir,
            "capability_id": tool_ref,
            "binding": binding,
        }
        return fn(node_id, args, ctx), "SUCCEEDED"

    if kind == "function":
        args = executor_mod.render_deep(config.get("arguments") or {}, env)
        entry = executor_mod.load_function(config["source"], sentinel_dir)
        # Enforce the deployment's `functions.<id>.network` policy. Unlisted
        # nodes default to denied, so adding a function node without touching
        # deployment.yaml cannot silently grant it the network.
        with sandbox.function_guard(deployment.functions, node_id):
            return entry(args), "SUCCEEDED"

    if kind == "condition":
        expr = config.get("expression") or ""
        return bool(executor_mod.eval_expr(expr, env)), "SUCCEEDED"

    if kind == "llm":
        raise executor_mod.LlmUnavailableError(
            f"no LLM runtime for node {node_id!r} in Phase 1 serve"
        )

    if kind == "emit":
        # Reuse the existing finding builder — it does the §14 dedupe
        # hash and evidence-ref walking.
        finding = executor_mod._build_finding(node_id, node, env, sentinel_dir)
        return finding, "SUCCEEDED"

    raise executor_mod.DagValidationError(f"unknown node kind: {kind}")


def _route_finding(
    emit_node_id: str,
    finding: dict[str, Any],
    deployment: deployment_mod.Deployment,
    state: state_mod.StateStore,
    run_id: str,
) -> None:
    """Dispatch a finding per deployment.yaml findingsRouting rules.

    First-match-wins per finding. Supported match shapes: emitNode,
    findingType, or `"*"` catch-all. If no rule matches, we still record
    the finding to state with delivery_status='unrouted' so nothing is
    silently lost.
    """
    rules = deployment.findings_routing or []
    finding_type = finding.get("type")
    chosen: dict[str, Any] | None = None
    for rule in rules:
        match = rule.get("match") or {}
        if match.get("emitNode") == emit_node_id:
            chosen = rule
            break
        if match.get("findingType") == finding_type:
            chosen = rule
            break
        if match.get("*") is True:
            chosen = rule
            break
    if chosen is None:
        state.record_finding(run_id, finding, sink_id="<unrouted>", delivery_status="unrouted")
        state.audit(
            "finding.unrouted",
            {
                "emit_node": emit_node_id,
                "finding_type": finding_type,
                "dedupe_hash": finding.get("dedupeHash"),
            },
            run_id=run_id,
            node_id=emit_node_id,
        )
        return

    sink_id = chosen["sink"]
    sink_params = chosen.get("sinkParams") or {}
    sink = sinks_mod.resolve_sink(sink_id)
    execution_cfg = deployment.execution or {}
    retry_cfg = execution_cfg.get("sinkRetry") or {"attempts": 1, "onExhausted": "drop-and-audit"}
    attempts = int(retry_cfg.get("attempts", 1))
    delivered = False
    detail: str | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = sink.deliver(finding, sink_params)
            if result.delivered:
                delivered = True
                detail = result.detail
                break
            detail = result.detail
        except Exception as e:
            detail = f"{type(e).__name__}: {e}"
    if delivered:
        state.record_finding(run_id, finding, sink_id=sink_id, delivery_status="delivered")
        state.audit(
            "finding.delivered",
            {
                "emit_node": emit_node_id,
                "sink_id": sink_id,
                "detail": detail,
                "dedupe_hash": finding.get("dedupeHash"),
            },
            run_id=run_id,
            node_id=emit_node_id,
        )
    else:
        on_ex = retry_cfg.get("onExhausted", "drop-and-audit")
        if on_ex == "spool-to-state":
            state.record_finding(run_id, finding, sink_id=sink_id, delivery_status="pending")
        else:
            state.record_finding(run_id, finding, sink_id=sink_id, delivery_status="dropped")
        state.audit(
            "finding.sink_failed",
            {
                "emit_node": emit_node_id,
                "sink_id": sink_id,
                "onExhausted": on_ex,
                "detail": detail,
                "dedupe_hash": finding.get("dedupeHash"),
            },
            run_id=run_id,
            node_id=emit_node_id,
        )


__all__ = ["run_serve"]
