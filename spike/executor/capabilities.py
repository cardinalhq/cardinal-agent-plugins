"""Capability bindings for the spike executor.

Two bindings are wired:

- ``observability.query-metrics`` — reads a pre-populated tool cache under
  ``runs/<runid>/tool-cache/<node-id>.json``. This decouples the executor
  from MCP client wiring: a driver (in this spike, a subagent) resolves
  pending queries against the Cardinal MCP tool
  ``mcp__plugin_cardinal_cardinal__lakerunner__execute_metrics_query`` and
  writes each response to its expected cache path. If the file is missing
  the executor writes the resolved arguments to
  ``runs/<runid>/pending-queries/<node-id>.json`` and raises
  ``MissingCacheError``.

- ``code.grep`` — shells out to ``grep -rn`` restricted to the requested
  path. Returns structured ``{matches: [{file, line, text}]}``.

Tradeoff (option a vs option b, per the task): we chose option (a) — a
pre-populated cache — because the executor is a plain Python process
that cannot reach an MCP server directly. The alternative (dump queries
to stdout, paste results in) would require a human in the loop for every
tool call and defeats the "run it 5 times" reliability requirement. The
cache-file protocol is scriptable end-to-end.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable


class MissingCacheError(RuntimeError):
    """Raised when a tool call has no cached response for the given node."""

    def __init__(self, node_id: str, cache_path: Path, args: dict):
        super().__init__(
            f"no cached tool response for node {node_id!r} at {cache_path}"
        )
        self.node_id = node_id
        self.cache_path = cache_path
        self.args = args


def query_metrics(
    node_id: str,
    args: dict[str, Any],
    run_dir: Path,
) -> dict[str, Any]:
    """observability.query-metrics binding.

    Reads ``runs/<runid>/tool-cache/<node-id>.json``. If missing, writes the
    resolved arguments to ``runs/<runid>/pending-queries/<node-id>.json`` so a
    driver can populate the cache and re-run.
    """
    cache_dir = run_dir / "tool-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{node_id}.json"
    if not cache_path.exists():
        pending_dir = run_dir / "pending-queries"
        pending_dir.mkdir(parents=True, exist_ok=True)
        (pending_dir / f"{node_id}.json").write_text(json.dumps(args, indent=2))
        raise MissingCacheError(node_id, cache_path, args)
    payload = json.loads(cache_path.read_text())
    # The MCP tool returns a rich object; the sentinel's declared output
    # schema requires {summary, series_total, ...}. Normalize a couple of
    # spellings we saw in the source capture (e.g. seriesTotal vs series_total).
    return _normalize_metric_response(payload)


def _normalize_metric_response(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"expected dict metric response, got {type(payload).__name__}")

    def pick(*keys, default=None):
        for k in keys:
            if k in payload and payload[k] is not None:
                return payload[k]
        return default

    summary = pick("summary")
    series_total = pick("series_total", "seriesTotal", "series_count")
    data_points_total = pick("data_points_total", "dataPointsTotal")
    ddsketches = pick("ddsketches", "series", "series_stats")

    # Ensure required top-level fields exist for schema validation.
    normalized: dict[str, Any] = dict(payload)
    if summary is not None:
        normalized["summary"] = summary
    if series_total is not None:
        try:
            normalized["series_total"] = int(series_total)
        except (TypeError, ValueError):
            normalized["series_total"] = 0
    else:
        normalized["series_total"] = int(len(ddsketches) if isinstance(ddsketches, dict) else 0)
    if data_points_total is not None:
        try:
            normalized["data_points_total"] = int(data_points_total)
        except (TypeError, ValueError):
            normalized["data_points_total"] = 0
    if isinstance(ddsketches, dict):
        normalized["ddsketches"] = ddsketches
    elif ddsketches is None:
        normalized["ddsketches"] = {}
    else:
        # Some responses key by list of {attributes, stats}; convert to dict.
        conv: dict[str, Any] = {}
        if isinstance(ddsketches, list):
            for i, item in enumerate(ddsketches):
                if isinstance(item, dict):
                    key = json.dumps(item.get("attributes") or item.get("labels") or {"i": i}, sort_keys=True)
                    conv[key] = item.get("stats") or item
        normalized["ddsketches"] = conv
    return normalized


def code_grep(node_id: str, args: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """code.grep binding — shells out to grep -rn."""
    pattern = args.get("pattern")
    path = args.get("path")
    if not pattern or not path:
        return {"matches": []}
    if not os.path.isdir(path) and not os.path.isfile(path):
        return {"matches": []}
    max_matches = int(args.get("maxMatches", 40))
    globs = args.get("fileGlobs") or []
    cmd = ["grep", "-rn", "-E", "--", pattern, path]
    for g in globs:
        cmd.insert(2, f"--include={g}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"matches": []}
    matches: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        matches.append({"file": parts[0], "line": int(parts[1]) if parts[1].isdigit() else 0, "text": parts[2]})
        if len(matches) >= max_matches:
            break
    return {"matches": matches}


def _tool_cache_read(node_id: str, args: dict[str, Any], run_dir: Path) -> Any:
    """Generic tool-cache reader.

    The MECHANIZE spec's v0 executor decouples tool invocation from the
    executor process — every tool node consumes a JSON file at
    ``runs/<runid>/tool-cache/<node-id>.json`` that a driver populates by
    running the corresponding MCP tool out-of-band. This helper is the
    common path for the observability capabilities that do NOT need
    response-shape normalization (list-services, error-overview,
    query-logs). ``observability.query-metrics`` still uses the
    ``query_metrics`` wrapper below to normalize the sketch keys.
    """
    cache_dir = run_dir / "tool-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{node_id}.json"
    if not cache_path.exists():
        pending_dir = run_dir / "pending-queries"
        pending_dir.mkdir(parents=True, exist_ok=True)
        (pending_dir / f"{node_id}.json").write_text(json.dumps(args, indent=2))
        raise MissingCacheError(node_id, cache_path, args)
    return json.loads(cache_path.read_text())


def list_services(node_id: str, args: dict[str, Any], run_dir: Path) -> Any:
    """observability.list-services — tool-cache reader."""
    return _tool_cache_read(node_id, args, run_dir)


def error_overview(node_id: str, args: dict[str, Any], run_dir: Path) -> Any:
    """observability.error-overview — tool-cache reader."""
    return _tool_cache_read(node_id, args, run_dir)


def query_logs(node_id: str, args: dict[str, Any], run_dir: Path) -> Any:
    """observability.query-logs — tool-cache reader."""
    return _tool_cache_read(node_id, args, run_dir)


def code_read(node_id: str, args: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    """code.read — read a file from the local filesystem.

    Unlike the observability capabilities, this executes directly (no
    cache) because a file read is deterministic and free.
    """
    path = args.get("path")
    if not path or not os.path.isfile(path):
        return {"path": path, "exists": False, "content": None}
    with open(path, encoding="utf-8", errors="replace") as f:
        content = f.read()
    return {"path": path, "exists": True, "content": content}


CAPABILITY_BINDINGS = {
    "observability.query-metrics": query_metrics,
    "observability.list-services": list_services,
    "observability.error-overview": error_overview,
    "observability.query-logs": query_logs,
    "code.grep": code_grep,
    "code.read": code_read,
}


def resolve(capability_id: str):
    if capability_id not in CAPABILITY_BINDINGS:
        raise KeyError(f"no binding registered for capability {capability_id!r}")
    return CAPABILITY_BINDINGS[capability_id]


# --------------------------------------------------------------------------- #
# Provider registry (Phase 1 of runtime-comms plan)                           #
# --------------------------------------------------------------------------- #
#
# The existing CAPABILITY_BINDINGS map above is the spike-era single-provider
# lookup used by the `execute` subcommand. The `serve` subcommand introduced
# in Phase 1 supports multiple providers per abstract capability, keyed by
# `deployment.yaml capabilityBindings.<cap>.provider`. New providers register
# via `@provider("capability.id", "provider-id")` — one file per provider.
#
# Signature: provider(capability_id, provider_id) decorates a callable of
# shape (node_id, args, ctx) -> dict, where `ctx` is a small dict carrying
# `run_dir`, `sentinel_dir`, and provider-specific state.


_PROVIDERS: dict[tuple[str, str], Callable[..., Any]] = {}


class UnknownProviderError(KeyError):
    """Raised when a capability binding names an unregistered provider."""


def provider(capability_id: str, provider_id: str):
    """Register a provider callable for (capability, provider) pair.

    Providers register at import time. Duplicate registration raises.
    """

    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        key = (capability_id, provider_id)
        if key in _PROVIDERS:
            raise RuntimeError(
                f"duplicate provider registration for {capability_id!r} / {provider_id!r}"
            )
        _PROVIDERS[key] = fn
        return fn

    return _decorator


def resolve_provider(capability_id: str, provider_id: str) -> Callable[..., Any]:
    key = (capability_id, provider_id)
    if key not in _PROVIDERS:
        # `fixture` resolves for ANY capability, not just the eagerly-registered
        # ones below. The impl reads `fixtures/<node-or-capability>.json` and
        # cares nothing for which capability it is standing in for, so a static
        # whitelist only ever produced false negatives: a Sentinel declaring a
        # capability outside the enumerated set failed its own trial with
        # UnknownProviderError even though its fixture file was sitting right
        # there. The compiler is allowed to mint new abstract capability ids
        # (mechanize CORE.md's `capability-registry-extension-needed` escape
        # hatch), so the runtime must not enumerate them ahead of time.
        if provider_id == "fixture":
            return _fixture_impl
        available = sorted(p for c, p in _PROVIDERS if c == capability_id)
        raise UnknownProviderError(
            f"no provider {provider_id!r} for capability {capability_id!r}; "
            f"registered: {available}"
        )
    return _PROVIDERS[key]


def registered_providers() -> list[tuple[str, str]]:
    return sorted(_PROVIDERS.keys())


# --------------------------------------------------------------------------- #
# Fixture provider — universal, test-only                                     #
# --------------------------------------------------------------------------- #
#
# Reads `<sentinel-dir>/fixtures/<capability>.json` and returns it verbatim.
# One file may hold either `{args...: result}` map keyed by JSON-canonical
# args, or a single result object. Simplest possible test provider.


def _fixture_impl(node_id: str, args: dict[str, Any], ctx: dict[str, Any]) -> Any:
    sentinel_dir: Path = ctx["sentinel_dir"]
    capability_id: str = ctx["capability_id"]
    fixtures_dir = sentinel_dir / "fixtures"
    # Prefer per-node override, fall back to per-capability.
    candidates = [
        fixtures_dir / f"{node_id}.json",
        fixtures_dir / f"{capability_id}.json",
        fixtures_dir / (capability_id.replace(".", "_") + ".json"),
    ]
    for p in candidates:
        if p.exists():
            payload = json.loads(p.read_text())
            if isinstance(payload, dict) and "_byArgs" in payload:
                # Optional keyed form.
                key = json.dumps(args, sort_keys=True, default=str)
                by_args = payload["_byArgs"]
                if key in by_args:
                    return by_args[key]
                if "_default" in payload:
                    return payload["_default"]
                raise KeyError(
                    f"fixture {p} has no entry for args {key!r} and no _default"
                )
            return payload
    raise FileNotFoundError(
        f"fixture provider found no file for capability {capability_id!r} "
        f"under {fixtures_dir} (looked for {[c.name for c in candidates]})"
    )


# Eagerly register the fixture provider for every capability the shared
# registry declares, so `registered_providers()` enumerates them. This list is
# NOT the set of capabilities fixtures work for — `resolve_provider` falls back
# to `_fixture_impl` for any capability asking for the `fixture` provider.
#
# Read from common/capabilities-registry.yaml rather than hand-maintained: the
# hardcoded copy went stale the moment the compiler was allowed to mint new
# abstract capability ids, and a second stale copy of the same list is exactly
# how the compile-time and runtime views of "what capabilities exist" drifted
# apart in the first place.
def _registry_capabilities() -> list[str]:
    # capabilities.py sits at <root>/spike/executor/, and the Dockerfile copies
    # `spike/executor/` and `common/` under the same root, so parents[2] is the
    # registry's parent in both the repo and the image.
    registry = Path(__file__).resolve().parents[2] / "common" / "capabilities-registry.yaml"
    if not registry.exists():
        return []
    try:
        import yaml  # noqa: PLC0415

        doc = yaml.safe_load(registry.read_text())
    except Exception:  # noqa: BLE001 — introspection only; never break import
        return []
    if not isinstance(doc, dict):
        return []
    caps = doc.get("capabilities") or {}
    # llm.* are model capabilities, not tool capabilities; they never bind to
    # a fixture tool provider.
    return sorted(c for c in caps if isinstance(c, str) and not c.startswith("llm."))


_FIXTURE_CAPABILITIES = _registry_capabilities()

for _cap in _FIXTURE_CAPABILITIES:
    provider(_cap, "fixture")(_fixture_impl)


# --------------------------------------------------------------------------- #
# Real providers                                                              #
# --------------------------------------------------------------------------- #
#
# Concrete providers live one-per-module under `providers/` and register
# themselves at import time. Importing them HERE — rather than expecting every
# entry point to remember an `import providers` line — means the registry is
# complete for anyone who has `capabilities` at all. `fixture` stays registered
# above and remains the default for tests; nothing below changes it.
#
# Note the deliberate absence of a try/except: a provider module that fails to
# import must break loudly at startup. Swallowing ImportError here would
# reproduce the exact failure this package exists to remove — a Sentinel that
# asked for live telemetry quietly finding no provider by that name.


def _import_providers() -> None:
    import sys

    pkg_dir = Path(__file__).resolve().parent
    if str(pkg_dir) not in sys.path:
        # `providers` is a top-level package relative to spike/executor/, the
        # same sys.path convention executor.py and runtime_serve.py use.
        sys.path.insert(0, str(pkg_dir))
    import providers

    providers.import_all()


_import_providers()


__all__ = [
    "MissingCacheError",
    "resolve",
    "CAPABILITY_BINDINGS",
    "provider",
    "resolve_provider",
    "registered_providers",
    "UnknownProviderError",
]
