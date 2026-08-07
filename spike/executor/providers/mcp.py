"""``mcp`` capability provider — calls the Cardinal MCP gateway.

Python port of the reference client in
``conductor/packages/mcp-gateway/aggregator/kube_fanout.go``. Hand-rolled for
the same reasons the Go one is: the MCP SDK transports expose no header hook,
there are no in-call notifications on this hop, per-call lifetime is short —
and in Python a fourth reason applies, namely that the ``mcp`` client package
would insist on an ``initialize`` handshake that this endpoint neither needs
nor benefits from. ``urllib.request`` + ``json`` is the whole dependency.

Wire shape (verified against conductor HEAD + go-sdk v1.6.1)
------------------------------------------------------------
* ONE stateless ``POST`` of a JSON-RPC ``tools/call``. No ``initialize``, no
  ``notifications/initialized``, no ``Mcp-Session-Id`` — the gateway runs
  ``StreamableHTTPOptions{Stateless: true}`` and synthesizes a
  default-initialized session per request.
* Headers: ``Content-Type: application/json`` (else HTTP 415),
  ``Accept: application/json, text/event-stream`` — BOTH media types are
  mandatory; the handler rejects a POST missing either with HTTP 400 before
  it parses any JSON-RPC — and ``X-CardinalHQ-API-Key: <maestro key>``.
* The gateway leaves ``JSONResponse`` false, so a *successful* response comes
  back as ``text/event-stream``. SSE is the hot path, not an edge case; a
  client that only handles ``application/json`` fails every real call.
* ``result.structuredContent`` is the only reliable data channel. For all four
  lakerunner tools targeted here ``content[0].text`` is a human-readable prose
  summary ("Found 12 services (…)"), never the JSON. The text fallback below
  is kept for forward compatibility with tools that leave ``Content`` nil, and
  for the ``isError`` branch where the text IS structured.
* Tool failures arrive as **HTTP 200 with ``result.isError: true``**, NOT as a
  JSON-RPC ``error``. Those are different failure modes with different causes
  and this module raises different exceptions for them. A status-code check
  alone reports success on every tool failure.

Binding shape (written by ``k8s/controller/projections.py``)
------------------------------------------------------------
``deployment.yaml``::

    capabilityBindings:
      observability.list-services:
        provider: mcp
        endpoint_env: CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_ENDPOINT
        token_env:    CARDINAL_CAP_OBSERVABILITY_LIST_SERVICES_TOKEN
        params: {instance: prod}        # optional argument defaults
        timeoutSeconds: 180             # optional

The env var NAMES come from the binding and are never recomputed here: the
binding is authoritative and only names variables that were actually injected.
``credential_ref`` (``env://VAR``) remains the local/dev path for the token.

Endpoint value: either the full gateway URL ending in ``/mcp`` (simplest for
whoever seeds the Secret), or a base URL plus ``params.org_id`` — this module
is the only place that knows the ``/api/orgs/{org}/mcp`` suffix, because the
provider ctx carries no sentinel inputs to source an org id from.
"""
from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

import capabilities as capabilities_mod


PROVIDER_ID = "mcp"

#: capability id -> aggregator-namespaced gateway tool name.
#: Namespacing is ``driver`` + ``__`` + ``native`` (aggregator/server.go:25-35);
#: ``splitToolName`` splits on the FIRST ``__`` only, so native names may
#: themselves contain ``__``. Build these, never parse them client-side.
CAPABILITY_TOOLS: dict[str, str] = {
    "observability.list-services": "lakerunner__list_services",
    "observability.error-overview": "lakerunner__error_overview",
    "observability.query-logs": "lakerunner__execute_logs_query",
    "observability.query-metrics": "lakerunner__execute_metrics_query",
}

#: 8 MiB, matching ``maxKubeFanoutResponseBytes`` in the reference client.
MAX_RESPONSE_BYTES = 8 << 20

#: Cap on how much of a non-200 body we quote back in an error message.
MAX_ERROR_BODY_BYTES = 4096

#: Deliberately NOT the reference client's 30s: that value is documented as
#: sized for kube list/get tools. The lakerunner handlers set their own,
#: longer, server-side deadlines (list_services 2m, execute_metrics_query 2m,
#: error_overview 3m), so a 30s client timeout aborts legitimate calls.
#: Override per binding with ``timeoutSeconds``.
DEFAULT_TIMEOUT_SECONDS = 180.0

_JSONRPC_VERSION = "2.0"
_REQUEST_ID = 1


# --------------------------------------------------------------------------- #
# Errors                                                                       #
# --------------------------------------------------------------------------- #
#
# Every one of these is raised, never returned. runtime_serve catches any
# exception from a provider, writes a `node.failed` audit row (mirrored to
# stdout as `[dag]`), cancels dependents, and exits 4 — the Job fails and
# `status.findingsCount` is never patched. That is the fail-loud contract; a
# provider that returned a partial result on error would silently manufacture
# a finding out of nothing.


class McpProviderError(RuntimeError):
    """Base for every failure of the ``mcp`` capability provider."""


class McpConfigError(McpProviderError):
    """The binding or the environment cannot describe a usable call."""


class McpMissingInstanceError(McpConfigError):
    """No ``instance`` argument — every lakerunner tool requires one."""


class McpTransportError(McpProviderError):
    """HTTP-level failure: non-200, connection refused, oversized body."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class McpAuthError(McpTransportError):
    """401/403 from maestro. Kept distinct so a bad token cannot be mistaken
    for a flaky network — Gate 0 tests exactly this path."""


class McpRateLimitError(McpTransportError):
    """429 from the aggregator's rate limiter."""

    def __init__(self, message: str, retry_after: str | None = None):
        super().__init__(message, status=429)
        self.retry_after = retry_after


class McpTimeoutError(McpTransportError):
    """The gateway did not answer within the binding's timeout."""


class McpProtocolError(McpProviderError):
    """The response was not a decodable JSON-RPC result, or carried a
    JSON-RPC ``error`` object (unknown tool, malformed request)."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class McpToolError(McpProviderError):
    """HTTP 200 with ``result.isError: true`` — the tool ran and refused.

    Distinct from :class:`McpProtocolError` on purpose: a JSON-RPC error means
    the gateway could not dispatch the call at all (wrong tool name, malformed
    envelope, a config problem), whereas ``isError`` means the call was
    dispatched and the tool itself said no (bad arguments, unknown instance,
    backend failure). They have different causes and different fixes.
    """

    def __init__(self, message: str, text: str | None = None, structured: Any = None):
        super().__init__(message)
        self.text = text
        self.structured = structured


class McpInstanceRequiredError(McpToolError):
    """The gateway's ``instance_required`` refusal, with the instances it
    would have accepted. Its own type because it has a one-step operator fix:
    bind the Sentinel's ``instance`` input."""

    def __init__(self, message: str, available: list[str] | None = None, **kw: Any):
        super().__init__(message, **kw)
        self.available = available or []


# --------------------------------------------------------------------------- #
# JSON encoding                                                                #
# --------------------------------------------------------------------------- #


def _rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json_default(o: Any) -> Any:
    """Mirror of ``executor._json_default``.

    Tool arguments arrive already rendered by ``executor.render_deep``, and a
    node argument whose template is exactly one ``${...}`` keeps its RAW
    evaluated value — so ``start: "${execution.now - inputs.window}"`` is a
    ``datetime`` and a duration input is a ``timedelta``. A bare
    ``json.dumps(args)`` raises TypeError on both. Duplicated rather than
    imported so this module does not depend on an executor private;
    ``test_providers_mcp.py`` pins the two implementations to agree.
    """
    if isinstance(o, datetime):
        return _rfc3339(o)
    if isinstance(o, timedelta):
        return f"{int(o.total_seconds())}s"
    if isinstance(o, set):
        return sorted(o)
    return str(o)


# --------------------------------------------------------------------------- #
# Binding / environment resolution                                             #
# --------------------------------------------------------------------------- #


def _binding_params(binding: dict[str, Any]) -> dict[str, Any]:
    params = binding.get("params")
    return params if isinstance(params, dict) else {}


def _resolve_env_var(
    binding: dict[str, Any],
    key: str,
    capability_id: str,
    secret_ref_key: str | None = None,
) -> str:
    """Read the value of the env var *named by the binding*.

    The names are never recomputed from the capability id: the binding is
    authoritative and only names variables the controller actually injected.
    """
    name = binding.get(key)
    if isinstance(name, str) and name.strip():
        value = _read_secret(f"env://{name.strip()}", capability_id, key)
        if not value.strip():
            raise McpConfigError(
                f"capability {capability_id!r} (provider {PROVIDER_ID!r}): "
                f"environment variable {name!r} named by binding.{key} is empty"
            )
        return value.strip()

    if secret_ref_key:
        ref = binding.get(secret_ref_key)
        if isinstance(ref, str) and ref.strip():
            value = _read_secret(ref.strip(), capability_id, secret_ref_key)
            if not value.strip():
                raise McpConfigError(
                    f"capability {capability_id!r} (provider {PROVIDER_ID!r}): "
                    f"secret ref named by binding.{secret_ref_key} resolved empty"
                )
            return value.strip()

    hint = {
        "endpoint_env": "the Sentinel CR's spec.capabilities entry needs "
        "`endpointSecretRef` (Secret key `endpoint`)",
        "token_env": "the Sentinel CR's spec.capabilities entry needs "
        "`tokenSecretRef` (Secret key `token`), or set `credential_ref: "
        "env://VAR` for a local run",
    }[key]
    raise McpConfigError(
        f"capability {capability_id!r} is bound to provider {PROVIDER_ID!r} but "
        f"its deployment.yaml binding has no {key!r}: {hint}. Refusing to fall "
        f"back to a default — a Sentinel must not silently call an endpoint "
        f"nobody configured."
    )


def _read_secret(ref: str, capability_id: str, key: str) -> str:
    # Imported lazily so this module stays importable when only the registry
    # is being inspected (e.g. lint listing registered providers).
    import secrets as secrets_mod

    try:
        return secrets_mod.resolve(ref)
    except Exception as e:  # noqa: BLE001 — re-raised as a config error below
        raise McpConfigError(
            f"capability {capability_id!r} (provider {PROVIDER_ID!r}): could not "
            f"resolve binding.{key}: {type(e).__name__}: {e}"
        ) from e


def _endpoint_url(binding: dict[str, Any], capability_id: str) -> str:
    """Build the gateway URL.

    Accepts either a full URL already ending in ``/mcp`` (what an operator
    seeding the Secret will normally paste), or a base URL plus
    ``params.org_id``. The provider ctx carries no sentinel inputs, so an org
    id can only come from the binding.
    """
    raw = _resolve_env_var(binding, "endpoint_env", capability_id)
    base = raw.rstrip("/")
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise McpConfigError(
            f"capability {capability_id!r} (provider {PROVIDER_ID!r}): endpoint "
            f"{_safe_url(base)!r} is not an http(s) URL"
        )
    if parsed.path.endswith("/mcp"):
        return base

    org_id = _binding_params(binding).get("org_id") or binding.get("org_id")
    if not isinstance(org_id, str) or not org_id.strip():
        raise McpConfigError(
            f"capability {capability_id!r} (provider {PROVIDER_ID!r}): endpoint "
            f"{_safe_url(base)!r} does not end in '/mcp' and the binding has no "
            f"`params.org_id`. Either seed the endpoint Secret with the full "
            f"URL (e.g. http://maestro-maestro.maestro.svc.cluster.local:4200"
            f"/api/orgs/<orgId>/mcp) or add `params: {{org_id: <orgId>}}` to "
            f"the binding."
        )
    quoted = urllib.parse.quote(org_id.strip(), safe="")
    return f"{base}/api/orgs/{quoted}/mcp"


def _timeout_seconds(binding: dict[str, Any], capability_id: str) -> float:
    raw = binding.get("timeoutSeconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError) as e:
        raise McpConfigError(
            f"capability {capability_id!r} (provider {PROVIDER_ID!r}): "
            f"timeoutSeconds={raw!r} is not a number"
        ) from e
    if value <= 0:
        raise McpConfigError(
            f"capability {capability_id!r} (provider {PROVIDER_ID!r}): "
            f"timeoutSeconds={raw!r} must be positive"
        )
    return value


def _safe_url(url: str) -> str:
    """Strip query + fragment before putting a URL in an error message.

    The gateway accepts an ``?apiKey=`` fallback, so a query string is a
    credential-bearing surface — and every exception message this module
    raises is written verbatim into the audit table AND printed to stdout.
    """
    parsed = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _redact(text: str, token: str) -> str:
    if token and token in text:
        text = text.replace(token, "***")
    return text


# --------------------------------------------------------------------------- #
# Arguments                                                                    #
# --------------------------------------------------------------------------- #


def _build_arguments(
    args: dict[str, Any],
    binding: dict[str, Any],
    capability_id: str,
    node_id: str,
    tool_name: str,
) -> dict[str, Any]:
    """Merge binding defaults under the node's rendered arguments, then check
    the invariants the gateway would otherwise spend a round trip on."""
    merged: dict[str, Any] = {}
    for key, value in _binding_params(binding).items():
        if key == "org_id":  # consumed by URL construction, not a tool arg
            continue
        merged[key] = value
    merged.update(args or {})

    instance = merged.get("instance")
    if not isinstance(instance, str) or not instance.strip():
        raise McpMissingInstanceError(
            f"node {node_id!r} calling {tool_name} for capability "
            f"{capability_id!r} has no 'instance' argument (got {instance!r}). "
            f"Every lakerunner tool requires the integration slug (e.g. "
            f"'prod'). Give the tool node `arguments.instance: "
            f"\"${{inputs.instance}}\"` and bind the Sentinel's `instance` "
            f"input, or set `params.instance` on the capability binding."
        )
    merged["instance"] = instance.strip()

    # Schema-invisible rule the gateway enforces in the handler, not in the
    # JSON schema: execute_metrics_query needs one of expression/metric_name.
    # Checking it here turns a round trip into an immediate, precise failure.
    if tool_name.endswith("execute_metrics_query"):
        has_expression = bool(str(merged.get("expression") or "").strip())
        has_metric_name = bool(str(merged.get("metric_name") or "").strip())
        if not has_expression and not has_metric_name:
            raise McpConfigError(
                f"node {node_id!r} calling {tool_name} must supply either "
                f"'expression' (PromQL) or 'metric_name'; got neither. The "
                f"gateway's JSON schema does not express this rule, so it "
                f"would otherwise fail as a tool error after a round trip."
            )
    return merged


# --------------------------------------------------------------------------- #
# HTTP                                                                         #
# --------------------------------------------------------------------------- #


def _urlopen(request: urllib.request.Request, timeout: float):
    """Seam for tests. Never called with a real URL in the test suite."""
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310


def _header(headers: Any, name: str, default: str = "") -> str:
    if headers is None:
        return default
    getter = getattr(headers, "get", None)
    if getter is None:
        return default
    value = getter(name, default)
    return value if isinstance(value, str) else default


def _read_capped(resp: Any, capability_id: str, url: str) -> bytes:
    body = resp.read(MAX_RESPONSE_BYTES + 1)
    if body is None:
        return b""
    if len(body) > MAX_RESPONSE_BYTES:
        raise McpTransportError(
            f"capability {capability_id!r}: gateway response from "
            f"{_safe_url(url)} exceeded {MAX_RESPONSE_BYTES} bytes; refusing to "
            f"buffer it. Narrow the query (shorter window, tighter filters, "
            f"lower limit)."
        )
    return body


def _post(
    url: str,
    token: str,
    payload: dict[str, Any],
    timeout: float,
    capability_id: str,
) -> tuple[bytes, str]:
    """POST the JSON-RPC envelope. Returns ``(body, content_type)``."""
    data = json.dumps(payload, default=_json_default).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            # BOTH media types are mandatory: the handler 400s a POST that
            # does not pre-agree to either form, before parsing any JSON-RPC.
            "Accept": "application/json, text/event-stream",
            "X-CardinalHQ-API-Key": token,
        },
    )
    try:
        resp = _urlopen(request, timeout)
    except urllib.error.HTTPError as e:
        raise _http_error(e, url, token, capability_id) from None
    except (TimeoutError, socket.timeout) as e:
        raise McpTimeoutError(
            f"capability {capability_id!r}: gateway at {_safe_url(url)} did not "
            f"respond within {timeout}s"
        ) from e
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise McpTimeoutError(
                f"capability {capability_id!r}: gateway at {_safe_url(url)} did "
                f"not respond within {timeout}s"
            ) from e
        raise McpTransportError(
            f"capability {capability_id!r}: cannot reach gateway at "
            f"{_safe_url(url)}: {_redact(str(reason), token)}"
        ) from e
    except OSError as e:
        raise McpTransportError(
            f"capability {capability_id!r}: cannot reach gateway at "
            f"{_safe_url(url)}: {type(e).__name__}: {_redact(str(e), token)}"
        ) from e

    # The read phase needs the SAME mapping as the connect phase above, and for
    # a stronger reason: the gateway leaves `JSONResponse` false, so it flushes
    # `200` + `Content-Type: text/event-stream` the moment the POST stream
    # opens and only writes the `data:` frame once the tool returns. urlopen()
    # therefore comes back almost immediately and essentially ALL of the tool's
    # latency lives here — which makes this, not the connect, the window where
    # the timeout expires or a maestro restart resets the socket. Unguarded,
    # those escape as raw TimeoutError/ConnectionResetError/IncompleteRead and
    # break this module's invariant that every failure is an McpProviderError
    # naming the capability and the safe URL.
    with _closing(resp):
        try:
            status = getattr(resp, "status", None)
            if status is None:
                status = getattr(resp, "code", 200)
            body = _read_capped(resp, capability_id, url)
            content_type = _header(getattr(resp, "headers", None), "Content-Type")
            if int(status) != 200:
                raise McpTransportError(
                    f"capability {capability_id!r}: gateway at {_safe_url(url)} "
                    f"returned HTTP {status}: "
                    f"{_redact(_truncate(body), token)}",
                    status=int(status),
                )
        except McpProviderError:
            # Already this module's own type (the non-200 branch, or the size
            # cap in _read_capped). Pass it through untouched — re-wrapping
            # would lose .status and change the non-200 semantics.
            raise
        except (TimeoutError, socket.timeout) as e:
            # socket.timeout IS TimeoutError, and TimeoutError IS an OSError,
            # so this branch must precede the generic OSError one below.
            raise McpTimeoutError(
                f"capability {capability_id!r}: gateway at {_safe_url(url)} did "
                f"not finish sending its response within {timeout}s"
            ) from e
        except http.client.IncompleteRead as e:
            # NOT an OSError — needs its own branch or it would escape raw.
            raise McpTransportError(
                f"capability {capability_id!r}: gateway at {_safe_url(url)} "
                f"truncated its response: {type(e).__name__}: "
                f"{_redact(str(e), token)}"
            ) from e
        except OSError as e:
            raise McpTransportError(
                f"capability {capability_id!r}: lost the connection to the "
                f"gateway at {_safe_url(url)} while reading its response: "
                f"{type(e).__name__}: {_redact(str(e), token)}"
            ) from e
    return body, content_type


class _closing:
    """Minimal contextlib.closing that tolerates a fake without .close()."""

    def __init__(self, obj: Any):
        self._obj = obj

    def __enter__(self) -> Any:
        return self._obj

    def __exit__(self, *exc: Any) -> None:
        closer = getattr(self._obj, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — closing must not mask the real error
                pass


def _truncate(body: bytes) -> str:
    text = body[:MAX_ERROR_BODY_BYTES].decode("utf-8", "replace")
    if len(body) > MAX_ERROR_BODY_BYTES:
        text += "… (truncated)"
    return text


def _http_error(
    e: urllib.error.HTTPError, url: str, token: str, capability_id: str
) -> McpTransportError:
    try:
        raw = e.read(MAX_ERROR_BODY_BYTES + 1) or b""
    except Exception:  # noqa: BLE001
        raw = b""
    body = _redact(_truncate(raw), token)
    status = int(getattr(e, "code", 0) or 0)
    where = _safe_url(url)
    if status in (401, 403):
        which = "rejected" if status == 401 else "refused for this org"
        return McpAuthError(
            f"capability {capability_id!r}: maestro {which} the API key "
            f"(HTTP {status}) at {where}. Check the Secret named by the "
            f"binding's token_env. Body: {body}",
            status=status,
        )
    if status == 429:
        retry_after = _header(getattr(e, "headers", None), "Retry-After") or None
        return McpRateLimitError(
            f"capability {capability_id!r}: gateway rate-limited this call "
            f"(HTTP 429) at {where}"
            + (f"; Retry-After: {retry_after}" if retry_after else "")
            + f". Body: {body}",
            retry_after=retry_after,
        )
    return McpTransportError(
        f"capability {capability_id!r}: gateway at {where} returned HTTP "
        f"{status}: {body}",
        status=status,
    )


# --------------------------------------------------------------------------- #
# Response decoding                                                            #
# --------------------------------------------------------------------------- #


def _extract_jsonrpc(body: bytes, content_type: str, capability_id: str) -> bytes:
    """Unwrap an SSE frame if present.

    The gateway leaves ``JSONResponse`` false, so this is the NORMAL path for a
    successful call. Frames are ``event: message\\ndata: <json>\\n\\n`` — note
    the space after ``data:`` (hence ``.strip()``) and that the JSON is always
    exactly one line because the server marshals without newlines. Taking the
    first NON-EMPTY ``data:`` payload keeps this correct if a priming event
    (which carries no data) is ever introduced.
    """
    if "text/event-stream" in (content_type or "").lower():
        for line in body.decode("utf-8", "replace").split("\n"):
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    return payload.encode("utf-8")
        raise McpProtocolError(
            f"capability {capability_id!r}: SSE response contained no non-empty "
            f"'data:' frame: {_truncate(body)}"
        )
    return body


def _content_text(result: dict[str, Any]) -> str | None:
    content = result.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                return text
    return None


def _raise_tool_error(
    result: dict[str, Any], capability_id: str, node_id: str, tool_name: str
) -> None:
    """``result.isError == True``: the tool ran (or refused) at HTTP 200."""
    text = _content_text(result)
    structured = result.get("structuredContent")

    payload: Any = None
    if text:
        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            payload = None

    if isinstance(payload, dict) and payload.get("error") == "instance_required":
        available = payload.get("availableInstances")
        available = [str(a) for a in available] if isinstance(available, list) else []
        raise McpInstanceRequiredError(
            f"node {node_id!r}: {tool_name} refused the call because the "
            f"'instance' argument did not name a known integration. Bind the "
            f"Sentinel's `instance` input (tool node "
            f"`arguments.instance: \"${{inputs.instance}}\"`) to one of: "
            f"{', '.join(available) if available else '<gateway listed none>'}.",
            available=available,
            text=text,
            structured=structured,
        )

    detail = text
    if not detail and structured is not None:
        detail = json.dumps(structured, default=str)
    raise McpToolError(
        f"node {node_id!r}: {tool_name} returned isError=true for capability "
        f"{capability_id!r}: {detail or '<no content block>'}",
        text=text,
        structured=structured,
    )


def _decode(
    body: bytes,
    content_type: str,
    capability_id: str,
    node_id: str,
    tool_name: str,
    request_id: Any,
) -> Any:
    raw = _extract_jsonrpc(body, content_type, capability_id)
    try:
        envelope = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise McpProtocolError(
            f"capability {capability_id!r}: gateway response is not JSON "
            f"({type(e).__name__}): {_truncate(raw)}"
        ) from e
    if not isinstance(envelope, dict):
        raise McpProtocolError(
            f"capability {capability_id!r}: gateway response is not a JSON-RPC "
            f"object, got {type(envelope).__name__}"
        )

    got_id = envelope.get("id")
    if got_id is not None and got_id != request_id:
        raise McpProtocolError(
            f"capability {capability_id!r}: JSON-RPC id mismatch — sent "
            f"{request_id!r}, got {got_id!r}"
        )

    # (1) JSON-RPC protocol error: the call was never dispatched.
    error = envelope.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = str(error.get("message") or "")
        hint = ""
        if code == -32602 and "unknown tool" in message.lower():
            hint = (
                f" The aggregator does not expose {tool_name} for this org — "
                f"tool availability is org-conditional on the lakerunner "
                f"integration being registered. This is a binding/config "
                f"problem, not a transient fault; call tools/list to see what "
                f"the org actually has."
            )
        raise McpProtocolError(
            f"capability {capability_id!r}: gateway returned JSON-RPC error "
            f"{code}: {message}.{hint}",
            code=code if isinstance(code, int) else None,
        )

    if "result" not in envelope:
        raise McpProtocolError(
            f"capability {capability_id!r}: gateway response had neither "
            f"'result' nor 'error': {_truncate(raw)}"
        )
    result = envelope["result"]
    if not isinstance(result, dict):
        raise McpProtocolError(
            f"capability {capability_id!r}: JSON-RPC 'result' is not an object, "
            f"got {type(result).__name__}"
        )

    # (2) Tool-level failure at HTTP 200 — a DIFFERENT failure mode.
    if result.get("isError") is True:
        _raise_tool_error(result, capability_id, node_id, tool_name)

    # (3) structuredContent is the payload for every tool we call.
    structured = result.get("structuredContent")
    if structured is not None:
        return _require_payload(structured, capability_id, node_id, tool_name)

    # (4) Forward-compatibility only: a tool that leaves Content nil gets the
    #     structured output serialized into a text block instead. None of the
    #     four lakerunner tools do this — their text block is prose — so this
    #     must never be relied on, and its failure must not look like success.
    text = _content_text(result)
    if text is not None:
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            parsed = None
        if parsed is not None:
            return _require_payload(parsed, capability_id, node_id, tool_name)

    raise McpProtocolError(
        f"capability {capability_id!r}: {tool_name} returned no usable payload "
        f"for node {node_id!r} — no 'structuredContent', and the text content "
        f"is not JSON. Result keys present: {sorted(result)}"
    )


def _require_payload(payload: Any, capability_id: str, node_id: str, tool_name: str) -> Any:
    """Refuse anything that would look like a successful empty answer.

    The serve path does NOT validate a tool node's declared output schema
    (``executor._validate_output`` is only wired into the legacy ``execute``
    path), so this is the only place a garbage payload gets stopped before it
    flows into downstream function nodes and fabricates a finding.
    """
    if not isinstance(payload, dict):
        raise McpProtocolError(
            f"capability {capability_id!r}: {tool_name} returned a "
            f"{type(payload).__name__} payload for node {node_id!r}; expected a "
            f"JSON object."
        )
    if not payload:
        raise McpProtocolError(
            f"capability {capability_id!r}: {tool_name} returned an EMPTY object "
            f"for node {node_id!r}. Refusing to treat that as a successful "
            f"result — downstream nodes would compute a finding from nothing."
        )
    return payload


# --------------------------------------------------------------------------- #
# Provider entry point                                                         #
# --------------------------------------------------------------------------- #


def call(node_id: str, args: dict[str, Any], ctx: dict[str, Any]) -> Any:
    """Provider impl: one stateless JSON-RPC ``tools/call`` against the gateway."""
    capability_id: str = ctx["capability_id"]
    binding: dict[str, Any] = ctx.get("binding") or {}

    # Legacy alias map for the four abstract ids that predate transcript-derived
    # capability inventories. Everything else passes through: the capability id
    # IS the observed gateway tool name the compiler recorded from the session,
    # and the gateway validates tool existence at call time.
    tool_name = CAPABILITY_TOOLS.get(capability_id, capability_id)

    arguments = _build_arguments(args, binding, capability_id, node_id, tool_name)
    url = _endpoint_url(binding, capability_id)
    token = _resolve_env_var(
        binding, "token_env", capability_id, secret_ref_key="credential_ref"
    )
    timeout = _timeout_seconds(binding, capability_id)

    payload = {
        "jsonrpc": _JSONRPC_VERSION,
        "id": _REQUEST_ID,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }

    body, content_type = _post(url, token, payload, timeout, capability_id)
    try:
        return _decode(
            body, content_type, capability_id, node_id, tool_name, _REQUEST_ID
        )
    except McpProviderError as e:
        # Belt and braces: the gateway echoes arguments in some error paths and
        # every message here lands in the audit table AND on stdout. Rewrite
        # args in place rather than re-raising a new instance so the exception
        # type and its attributes (.available, .status, .code) survive.
        redacted = _redact(str(e), token)
        if redacted != str(e):
            e.args = (redacted,) + tuple(e.args[1:])
        raise


# Registered as a universal provider: capability inventories are
# transcript-derived (CORE.md Stage 2.1), so the set of ids cannot be
# enumerated ahead of time. ``ctx["capability_id"]`` selects the tool name —
# via CAPABILITY_TOOLS for the four legacy abstract ids, passthrough for
# everything else. Per-capability registrations are kept for the legacy ids
# so `registered_providers()` still lists them explicitly.
for _capability_id in CAPABILITY_TOOLS:
    capabilities_mod.provider(_capability_id, PROVIDER_ID)(call)
capabilities_mod.universal_provider(PROVIDER_ID)(call)


__all__ = [
    "PROVIDER_ID",
    "CAPABILITY_TOOLS",
    "MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "McpProviderError",
    "McpConfigError",
    "McpMissingInstanceError",
    "McpTransportError",
    "McpAuthError",
    "McpRateLimitError",
    "McpTimeoutError",
    "McpProtocolError",
    "McpToolError",
    "McpInstanceRequiredError",
    "call",
]
