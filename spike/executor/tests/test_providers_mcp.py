"""Unit tests for the ``mcp`` capability provider.

No test in this file touches the network: every one replaces
``providers.mcp._urlopen`` (the single seam through which the module reaches
urllib) with a fake. A test that forgets to patch it fails on
``_urlopen not patched`` rather than dialling out — see ``_forbid_network``.
"""
from __future__ import annotations

import email.message
import http.client
import io
import json
import socket
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

import capabilities as capabilities_mod
import executor as executor_mod
import providers.mcp as mcp


ENDPOINT_VAR = "TEST_CAP_MCP_ENDPOINT"
TOKEN_VAR = "TEST_CAP_MCP_TOKEN"
TOKEN = "sk-not-a-real-token-0123456789"
FULL_URL = "http://maestro-maestro.maestro.svc.cluster.local:4200/api/orgs/acme/mcp"

CAP_LIST = "observability.list-services"
CAP_METRICS = "observability.query-metrics"


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #


class FakeResponse:
    """Stand-in for ``http.client.HTTPResponse``.

    ``read_raises`` models the failure window that matters in production: the
    gateway flushes ``200`` + ``text/event-stream`` as soon as the POST stream
    opens and only writes the ``data:`` frame when the tool returns, so a
    timeout / pod restart / truncated stream surfaces from ``read()``, long
    after ``urlopen()`` returned. Without it no test can reach that path.
    """

    def __init__(
        self,
        body=b"",
        content_type="application/json",
        status=200,
        headers=None,
        read_raises=None,
    ):
        self._body = body if isinstance(body, bytes) else body.encode("utf-8")
        self._pos = 0
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.status = status
        self.closed = False
        self.read_raises = read_raises

    def read(self, n=-1):
        if self.read_raises is not None:
            raise self.read_raises
        if n is None or n < 0:
            chunk = self._body[self._pos :]
        else:
            chunk = self._body[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk

    def close(self):
        self.closed = True


def http_error(code, body=b"", headers=None):
    hdrs = email.message.Message()
    for key, value in (headers or {}).items():
        hdrs[key] = value
    return urllib.error.HTTPError(FULL_URL, code, "err", hdrs, io.BytesIO(body))


def envelope(result, request_id=1):
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


def sse(payload_json):
    """Frame exactly as go-sdk's writeEvent does: note the space after data:."""
    return f"event: message\ndata: {payload_json}\n\n"


@pytest.fixture(autouse=True)
def _forbid_network(monkeypatch):
    def _boom(request, timeout):
        raise AssertionError("_urlopen not patched — test would hit the network")

    monkeypatch.setattr(mcp, "_urlopen", _boom)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv(ENDPOINT_VAR, FULL_URL)
    monkeypatch.setenv(TOKEN_VAR, TOKEN)


def binding(**overrides):
    b = {"provider": "mcp", "endpoint_env": ENDPOINT_VAR, "token_env": TOKEN_VAR}
    b.update(overrides)
    return b


def ctx(capability_id=CAP_LIST, bind=None, tmp_path=None):
    return {
        "run_dir": tmp_path,
        "sentinel_dir": tmp_path,
        "capability_id": capability_id,
        "binding": binding() if bind is None else bind,
    }


class Recorder:
    """Captures the outgoing Request and returns a canned response."""

    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.request = None
        self.timeout = None
        self.calls = 0

    def __call__(self, request, timeout):
        self.calls += 1
        self.request = request
        self.timeout = timeout
        if self.raises is not None:
            raise self.raises
        return self.response

    @property
    def body(self):
        return json.loads(self.request.data.decode("utf-8"))

    @property
    def arguments(self):
        return self.body["params"]["arguments"]


def install(monkeypatch, response=None, raises=None) -> Recorder:
    rec = Recorder(response=response, raises=raises)
    monkeypatch.setattr(mcp, "_urlopen", rec)
    return rec


# --------------------------------------------------------------------------- #
# Registration                                                                 #
# --------------------------------------------------------------------------- #


def test_registers_mcp_for_all_four_observability_capabilities():
    registered = set(capabilities_mod.registered_providers())
    for capability_id in (
        "observability.list-services",
        "observability.error-overview",
        "observability.query-logs",
        "observability.query-metrics",
    ):
        assert (capability_id, "mcp") in registered


def test_capability_to_gateway_tool_mapping_is_exact():
    assert mcp.CAPABILITY_TOOLS == {
        "observability.list-services": "lakerunner__list_services",
        "observability.error-overview": "lakerunner__error_overview",
        "observability.query-logs": "lakerunner__execute_logs_query",
        "observability.query-metrics": "lakerunner__execute_metrics_query",
    }


def test_fixture_provider_is_universal_and_is_the_test_default():
    # No per-capability fixture registrations exist — `fixture` is a universal
    # provider serving any id (capability inventories are transcript-derived).
    assert capabilities_mod.resolve_provider(CAP_LIST, "fixture") is not mcp.call
    assert capabilities_mod.resolve_provider("never-seen-before", "fixture") is not mcp.call


def test_resolve_provider_returns_the_mcp_impl():
    assert capabilities_mod.resolve_provider(CAP_LIST, "mcp") is mcp.call


# --------------------------------------------------------------------------- #
# Request shape                                                                #
# --------------------------------------------------------------------------- #


def test_request_is_a_single_stateless_jsonrpc_post_with_both_headers(env, monkeypatch, tmp_path):
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"services": ["a"]}})))

    out = mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))

    assert out == {"services": ["a"]}
    assert rec.calls == 1  # ONE call: no initialize handshake
    req = rec.request
    assert req.get_method() == "POST"
    assert req.full_url == FULL_URL
    assert req.get_header("Content-type") == "application/json"
    # Both media types are mandatory: the gateway 400s a POST missing either.
    assert req.get_header("Accept") == "application/json, text/event-stream"
    assert req.get_header("X-cardinalhq-api-key") == TOKEN
    assert req.get_header("Mcp-session-id") is None
    body = rec.body
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "lakerunner__list_services"
    assert body["params"]["arguments"] == {"instance": "prod"}
    assert "_meta" not in body["params"]


def test_datetime_and_timedelta_arguments_serialize_like_the_executor(env, monkeypatch, tmp_path):
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    args = {
        "instance": "prod",
        "start": datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc),
        "end": datetime(2026, 8, 5, 13, 0, 0, tzinfo=timezone.utc),
        "window": timedelta(hours=1),
    }
    mcp.call("n1", args, ctx(tmp_path=tmp_path))
    assert rec.arguments["start"] == "2026-08-05T12:00:00Z"
    assert rec.arguments["end"] == "2026-08-05T13:00:00Z"
    assert rec.arguments["window"] == "3600s"


def test_json_default_agrees_with_executor_json_default():
    samples = [
        datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        datetime(2026, 1, 2, 3, 4, 5),
        timedelta(seconds=90),
        {"b", "a"},
    ]
    for sample in samples:
        assert mcp._json_default(sample) == executor_mod._json_default(sample)


def test_binding_params_supply_argument_defaults_but_node_args_win(env, monkeypatch, tmp_path):
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    bind = binding(params={"instance": "from-binding", "limit": 50, "org_id": "acme"})
    mcp.call("n1", {"limit": 10}, ctx(bind=bind, tmp_path=tmp_path))
    assert rec.arguments["instance"] == "from-binding"
    assert rec.arguments["limit"] == 10
    # org_id is URL material, not a tool argument.
    assert "org_id" not in rec.arguments


def test_timeout_defaults_to_180s_and_is_overridable_per_binding(env, monkeypatch, tmp_path):
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert rec.timeout == 180.0
    assert mcp.DEFAULT_TIMEOUT_SECONDS == 180.0

    rec2 = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    mcp.call("n1", {"instance": "prod"}, ctx(bind=binding(timeoutSeconds=12), tmp_path=tmp_path))
    assert rec2.timeout == 12.0


def test_response_size_cap_default_is_8_mib():
    assert mcp.MAX_RESPONSE_BYTES == 8 << 20


# --------------------------------------------------------------------------- #
# Success decoding                                                             #
# --------------------------------------------------------------------------- #


def test_success_via_structured_content(env, monkeypatch, tmp_path):
    payload = {"services": [{"name": "checkout"}], "shown": 1, "total_count": 1}
    install(
        monkeypatch,
        FakeResponse(
            envelope(
                {
                    # The real gateway ALSO sends a prose text block; the
                    # provider must ignore it in favour of structuredContent.
                    "content": [{"type": "text", "text": "Found 1 service"}],
                    "structuredContent": payload,
                }
            )
        ),
    )
    assert mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path)) == payload


def test_success_via_text_fallback_when_structured_content_absent(env, monkeypatch, tmp_path):
    payload = {"services": [], "shown": 0, "total_count": 0}
    install(
        monkeypatch,
        FakeResponse(envelope({"content": [{"type": "text", "text": json.dumps(payload)}]})),
    )
    assert mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path)) == payload


def test_sse_framed_response_is_the_normal_path(env, monkeypatch, tmp_path):
    payload = {"services": ["a"]}
    install(
        monkeypatch,
        FakeResponse(
            sse(envelope({"structuredContent": payload})),
            content_type="text/event-stream",
        ),
    )
    assert mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path)) == payload


def test_sse_skips_empty_data_frames_and_takes_the_first_payload(env, monkeypatch, tmp_path):
    body = "event: prime\ndata:\n\n" + sse(envelope({"structuredContent": {"ok": True}}))
    install(monkeypatch, FakeResponse(body, content_type="text/event-stream"))
    assert mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path)) == {"ok": True}


def test_sse_with_no_data_frame_raises(env, monkeypatch, tmp_path):
    install(monkeypatch, FakeResponse("event: message\n\n", content_type="text/event-stream"))
    with pytest.raises(mcp.McpProtocolError, match="no non-empty"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


# --------------------------------------------------------------------------- #
# Failure modes — never a partial/empty success                                #
# --------------------------------------------------------------------------- #


def test_is_error_true_raises_tool_error_not_a_result(env, monkeypatch, tmp_path):
    install(
        monkeypatch,
        FakeResponse(
            envelope(
                {
                    "isError": True,
                    "content": [{"type": "text", "text": 'validating "arguments": missing instance'},],
                }
            )
        ),
    )
    with pytest.raises(mcp.McpToolError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert "isError=true" in str(excinfo.value)
    assert 'validating "arguments"' in str(excinfo.value)
    assert "lakerunner__list_services" in str(excinfo.value)
    # Distinct from a JSON-RPC protocol error.
    assert not isinstance(excinfo.value, mcp.McpProtocolError)


def test_instance_required_gets_its_own_error_with_available_instances(env, monkeypatch, tmp_path):
    text = json.dumps({"error": "instance_required", "availableInstances": ["prod", "otel-demo"]})
    install(
        monkeypatch,
        FakeResponse(envelope({"isError": True, "content": [{"type": "text", "text": text}]})),
    )
    with pytest.raises(mcp.McpInstanceRequiredError) as excinfo:
        mcp.call("n1", {"instance": "nope"}, ctx(tmp_path=tmp_path))
    assert excinfo.value.available == ["prod", "otel-demo"]
    assert "prod" in str(excinfo.value)
    assert "instance" in str(excinfo.value)
    assert isinstance(excinfo.value, mcp.McpToolError)


def test_jsonrpc_error_object_is_a_different_failure_mode(env, monkeypatch, tmp_path):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": 'unknown tool "x"'}}
    )
    install(monkeypatch, FakeResponse(body))
    with pytest.raises(mcp.McpProtocolError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert excinfo.value.code == -32602
    assert "tools/list" in str(excinfo.value)  # config hint, not a retry hint
    assert not isinstance(excinfo.value, mcp.McpToolError)


def test_missing_result_raises(env, monkeypatch, tmp_path):
    install(monkeypatch, FakeResponse(json.dumps({"jsonrpc": "2.0", "id": 1})))
    with pytest.raises(mcp.McpProtocolError, match="neither 'result' nor 'error'"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_prose_text_with_no_structured_content_raises_rather_than_returning_nothing(
    env, monkeypatch, tmp_path
):
    # The real shape if the gateway ever stopped setting structuredContent:
    # content[0].text is prose, not JSON. Must NOT look like success.
    install(
        monkeypatch,
        FakeResponse(envelope({"content": [{"type": "text", "text": "Found 12 services"}]})),
    )
    with pytest.raises(mcp.McpProtocolError, match="no usable payload"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_empty_structured_content_is_not_treated_as_success(env, monkeypatch, tmp_path):
    install(monkeypatch, FakeResponse(envelope({"structuredContent": {}})))
    with pytest.raises(mcp.McpProtocolError, match="EMPTY object"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_non_object_structured_content_raises(env, monkeypatch, tmp_path):
    install(monkeypatch, FakeResponse(envelope({"structuredContent": [1, 2, 3]})))
    with pytest.raises(mcp.McpProtocolError, match="expected a JSON object"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_unparseable_body_raises(env, monkeypatch, tmp_path):
    install(monkeypatch, FakeResponse("<html>502 Bad Gateway</html>"))
    with pytest.raises(mcp.McpProtocolError, match="not JSON"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_jsonrpc_id_mismatch_raises(env, monkeypatch, tmp_path):
    install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}}, request_id=99)))
    with pytest.raises(mcp.McpProtocolError, match="id mismatch"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


# --------------------------------------------------------------------------- #
# Transport failures                                                           #
# --------------------------------------------------------------------------- #


def test_timeout_raises_timeout_error(env, monkeypatch, tmp_path):
    install(monkeypatch, raises=TimeoutError("timed out"))
    with pytest.raises(mcp.McpTimeoutError, match="did not respond within 180.0s"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_timeout_wrapped_in_urlerror_raises_timeout_error(env, monkeypatch, tmp_path):
    install(monkeypatch, raises=urllib.error.URLError(TimeoutError("timed out")))
    with pytest.raises(mcp.McpTimeoutError):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_connection_failure_raises_transport_error(env, monkeypatch, tmp_path):
    install(monkeypatch, raises=urllib.error.URLError("Connection refused"))
    with pytest.raises(mcp.McpTransportError, match="cannot reach gateway"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


# The gateway leaves JSONResponse false, so urlopen() returns as soon as the
# SSE headers are flushed and ~all of the tool's latency lives in the body
# read. Every socket-level failure in the window that actually matters happens
# HERE, not at connect — so the read phase needs the same exception mapping.


def test_read_phase_timeout_raises_timeout_error(env, monkeypatch, tmp_path):
    resp = FakeResponse(read_raises=TimeoutError("timed out"))
    install(monkeypatch, resp)
    with pytest.raises(mcp.McpTimeoutError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    message = str(excinfo.value)
    assert CAP_LIST in message
    assert FULL_URL in message
    assert "180.0s" in message
    assert resp.closed  # _closing still runs on the way out


def test_read_phase_socket_timeout_alias_raises_timeout_error(env, monkeypatch, tmp_path):
    # socket.timeout IS TimeoutError, which IS an OSError: if the generic
    # OSError branch were ordered first this would be an McpTransportError.
    install(monkeypatch, FakeResponse(read_raises=socket.timeout("timed out")))
    with pytest.raises(mcp.McpTimeoutError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert CAP_LIST in str(excinfo.value)


def test_read_phase_connection_reset_raises_transport_error(env, monkeypatch, tmp_path):
    # What a maestro pod restart / OOM-kill mid-query looks like to the client.
    resp = FakeResponse(read_raises=ConnectionResetError("Connection reset by peer"))
    install(monkeypatch, resp)
    with pytest.raises(mcp.McpTransportError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    message = str(excinfo.value)
    assert not isinstance(excinfo.value, mcp.McpTimeoutError)
    assert CAP_LIST in message
    assert FULL_URL in message
    assert "ConnectionResetError" in message
    assert resp.closed


def test_read_phase_incomplete_read_raises_transport_error(env, monkeypatch, tmp_path):
    # IncompleteRead is NOT an OSError, so it needs its own branch.
    exc = http.client.IncompleteRead(b'{"jsonrpc"', 4096)
    assert not isinstance(exc, OSError)
    install(monkeypatch, FakeResponse(read_raises=exc))
    with pytest.raises(mcp.McpTransportError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    message = str(excinfo.value)
    assert CAP_LIST in message
    assert FULL_URL in message
    assert "IncompleteRead" in message


def test_read_phase_failure_never_leaks_the_token_or_the_query_string(monkeypatch, tmp_path):
    monkeypatch.setenv(ENDPOINT_VAR, FULL_URL + "?apiKey=leaky-secret")
    monkeypatch.setenv(TOKEN_VAR, TOKEN)
    install(monkeypatch, FakeResponse(read_raises=ConnectionResetError(f"reset {TOKEN}")))
    with pytest.raises(mcp.McpTransportError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert TOKEN not in str(excinfo.value)
    assert "leaky-secret" not in str(excinfo.value)


def test_oversized_response_raises_and_is_never_partially_decoded(env, monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "MAX_RESPONSE_BYTES", 64)
    payload = envelope({"structuredContent": {"services": ["x" * 200]}})
    install(monkeypatch, FakeResponse(payload))
    with pytest.raises(mcp.McpTransportError, match="exceeded 64 bytes"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_401_raises_a_distinct_auth_error_and_never_leaks_the_token(env, monkeypatch, tmp_path):
    install(monkeypatch, raises=http_error(401, b"unauthorized"))
    with pytest.raises(mcp.McpAuthError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert excinfo.value.status == 401
    assert TOKEN not in str(excinfo.value)
    assert "token_env" in str(excinfo.value)


def test_403_is_an_auth_error(env, monkeypatch, tmp_path):
    install(monkeypatch, raises=http_error(403, b"forbidden"))
    with pytest.raises(mcp.McpAuthError):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_429_surfaces_retry_after(env, monkeypatch, tmp_path):
    install(monkeypatch, raises=http_error(429, b"slow down", headers={"Retry-After": "7"}))
    with pytest.raises(mcp.McpRateLimitError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert excinfo.value.retry_after == "7"


def test_500_raises_transport_error_with_truncated_body(env, monkeypatch, tmp_path):
    install(monkeypatch, raises=http_error(500, b"boom"))
    with pytest.raises(mcp.McpTransportError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert excinfo.value.status == 500
    assert "boom" in str(excinfo.value)


def test_token_echoed_in_an_error_body_is_redacted(env, monkeypatch, tmp_path):
    install(monkeypatch, raises=http_error(400, f"bad key {TOKEN}".encode()))
    with pytest.raises(mcp.McpTransportError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert TOKEN not in str(excinfo.value)
    assert "***" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Arguments / binding / config                                                 #
# --------------------------------------------------------------------------- #


def test_missing_instance_fails_before_any_http_call(env, monkeypatch, tmp_path):
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    with pytest.raises(mcp.McpMissingInstanceError) as excinfo:
        mcp.call("resolve-service", {"filters": {}}, ctx(tmp_path=tmp_path))
    assert rec.calls == 0
    message = str(excinfo.value)
    assert "resolve-service" in message
    assert "lakerunner__list_services" in message
    assert "inputs.instance" in message


def test_blank_instance_is_rejected(env, monkeypatch, tmp_path):
    install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    with pytest.raises(mcp.McpMissingInstanceError):
        mcp.call("n1", {"instance": "   "}, ctx(tmp_path=tmp_path))


def test_instance_from_sentinel_inputs_reaches_the_tool_arguments(env, monkeypatch, tmp_path):
    # `${inputs.instance}` is rendered by runtime_serve before the provider is
    # called; the provider's job is to carry it through untouched.
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    mcp.call("n1", {"instance": "otel-demo", "limit": 5}, ctx(tmp_path=tmp_path))
    assert rec.arguments["instance"] == "otel-demo"


def test_metrics_query_without_expression_or_metric_name_fails_locally(env, monkeypatch, tmp_path):
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    with pytest.raises(mcp.McpConfigError, match="expression"):
        mcp.call("n1", {"instance": "prod"}, ctx(capability_id=CAP_METRICS, tmp_path=tmp_path))
    assert rec.calls == 0


def test_metrics_query_with_metric_name_is_accepted(env, monkeypatch, tmp_path):
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    mcp.call(
        "n1",
        {"instance": "prod", "metric_name": "http.server.duration"},
        ctx(capability_id=CAP_METRICS, tmp_path=tmp_path),
    )
    assert rec.calls == 1
    assert rec.body["params"]["name"] == "lakerunner__execute_metrics_query"


def test_missing_endpoint_env_in_binding_raises_and_does_not_default_a_url(env, monkeypatch, tmp_path):
    bind = {"provider": "mcp", "token_env": TOKEN_VAR}
    with pytest.raises(mcp.McpConfigError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(bind=bind, tmp_path=tmp_path))
    assert "endpointSecretRef" in str(excinfo.value)
    assert CAP_LIST in str(excinfo.value)


def test_missing_token_env_in_binding_raises(env, monkeypatch, tmp_path):
    bind = {"provider": "mcp", "endpoint_env": ENDPOINT_VAR}
    with pytest.raises(mcp.McpConfigError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(bind=bind, tmp_path=tmp_path))
    assert "tokenSecretRef" in str(excinfo.value)


def test_env_var_named_by_binding_but_unset_raises(monkeypatch, tmp_path):
    monkeypatch.delenv(ENDPOINT_VAR, raising=False)
    monkeypatch.setenv(TOKEN_VAR, TOKEN)
    with pytest.raises(mcp.McpConfigError, match="not set in the process environment"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_blank_token_env_value_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(ENDPOINT_VAR, FULL_URL)
    monkeypatch.setenv(TOKEN_VAR, "   ")
    with pytest.raises(mcp.McpConfigError, match="is empty"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_env_var_names_are_read_from_the_binding_not_recomputed(monkeypatch, tmp_path):
    # The controller-projected names would be CARDINAL_CAP_OBSERVABILITY_
    # LIST_SERVICES_{ENDPOINT,TOKEN}; the binding here names different ones and
    # the provider must honour the binding.
    monkeypatch.setenv("WEIRD_ENDPOINT_NAME", FULL_URL)
    monkeypatch.setenv("WEIRD_TOKEN_NAME", TOKEN)
    monkeypatch.delenv("CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT", raising=False)
    bind = {
        "provider": "mcp",
        "endpoint_env": "WEIRD_ENDPOINT_NAME",
        "token_env": "WEIRD_TOKEN_NAME",
    }
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    mcp.call("n1", {"instance": "prod"}, ctx(bind=bind, tmp_path=tmp_path))
    assert rec.request.full_url == FULL_URL
    assert rec.request.get_header("X-cardinalhq-api-key") == TOKEN


def test_credential_ref_is_accepted_as_the_local_dev_token_path(monkeypatch, tmp_path):
    monkeypatch.setenv(ENDPOINT_VAR, FULL_URL)
    monkeypatch.setenv("LOCAL_DEV_TOKEN", "local-token")
    bind = {
        "provider": "mcp",
        "endpoint_env": ENDPOINT_VAR,
        "credential_ref": "env://LOCAL_DEV_TOKEN",
    }
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    mcp.call("n1", {"instance": "prod"}, ctx(bind=bind, tmp_path=tmp_path))
    assert rec.request.get_header("X-cardinalhq-api-key") == "local-token"


def test_token_env_wins_over_credential_ref(env, monkeypatch, tmp_path):
    monkeypatch.setenv("LOCAL_DEV_TOKEN", "local-token")
    bind = binding(credential_ref="env://LOCAL_DEV_TOKEN")
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    mcp.call("n1", {"instance": "prod"}, ctx(bind=bind, tmp_path=tmp_path))
    assert rec.request.get_header("X-cardinalhq-api-key") == TOKEN


def test_base_endpoint_plus_org_id_builds_the_gateway_path(monkeypatch, tmp_path):
    monkeypatch.setenv(ENDPOINT_VAR, "https://app.cardinalhq.io/")
    monkeypatch.setenv(TOKEN_VAR, TOKEN)
    bind = binding(params={"org_id": "org 1/2"})
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    mcp.call("n1", {"instance": "prod"}, ctx(bind=bind, tmp_path=tmp_path))
    assert rec.request.full_url == "https://app.cardinalhq.io/api/orgs/org%201%2F2/mcp"


def test_base_endpoint_without_org_id_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(ENDPOINT_VAR, "https://app.cardinalhq.io")
    monkeypatch.setenv(TOKEN_VAR, TOKEN)
    with pytest.raises(mcp.McpConfigError, match="org_id"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_non_http_endpoint_raises(monkeypatch, tmp_path):
    monkeypatch.setenv(ENDPOINT_VAR, "file:///etc/passwd/mcp")
    monkeypatch.setenv(TOKEN_VAR, TOKEN)
    with pytest.raises(mcp.McpConfigError, match="not an http"):
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))


def test_endpoint_query_string_is_stripped_from_error_messages(monkeypatch, tmp_path):
    monkeypatch.setenv(ENDPOINT_VAR, FULL_URL + "?apiKey=leaky-secret")
    monkeypatch.setenv(TOKEN_VAR, TOKEN)
    install(monkeypatch, raises=urllib.error.URLError("Connection refused"))
    with pytest.raises(mcp.McpTransportError) as excinfo:
        mcp.call("n1", {"instance": "prod"}, ctx(tmp_path=tmp_path))
    assert "leaky-secret" not in str(excinfo.value)


def test_unmapped_capability_passes_through_as_gateway_tool_name(env, monkeypatch, tmp_path):
    """Transcript-derived ids ARE gateway tool names — no mapping required.

    The old behavior raised McpConfigError for any id outside CAPABILITY_TOOLS,
    which made every freshly-compiled Sentinel unrunnable until someone added a
    mapping. The gateway itself validates tool existence at call time.
    """
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    out = mcp.call(
        "n1", {"instance": "prod"},
        ctx(capability_id="lakerunner__list_log_streams", tmp_path=tmp_path),
    )
    assert out == {"ok": True}
    assert rec.body["params"]["name"] == "lakerunner__list_log_streams"


def test_bad_timeout_seconds_raises(env, monkeypatch, tmp_path):
    install(monkeypatch, FakeResponse(envelope({"structuredContent": {"ok": True}})))
    with pytest.raises(mcp.McpConfigError, match="timeoutSeconds"):
        mcp.call("n1", {"instance": "prod"}, ctx(bind=binding(timeoutSeconds=0), tmp_path=tmp_path))


def test_every_failure_is_an_mcp_provider_error(env, monkeypatch, tmp_path):
    for exc in (
        mcp.McpConfigError,
        mcp.McpMissingInstanceError,
        mcp.McpTransportError,
        mcp.McpAuthError,
        mcp.McpRateLimitError,
        mcp.McpTimeoutError,
        mcp.McpProtocolError,
        mcp.McpToolError,
        mcp.McpInstanceRequiredError,
    ):
        assert issubclass(exc, mcp.McpProviderError)
        # RuntimeError so runtime_serve's broad `except Exception` records a
        # node.failed row rather than the process dying uncaught.
        assert issubclass(exc, RuntimeError)


# --------------------------------------------------------------------------- #
# Integration: the provider is reachable through the real serve path           #
# --------------------------------------------------------------------------- #
#
# The unit tests above call `mcp.call` directly, so they would still pass if
# the provider registered nowhere. These two drive `runtime_serve.run_serve`
# with a real sentinel.yaml + deployment.yaml, which is the only thing that
# proves (a) `import capabilities` populates the registry with `mcp` and
# (b) a provider failure lands as a `node.failed` audit row + exit code 4
# rather than a silent fall-through to the legacy tool-cache path.


def _write_mcp_sentinel(tmp_path):
    import yaml

    sdir = tmp_path / "sentinel"
    sdir.mkdir()
    sentinel_doc = {
        "schemaVersion": "mechanize.dev/v1alpha1",
        "kind": "Sentinel",
        "metadata": {"name": "t"},
        "spec": {
            "inputs": {"instance": {"type": "string", "required": True}},
            "nodes": {
                "resolve-service": {
                    "kind": "tool",
                    "config": {
                        "toolRef": CAP_LIST,
                        "arguments": {"instance": "${inputs.instance}"},
                    },
                }
            },
        },
    }
    (sdir / "sentinel.yaml").write_text(yaml.safe_dump(sentinel_doc, sort_keys=False))
    (sdir / "deployment.yaml").write_text(
        yaml.safe_dump(
            {
                "schemaVersion": "mechanize.dev/v1alpha1",
                "kind": "SentinelDeployment",
                "runtime": "manual",
                "capabilityBindings": {
                    CAP_LIST: {
                        "provider": "mcp",
                        "endpoint_env": ENDPOINT_VAR,
                        "token_env": TOKEN_VAR,
                    }
                },
            },
            sort_keys=False,
        )
    )
    (sdir / "inputs.json").write_text(json.dumps({"instance": "otel-demo"}))
    return sdir


def _serve_mcp(tmp_path):
    import runtime_serve

    sdir = _write_mcp_sentinel(tmp_path)
    return sdir, runtime_serve.run_serve(
        sentinel_dir=sdir,
        deployment_path=sdir / "deployment.yaml",
        inputs_path=sdir / "inputs.json",
        state_path=tmp_path / "state.sqlite",
        run_id_override="run-test",
        poll_interval=0.01,
    )


def test_serve_path_resolves_the_mcp_provider_and_passes_the_bound_instance(
    env, monkeypatch, tmp_path
):
    rec = install(monkeypatch, FakeResponse(envelope({"structuredContent": {"services": ["a"]}})))
    _, exit_code = _serve_mcp(tmp_path)
    assert exit_code == 0
    assert rec.calls == 1
    # `${inputs.instance}` -> inputs.json -> tool arguments, untouched.
    assert rec.arguments["instance"] == "otel-demo"


def test_serve_path_records_node_failed_when_the_gateway_returns_is_error(
    env, monkeypatch, tmp_path
):
    import state as state_mod

    install(
        monkeypatch,
        FakeResponse(
            envelope({"isError": True, "content": [{"type": "text", "text": "backend down"}]})
        ),
    )
    _, exit_code = _serve_mcp(tmp_path)
    assert exit_code == 4  # Job fails -> findingsCount is never patched
    with state_mod.StateStore.open(tmp_path / "state.sqlite") as store:
        rows = store.list_audit("run-test")
    failed = [r for r in rows if r["event_type"] == "node.failed"]
    assert len(failed) == 1
    payload = json.loads(failed[0]["payload_json"])
    assert payload["node"] == "resolve-service"
    assert "McpToolError" in payload["error"]
    assert "backend down" in payload["error"]
    assert TOKEN not in payload["error"]
    assert TOKEN not in payload["traceback"]
