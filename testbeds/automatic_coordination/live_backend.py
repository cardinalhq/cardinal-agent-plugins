#!/usr/bin/env python3
"""Authenticated request lifecycle acceptance in a dedicated Cloud Investigation.

Never prints credentials. Temporary grants are revoked even on failure. The
Investigation's immutable acceptance records remain available for review.
"""
import argparse
import json
from pathlib import Path
import sys
import uuid
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run as bed
from cardinal_core import investigation_consumer as c, investigation_events as ie
from cardinal_core import investigation_state_sync as sync, investigation_grants as gr
from cardinal_core.evidence_promote import no_redirect_opener
CLIENT = "codex/0.39.0"


def mcp_read(conn, inv):
    headers = {"X-CardinalHQ-API-Key": conn["key"], "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream", "X-Cardinal-Client": CLIENT}
    url = conn["origin"] + "/api/orgs/" + urllib.parse.quote(conn["org"], safe="") + "/mcp"
    def call(method, params, ident):
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if ident is not None: body["id"] = ident
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with no_redirect_opener().open(req, timeout=20) as response:
            if response.headers.get("Mcp-Session-Id"): headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
            raw = response.read(4 << 20).decode()
        if ident is None: return None
        rows = [json.loads(line[5:].strip()) for line in raw.splitlines() if line.startswith("data:")] if raw.lstrip().startswith(("data:", "event:")) else [json.loads(raw)]
        row = next(value for value in rows if value.get("id") == ident)
        if "error" in row: raise RuntimeError("MCP protocol refused acceptance")
        return row["result"]
    initialized = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "automatic-coordination-acceptance", "version": "1"}}, 1)
    headers["MCP-Protocol-Version"] = initialized["protocolVersion"]
    call("notifications/initialized", {}, None)
    result = call("tools/call", {"name": "storyboard__read_investigation_events", "arguments": {
        "investigation_id": inv, "snapshot": True, "request_protocol": 1,
        "types": ["request.created", "request.responded", "request.resolved"]}}, 2)
    if result.get("isError"): raise RuntimeError("MCP snapshot refused acceptance")
    return result.get("structuredContent") or json.loads(next(x["text"] for x in result["content"] if x["type"] == "text"))


def main(output):
    author, checks, grants = bed.live_connection(), {}, []
    inv, suffix = None, uuid.uuid4().hex
    sid, peer = "accept-requester-" + suffix, "accept-responder-" + suffix
    def expect_refusal(name, action, status, code):
        try: action(); checks[name] = False
        except sync.ServerError as error: checks[name] = error.status == status and error.body.get("error") == code
    try:
        inv = sync.create_investigation(author, "Automatic coordination backend acceptance " + suffix, client=CLIENT)["investigation_id"]
        meta = sync.get_investigation(author, inv, client=CLIENT)
        checks["capability"] = meta["capabilities"].get("coordination_requests") == {"enabled": True, "version": 1}
        if not checks["capability"]: raise RuntimeError("Deploy backend before acceptance")
        principal = c.principal_key(meta["author"])
        def append(kind, payload, session=sid, conn=author, key=None):
            return ie.append_event(conn, inv, kind, payload, client=CLIENT, session_id=None if conn.get("grant_id") else session,
                idempotency_key=key or "accept-" + uuid.uuid4().hex)["event"]
        def request(target=None, session=sid, conn=author):
            return append("request.created", {"text": "Review acceptance result", "actions": ["commit"],
                "assignee": target or {"principal": principal, "session": peer}}, session, conn)
        for outcome in ("complete", "blocked", "abstain", "reject"):
            q = request()["seq"]
            expect_refusal(outcome + "_wrong_actor", lambda: append("request.responded", {"request": q, "outcome": outcome, "text": "Wrong actor"}), 403, "request_invalid_actor")
            response = append("request.responded", {"request": q, "outcome": outcome, "text": "Acceptance result"}, peer)
            if outcome != "complete":
                expect_refusal(outcome + "_not_approval", lambda: append("request.resolved", {"request": q, "outcome": "accepted", "response": response["seq"], "text": "Cannot accept"}), 409, "request_requires_complete_response")
            if outcome == "blocked":
                response = append("request.responded", {"request": q, "outcome": "complete", "text": "Prerequisite supplied"}, peer)
            payload = {"request": q, "outcome": "accepted", "response": response["seq"], "text": "Reviewed exact result"} if outcome in ("complete", "blocked") else {
                "request": q, "outcome": "cancelled", "text": "Reassign after explicit non-completion"}
            checks[outcome + "_resolved"] = append("request.resolved", payload)["payload"]["outcome"] == payload["outcome"]
        q = request()["seq"]
        expect_refusal("cycle_refused", lambda: request({"principal": principal, "session": sid}, peer), 409, "request_cycle")
        append("request.resolved", {"request": q, "outcome": "cancelled", "text": "Cycle test complete"})
        old = gr.grant(author, inv, gr.parse_scopes("supervise"), ttl_seconds=900, label="coordination-acceptance-" + suffix, client=CLIENT)
        grants.append(old["grant_id"])
        q = request({"principal": old["principal"], "session": None})["seq"]
        successor = gr.grant(author, inv, gr.parse_scopes("supervise"), ttl_seconds=900, label="coordination-acceptance-successor-" + suffix,
            client=CLIENT, succeeds={"grant_id": old["grant_id"], "token": old["token"]})
        grants.append(successor["grant_id"])
        scoped = gr.token_connection({gr.TOKEN_ENV: successor["token"], gr.ORIGIN_ENV: author["origin"]})
        page = ie.read_snapshot(scoped, inv, client=CLIENT, request_protocol=1, previous_grant_ids=[old["grant_id"]], timeout=10)
        checks["authenticated_predecessor_proof"] = page["viewer"]["previous_grant_ids"] == [old["grant_id"]] and page["viewer"]["lineage_root_id"] == old["grant_id"]
        r = append("request.responded", {"request": q, "outcome": "complete", "text": "Verified successor result"}, conn=scoped)
        checks["successor_response"] = r["producer"]["principal"]["id"] == successor["grant_id"] and r["producer"]["session_id"] is None
        append("request.resolved", {"request": q, "outcome": "accepted", "response": r["seq"], "text": "Accepted successor's exact result"})
        other = gr.grant(author, inv, gr.parse_scopes("supervise"), ttl_seconds=900, label="coordination-acceptance-other-" + suffix, client=CLIENT)
        grants.append(other["grant_id"])
        expect_refusal("unrelated_predecessor_refused", lambda: ie.read_snapshot(scoped, inv, client=CLIENT, request_protocol=1,
            previous_grant_ids=[other["grant_id"]], timeout=10), 403, "coordination_consumer_not_predecessor")
        via_mcp = mcp_read(author, inv)
        checks["mcp_protocol_and_lifecycle"] = via_mcp["scan_complete"] and {"request.created", "request.responded", "request.resolved"} <= {x["type"] for x in via_mcp["events"]}
        checks["all_requests_resolved"] = all(r["resolved"] for r in __import__("cardinal_core.investigation_requests", fromlist=["fold"]).fold({},
            [row["event"] for row in bed.live_audit(author, inv)]).values())
    finally:
        failures = []
        for gid in grants:
            try: gr.revoke(author, gid, client=CLIENT)
            except Exception as error: failures.append(type(error).__name__)
        checks["temporary_grants_revoked"] = not failures
        result = {"investigation_id": inv, "checks": checks, "passed": bool(checks) and all(checks.values())}
        output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
        if failures: raise RuntimeError("Acceptance grant cleanup failed")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("output", type=Path)
    sys.exit(main(p.parse_args().output))
