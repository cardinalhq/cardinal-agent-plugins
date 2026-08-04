"""Findings sink registry.

A sink accepts a finding + sink params and reports back a delivery
status. Sinks register via ``@sink("sink-id")`` at import time; adding a
new sink = one file in this package. The runtime imports every module in
this package via the auto-import loop below.

Phase 1 ships ``stdout``. Phase 2 adds ``slack``, ``outcomes-dashboard``,
``pagerduty``, ``jira``.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class SinkError(RuntimeError):
    pass


@dataclass
class SinkResult:
    delivered: bool
    detail: str | None = None


class Sink(Protocol):
    id: str

    def deliver(self, finding: dict[str, Any], params: dict[str, Any]) -> SinkResult: ...


_SINKS: dict[str, Sink] = {}


def sink(sink_id: str):
    def _decorator(cls: type) -> type:
        if sink_id in _SINKS:
            raise RuntimeError(f"duplicate sink registration: {sink_id!r}")
        instance = cls()
        setattr(instance, "id", sink_id)
        _SINKS[sink_id] = instance
        return cls

    return _decorator


def resolve_sink(sink_id: str) -> Sink:
    if sink_id not in _SINKS:
        raise KeyError(
            f"no sink registered for {sink_id!r}; registered: {sorted(_SINKS)}"
        )
    return _SINKS[sink_id]


def registered_sinks() -> list[str]:
    return sorted(_SINKS.keys())


def _auto_import() -> None:
    pkg_dir = Path(__file__).parent
    for mod_info in pkgutil.iter_modules([str(pkg_dir)]):
        if mod_info.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod_info.name}")
        except ImportError:
            continue


_auto_import()


__all__ = [
    "Sink",
    "SinkError",
    "SinkResult",
    "sink",
    "resolve_sink",
    "registered_sinks",
]
