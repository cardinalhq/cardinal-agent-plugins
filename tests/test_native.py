"""Native adapters share the existing Cardinal event contract and credential flow."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "common" / "native"))
sys.path.insert(0, str(ROOT / "core"))
import cardinal_native as native
from cardinal_core.paths import AgentPaths
from test_contract import REQUIRED_KEYS


def unpack(records):
    return [{a["key"]: next(iter(a["value"].values())) for a in record["attributes"]} for record in records]


def fake_git(args, cwd, **_):
    if "--abbrev-ref" in args:
        return "feat/adapters"
    return {"HEAD": "abc123", "origin": "https://github.com/cardinalhq/fixture.git", "--show-toplevel": "/fixture"}.get(args[-1])


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = AgentPaths(Path(self.temp.name))
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("CARDINAL_DECISIONS", None)
        # No test may reach the real `gh`.
        gh = patch.object(native.decisions, "_gh_pr_view", return_value=(None, None))
        self.gh = gh.start()
        self.addCleanup(gh.stop)
        self.bundle = {
            "ingest": {"endpoint": "http://localhost:9000", "api_key": "ingest-secret", "key_id": "ik"},
            "mcp": {"url": "http://localhost:9001/mcp", "api_key": "mcp-secret", "key_id": "mk"},
            "org": {"id": "org", "slug": "fixture"}, "user": {"email": "test@example.com"},
        }

    def save(self, runtime="pi"):
        native.save_bundle(self.paths, self.bundle, "https://app.cardinalhq.io", runtime, "0.1.0", "test")

    def test_both_adapters_meet_existing_cross_adapter_contract(self):
        git = lambda args, cwd: {"HEAD": "abc123", "--abbrev-ref": "feat/adapters", "origin": "https://github.com/cardinalhq/fixture.git"}.get(args[-1]) if "--abbrev-ref" not in args else "feat/adapters"
        for runtime in ("opencode", "pi"):
            self.save(runtime)
            progress = native.session.load_progress(self.paths, runtime)
            base = {"session_id": "session", "event_id": "u1", "timestamp": 1000, "cwd": "/fixture"}
            with patch.object(native.initiative, "git", side_effect=git):
                records = native.records_for_event(runtime, self.paths, {**base, "kind": "user"}, progress)
            records += native.records_for_event(runtime, self.paths, {**base, "kind": "model", "model_call_id": "m1", "model": "fixture-model", "usage": {"input_tokens": 10, "output_tokens": 2}}, progress)
            records += native.records_for_event(runtime, self.paths, {**base, "kind": "tool", "model_call_id": "m1", "tool_name": "bash", "success": False, "command": "pytest secret"}, progress)
            for attrs in unpack(records):
                event = attrs["event_name"]
                self.assertFalse(REQUIRED_KEYS[event] - attrs.keys(), (runtime, event))
            self.assertNotIn("secret", json.dumps(records))

    def test_unknown_or_invalid_usage_is_not_fabricated(self):
        self.save()
        progress = native.session.load_progress(self.paths, "s")
        event = {"session_id": "s", "event_id": "m", "kind": "model", "model": "unknown", "model_call_id": "m", "usage": {"input_tokens": float("nan"), "output_tokens": -1}}
        self.assertEqual(native.records_for_event("pi", self.paths, event, progress), [])
        event["usage"] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": float("inf")}
        records = unpack(native.records_for_event("pi", self.paths, event, progress))
        self.assertEqual(records[0]["input_tokens"], "0")
        self.assertNotIn("cost_usd", records[0])

    def test_usage_stays_on_original_turn_when_completed_after_next_prompt(self):
        self.save()
        p = native.session.load_progress(self.paths, "s")
        common = {"session_id": "s", "event_id": "e"}
        for event in ({"kind": "user"}, {"kind": "model_start", "model_call_id": "m1"}, {"kind": "user"}):
            native.records_for_event("opencode", self.paths, {**common, **event}, p)
        records = unpack(native.records_for_event("opencode", self.paths, {**common, "kind": "model", "model_call_id": "m1", "model": "fixture", "usage": {"input_tokens": 1}}, p))
        self.assertEqual(records[0]["user_turn_seq"], "1")

    def test_credentials_are_private_and_state_has_no_plaintext_keys(self):
        self.save()
        self.assertEqual(self.paths.secrets_path.stat().st_mode & 0o777, 0o600)
        state = self.paths.state_path.read_text()
        self.assertNotIn("ingest-secret", state); self.assertNotIn("mcp-secret", state)
        self.assertEqual(self.paths.read_secrets()["mcp_api_key"], "mcp-secret")

    def test_disconnect_preserves_foreign_settings_and_tracks_manual_ingest_revocation(self):
        self.save()
        settings = self.paths.home / "settings.json"
        settings.write_text('{"other":"keep"}')
        args = type("Args", (), {"local_only": False, "runtime": "pi"})()
        with patch.object(native.deviceflow, "revoke_maestro_key", return_value=(False, "offline")) as revoke:
            with self.assertRaisesRegex(ValueError, "credentials retained"):
                native.disconnect(args, self.paths)
            self.assertEqual(revoke.call_count, 1)
            self.assertEqual(revoke.call_args.args[1], "mk")
        self.assertTrue(self.paths.secrets_path.exists())
        with patch.object(native.deviceflow, "revoke_maestro_key", return_value=(True, "ok")) as revoke, contextlib.redirect_stdout(io.StringIO()):
            native.disconnect(args, self.paths)
            self.assertEqual(revoke.call_args.args[1], "mk")
        self.assertFalse(self.paths.secrets_path.exists())
        self.assertEqual(settings.read_text(), '{"other":"keep"}')
        receipt = (self.paths.home / "cardinal-revocations.json").read_text()
        self.assertEqual(json.loads(receipt)["ingest_key_id"], "ik")
        self.assertNotIn("secret", receipt)

    def test_connect_requests_correct_client_and_scopes_and_clears_pending(self):
        for runtime in ("opencode", "pi"):
            args = type("Args", (), {"runtime": runtime, "telemetry_only": False, "host": "https://app.cardinalhq.io", "version": "0.1.0", "deployment_env": "test"})()
            grant = {"device_code": "private", "verification_uri": "https://example.com/approve", "user_code": "CODE", "interval": 1, "expires_in": 60}
            with patch.object(native.deviceflow, "start_device_code", return_value=grant) as start, patch.object(native.deviceflow, "poll_device_token", return_value=self.bundle), patch.object(native, "status", return_value=0), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(native.connect(args, self.paths), 0)
                self.assertEqual(start.call_args.args[1:], (["ingest:write", "mcp:invoke"], f"cardinal-{runtime}-plugin"))
            self.assertFalse(self.paths.pending_path.exists())
            self.paths.state_path.unlink()

    def test_batch_dedup_is_persistent_and_uses_one_otlp_request(self):
        self.save()
        events = [{"session_id": "s", "event_id": str(i), "model_call_id": str(i), "kind": "model", "model": "fixture", "usage": {"input_tokens": 1}} for i in range(3)]
        with patch.object(native.otlp, "emit_records") as emit:
            native.telemetry("pi", self.paths, events + events, "0.1.0")
            self.assertEqual(len(emit.call_args.args[0]), 6)
            native.telemetry("pi", self.paths, events, "0.1.0")
            self.assertEqual(emit.call_args.args[0], [])

    def test_no_connection_means_no_runtime_files(self):
        native.telemetry("pi", self.paths, [{"session_id": "s", "event_id": "e", "kind": "user"}], "0.1.0")
        self.assertFalse(self.paths.runtime_dir.exists())

    def git_state(self):
        self.save()
        progress = native.session.load_progress(self.paths, "s")
        with patch.object(native.initiative, "git", side_effect=fake_git):
            records = native.records_for_event("pi", self.paths, {"session_id": "s", "event_id": "s", "kind": "session", "cwd": "/fixture"}, progress)
        [attrs] = unpack(records)
        self.assertEqual(attrs["event_name"], "cardinal.git_state")
        return attrs

    def test_git_state_carries_branch_pr_with_bounded_gh_timeout(self):
        self.gh.return_value = (42, "https://github.com/cardinalhq/fixture/pull/42")
        attrs = self.git_state()
        self.assertEqual(attrs["cardinal_pr_number"], "42")
        self.assertEqual(attrs["cardinal_pr_url"], "https://github.com/cardinalhq/fixture/pull/42")
        self.assertEqual(self.gh.call_args.args, ("/fixture", "feat/adapters", native.PR_TIMEOUT_SEC))
        self.assertLessEqual(native.PR_TIMEOUT_SEC, 1.5)
        self.git_state()
        self.assertEqual(self.gh.call_count, 1, "lookups are cached per repo + branch")

    def test_git_state_omits_pr_when_unresolved_or_lookup_fails(self):
        attrs = self.git_state()
        self.assertEqual(attrs["cardinal_head_sha"], "abc123")
        self.assertNotIn("cardinal_pr_number", attrs)
        self.assertNotIn("cardinal_pr_url", attrs)
        with patch.object(native.decisions, "resolve_pr", side_effect=RuntimeError("boom")):
            attrs = self.git_state()
        self.assertEqual(attrs["cardinal_branch"], "feat/adapters")
        self.assertNotIn("cardinal_pr_number", attrs)

    def cli(self, *argv, runtime="pi", env=None):
        out, err = io.StringIO(), io.StringIO()
        environ = {f"CARDINAL_{runtime.upper()}_HOME": self.temp.name, **(env or {})}
        argv = ["cardinal_native.py", "--runtime", runtime, "--version", "9.9.9", *argv]
        with patch.dict(os.environ, environ), patch.object(sys, "argv", argv), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = native.main()
        return code, out.getvalue(), err.getvalue()

    def test_decision_capture_is_opt_in(self):
        self.save()
        code, _, err = self.cli("decision", "record", "--session", "s1", "--choice", "Use SQLite")
        self.assertEqual(code, native.EXIT_OFF)
        self.assertIn("cardinal-pi decision on", err)
        self.assertEqual(self.cli("decision", "context", "--session", "s1")[:2], (0, ""))
        code, out, _ = self.cli("decision", "status")
        self.assertIn("Decision capture: off", out)
        self.assertIn("Cardinal telemetry: connected", out)
        code, out, _ = self.cli("decision", "context", "--session", "s1", env={"CARDINAL_DECISIONS": "1"})
        self.assertIn("Cardinal decision capture is on", out)

    def test_decision_record_emits_tagged_event_and_feeds_context(self):
        self.save()
        self.assertEqual(self.cli("decision", "on")[0], 0)
        self.gh.return_value = (7, "https://github.com/cardinalhq/fixture/pull/7")
        with patch.object(native.initiative, "git", side_effect=fake_git), patch.object(native.otlp, "emit_records") as emit:
            code, out, err = self.cli("decision", "record", "--session=s1", "--choice=Use SQLite", "--question", "Which store?",
                                      "--why", "Zero ops", "--alt=-Postgres", "--by", "user")
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded decision use-sqlite: Use SQLite (PR #7)", out)
        records, conn, _resource = emit.call_args.args
        self.assertIsNotNone(conn)
        self.assertEqual(emit.call_args.kwargs["scope_name"], "cardinal-pi-plugin")
        [attrs] = unpack(records)
        self.assertEqual(attrs["event_name"], "cardinal.decision")
        self.assertEqual(attrs["session_id"], "s1")
        self.assertEqual(attrs["agent_runtime"], "pi")
        self.assertEqual(attrs["cardinal.repo"], "cardinalhq/fixture")
        self.assertEqual(attrs["cardinal.head_sha"], "abc123")
        self.assertEqual(attrs["cardinal.pr_number"], "7")
        self.assertEqual(attrs["cardinal.decision.decided_by"], "user")
        self.assertEqual(json.loads(attrs["cardinal.decision.alternatives"]), ["-Postgres"])

        _, out, _ = self.cli("decision", "context", "--session", "s1", "--tool", "cardinal_record_decision")
        self.assertIn("`cardinal_record_decision` tool", out)
        self.assertIn("- use-sqlite: Use SQLite", out)
        _, out, _ = self.cli("decision", "context", "--session", "s1")
        self.assertIn("cardinal-pi.js\" decision record --session s1", out)
        _, out, _ = self.cli("decision", "status", "--session", "s1", env={"CARDINAL_DECISIONS": "0"})
        self.assertIn("Decision capture: off (CARDINAL_DECISIONS=0)", out)
        self.assertIn("- use-sqlite: Use SQLite", out)
        self.assertEqual(self.cli("decision", "off")[0], 0)
        self.assertFalse(native.decisions.is_enabled(self.paths.runtime_dir))

    def test_decision_record_budget_fits_bridge_and_json_refreshes_context(self):
        self.save()
        self.cli("decision", "on")
        self.assertLess(4 * 1.0 + native.CLUSTER_TIMEOUT_SEC + native.PR_TIMEOUT_SEC + native.DECISION_EMIT_TIMEOUT_SEC, 15.0,
                        "worst case must stay well inside the bridge's 20s kill")
        domains = ([{"id": "d18:core", "cover": "core/"}], "d18-v1:cap=100,min=10")
        with patch.object(native.initiative, "git", side_effect=fake_git), patch.object(native.otlp, "emit_records"), \
                patch.object(native.decisions, "load_domains", return_value=domains) as load, \
                patch.object(native.decisions, "match_clusters", return_value=["d18:core"]):
            code, out, err = self.cli("decision", "record", "--session", "s1", "--choice", "Use SQLite",
                                      "--anchor", "core/x.py", "--json", "--tool", "cardinal_record_decision")
        self.assertEqual(code, 0, err)
        self.assertEqual(load.call_args.kwargs["timeout"], native.CLUSTER_TIMEOUT_SEC)
        self.assertEqual(self.gh.call_args.args[2], native.PR_TIMEOUT_SEC)
        result = json.loads(out)
        self.assertEqual(result["message"], "Recorded decision use-sqlite: Use SQLite (clusters d18:core)")
        self.assertIn("- use-sqlite: Use SQLite", result["context"])
        self.assertIn("`cardinal_record_decision` tool", result["context"])

    def test_decision_record_without_connection_is_local_and_validates(self):
        self.cli("decision", "on", runtime="opencode")
        with patch.object(native.initiative, "git", return_value=None), patch.object(native.otlp, "emit_records") as emit:
            code, out, _ = self.cli("decision", "record", "--session", "s1", "--choice", "Keep it", runtime="opencode")
            self.assertEqual(code, 0)
            self.assertIn("only saved locally (run cardinal-opencode connect)", out)
            code, _, err = self.cli("decision", "record", "--session", "s1", "--choice", "Other", "--id", "Bad Id!", runtime="opencode")
            self.assertEqual(code, native.EXIT_USAGE, err)
        emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
