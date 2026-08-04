"""Parse + validate deployment.yaml.

Phase 1 of the runtime-comms plan. This is the sidecar to sentinel.yaml that
binds abstract capabilities and operator-comms rails to concrete providers.
`metadata.name` is deliberately absent — derived from the sibling
sentinel.yaml — so we do not carry a copy-paste bait.

See:
- common/deployment-schema.yaml (the JSON Schema this validates against)
- common/capabilities-registry.yaml (the id lookup source for R10/R13/R19)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator


DEFAULT_SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent.parent / "common" / "deployment-schema.yaml"
)
DEFAULT_REGISTRY_PATH = (
    Path(__file__).resolve().parent.parent.parent / "common" / "capabilities-registry.yaml"
)


class DeploymentValidationError(ValueError):
    """Raised when deployment.yaml does not validate."""


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
    capability_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
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


def load_schema(path: Path | None = None) -> dict[str, Any]:
    p = path or DEFAULT_SCHEMA_PATH
    with p.open() as f:
        return yaml.safe_load(f)


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
        capability_bindings=raw.get("capabilityBindings") or {},
        input_bindings=raw.get("inputBindings") or {},
        llm_bindings=raw.get("llmBindings") or {},
        findings_routing=raw.get("findingsRouting") or [],
        functions=raw.get("functions") or {},
        execution=raw.get("execution") or {},
        raw=raw,
    )


__all__ = [
    "Deployment",
    "DeploymentValidationError",
    "load_deployment",
    "load_schema",
    "DEFAULT_SCHEMA_PATH",
    "DEFAULT_REGISTRY_PATH",
]
