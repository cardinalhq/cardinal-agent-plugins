"""Channel driver registry.

A channel driver is what publishes an ask_human question to an operator
rail (Slack, CLI, ticket, ...) and reads the reply back. Drivers register
via `@channel("integration-id")` at import time. Adding a new channel =
one file in `channels/` with a `@channel(...)` decorator — the runtime
imports it via the auto-import loop below.

Every driver implements the `ChannelDriver` protocol:

    publish(publish_ctx) -> PublishHandle
    wait_for_reply(handle, deadline) -> Reply | None

Reply carries the raw operator text plus operator identity so identity
policy can be enforced before the parser is invoked. The runtime treats
`None` as "no reply by deadline" and applies the escalation/timeout
policy.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol


class ChannelDriverError(RuntimeError):
    """Raised for driver-level failures (connection, auth, ...)."""


@dataclass
class PublishContext:
    """Everything a driver needs at publish time.

    ``binding`` is the full ``askHumanBindings.<node-id>`` dict from
    deployment.yaml so drivers can read their own params (channel_id,
    token_ref, ...). ``resolve_secret`` is passed in so drivers do not
    import secrets directly — keeps the driver interface pure.
    """

    node_id: str
    question: str
    evidence: dict[str, Any]
    binding: dict[str, Any]
    channel_params: dict[str, Any]
    resolve_secret: Callable[[str], str]
    # Runtime-controlled context (mostly for tests).
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class PublishHandle:
    """Opaque-ish reference the driver returns for later reply lookup.

    For Slack this holds thread ts + channel id. For CLI it's a synthetic
    id. For test.mock it's the node id.
    """

    channel_id: str
    reference: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Reply:
    """A raw operator reply, pre-normalization.

    ``operator_id`` is the string the driver reports for the person
    replying (e.g. Slack user id, CLI-configured user, ...). Identity
    policy is enforced by askhuman.py, NOT by the driver — the driver's
    only job is to surface identity truthfully.
    """

    raw_text: str
    operator_id: str | None
    received_at: datetime
    channel_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ChannelDriver(Protocol):
    id: str

    def publish(self, ctx: PublishContext) -> PublishHandle: ...

    def wait_for_reply(
        self, handle: PublishHandle, deadline: datetime
    ) -> Reply | None: ...


_DRIVERS: dict[str, ChannelDriver] = {}


def channel(driver_id: str):
    """Register a channel driver class. Instantiated eagerly at import."""

    def _decorator(cls: type) -> type:
        if driver_id in _DRIVERS:
            raise RuntimeError(f"duplicate channel driver registration: {driver_id!r}")
        instance = cls()
        # Force id attribute for downstream inspection.
        setattr(instance, "id", driver_id)
        _DRIVERS[driver_id] = instance
        return cls

    return _decorator


def resolve_channel(channel_id: str) -> ChannelDriver:
    if channel_id not in _DRIVERS:
        raise KeyError(
            f"no channel driver registered for {channel_id!r}; "
            f"registered: {sorted(_DRIVERS)}"
        )
    return _DRIVERS[channel_id]


def registered_channels() -> list[str]:
    return sorted(_DRIVERS.keys())


def _auto_import() -> None:
    """Import every module in this package so decorators fire.

    Silently swallows ImportError from optional drivers (e.g. slack_socket
    without slack-sdk installed) — those drivers simply won't be
    registered.
    """
    pkg_dir = Path(__file__).parent
    for mod_info in pkgutil.iter_modules([str(pkg_dir)]):
        if mod_info.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod_info.name}")
        except ImportError:
            # Optional dependency missing. Callers get a clear error later
            # when they try to resolve() this channel by id.
            continue


_auto_import()


__all__ = [
    "ChannelDriver",
    "ChannelDriverError",
    "PublishContext",
    "PublishHandle",
    "Reply",
    "channel",
    "resolve_channel",
    "registered_channels",
]
