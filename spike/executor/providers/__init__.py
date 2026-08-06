"""Capability provider implementations.

The provider *registry* lives in ``capabilities.py``; this package holds the
concrete implementations. One module per provider, each registering itself at
import time via ``capabilities.provider(capability_id, provider_id)``.

``capabilities.py`` imports every module in this package at the bottom of its
own module body, so importing ``capabilities`` is enough to populate the
registry — no caller needs to remember to import a provider.

Unlike ``sinks``/``channels``, the auto-import loop here deliberately does NOT
swallow ``ImportError``. A silently-missing capability provider is exactly the
failure mode this package exists to remove: it degrades a Sentinel that asked
for live telemetry into an ``UnknownProviderError`` at DAG time (or, worse in
earlier revisions, into a legacy tool-cache read). Fail at import instead.
"""
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path


#: provider module name -> the earliest ``spike/executor/VERSION`` whose
#: published image (``ghcr.io/cardinalhq/sentinel-executor:v<VERSION>``)
#: actually contains that module.
#:
#: This exists because a provider is only real once an image carrying it is
#: published: ``.github/workflows/executor-image.yml`` is a no-op if the tag
#: already exists in GHCR, so adding a provider module without bumping
#: ``VERSION`` publishes nothing, and any CR pinned to the previous tag dies
#: with ``UnknownProviderError`` at its first tool node. Every entry here is
#: a claim about a *published* artifact, so a new provider must bump
#: ``VERSION`` in the same change that adds it (pinned by
#: ``spike/executor/tests/test_release_versions.py``).
FIRST_SHIPPED_IN_VERSION: dict[str, str] = {
    "mcp": "0.1.3",
}


def import_all() -> list[str]:
    """Import every provider module in this package. Returns module names."""
    pkg_dir = Path(__file__).resolve().parent
    names: list[str] = []
    for mod_info in pkgutil.iter_modules([str(pkg_dir)]):
        if mod_info.name.startswith("_"):
            continue
        importlib.import_module(f"{__name__}.{mod_info.name}")
        names.append(mod_info.name)
    return sorted(names)


__all__ = ["FIRST_SHIPPED_IN_VERSION", "import_all"]
