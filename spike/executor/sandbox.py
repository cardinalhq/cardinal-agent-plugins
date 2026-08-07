"""Enforcement for the `functions.<id>` policy block in deployment.yaml.

**The gap this closes.** `deployment.yaml` lets an operator write
`functions: {my-node: {network: disabled, filesystem: none}}`, `lint_remote`
requires the keys be present, and the JSON schema gives them defaults — and
then nothing read them. `executor.py`, `runtime_serve.py` and the k8s
controller had zero references to either key, and there was no NetworkPolicy
or seccomp profile behind them. Every function body ran with whatever access
the executor process had. Two consequences, both bad:

  * A reviewer reading a deployment.yaml saw `network: disabled` and
    reasonably concluded the function could not reach the network.
  * `trial.py` promises in its own docstring that a trial "reaches the
    network for nothing", because "a compile-time check that quietly queried
    production would be worse than no check". A generated function body doing
    an outbound call would have made that claim false while the trial
    reported itself hermetic.

**What this is, precisely.** A guard, not a sandbox. It denies the socket
layer in-process for the duration of one function call, which stops both
accidental network use and the ordinary deliberate kind (`urllib`, `requests`,
`http.client` — everything that ultimately calls `socket`). It does NOT
contain hostile code: a body that shells out via `subprocess`, or re-imports
`socket` through `importlib` after grabbing a raw handle, can get around it.
Treat it as making the declaration honest for code you compiled, not as a
boundary against code you don't trust. A real boundary needs process or
kernel isolation (subprocess + seccomp/landlock, or a per-function container)
and is tracked separately.

**Filesystem is still not enforced.** `filesystem: none` remains decorative:
intercepting file access in-process would mean patching `open`, `os`, `io`,
`pathlib` and `subprocess` and would still miss C-extension paths, so a guard
here would imply a guarantee it cannot keep. It is better to leave one
unenforced key clearly documented than to ship a second one that looks
enforced and isn't. `assert_filesystem_unenforced` exists so callers can be
explicit about knowing this.

Execution is sequential — `runtime_serve._run` is a single `for node_id in
order:` loop — so patching module-level socket attributes for the duration of
one call cannot leak into a concurrently-running tool node. If node execution
ever becomes concurrent, this approach must be revisited before that lands.
"""
from __future__ import annotations

import contextlib
import socket
from typing import Any, Iterator

#: Policy value that means "this function body may reach the network".
NETWORK_ENABLED = "enabled"
#: Policy value that means it may not. Also the default when unspecified —
#: an unlisted function is denied, matching deployment-schema.yaml's default
#: and failing closed rather than open.
NETWORK_DISABLED = "disabled"


class NetworkAccessDenied(RuntimeError):
    """A function body attempted network access under `network: disabled`."""


def network_policy(deployment_functions: dict[str, Any] | None, node_id: str) -> str:
    """Resolve the effective network policy for one function node.

    Defaults to `disabled` for a node with no entry, so adding a function node
    without touching deployment.yaml cannot silently grant it the network.
    """
    entry = (deployment_functions or {}).get(node_id) or {}
    value = entry.get("network", NETWORK_DISABLED)
    return NETWORK_ENABLED if value == NETWORK_ENABLED else NETWORK_DISABLED


@contextlib.contextmanager
def network_denied(node_id: str) -> Iterator[None]:
    """Deny the socket layer for the duration of the block.

    Restores the real attributes on the way out, including when the body
    raises — a leaked patch would break every later tool node's provider call
    in a way that looks like an unrelated network outage.
    """
    saved = (socket.socket, socket.create_connection, socket.getaddrinfo)

    def _deny(*_args: Any, **_kwargs: Any):
        raise NetworkAccessDenied(
            f"function node {node_id!r} attempted network access, but its "
            f"deployment policy is `network: {NETWORK_DISABLED}`. Grant it "
            f"explicitly with `functions.{node_id}.network: {NETWORK_ENABLED}` "
            f"in deployment.yaml if the call is intended — and note that a "
            f"network-reaching function cannot be trial-executed hermetically."
        )

    socket.socket = _deny  # type: ignore[assignment]
    socket.create_connection = _deny  # type: ignore[assignment]
    socket.getaddrinfo = _deny  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket, socket.create_connection, socket.getaddrinfo = saved  # type: ignore[assignment]


@contextlib.contextmanager
def function_guard(
    deployment_functions: dict[str, Any] | None, node_id: str
) -> Iterator[None]:
    """Apply the declared `functions.<node_id>` policy around one call."""
    if network_policy(deployment_functions, node_id) == NETWORK_ENABLED:
        yield
        return
    with network_denied(node_id):
        yield


def assert_filesystem_unenforced() -> str:
    """Name the remaining gap, so it is cited rather than assumed handled."""
    return (
        "filesystem policy is declared in deployment.yaml and validated by "
        "lint_remote, but NOT enforced at runtime; see sandbox.py module docstring"
    )


__all__ = [
    "NETWORK_DISABLED",
    "NETWORK_ENABLED",
    "NetworkAccessDenied",
    "assert_filesystem_unenforced",
    "function_guard",
    "network_denied",
    "network_policy",
]
