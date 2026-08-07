"""Parse + validate deployment.yaml.

Phase 1 of the runtime-comms plan. This is the sidecar to sentinel.yaml that
binds abstract capabilities and operator-comms rails to concrete providers.
`metadata.name` is deliberately absent — derived from the sibling
sentinel.yaml — so we do not carry a copy-paste bait.

Capability bindings are keyed by capability id (the value a tool node's
``config.toolRef`` names) and carry ``{provider, endpoint_env?, token_env?,
credential_ref?}``. In-cluster they are written by
``k8s/controller/projections.py`` from the Sentinel CR's ``spec.capabilities``;
locally they come from the sentinel directory's own deployment.yaml. Lookup is
strict — see :class:`CapabilityBindings`.

See:
- common/deployment-schema.yaml (the JSON Schema this validates against)
- common/integrations.yaml (policy source for R10/R13/R15/R19)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "common" / "deployment-schema.yaml"
)
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent.parent / "common" / "integrations.yaml"
)


class DeploymentValidationError(ValueError):
    """Raised when deployment.yaml does not validate."""


class CapabilityNotBoundError(RuntimeError):
    """Raised when a node references a capability with no deployment binding.

    This is deliberately loud. Before it existed, a missing binding made
    ``capability_bindings.get(id)`` return ``None`` and the runtime fell
    through to the legacy spike tool-cache path — so a Sentinel CR that
    carefully declared ``provider: mcp`` ran against hand-populated JSON files
    instead, with no error anywhere. A capability that is declared but not
    bound must stop the run, not quietly change what it means.
    """


_RAISE = object()


class CapabilityBindings(dict):
    """``{capability_id: binding}`` that refuses to answer "no binding" quietly.

    Behaves like a dict except that a lookup of an unbound capability raises
    :class:`CapabilityNotBoundError` naming the capability and listing what IS
    bound — for both ``bindings[cap]`` and the bare ``bindings.get(cap)`` that
    the runtime uses. Passing an explicit default (``bindings.get(cap, None)``)
    still returns it: an escape hatch is fine as long as it has to be typed out.
    """

    __slots__ = ("source",)

    def __init__(self, *args: Any, source: Any = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.source = source

    def _unbound(self, capability_id: Any) -> CapabilityNotBoundError:
        bound = ", ".join(sorted(map(str, self.keys()))) or "<none>"
        where = f" in {self.source}" if self.source else ""
        return CapabilityNotBoundError(
            f"capability {capability_id!r} has no binding in deployment.yaml"
            f"{where}; bound capabilities are: {bound}. Add it to the Sentinel "
            f"CR's spec.capabilities (id + provider) or to the sentinel "
            f"directory's deployment.yaml capabilityBindings."
        )

    def __missing__(self, capability_id: Any) -> Any:
        raise self._unbound(capability_id)

    def get(self, capability_id: Any, default: Any = _RAISE) -> Any:  # type: ignore[override]
        if capability_id in self:
            return dict.__getitem__(self, capability_id)
        if default is _RAISE:
            raise self._unbound(capability_id)
        return default


@dataclass
class Deployment:
    """Typed view of a parsed deployment.yaml.

    All fields are always present as dicts/lists (default empty) so callers
    can do `binding = deployment.ask_human_bindings.get(node_id)` without
    None-guarding the container.
    """

    runtime: str
    default_parser_model: str | None
    ask_human_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    capability_bindings: CapabilityBindings = field(default_factory=CapabilityBindings)
    input_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    llm_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    findings_routing: list[dict[str, Any]] = field(default_factory=list)
    functions: dict[str, dict[str, Any]] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def parser_model_for(self, node_id: str) -> str | None:
        """Resolve §14a parser model for an ask_human node.

        Per-node override → runtime.defaultParserModel → None. Callers must
        treat None as an error for `prose-llm-parse` nodes (see R20).
        """
        binding = self.ask_human_bindings.get(node_id) or {}
        return binding.get("parserModel") or self.default_parser_model

    def binding_for(self, capability_id: str) -> dict[str, Any]:
        """Return the binding for ``capability_id`` or raise.

        The documented accessor for capability providers. Never returns
        ``None`` — an unbound capability raises
        :class:`CapabilityNotBoundError`.
        """
        return self.capability_bindings[capability_id]


def load_schema(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_SCHEMA_PATH
    with p.open() as f:
        return yaml.safe_load(f)


_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _capability_bindings(raw: Any, path: Any) -> CapabilityBindings:
    """Build the strict binding map, rejecting unusable bindings by name.

    The JSON Schema already constrains the container; this adds the checks that
    are about *usability* rather than shape, and reports them with the
    capability id in the message so an operator knows which CR entry to fix.
    """
    if raw is None:
        return CapabilityBindings(source=path)
    if not isinstance(raw, dict):
        raise DeploymentValidationError(
            f"deployment.yaml at {path}: capabilityBindings must be a mapping "
            f"keyed by capability id, got {type(raw).__name__}"
        )
    for capability_id, binding in raw.items():
        if not isinstance(binding, dict):
            raise DeploymentValidationError(
                f"deployment.yaml at {path}: binding for capability "
                f"{capability_id!r} must be a mapping, got {type(binding).__name__}"
            )
        provider = binding.get("provider")
        if not isinstance(provider, str) or not provider.strip():
            raise DeploymentValidationError(
                f"deployment.yaml at {path}: capability {capability_id!r} has no "
                f"usable 'provider' (got {provider!r})"
            )
        for key in ("endpoint_env", "token_env"):
            if key not in binding:
                continue
            value = binding[key]
            if not isinstance(value, str) or not _ENV_NAME_RE.match(value):
                raise DeploymentValidationError(
                    f"deployment.yaml at {path}: capability {capability_id!r} "
                    f"{key}={value!r} is not a usable environment variable name"
                )
    return CapabilityBindings(raw, source=path)


def load_deployment(path: Path, schema_path: Path | None = None) -> Deployment:
    with Path(path).open() as f:
        raw = yaml.safe_load(f) or {}
    schema = load_schema(schema_path)
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(raw), key=lambda e: list(e.path))
    if errors:
        rendered = "; ".join(
            f"{list(e.path) or '<root>'}: {e.message}" for e in errors[:5]
        )
        raise DeploymentValidationError(
            f"deployment.yaml at {path} failed schema validation: {rendered}"
        )
    return Deployment(
        runtime=raw["runtime"],
        default_parser_model=raw.get("defaultParserModel"),
        ask_human_bindings=raw.get("askHumanBindings") or {},
        capability_bindings=_capability_bindings(raw.get("capabilityBindings"), path),
        input_bindings=raw.get("inputBindings") or {},
        llm_bindings=raw.get("llmBindings") or {},
        findings_routing=raw.get("findingsRouting") or [],
        functions=raw.get("functions") or {},
        execution=raw.get("execution") or {},
        raw=raw,
    )


__all__ = [
    "CapabilityBindings",
    "CapabilityNotBoundError",
    "Deployment",
    "DeploymentValidationError",
    "load_deployment",
    "load_schema",
    "DEFAULT_SCHEMA_PATH",
    "DEFAULT_REGISTRY_PATH",
]
