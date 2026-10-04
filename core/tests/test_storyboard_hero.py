"""cardinal_core.storyboard_hero: which local render a published act's link
preview gets, and the PUT that uploads it (rich unfurls design §4.2, H1)."""

from __future__ import annotations

import base64
import builtins
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from cardinal_core import storyboard_hero as sh

SB = "sb_0123456789abcdef01234567"
ORG = "00000000-0000-4000-8000-00000000c0de"
KEY = "cardinal-mcp-key-123"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PATH = f"/api/orgs/{ORG}/storyboards/{SB}/acts/1/hero?revision=17&scene=shipping"


class FakeMaestro:
    def __init__(self):
        self.requests: list = []
        self.answers: list = []  # (status, headers, body)
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                n = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                      "body": self.rfile.read(n)})
                status, headers, body = stub.answers.pop(0) if stub.answers else (
                    200, {"Content-Type": "application/json"}, b'{"stored":true,"act":1}')
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_PUT = _answer
            do_GET = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class HeroPngTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.rev = self.root / SB / "r17"
        self.rev.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _png(self, name: str, tail: bytes = b"") -> Path:
        p = self.rev / name
        p.write_bytes(PNG + tail)
        return p

    def test_the_cover_render_is_preferred_when_designated(self):
        self._png("shipping-0.png")
        self._png("shipping-2.png")
        cover = self._png("shipping-cover.png", b"cover")
        pick = sh.hero_png(self.root, SB, 17, "shipping", True)
        self.assertEqual((pick.status, pick.path, pick.cover), ("ok", cover, True))
        self.assertEqual(pick.png, PNG + b"cover")

    def test_the_last_step_otherwise_and_dark_or_cover_files_never_count(self):
        for name in ("shipping-0.png", "shipping-1.png", "shipping-3-dark.png", "shipping-cover-dark.png",
                     "shipping-x.png", "shipping-10.png.tmp", "shippingx-9.png", "other-7.png"):
            self._png(name)
        last = self._png("shipping-3.png", b"step3")
        self._png("shipping-2.png")
        self._png("shipping-cover.png")
        pick = sh.hero_png(self.root, SB, 17, "shipping", False)
        self.assertEqual((pick.status, pick.path, pick.cover), ("ok", last, False))
        self.assertEqual(pick.png, PNG + b"step3")
        # Numeric, not lexical: 10 beats 9.
        self._png("shipping-9.png")
        ten = self._png("shipping-10.png")
        self.assertEqual(sh.hero_png(self.root, SB, 17, "shipping", False).path, ten)
        # Designated but no cover file: the last step of the same scene.
        (self.rev / "shipping-cover.png").unlink()
        self.assertEqual(sh.hero_png(self.root, SB, 17, "shipping", True).path, ten)

    def test_missing_and_invalid_inputs(self):
        self.assertEqual(sh.hero_png(self.root, SB, 17, "shipping", True).status, "missing")
        self._png("shipping-0.png")
        self.assertEqual(sh.hero_png(self.root, SB, 18, "shipping", False).status, "missing", "another revision")
        for sb, rev, scene in (("../x", 17, "shipping"), (SB, -1, "shipping"), (SB, True, "shipping"),
                               (SB, "17", "shipping"), (SB, 17, "../shipping"), (SB, 17, None)):
            self.assertEqual(sh.hero_png(self.root, sb, rev, scene, False).status, "missing", (sb, rev, scene))
        (self.rev / "shipping-0.png").write_bytes(b"<svg/>")
        self.assertEqual(sh.hero_png(self.root, SB, 17, "shipping", False).status, "missing", "not a PNG")

    def test_over_the_cap_without_pillow(self):
        big = self._png("shipping-0.png", b"\0" * 64)
        real_import = builtins.__import__

        def no_pillow(name, *a, **kw):
            if name == "PIL" or name.startswith("PIL."):
                raise ImportError("no Pillow")
            return real_import(name, *a, **kw)

        with mock.patch("builtins.__import__", no_pillow):
            pick = sh.hero_png(self.root, SB, 17, "shipping", False, cap=32)
        self.assertEqual((pick.status, pick.path, pick.png), ("over_cap", big, None))
        self.assertEqual(pick.size, len(PNG) + 64)


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.maestro = FakeMaestro()
        self.conn = {"origin": self.maestro.origin, "org": ORG, "key": KEY}

    def tearDown(self):
        self.maestro.close()

    def test_put_carries_the_key_the_client_and_the_png(self):
        res = sh.upload_hero(self.conn, PATH, PNG, 5, "claude-plugin/0.40.0")
        self.assertEqual((res.status, res.body, res.error), (200, {"stored": True, "act": 1}, None))
        [req] = self.maestro.requests
        self.assertEqual((req["method"], req["path"], req["body"]), ("PUT", PATH, PNG))
        self.assertEqual(req["headers"]["x-cardinalhq-api-key"], KEY)
        self.assertEqual(req["headers"]["x-cardinal-client"], "claude-plugin/0.40.0")
        self.assertEqual(req["headers"]["content-type"], "image/png")

    def test_http_errors_are_returned_not_raised(self):
        self.maestro.answers.append((409, {"Content-Type": "application/json"},
                                     b'{"stored":false,"error":"stale_revision","message":"changed"}'))
        res = sh.upload_hero(self.conn, PATH, PNG, 5, "claude-plugin/x")
        self.assertEqual(res.status, 409)
        self.assertEqual(res.body["error"], "stale_revision")

    def test_a_redirect_is_not_followed(self):
        self.maestro.answers.append((307, {"Location": self.maestro.origin + "/elsewhere"}, b""))
        res = sh.upload_hero(self.conn, PATH, PNG, 5, "claude-plugin/x")
        self.assertEqual(res.status, 307)
        self.assertEqual([r["path"] for r in self.maestro.requests], [PATH], "the key never follows a redirect")

    def test_only_this_orgs_hero_route(self):
        for path in (f"/api/orgs/other-org/storyboards/{SB}/acts/1/hero?revision=1&scene=s",
                     f"/api/orgs/{ORG}/storyboards/{SB}/scenes/s/preview.html?revision=1",
                     f"/api/orgs/{ORG}/storyboards/{SB}/acts/1/hero",
                     f"https://evil.example/api/orgs/{ORG}/storyboards/{SB}/acts/1/hero?revision=1",
                     f"/api/orgs/{ORG}/storyboards/sb_x/acts/1/hero?revision=1", None):
            with self.assertRaises(ValueError, msg=path):
                sh.upload_hero(self.conn, path, PNG, 5, "claude-plugin/x")
        self.assertEqual(self.maestro.requests, [])

    def test_no_answer_is_reported(self):
        self.maestro.close()
        res = sh.upload_hero(self.conn, PATH, PNG, 2, "claude-plugin/x")
        self.assertIsNone(res.status)
        self.assertTrue(res.error)
        self.maestro = FakeMaestro()  # for tearDown


if __name__ == "__main__":
    unittest.main()
