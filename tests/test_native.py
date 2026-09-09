"""Native adapters share the existing Cardinal event contract and credential flow."""
from __future__ import annotations

import contextlib
import io
import json
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


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = AgentPaths(Path(self.temp.name))
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


if __name__ == "__main__":
    unittest.main()
