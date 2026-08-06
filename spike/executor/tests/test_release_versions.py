"""Release-version drift catcher: the default image must contain the providers.

The failure this pins is not hypothetical — it was live in the working tree.
`spike/executor/providers/mcp.py` was added while `spike/executor/VERSION`
stayed at `0.1.2`, and both the controller default
(`k8s/controller/reconciler.py:DEFAULT_EXECUTOR_IMAGE`) and the operator-facing
deploy-sentinel skill told the operator to run
`ghcr.io/cardinalhq/sentinel-executor:v0.1.2`.

`.github/workflows/executor-image.yml` tags `v<VERSION>` and is an explicit
no-op if the tag already exists in GHCR, so merging would have published
nothing and `v0.1.2` would have stayed the mcp-less image. Every scheduled run
of a Sentinel binding `provider: mcp` would have died with
`UnknownProviderError` at its first tool node.

The invariant these tests encode: **a provider module in the tree is only
usable once VERSION is at or past the version its published image carries it
in**, and every place that names a default image must agree with VERSION.
"""
from __future__ import annotations

import pkgutil
import re
from pathlib import Path

import pytest

import providers

REPO_ROOT = Path(__file__).resolve().parents[3]
VERSION_FILE = REPO_ROOT / "spike" / "executor" / "VERSION"
PROVIDERS_DIR = REPO_ROOT / "spike" / "executor" / "providers"
RECONCILER = REPO_ROOT / "k8s" / "controller" / "reconciler.py"
DEPLOY_SKILL = (
    REPO_ROOT / "adapters" / "claude" / "skills" / "deploy-sentinel" / "SKILL.md"
)

#: v0.1.0 was amd64-only (see .github/workflows/executor-image.yml history);
#: prod nodes are arm64 Graviton, so anything below this ImagePullBackOffs.
MULTI_ARCH_FLOOR = (0, 1, 1)


def _parse(version: str) -> tuple[int, ...]:
    parts = version.strip().lstrip("v").split(".")
    assert len(parts) == 3, f"expected X.Y.Z, got {version!r}"
    return tuple(int(p) for p in parts)


def _executor_version() -> str:
    return VERSION_FILE.read_text().strip()


def _provider_module_names() -> list[str]:
    return sorted(
        m.name
        for m in pkgutil.iter_modules([str(PROVIDERS_DIR)])
        if not m.name.startswith("_")
    )


def _controller_default_image() -> str:
    match = re.search(
        r'^DEFAULT_EXECUTOR_IMAGE\s*=\s*"([^"]+)"',
        RECONCILER.read_text(),
        re.MULTILINE,
    )
    assert match, "DEFAULT_EXECUTOR_IMAGE not found in k8s/controller/reconciler.py"
    return match.group(1)


def _skill_default_image() -> str:
    match = re.search(
        r'Ask: "Executor image\?" Default: `([^`]+)`',
        DEPLOY_SKILL.read_text(),
    )
    assert match, "deploy-sentinel SKILL.md §4.7 no longer states a default image"
    return match.group(1)


def test_every_provider_module_declares_the_version_it_first_ships_in():
    """A new provider must record which published tag carries it.

    Without this, the next provider repeats the mcp mistake silently: the
    module exists in the tree, the registry resolves it locally, and every
    published image lacks it.
    """
    declared = set(providers.FIRST_SHIPPED_IN_VERSION)
    present = set(_provider_module_names())
    assert present <= declared, (
        f"provider module(s) {sorted(present - declared)} have no entry in "
        f"providers.FIRST_SHIPPED_IN_VERSION — add one and bump "
        f"spike/executor/VERSION to match, or no published image contains them"
    )
    assert declared <= present, (
        f"providers.FIRST_SHIPPED_IN_VERSION names {sorted(declared - present)}, "
        f"which is not a module under spike/executor/providers/"
    )


def test_executor_version_is_at_least_every_provider_floor():
    """VERSION must be >= the version each in-tree provider first ships in.

    This is the assertion that fails on the exact bug: VERSION 0.1.2 with
    providers/mcp.py present and FIRST_SHIPPED_IN_VERSION['mcp'] == '0.1.3'.
    """
    version = _executor_version()
    for name in _provider_module_names():
        floor = providers.FIRST_SHIPPED_IN_VERSION[name]
        assert _parse(version) >= _parse(floor), (
            f"spike/executor/VERSION is {version} but provider {name!r} first "
            f"ships in {floor} — v{version} is published without it, so any CR "
            f"pinned to v{version} raises UnknownProviderError at its first "
            f"tool node. Bump VERSION to at least {floor}."
        )


def test_executor_version_is_multi_arch_capable():
    version = _executor_version()
    assert _parse(version) >= MULTI_ARCH_FLOOR, (
        f"spike/executor/VERSION is {version}; tags below "
        f"v{'.'.join(map(str, MULTI_ARCH_FLOOR))} are amd64-only and fail to "
        f"pull on the prod arm64 nodes"
    )


def test_controller_default_image_tracks_executor_version():
    image = _controller_default_image()
    version = _executor_version()
    assert image == f"ghcr.io/cardinalhq/sentinel-executor:v{version}", (
        f"reconciler.DEFAULT_EXECUTOR_IMAGE is {image!r} but "
        f"spike/executor/VERSION is {version!r}"
    )


def test_deploy_sentinel_skill_default_image_matches_the_controller_default():
    """The operator-facing default and the compiled-in default must agree.

    They diverged silently once already; an operator who accepts the skill's
    default gets whatever tag the doc happens to name, not what the controller
    would have chosen.
    """
    skill_image = _skill_default_image()
    assert skill_image == _controller_default_image(), (
        f"deploy-sentinel SKILL.md §4.7 defaults to {skill_image!r} but "
        f"reconciler.DEFAULT_EXECUTOR_IMAGE is {_controller_default_image()!r}"
    )


@pytest.mark.parametrize("name", _provider_module_names())
def test_skill_default_image_is_new_enough_for_every_provider(name):
    """The tag the skill hands an operator must contain every provider.

    §4.5 tells the operator to answer "live telemetry" → `provider: mcp` and
    §4.7 hands them a default tag. If that tag predates the provider, the two
    instructions compose into a CR that fails on every scheduled run.
    """
    tag = _skill_default_image().rsplit(":", 1)[1]
    floor = providers.FIRST_SHIPPED_IN_VERSION[name]
    assert _parse(tag) >= _parse(floor), (
        f"deploy-sentinel SKILL.md §4.7 defaults to {tag}, which predates "
        f"provider {name!r} (first shipped in v{floor}); the skill also offers "
        f"{name!r} in §4.5"
    )
