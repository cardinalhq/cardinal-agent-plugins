"""Tests for `fixture` resolving against capabilities outside the known list.

The bug these pin: the fixture provider was registered only for the six
capability ids enumerated in `_FIXTURE_CAPABILITIES`. But `_fixture_impl` reads
`fixtures/<node-or-capability>.json` and is entirely capability-agnostic, and
the mechanize compiler is explicitly allowed to mint new abstract capability
ids (CORE.md's `capability-registry-extension-needed` escape hatch). So any
Sentinel using a new id failed its own Stage 10 trial with UnknownProviderError
while its fixture file sat unread on disk.
"""
from __future__ import annotations

import json

import pytest

import capabilities as capabilities_mod


NEW_CAP = "observability.fetch-status-summary"  # not in _FIXTURE_CAPABILITIES


def test_fixture_resolves_for_any_capability():
    assert capabilities_mod.resolve_provider(NEW_CAP, "fixture") is capabilities_mod._fixture_impl


def test_fixture_resolves_for_an_entirely_novel_capability_family():
    assert capabilities_mod.resolve_provider("totally.invented", "fixture") is capabilities_mod._fixture_impl


def test_unknown_provider_still_raises():
    """Universality is per-provider opt-in — an unimplemented provider id
    (like the registry-era fictions `lakerunner`, `http-get`) must still fail."""
    with pytest.raises(capabilities_mod.UnknownProviderError):
        capabilities_mod.resolve_provider(NEW_CAP, "lakerunner")


def test_resolved_fixture_reads_the_node_file(tmp_path):
    """End-to-end: the fallback-resolved impl serves the captured fixture."""
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "fetch-status-summary.json").write_text(json.dumps({"text": "All Systems Operational"}))

    impl = capabilities_mod.resolve_provider(NEW_CAP, "fixture")
    out = impl(
        "fetch-status-summary",
        {"url": "https://example.test/status.json"},
        {"sentinel_dir": tmp_path, "capability_id": NEW_CAP},
    )
    assert out == {"text": "All Systems Operational"}


def test_missing_fixture_still_fails_loudly(tmp_path):
    """The fallback must not turn a missing capture into a silent empty result."""
    (tmp_path / "fixtures").mkdir()
    impl = capabilities_mod.resolve_provider(NEW_CAP, "fixture")
    with pytest.raises(FileNotFoundError):
        impl("no-such-node", {}, {"sentinel_dir": tmp_path, "capability_id": NEW_CAP})
