"""The deploy-sentinel skill must produce a Sentinel directory that loads.

`deployment.yaml` in the Sentinel directory is the ONLY source of
`schemaVersion`, `kind` and `runtime` — `projections.project_deployment`
merges the CR's capabilities/sinks into it and copies nothing else, and all
three are in `required:` of `common/deployment-schema.yaml`. A directory
without one therefore projects a schema-invalid `/config/deployment.yaml` and
the executor dies at pod start with an error naming `runtime`.

The skill used to label that file "(optional)" and never told the operator to
create one, while `/mechanize` emits only `sentinel.yaml` + `rationale.md` +
`functions/` — so the documented happy path always produced a directory that
CrashLoops. These tests pin the doc against the runtime it describes: the
template the skill tells the operator to write must survive the real
projection and the real loader.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROLLER_DIR = REPO_ROOT / "k8s" / "controller"
if str(CONTROLLER_DIR) not in sys.path:
    sys.path.insert(0, str(CONTROLLER_DIR))

# conftest.py already put spike/executor/ on sys.path.
from deployment import load_deployment  # noqa: E402

from projections import project_deployment  # noqa: E402

SKILL = REPO_ROOT / "adapters" / "claude" / "skills" / "deploy-sentinel" / "SKILL.md"
SCHEMA = REPO_ROOT / "common" / "deployment-schema.yaml"


def _skill_text() -> str:
    return SKILL.read_text()


def _deployment_templates() -> list[dict]:
    """Every fenced yaml block in the skill that is a SentinelDeployment."""
    blocks = re.findall(r"```yaml\n(.*?)```", _skill_text(), re.DOTALL)
    out = []
    for block in blocks:
        try:
            doc = yaml.safe_load(block)
        except yaml.YAMLError:
            continue
        if isinstance(doc, dict) and doc.get("kind") == "SentinelDeployment":
            out.append(doc)
    return out


def _schema_required() -> list[str]:
    return yaml.safe_load(SCHEMA.read_text())["required"]


def test_skill_does_not_call_the_directory_deployment_yaml_optional():
    """Stage 3 item 3 must not read "(optional)" for deployment.yaml."""
    text = _skill_text()
    match = re.search(r"^3\.\s+\*\*`deployment\.yaml`\*\*(.*)$", text, re.MULTILINE)
    assert match, "Stage 3 item 3 no longer names deployment.yaml"
    label = match.group(1)
    assert "optional" not in label.lower() or "not optional" in label.lower(), (
        f"deploy-sentinel Stage 3 still labels deployment.yaml {label.strip()!r}; "
        f"it is required — schemaVersion/kind/runtime come from nowhere else"
    )
    assert "required" in label.lower(), (
        "Stage 3 item 3 must say deployment.yaml is required"
    )


def test_skill_tells_the_operator_mechanize_does_not_emit_deployment_yaml():
    """The gap that makes this mandatory step invisible must be stated."""
    text = _skill_text().lower().replace("`", "").replace("*", "")
    assert "/mechanize does not emit one" in text, (
        "the skill must tell the operator that /mechanize emits no "
        "deployment.yaml, or a freshly mechanized directory silently skips "
        "the only step that makes it deployable"
    )


def test_skill_ships_a_deployment_template():
    assert _deployment_templates(), (
        "deploy-sentinel SKILL.md contains no `kind: SentinelDeployment` yaml "
        "block — the operator is told the file is required but not what to put "
        "in it"
    )


@pytest.mark.parametrize("key", _schema_required())
def test_skill_template_carries_every_schema_required_key(key):
    """Drift-catcher: adding a `required:` key to the schema breaks the doc."""
    for template in _deployment_templates():
        assert key in template, (
            f"deploy-sentinel's deployment.yaml template omits {key!r}, which "
            f"is in common/deployment-schema.yaml `required:` — a directory "
            f"written from this template fails load_deployment at pod start"
        )


def test_skill_template_survives_projection_and_loads(tmp_path):
    """End to end: doc template -> controller projection -> load_deployment.

    This is the test that fails outright when the skill ships no template at
    all, and fails with the pod-start error when the template is incomplete.
    """
    templates = _deployment_templates()
    assert templates, "no SentinelDeployment template in the skill"
    cr_capabilities = [
        {
            "id": "observability.list-services",
            "provider": "mcp",
            "endpointSecretRef": "cardinal-mcp-gateway",
            "tokenSecretRef": "cardinal-mcp-gateway",
        }
    ]
    for template in templates:
        projected = project_deployment(
            [{"id": "stdout"}], cr_capabilities, template
        )
        path = tmp_path / "deployment.yaml"
        path.write_text(projected)
        dep = load_deployment(path)
        assert dep.runtime, "projected deployment has no runtime"
        assert dep.findings_routing, (
            "the skill's template must carry findingsRouting — spec.sinks on "
            "the CR is a dead key, so without a routing rule no finding is "
            "ever delivered"
        )
        assert dep.binding_for("observability.list-services")["provider"] == "mcp"


def test_skill_template_runtime_id_is_registered():
    """Lint rule R15 FAILs any runtime id not in the integrations policy."""
    registry = yaml.safe_load(
        (REPO_ROOT / "common" / "integrations.yaml").read_text()
    )
    known = {
        r["id"] for r in (registry.get("integrations") or {}).get("runtime") or []
    }
    for template in _deployment_templates():
        assert template["runtime"] in known, (
            f"deploy-sentinel's template uses runtime "
            f"{template['runtime']!r}, which is not one of {sorted(known)} — "
            f"remote lint R15 fails it"
        )


def test_skill_template_does_not_enable_fixtures_by_default():
    """allowFixtures is the gate that keeps synthetic findings out of prod."""
    for template in _deployment_templates():
        assert not (template.get("execution") or {}).get("allowFixtures"), (
            "the skill's default template must not set "
            "execution.allowFixtures: true"
        )
