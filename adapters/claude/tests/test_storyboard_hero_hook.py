"""hooks/storyboard-hero.py: after storyboard__publish, upload the local render
of the act's cover (or hero) scene to the path publish answered (rich unfurls
design §4.2, H1). Driven end to end against a local http.server maestro."""

from __future__ import annotations

import base64
import builtins
import importlib.util
import json
import os
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
HOOK = PLUGIN_ROOT / "hooks" / "storyboard-hero.py"
SB = "sb_29ec61d324cb7c3bad43c6dc"
ORG = "00000000-0000-4000-8000-00000000c0de"
KEY = "cardinal-mcp-key-123"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
UPLOAD = f"/api/orgs/{ORG}/storyboards/{SB}/acts/1/hero?revision=17&scene=shipping"
PLUGIN_VERSION = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]


def publish_result(designated: bool = False, **over) -> dict:
    result = {
        "published": True,
        "storyboard_id": SB,
        "act": 1,
        "revision": 17,
        "card": {
            "hero_scene_id": "shipping",
            "designated_cover": designated,
            "hero_upload": {"method": "PUT", "path": UPLOAD, "content_type": "image/png", "max_bytes": 2097152},
        },
        "published_at": "2026-10-03T00:00:00.000Z",
        "view_url": f"https://app.example/storyboards/{SB}",
        "scenes": ["sawtooth", "cause", "shipping"],
    }
    result.update(over)
    return result


class FakeMaestro:
    def __init__(self):
        self.requests: list = []
        self.answers: list = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_PUT(self):
                n = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({"path": self.path, "body": self.rfile.read(n),
                                      "headers": {k.lower(): v for k, v in self.headers.items()}})
                status, body = stub.answers.pop(0) if stub.answers else (
                    200, {"stored": True, "act": 1, "scene_id": "shipping"})
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class StoryboardHeroHookTests(unittest.TestCase):
    def setUp(self):
        if not (PLUGIN_ROOT / "hooks" / "cardinal_core" / "storyboard_hero.py").exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.maestro = FakeMaestro()
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"env": {
            "CARDINAL_MCP_URL": f"{self.maestro.origin}/api/orgs/{ORG}/mcp", "CARDINAL_MCP_API_KEY": KEY}}))
        self.rev = self.home / ".claude" / "cardinal" / "storyboards" / SB / "r17"
        self.rev.mkdir(parents=True)

    def tearDown(self):
        self.maestro.close()
        self.tmp.cleanup()

    def _png(self, name: str, tail: bytes = b"") -> bytes:
        (self.rev / name).write_bytes(PNG + tail)
        return PNG + tail

    def _env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items()
               if k not in ("CARDINAL_MCP_URL", "CARDINAL_MCP_API_KEY", "CARDINAL_STORYBOARD_HERO")}
        env["HOME"] = str(self.home)
        env.update(extra)
        return env

    def _run(self, result=None, tool_name="mcp__plugin_cardinal_cardinal__storyboard__publish", response=None,
             **env) -> str:
        result = publish_result() if result is None else result
        payload = {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": tool_name,
                   "tool_input": {"storyboard_id": SB},
                   "tool_response": response if response is not None else
                   {"content": json.dumps(result), "structuredContent": result}}
        res = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True,
                             timeout=30, env=self._env(**env), cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        if not res.stdout:
            return ""
        out = json.loads(res.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        return out["additionalContext"]

    def test_the_designated_cover_render_is_uploaded(self):
        self._png("shipping-1.png")
        cover = self._png("shipping-cover.png", b"cover")
        ctx = self._run(publish_result(designated=True))
        self.assertEqual(ctx, "Cardinal: link previews use your cover render of shipping (revision 17).")
        [req] = self.maestro.requests
        self.assertEqual(req["path"], UPLOAD, "the publish path, unchanged")
        self.assertEqual(req["body"], cover)
        self.assertEqual(req["headers"]["x-cardinalhq-api-key"], KEY)
        self.assertEqual(req["headers"]["x-cardinal-client"], f"claude-plugin/{PLUGIN_VERSION}")
        self.assertEqual(req["headers"]["content-type"], "image/png")

    def test_the_last_step_when_no_cover_is_designated(self):
        self._png("shipping-0.png")
        self._png("shipping-1.png")
        last = self._png("shipping-3.png", b"step3")
        self._png("shipping-2.png")
        self._png("shipping-4-dark.png")
        self._png("shipping-cover.png")
        ctx = self._run(publish_result(designated=False))
        self.assertIn("public links use your rendered shipping (revision 17)", ctx)
        self.assertIn("member link previews use Cardinal's summary card", ctx)
        self.assertEqual([r["body"] for r in self.maestro.requests], [last])
        # Designated but never rendered as a cover: the scene's last step.
        self.maestro.requests.clear()
        (self.rev / "shipping-cover.png").unlink()
        ctx = self._run(publish_result(designated=True))
        self.assertIn("link previews use your rendered shipping (revision 17, its last reveal step", ctx)
        self.assertEqual([r["body"] for r in self.maestro.requests], [last])

    def test_the_pre_publish_revision_names_the_directory(self):
        newer = self.rev.parent / "r18"
        newer.mkdir()
        (newer / "shipping-0.png").write_bytes(PNG)
        ctx = self._run()
        self.assertEqual(ctx, "Cardinal: no local render of shipping at revision 17; "
                              "link previews use Cardinal's summary card.")
        self.assertEqual(self.maestro.requests, [])

    def test_no_card_no_upload_and_unpublished_results_are_silent(self):
        self._png("shipping-0.png")
        for result in (publish_result(card=None), publish_result(card={"hero_scene_id": None, "hero_upload": None}),
                       publish_result(published=False), publish_result(revision=None),
                       {k: v for k, v in publish_result().items() if k != "card"}):
            self.assertEqual(self._run(result), "", result)
        for response in ('Error: MCP error 422: {"error":"validation_failed"}', "", [], 42):
            self.assertEqual(self._run(response=response), "", response)
        self.assertEqual(self.maestro.requests, [])

    def test_another_server_or_tool_is_a_no_op(self):
        self._png("shipping-0.png")
        for name in ("mcp__evil__storyboard__publish", "mcp__plugin_other_cardinal__storyboard__publish",
                     "mcp__plugin_cardinal_cardinal__storyboard__preview", "storyboard__publish", "Bash"):
            self.assertEqual(self._run(tool_name=name), "", name)
        self.assertEqual(self.maestro.requests, [])
        self.assertIn("revision 17", self._run(tool_name="mcp__cardinal__storyboard__publish"))

    def test_a_rejected_upload_is_reported(self):
        self._png("shipping-0.png")
        self.maestro.answers.append((409, {"stored": False, "error": "stale_revision",
                                           "message": f"storyboard {SB} changed since the publish"}))
        ctx = self._run()
        self.assertIn("was not uploaded (HTTP 409 stale_revision: storyboard", ctx)
        self.assertIn("link previews use Cardinal's summary card", ctx)

    def test_text_content_and_blocks_are_unwrapped(self):
        self._png("shipping-0.png")
        self.assertIn("revision 17", self._run(response={"content": json.dumps(publish_result())}))
        self.assertIn("revision 17", self._run(response=[{"type": "text", "text": json.dumps(publish_result())}]))

    def test_opt_out_and_not_connected(self):
        self._png("shipping-0.png")
        self.assertEqual(self._run(CARDINAL_STORYBOARD_HERO="0"), "")
        settings = self.home / ".claude" / "settings.json"
        env = json.loads(settings.read_text())
        env["env"]["CARDINAL_STORYBOARD_HERO"] = "off"
        settings.write_text(json.dumps(env))
        self.assertEqual(self._run(), "")
        settings.write_text("{}")
        self.assertEqual(self._run(), "")
        self.assertEqual(self.maestro.requests, [])

    def test_over_the_cap_without_pillow(self):
        spec = importlib.util.spec_from_file_location("storyboard_hero_hook_under_test", HOOK)
        hook = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(hook)
        (self.rev / "shipping-0.png").write_bytes(PNG + b"\0" * (2 * 1024 * 1024))
        real_import = builtins.__import__

        def no_pillow(name, *a, **kw):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("no Pillow")
            return real_import(name, *a, **kw)

        conn = {"origin": self.maestro.origin, "org": ORG, "key": KEY}
        with mock.patch("builtins.__import__", no_pillow):
            ctx = hook.message(publish_result(), self.home, conn)
        self.assertIn(f"is {len(PNG) + 2 * 1024 * 1024} bytes, over the 2097152-byte upload cap (install Pillow", ctx)
        self.assertEqual(self.maestro.requests, [])

    def test_registered_on_publish_with_a_short_timeout(self):
        groups = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"]
        found = [(g["matcher"], h) for g in groups for h in g["hooks"] if "storyboard-hero.py" in h["command"]]
        self.assertEqual(len(found), 1)
        matcher, entry = found[0]
        self.assertEqual(entry["command"], "${CLAUDE_PLUGIN_ROOT}/hooks/storyboard-hero.py")
        self.assertEqual(entry["timeout"], 10)
        self.assertFalse(entry.get("async"), "additionalContext is dropped from async hooks")
        for name in ("mcp__plugin_cardinal_cardinal__storyboard__publish", "mcp__cardinal__storyboard__publish"):
            self.assertRegex(name, "^(?:" + matcher + ")$")
        for name in ("mcp__plugin_cardinal_cardinal__storyboard__preview", "mcp__evil__storyboard__publish"):
            self.assertNotRegex(name, "^(?:" + matcher + ")$")
        self.assertTrue(os.access(HOOK, os.X_OK))


if __name__ == "__main__":
    unittest.main()
