"""Tests for cardinal_core.evidence (the local evidence spool).

Run from core/:  python3 -m unittest tests.test_evidence -v
"""

from __future__ import annotations

import json
import os
import random
import re
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from cardinal_core import evidence as ev

VECTORS = json.loads((Path(__file__).resolve().parent / "testdata" / "scrub_vectors.json").read_text())


class ScrubVectorTests(unittest.TestCase):
    """The port must agree with conductor's Go scrub on every shared vector
    (the file's _comment names the Go source and commit)."""

    def test_vectors_name_their_conductor_source(self):
        self.assertIn("packages/mcp-gateway/storyboard/receipts/receipts.go", VECTORS["_comment"])
        self.assertIn("credential_keys.json", VECTORS["_comment"])
        for section in ("scrub", "text", "plain"):
            self.assertGreater(len(VECTORS[section]), 5, section)

    def test_credential_key_corpus(self):
        keys = VECTORS["keys"]
        for k in keys["credential"]:
            self.assertTrue(ev.is_credential_key(k), k)
        for k in keys["evidence_both"] + keys["evidence_gateway_only"]:
            self.assertFalse(ev.is_credential_key(k), k)
        for k in keys["headers"]:
            self.assertTrue(ev.is_headers_key(k), k)
        for name in keys["secret_pair_names"]:
            self.assertEqual(ev.scrub({"name": name, "value": "LEAK"})["value"], ev.REDACTED, name)
        for name in keys["plain_pair_names"]:
            self.assertEqual(ev.scrub({"name": name, "value": "ok"})["value"], "ok", name)

    def test_structured_scrub_matches_go(self):
        for case in VECTORS["scrub"]:
            self.assertEqual(ev.scrub(case["in"]), case["out"], json.dumps(case["in"])[:200])

    def test_text_scrub_matches_go_byte_for_byte(self):
        for case in VECTORS["text"]:
            self.assertEqual(ev.scrub_text(case["in"]), case["out"], case["in"])

    def test_plain_text_redaction_matches_go(self):
        for case in VECTORS["plain"]:
            self.assertEqual(ev.redact_plain_text(case["in"]), case["out"], case["in"])

    def test_literal_redactions(self):
        self.assertEqual(ev.REDACTED, "[redacted]")
        out = ev.scrub({"api_key": "LEAK", "rows": [{"password": "LEAK"}], "note": "ghp_" + "A" * 30})
        self.assertNotIn("LEAK", json.dumps(out))
        self.assertEqual(out["note"], "[redacted]")
        # Evidence that is not secret stays.
        keep = {"input_tokens": 12, "nextPageToken": "cursor-1", "automountServiceAccountToken": True,
                "commit_hash": "deadbeef"}
        self.assertEqual(ev.scrub(keep), keep)


class LinearScannerParityTests(unittest.TestCase):
    """_key_sep_matches and _url_userinfo_spans replace two Go patterns that
    are quadratic under Python's backtracking re; they must return exactly
    what the literal patterns do (leftmost-first FindAll)."""

    def test_key_sep_scanner_equals_go_pattern(self):
        go = re.compile(ev.GO_KEY_SEP)
        rnd = random.Random(1942)
        alphabet = list("ab9_.-\"'\\ \t:=>ntrP")
        for _ in range(30000):
            s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 16)))
            want = [(m.start(), m.group(1), m.end()) for m in go.finditer(s)]
            self.assertEqual(list(ev._key_sep_matches(s)), want, repr(s))

    def test_url_userinfo_scanner_equals_go_pattern(self):
        go = re.compile(ev.GO_URL_USERINFO, re.ASCII)
        rnd = random.Random(1943)
        alphabet = list("ab9+.-:/@ \t\nZ_")
        for _ in range(30000):
            s = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, 16)))
            want = [(m.end(1), m.end()) for m in go.finditer(s)]
            self.assertEqual(ev._url_userinfo_spans(s), want, repr(s))

    def test_pathological_runs_stay_fast(self):
        for text in ("k" * (1 << 20), "a" * (1 << 19) + "://" + "b" * (1 << 19), "\\" * (1 << 19) + "=",
                     "k=:" * (1 << 16)):
            t0 = time.monotonic()
            ev.redact_plain_text(text)
            ev.scrub_text(text)
            self.assertLess(time.monotonic() - t0, 2.0)


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name))
        self.projects = self.root / ".claude" / "projects"

    def tearDown(self):
        self.tmp.cleanup()

    def test_string_result(self):
        self.assertEqual(ev.normalize("rows: 3"), {"text": ["rows: 3"]})

    def test_content_and_structured_content_drops_the_json_mirror(self):
        sc = {"rows": [1, 2], "b": "x"}
        body = ev.normalize({"content": json.dumps(sc), "structuredContent": sc})
        self.assertEqual(body, {"structured": sc})
        body = ev.normalize({"content": "summary", "structuredContent": sc})
        self.assertEqual(body, {"structured": sc, "text": ["summary"]})

    def test_content_blocks(self):
        blocks = [{"type": "text", "text": "a"}, {"type": "image", "data": "..."}, {"type": "text", "text": "b"}]
        self.assertEqual(ev.normalize(blocks), {"text": ["a", "b"], "other_blocks": 1})
        self.assertEqual(ev.normalize({"content": blocks}), {"text": ["a", "b"], "other_blocks": 1})

    def test_other_json_is_structured(self):
        self.assertEqual(ev.normalize({"rows": []}), {"structured": {"rows": []}})
        self.assertEqual(ev.normalize(None), {})

    def _notice(self, path: Path) -> str:
        return (f"Error: result (71,204 characters) exceeds maximum allowed tokens. Output has been saved to "
                f"{path}.\nFormat: JSON\nUse offset and limit parameters to read specific portions of the file.")

    def test_spill_notice_under_the_allowed_root_is_followed(self):
        spill = self.projects / "-w" / "sess" / "tool-results" / "mcp-x 1.txt"
        spill.parent.mkdir(parents=True)
        spill.write_text('{"rows":[1,2,3]}')
        body = ev.normalize(self._notice(spill), spill_root=self.projects)
        self.assertEqual(body, {"spilled": True, "spilled_bytes": 16, "text": ['{"rows":[1,2,3]}']})
        body = ev.normalize({"content": [{"type": "text", "text": self._notice(spill)}]}, spill_root=self.projects)
        self.assertTrue(body["spilled"])
        # Without a spill root the notice is kept as text.
        self.assertEqual(ev.normalize(self._notice(spill)), {"text": [self._notice(spill)]})

    def test_spill_outside_root_symlink_escape_relative_and_oversize_are_refused(self):
        outside = self.root / "elsewhere" / "secret.txt"
        outside.parent.mkdir(parents=True)
        outside.write_text("top secret")
        self.assertIsNone(ev.spill_path(self._notice(outside), self.projects))
        link = self.projects / "p" / "link.txt"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        self.assertIsNone(ev.spill_path(self._notice(link), self.projects))
        self.assertIsNone(ev.spill_path("Output has been saved to ../../etc/passwd.", self.projects))
        big = self.projects / "p" / "big.txt"
        big.write_text("x" * 100)
        self.assertIsNone(ev.spill_path(self._notice(big), self.projects, max_bytes=99))
        self.assertEqual(ev.spill_path(self._notice(big), self.projects, max_bytes=100), big)
        body = ev.normalize(self._notice(outside), spill_root=self.projects)
        self.assertNotIn("top secret", json.dumps(body))

    def test_spill_candidates_one_line_notice(self):
        p = "/Users/John Doe/.claude/projects/x/preview 1.txt"
        self.assertIn(p, ev.spill_candidates(f"Output has been saved to {p}. Use offset and limit to read it."))
        self.assertEqual(ev.spill_candidates("no notice here"), [])


class CapTests(unittest.TestCase):
    def test_small_value_is_scrubbed_not_truncated(self):
        out, truncated = ev.scrub_and_cap({"password": "LEAK", "n": 1})
        self.assertFalse(truncated)
        self.assertEqual(out, {"password": "[redacted]", "n": 1})

    def test_large_value_becomes_a_truncated_body_within_the_cap(self):
        rows = [{"i": i, "token": f"LEAK{i}", "msg": "x" * 40} for i in range(6000)]
        out, truncated = ev.scrub_and_cap({"structured": {"rows": rows}})
        self.assertTrue(truncated)
        self.assertEqual(set(out), {"truncated", "original_bytes", "prefix"})
        self.assertIs(out["truncated"], True)
        self.assertLessEqual(len(ev.encode_json(out).encode("utf-8")), ev.MAX_RESULT_BYTES)
        self.assertGreater(out["original_bytes"], ev.MAX_RESULT_BYTES)
        self.assertNotIn("LEAK", out["prefix"])
        self.assertTrue(out["prefix"].startswith('{"structured":{"rows":[{"i":0,'))

    def test_too_large_to_scrub_whole_is_still_scrubbed_structurally(self):
        text = json.dumps({"password": "LEAK-A", "rows": ["y" * 100] * 50})
        out, truncated = ev.scrub_and_cap({"text": [text]}, max_bytes=2048, structural_limit=1024)
        self.assertTrue(truncated)
        self.assertEqual(set(out), {"truncated", "original_bytes", "prefix"})
        self.assertNotIn("LEAK", out["prefix"])
        self.assertIn('\\"password\\":\\"[redacted]\\"', out["prefix"])
        self.assertLessEqual(len(ev.encode_json(out).encode("utf-8")), 2048)

    def test_bounded_scrub_is_a_prefix_of_the_whole_scrub(self):
        rows = [{"i": i, "name": "DB_PASSWORD" if i % 3 == 0 else "LEVEL", "value": f"v{i}",
                 "headers": {"X-Tenant": f"t{i}", "name": "keep"}, "auth": {"secretRef": "s", "key": f"k{i}"},
                 "note": f"postgres://u:p{i}@db/x ghp_{'A' * 30}", "nested": json.dumps({"api_key": f"n{i}"})}
                for i in range(300)]
        value = {"structured": {"rows": rows}, "text": [json.dumps({"rows": rows})]}
        whole = ev.encode_json(ev.scrub(value))
        for limit in (1, 50, 997, 4096, 30000, len(whole) + 10):
            text, complete = ev.bounded_scrub(value, limit)
            self.assertTrue(whole.startswith(text), limit)
            self.assertGreaterEqual(len(text), min(limit, len(whole)), limit)
            self.assertEqual(complete, text == whole, limit)

    def _pod_list(self, n: int) -> dict:
        # `kubectl get pods -o json` shape: a credential env var as a
        # {name, value} pair and a probe's headers object.
        def pod(i):
            return {"metadata": {"name": f"pod-{i}", "annotations": {"note": "x" * 200}},
                    "spec": {"containers": [{"name": "app",
                                             "env": [{"name": "DB_PASSWORD", "value": "hunter2-plaintext"},
                                                     {"name": "LOG_LEVEL", "value": "info"}],
                                             "livenessProbe": {"httpGet": {
                                                 "path": "/healthz",
                                                 "httpHeaders": [{"name": "X-Tenant", "value": "tenant-hdr-yyy"}],
                                                 "headers": {"X-Tenant": "tenant-secret-zzz"}}}}]}}
        return {"kind": "PodList", "items": [pod(i) for i in range(n)]}

    def test_result_over_the_structural_limit_keeps_pair_and_header_rules(self):
        pods = self._pod_list(8000)
        text = json.dumps(pods)
        self.assertGreater(len(text), ev.MAX_STRUCTURAL_SCRUB_BYTES)
        for body in ({"text": [text]}, {"structured": pods}, {"structured": pods, "text": [text]},
                     {"text": [json.dumps({"content": [{"type": "text", "text": text}]})]}):
            out, truncated = ev.scrub_and_cap(body)
            self.assertTrue(truncated)
            stored = json.dumps(out)
            self.assertLessEqual(len(ev.encode_json(out).encode("utf-8")), ev.MAX_RESULT_BYTES)
            self.assertNotIn("hunter2-plaintext", stored)
            self.assertNotIn("tenant-secret-zzz", stored)
            self.assertIn("[redacted]", stored)
            self.assertIn("LOG_LEVEL", stored)
            self.assertIn("info", stored)
            self.assertGreater(out["original_bytes"], ev.MAX_STRUCTURAL_SCRUB_BYTES)
            self.assertGreater(len(out["prefix"]), ev.MAX_RESULT_BYTES // 2)

    def test_json_cut_short_over_the_structural_limit_keeps_pair_and_header_rules(self):
        text = json.dumps(self._pod_list(8000))
        cut = text[: len(text) - 1000]  # a spill read to MAX_SPILL_READ_BYTES does not decode
        out, truncated = ev.scrub_and_cap({"text": [cut]})
        self.assertTrue(truncated)
        stored = json.dumps(out)
        self.assertNotIn("hunter2-plaintext", stored)
        self.assertNotIn("tenant-secret-zzz", stored)
        self.assertIn("LOG_LEVEL", stored)

    def test_redact_json_text(self):
        cases = [
            ('[{"name": "DB_PASSWORD", "value": "hunter2"}, {"name": "LEVEL", "value": "info"}',
             '[{"name":"DB_PASSWORD","value":"[redacted]"}, {"name": "LEVEL", "value": "info"}'),
            ('{"name":"API_TOKEN","value":"ab}cd"}', '{"name":"API_TOKEN","value":"[redacted]"}'),
            ('{"env":[{"name":"DB_PASSWORD","value":"cut-her', '{"env":[{"name":"DB_PASSWORD","value":"[redacted]'),
            ('{"headers": {"X-Tenant": "t-1", "name": "keep", "Accept": ["a", "b"]}, "after": "ok"}',
             '{"headers": {"X-Tenant": "[redacted]", "name": "keep", "Accept": ["[redacted]", "[redacted]"]}, '
             '"after": "ok"}'),
            ('{"requestHeaders": {"Cookie": "sess=1", "X-A": "open-ended', '{"requestHeaders": {"Cookie": "[redacted]", "X-A": "[redacted]'),
            ('{"header": "not-a-headers-object"}', '{"header": "not-a-headers-object"}'),
            ('{"name": "LEVEL", "value": "info"}', '{"name": "LEVEL", "value": "info"}'),
        ]
        for text, want in cases:
            self.assertEqual(ev.redact_json_text(text), want, text)

    def test_redact_json_text_stays_fast(self):
        for text in ('{"headers":{' + '"a":"b",' * (1 << 16), '{' * (1 << 18), '{"a":1}' * (1 << 16),
                     '"name":"password","value":' * (1 << 14)):
            t0 = time.monotonic()
            ev.redact_json_text(text)
            self.assertLess(time.monotonic() - t0, 2.0)

    def test_a_token_cut_at_the_prefix_end_is_dropped(self):
        tok = "ghp_" + "Z" * 36
        value = {"text": ["a " * 400 + tok + " tail " * 400]}
        raw = ev.encode_json(value)
        cut = raw.index(tok) + 12  # mid-token
        out, _ = ev.scrub_and_cap(value, max_bytes=cut + 150, structural_limit=10)
        self.assertNotIn("ghp_ZZ", out["prefix"])

    def test_multibyte_prefix_is_cut_on_a_character_boundary(self):
        out, truncated = ev.scrub_and_cap({"text": ["é" * 5000]}, max_bytes=1024)
        self.assertTrue(truncated)
        out["prefix"].encode("utf-8")
        self.assertLessEqual(len(ev.encode_json(out).encode("utf-8")), 1024)


class EntryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = ev.default_root(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def _entry(self, **over):
        kw = dict(server="grafana", tool="query_prometheus", tool_name="mcp__grafana__query_prometheus",
                  tool_input={"expr": "up", "api_key": "LEAK-ARG"},
                  tool_response={"content": "ok", "structuredContent": {"series": [1], "token": "LEAK-RES"}},
                  session_id="sess-1", called_at="2026-09-28T12:00:00.000000Z")
        kw.update(over)
        return ev.build_entry(**kw)

    def test_default_root_and_split(self):
        self.assertEqual(ev.default_root(Path("/h")), Path("/h/.cardinal/evidence"))
        self.assertEqual(ev.split_mcp_tool("mcp__grafana__query_prometheus"), ("grafana", "query_prometheus"))
        self.assertEqual(ev.split_mcp_tool("mcp__plugin_cardinal_cardinal__storyboard__preview"),
                         ("plugin_cardinal_cardinal", "storyboard__preview"))
        for bad in ("Bash", "mcp__", "mcp__x", "mcp____y", None, 3):
            self.assertIsNone(ev.split_mcp_tool(bad), bad)

    def test_entry_shape_id_and_scrub(self):
        e = self._entry()
        self.assertEqual(e["schema"], "cardinal.evidence.v1")
        self.assertEqual(e["tier"], "captured")
        self.assertRegex(e["evidence_id"], r"^ev_[0-9a-f]{12}$")
        self.assertEqual(e["evidence_id"], ev.evidence_id("grafana", "query_prometheus", e["args"], e["called_at"]))
        self.assertEqual(e["args"], {"expr": "up", "api_key": "[redacted]"})
        self.assertEqual(e["result"], {"structured": {"series": [1], "token": "[redacted]"}, "text": ["ok"]})
        self.assertFalse(e["truncated"])
        self.assertNotIn("LEAK", json.dumps(e))
        # Deterministic for the same call, distinct for another time.
        self.assertEqual(self._entry()["evidence_id"], e["evidence_id"])
        self.assertNotEqual(self._entry(called_at="2026-09-28T12:00:00.000001Z")["evidence_id"], e["evidence_id"])

    def test_write_is_atomic_and_private(self):
        e = self._entry()
        path = ev.write_entry(self.root, e)
        self.assertEqual(path, self.root / "sess-1" / f"{e['evidence_id']}.json")
        self.assertEqual(json.loads(path.read_text()), e)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual([p.name for p in path.parent.iterdir()], [path.name], "no temp file left behind")
        self.assertEqual(ev.read_entry(self.root, e["evidence_id"]), e)
        self.assertIsNone(ev.read_entry(self.root, "../../etc/passwd"))

    def test_concurrent_writes_into_a_new_session_all_land(self):
        # Parallel MCP calls in a new session race to create the root and
        # the session directory; every capture must still be written.
        for trial in range(20):
            root = self.home / f"r{trial}" / ".cardinal" / "evidence"
            entries = [self._entry(called_at=f"2026-09-28T12:00:00.{i:06d}Z") for i in range(8)]
            barrier = threading.Barrier(len(entries))
            errors = []

            def write(e):
                try:
                    barrier.wait()
                    ev.write_entry(root, e)
                except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                    errors.append(exc)

            threads = [threading.Thread(target=write, args=(e,)) for e in entries]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])
            for e in entries:
                self.assertTrue((root / "sess-1" / f"{e['evidence_id']}.json").is_file())
            self.assertEqual(stat.S_IMODE(os.stat(root / "sess-1").st_mode), 0o700)

    def test_session_dir_removed_by_gc_before_the_write_is_recreated(self):
        real = tempfile.mkstemp
        calls = []

        def racing_mkstemp(*a, **kw):
            calls.append(1)
            if len(calls) == 1:
                os.rmdir(kw["dir"])  # gc() emptied and removed it
            return real(*a, **kw)

        with mock.patch.object(ev.tempfile, "mkstemp", side_effect=racing_mkstemp):
            path = ev.write_entry(self.root, self._entry())
        self.assertEqual(len(calls), 2)
        self.assertTrue(path.is_file())

    def test_read_entry_refuses_a_symlinked_entry(self):
        e = self._entry()
        outside = self.home / "planted.json"
        outside.write_text(json.dumps({"evidence_id": e["evidence_id"], "planted": True}))
        sdir = self.root / "sess-1"
        sdir.mkdir(parents=True)
        (sdir / f"{e['evidence_id']}.json").symlink_to(outside)
        self.assertIsNone(ev.read_entry(self.root, e["evidence_id"]))

    def test_loose_permissions_are_tightened(self):
        self.root.mkdir(parents=True, mode=0o755)
        os.chmod(self.root, 0o755)
        path = ev.write_entry(self.root, self._entry())
        self.assertEqual(stat.S_IMODE(os.stat(self.root).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(path.parent).st_mode), 0o700)

    def test_symlinked_session_dir_is_refused(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        self.root.mkdir(parents=True)
        (self.root / "sess-1").symlink_to(elsewhere)
        with self.assertRaises(OSError):
            ev.write_entry(self.root, self._entry())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_bad_or_missing_session_id_goes_to_no_session(self):
        for sid in (None, "../../x", "a" * 200, 7):
            e = self._entry(session_id=sid)
            self.assertIsNone(e["session_id"])
            path = ev.write_entry(self.root, e)
            self.assertEqual(path.parent.name, "no-session")

    def test_opt_out(self):
        self.assertFalse(ev.capture_disabled(self.root, env={}))
        for v in ("0", "false", "OFF", "no"):
            self.assertTrue(ev.capture_disabled(self.root, env={"CARDINAL_EVIDENCE_CAPTURE": v}), v)
        self.assertFalse(ev.capture_disabled(self.root, env={"CARDINAL_EVIDENCE_CAPTURE": "1"}))
        self.root.mkdir(parents=True)
        (self.root / "disabled").touch()
        self.assertTrue(ev.capture_disabled(self.root, env={}))


class CaptureTests(unittest.TestCase):
    """capture(): the per-call step the Claude, Cursor and Gemini hooks share."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(os.path.realpath(self.tmp.name))
        self.root = ev.default_root(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def _capture(self, env=None, **over):
        kw = dict(server="grafana", tool="query_prometheus", tool_name="mcp__grafana__query_prometheus",
                  tool_input={"expr": "up", "password": "LEAK-ARG"},
                  tool_response={"content": [{"type": "text", "text": "series: 3"}]},
                  session_id="conv-1", tool_use_id="tu_1", agent="cursor/1.7.29")
        kw.update(over)
        return ev.capture(self.home, env={} if env is None else env, **kw)

    def test_client_string(self):
        self.assertEqual(ev.client_string("cursor", "1.7.29"), "cursor/1.7.29")
        self.assertEqual(ev.client_string("gemini", "0.50.0-nightly.20260901"), "gemini/0.50.0-nightly.20260901")
        # Missing or not a plain version token: the bare name, never free text.
        for v in (None, "", 7, "1.0; rm -rf /", "../1", "1.0/2", "v" * 65, "\n1.0", "1.0\n", " 1.0", ".1"):
            self.assertEqual(ev.client_string("cursor", v), "cursor", repr(v))
        for bad in ("", "Cursor", "a/b", "x y", "cursor\n", None):
            with self.assertRaises(ValueError):
                ev.client_string(bad, "1.0")

    def test_captured_line(self):
        self.assertEqual(ev.captured_line("ev_0123456789ab", "grafana", "query_prometheus"),
                         "[evidence:ev_0123456789ab] captured locally from grafana/query_prometheus")

    def test_capture_writes_a_scrubbed_private_entry(self):
        e = self._capture()
        path = self.root / "conv-1" / f"{e['evidence_id']}.json"
        self.assertTrue(path.is_file())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.root / "conv-1").stat().st_mode), 0o700)
        on_disk = json.loads(path.read_text())
        self.assertEqual(on_disk, e)
        self.assertEqual(on_disk["agent"], "cursor/1.7.29")
        self.assertEqual(on_disk["tool_use_id"], "tu_1")
        self.assertEqual(on_disk["args"], {"expr": "up", "password": "[redacted]"})
        self.assertEqual(on_disk["result"], {"text": ["series: 3"]})
        self.assertNotIn("LEAK", path.read_text())

    def test_capture_honours_the_opt_out(self):
        self.assertIsNone(self._capture(env={"CARDINAL_EVIDENCE_CAPTURE": "0"}))
        self.assertFalse(self.root.exists())
        self.root.mkdir(parents=True)
        (self.root / "disabled").touch()
        self.assertIsNone(self._capture())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["disabled"])

    def test_capture_raises_on_a_refused_spool_for_the_caller_to_swallow(self):
        elsewhere = self.home / "elsewhere"
        elsewhere.mkdir()
        (self.home / ".cardinal").mkdir()
        os.symlink(elsewhere, self.root)
        with self.assertRaises(OSError):
            self._capture()
        self.assertEqual(list(elsewhere.iterdir()), [])


class GCTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(os.path.realpath(self.tmp.name)) / "evidence"
        self.root.mkdir(mode=0o700)
        self.now = time.time()

    def tearDown(self):
        self.tmp.cleanup()

    def _file(self, rel: str, age_s: float) -> Path:
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}")
        t = self.now - age_s
        os.utime(p, (t, t))
        return p

    def test_removes_expired_entries_and_stale_temp_files_only(self):
        day = 24 * 3600
        old = self._file("s1/ev_000000000001.json", 15 * day)
        fresh = self._file("s1/ev_000000000002.json", 13 * day)
        tmp_old = self._file("s1/.ev_abc.tmp", 2 * 3600)
        tmp_new = self._file("s1/.ev_def.tmp", 10)
        other = self._file("s1/notes.txt", 30 * day)
        gone = self._file("s2/ev_000000000003.json", 20 * day)
        disabled = self._file("disabled", 30 * day)
        removed = ev.gc(self.root, now=self.now, force=True)
        self.assertEqual(removed, 3)
        self.assertFalse(old.exists())
        self.assertFalse(tmp_old.exists())
        self.assertFalse(gone.exists())
        self.assertFalse((self.root / "s2").exists(), "an emptied session dir is removed")
        for keep in (fresh, tmp_new, other, disabled):
            self.assertTrue(keep.exists(), keep)

    def test_runs_at_most_once_per_interval(self):
        self._file("s1/ev_000000000001.json", 20 * 24 * 3600)
        self.assertEqual(ev.gc(self.root, now=self.now), 1)
        self._file("s1/ev_000000000002.json", 20 * 24 * 3600)
        self.assertEqual(ev.gc(self.root, now=self.now + 60), 0, "stamp throttles")
        self.assertEqual(ev.gc(self.root, now=self.now + ev.GC_INTERVAL_S + 1), 1)

    def test_is_time_bounded(self):
        for i in range(50):
            self._file(f"s{i}/ev_{i:012x}.json", 20 * 24 * 3600)
        self.assertEqual(ev.gc(self.root, now=self.now, force=True, budget_s=0), 0)
        self.assertEqual(ev.gc(self.root, now=self.now, force=True), 50)

    def test_symlinked_or_missing_root_is_left_alone(self):
        victim = Path(self.tmp.name) / "victim"
        victim.mkdir()
        f = victim / "s1" / "ev_000000000001.json"
        f.parent.mkdir()
        f.write_text("{}")
        os.utime(f, (0, 0))
        link = Path(self.tmp.name) / "link"
        link.symlink_to(victim)
        self.assertEqual(ev.gc(link, force=True), 0)
        self.assertTrue(f.exists())
        self.assertEqual(ev.gc(Path(self.tmp.name) / "missing", force=True), 0)


if __name__ == "__main__":
    unittest.main()
