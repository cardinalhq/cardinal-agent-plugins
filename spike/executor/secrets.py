"""Secret-ref resolver.

Phase 1 registers `env://<VAR>` only. `k8s-secret://`, `vault://`,
`aws-sm://` are refused with a clear error naming Phase 2 as their target
so Sentinels won't silently paper over the missing implementation.

Additional resolvers register via `@scheme("myscheme")` in Phase 2.
"""
from __future__ import annotations

import os
from typing import Callable
from urllib.parse import urlparse


class SecretResolutionError(RuntimeError):
    """Raised when a secret_ref cannot be resolved."""


class UnsupportedSchemeError(SecretResolutionError):
    """Raised for schemes reserved for later phases."""


_RESOLVERS: dict[str, Callable[[str], str]] = {}

# Schemes that lint recognizes as valid but that this phase refuses to
# resolve. Keeping them here means the user sees a nice error rather than a
# generic "unknown scheme".
_PHASE2_SCHEMES = {"k8s-secret", "vault", "aws-sm"}


def scheme(name: str) -> Callable[[Callable[[str], str]], Callable[[str], str]]:
    """Register a resolver for a URL scheme."""

    def _decorator(fn: Callable[[str], str]) -> Callable[[str], str]:
        if name in _RESOLVERS:
            raise RuntimeError(f"duplicate resolver for scheme {name!r}")
        _RESOLVERS[name] = fn
        return fn

    return _decorator


@scheme("env")
def _resolve_env(target: str) -> str:
    # `env://VAR` → parsed as netloc=VAR, path=''. Some parsers put it in
    # path. Coerce.
    name = target.strip("/")
    if not name:
        raise SecretResolutionError("env:// requires a variable name")
    val = os.environ.get(name)
    if val is None:
        raise SecretResolutionError(f"env://{name} is not set in the process environment")
    return val


def resolve(secret_ref: str) -> str:
    """Resolve a `<scheme>://<target>` secret ref to its string value.

    Callers always receive a plain string. Never log the return value.
    """
    if not isinstance(secret_ref, str) or "://" not in secret_ref:
        raise SecretResolutionError(
            f"secret ref must be `<scheme>://<target>`, got {secret_ref!r}"
        )
    parsed = urlparse(secret_ref)
    s = parsed.scheme
    if s in _PHASE2_SCHEMES:
        raise UnsupportedSchemeError(
            f"secret scheme {s!r} is Phase 2 (not implemented in Phase 1); "
            f"use env:// or wait for the runtime daemon"
        )
    resolver = _RESOLVERS.get(s)
    if resolver is None:
        raise SecretResolutionError(f"no resolver registered for scheme {s!r}")
    # netloc holds the target for `env://VAR`; path holds the rest for
    # richer schemes. Concatenate so a single-argument resolver works
    # uniformly.
    target = parsed.netloc + parsed.path
    return resolver(target)


def registered_schemes() -> list[str]:
    return sorted(_RESOLVERS.keys())


__all__ = [
    "SecretResolutionError",
    "UnsupportedSchemeError",
    "resolve",
    "scheme",
    "registered_schemes",
]
