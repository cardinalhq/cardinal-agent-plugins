"""Tests for enforcement of the deployment `functions.<id>` policy block.

The gap: `network: disabled` was declared in deployment.yaml, required by
lint_remote, defaulted in the JSON schema — and read by nothing. Function
bodies ran with whatever access the executor process had, which also made
trial.py's "reaches the network for nothing" claim untrue for any Sentinel
whose function body called out.
"""
from __future__ import annotations

import socket

import pytest

import sandbox


# --------------------------------------------------------------------------- #
# Policy resolution — fails closed                                             #
# --------------------------------------------------------------------------- #


def test_unlisted_function_defaults_to_denied():
    """Adding a function node without touching deployment.yaml must not grant network."""
    assert sandbox.network_policy({}, "new-node") == sandbox.NETWORK_DISABLED


def test_none_functions_block_defaults_to_denied():
    assert sandbox.network_policy(None, "any") == sandbox.NETWORK_DISABLED


def test_explicit_enabled_is_honoured():
    fns = {"fetch": {"network": "enabled"}}
    assert sandbox.network_policy(fns, "fetch") == sandbox.NETWORK_ENABLED


def test_explicit_disabled_is_honoured():
    fns = {"parse": {"network": "disabled"}}
    assert sandbox.network_policy(fns, "parse") == sandbox.NETWORK_DISABLED


def test_unrecognised_value_is_treated_as_denied():
    """Anything that isn't exactly `enabled` denies — no truthy-string surprises."""
    assert sandbox.network_policy({"n": {"network": "yes"}}, "n") == sandbox.NETWORK_DISABLED


# --------------------------------------------------------------------------- #
# Enforcement                                                                  #
# --------------------------------------------------------------------------- #


def test_socket_creation_is_denied_inside_the_guard():
    with pytest.raises(sandbox.NetworkAccessDenied):
        with sandbox.network_denied("fetch-thing"):
            socket.socket(socket.AF_INET, socket.SOCK_STREAM)


def test_dns_resolution_is_denied_inside_the_guard():
    """getaddrinfo is patched too — otherwise a body leaks hostnames before failing."""
    with pytest.raises(sandbox.NetworkAccessDenied):
        with sandbox.network_denied("fetch-thing"):
            socket.getaddrinfo("example.invalid", 80)


def test_create_connection_is_denied_inside_the_guard():
    with pytest.raises(sandbox.NetworkAccessDenied):
        with sandbox.network_denied("fetch-thing"):
            socket.create_connection(("example.invalid", 80))


def test_the_denial_names_the_node_and_the_grant():
    with pytest.raises(sandbox.NetworkAccessDenied) as excinfo:
        with sandbox.network_denied("fetch-status"):
            socket.socket()
    message = str(excinfo.value)
    assert "fetch-status" in message
    assert "network: enabled" in message


def test_urllib_is_blocked_because_it_bottoms_out_at_socket():
    """The guard has to stop the ordinary way a body would call out, not just raw sockets."""
    import urllib.request

    with pytest.raises(Exception) as excinfo:
        with sandbox.network_denied("fetch-thing"):
            urllib.request.urlopen("http://example.invalid/status", timeout=1)
    assert "NetworkAccessDenied" in repr(excinfo.value) or isinstance(
        excinfo.value, sandbox.NetworkAccessDenied
    )


# --------------------------------------------------------------------------- #
# Restoration — a leaked patch would break later tool nodes                    #
# --------------------------------------------------------------------------- #


def test_socket_is_restored_on_clean_exit():
    original = socket.socket
    with sandbox.network_denied("n"):
        pass
    assert socket.socket is original


def test_socket_is_restored_when_the_body_raises():
    """A leaked patch would surface much later as a bogus network outage."""
    original = socket.socket
    with pytest.raises(ValueError):
        with sandbox.network_denied("n"):
            raise ValueError("body blew up")
    assert socket.socket is original


def test_socket_is_restored_after_a_denial():
    original = socket.socket
    with pytest.raises(sandbox.NetworkAccessDenied):
        with sandbox.network_denied("n"):
            socket.socket()
    assert socket.socket is original


# --------------------------------------------------------------------------- #
# function_guard — the policy-applying wrapper                                 #
# --------------------------------------------------------------------------- #


def test_function_guard_permits_network_when_granted():
    original = socket.socket
    with sandbox.function_guard({"fetch": {"network": "enabled"}}, "fetch"):
        assert socket.socket is original  # not patched at all


def test_function_guard_denies_when_not_granted():
    with pytest.raises(sandbox.NetworkAccessDenied):
        with sandbox.function_guard({"fetch": {"network": "disabled"}}, "fetch"):
            socket.socket()


def test_function_guard_denies_an_unlisted_node():
    with pytest.raises(sandbox.NetworkAccessDenied):
        with sandbox.function_guard({}, "unlisted"):
            socket.socket()


def test_filesystem_gap_is_stated_rather_than_implied():
    """The remaining unenforced key must stay loudly documented, not silently absent."""
    assert "NOT enforced" in sandbox.assert_filesystem_unenforced()
