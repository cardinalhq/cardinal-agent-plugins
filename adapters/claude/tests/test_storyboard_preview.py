"""Storyboard skills: the local preview renderer (skills/canvas/scripts/
render_preview.py) and the SessionStart session-id hook
(hooks/storyboard-session.py).

The renderer is driven end to end against a fake "chromium" that speaks the
--remote-debugging-pipe protocol (fd 3 in, fd 4 out, NUL-framed JSON) and a
stub maestro serving the preview-bundle route. A real-Chrome smoke test runs
only with CARDINAL_CHROMIUM_SMOKE=1 (see RealChromeSmokeTests).
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
RENDER_PREVIEW = PLUGIN_ROOT / "skills" / "canvas" / "scripts" / "render_preview.py"
SESSION_HOOK = PLUGIN_ROOT / "hooks" / "storyboard-session.py"

SB_ID = "sb_0123456789abcdef01234567"
ORG = "org-1"
KEY = "cardinal-mcp-key-123"
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_preview_under_test", RENDER_PREVIEW)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rp = _load_renderer()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_CHROMIUM = textwrap.dedent('''\
    #!{python}
    """Fake Chromium: answers the CDP subset render_preview.py uses over
    --remote-debugging-pipe and logs what it saw to $FAKE_CHROME_LOG."""
    import base64, json, os, sys
    PNG = base64.b64decode({png!r})
    MODE = os.environ.get("FAKE_CHROME_MODE", "ok")
    LOG = os.environ["FAKE_CHROME_LOG"]
    def log(kind, **kw):
        with open(LOG, "a") as f:
            f.write(json.dumps(dict(kind=kind, **kw)) + "\\n")
    if "--version" in sys.argv:
        print(os.environ.get("FAKE_CHROME_VERSION", "Google Chrome 150.0.7000.1"))
        sys.exit(0)
    log("argv", argv=sys.argv[1:], tz=os.environ.get("TZ"))
    if MODE == "nosandbox":
        sys.stderr.write("[FATAL:zygote_host_impl_linux.cc] No usable sandbox! user namespaces\\n")
        sys.exit(1)
    rd, wr = 3, 4
    def send(obj):
        data = json.dumps(obj).encode() + b"\\0"
        while data:
            n = os.write(wr, data)
            data = data[n:]
    buf = b""
    page_url = None
    while True:
        chunk = os.read(rd, 65536)
        if not chunk:
            break
        buf += chunk
        while b"\\0" in buf:
            raw, buf = buf.split(b"\\0", 1)
            msg = json.loads(raw)
            m, p, sid = msg["method"], msg.get("params", {{}}), msg.get("sessionId")
            log("cmd", method=m, params=p, session=sid)
            res = {{}}
            events = []
            if m == "Browser.getVersion":
                res = {{"product": "HeadlessChrome/150.0.7000.1"}}
            elif m == "Target.createBrowserContext":
                res = {{"browserContextId": "CTX"}}
            elif m == "Target.createTarget":
                res = {{"targetId": "MAIN"}}
            elif m == "Target.attachToTarget":
                res = {{"sessionId": "S1"}}
            elif m == "Page.getFrameTree":
                res = {{"frameTree": {{"frame": {{"id": "MAIN"}}}}}}
            elif m == "Page.navigate":
                page_url = p["url"].split("#", 1)[0]
                res = {{"frameId": "MAIN"}}
                ev = lambda method, params: events.append({{"method": method, "params": params, "sessionId": sid}})
                ev("Fetch.requestPaused", {{"requestId": "r-own", "request": {{"url": page_url}}}})
                ev("Fetch.requestPaused", {{"requestId": "r-evil", "request": {{"url": "https://evil.example/leak"}}}})
                ev("Fetch.requestPaused", {{"requestId": "r-data", "request": {{"url": "data:image/png;base64,AA"}}}})
                ev("Page.frameAttached", {{"frameId": "CV", "parentFrameId": "MAIN"}})
                ev("Page.frameNavigated", {{"frame": {{"id": "CV", "parentId": "MAIN", "url": "about:srcdoc"}}}})
                ev("Page.domContentEventFired", {{}})
            elif m == "Runtime.evaluate":
                expr = p.get("expression", "")
                if "abort(" in expr:
                    res = {{"result": {{"type": "undefined"}}}}
                elif "reveal(" in expr:
                    if MODE == "grandchild":
                        send({{"method": "Page.frameAttached", "params": {{"frameId": "G", "parentFrameId": "CV"}}, "sessionId": sid}})
                        continue  # never answer: the lock must stop the wait
                    if MODE == "reveal_error":
                        res = {{"result": {{"type": "object", "value": {{"ok": False, "error": "ready timeout"}}}}}}
                    else:
                        res = {{"result": {{"type": "object", "value": {{"ok": True, "steps": 2}}}}}}
                elif "getBoundingClientRect" in expr:
                    res = {{"result": {{"type": "object", "value": {{
                        "rect": {{"x": 0, "y": 0, "width": 1280, "height": 900}},
                        "state": "settled", "step": 0, "steps": 2, "height": 900,
                        "frameErrors": [], "protocolErrors": []}}}}}}
                elif "report()" in expr:
                    res = {{"result": {{"type": "object", "value": {{
                        "state": "settled", "step": 1, "steps": 2, "height": 900, "error": None,
                        "frameErrors": [], "protocolErrors": []}}}}}}
            elif m == "Page.captureScreenshot":
                res = {{"data": base64.b64encode(PNG).decode()}}
            elif m == "Browser.close":
                send({{"id": msg["id"], "result": {{}}}})
                sys.exit(0)
            send({{"id": msg["id"], "result": res, **({{"sessionId": sid}} if sid else {{}})}})
            for e in events:
                send(e)
''')


def make_fake_chromium(dirpath: Path) -> Path:
    path = dirpath / "fake-chromium"
    path.write_text(FAKE_CHROMIUM.format(python=sys.executable, png=base64.b64encode(TINY_PNG).decode()))
    path.chmod(0o755)
    return path


def read_log(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class StubMaestro:
    """Serves GET /api/orgs/<org>/storyboards/<sb>/scenes/<scene>/preview.html."""

    def __init__(self, pages: dict | None = None):
        self.pages = pages or {}
        self.requests: list = []
        self.script: list = []  # queued (status, headers, body) answers
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                stub.requests.append({"path": self.path, "key": self.headers.get("X-CardinalHQ-API-Key")})
                if stub.script:
                    status, headers, body = stub.script.pop(0)
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                scene = self.path.split("/scenes/", 1)[-1].split("/", 1)[0]
                body = stub.pages.get(scene)
                if body is None or self.headers.get("X-CardinalHQ-API-Key") != KEY:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b'{"error":"scene_not_found"}')
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def bundle_ref(scene: str, body: bytes, revision: int = 3, org: str = ORG, url: str | None = None) -> dict:
    ref = {
        "path": f"/api/orgs/{org}/storyboards/{SB_ID}/scenes/{scene}/preview.html?revision={revision}",
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "revision": revision,
    }
    if url:
        ref["url"] = url
    return ref


def preview_result(scenes: list) -> dict:
    return {
        "storyboard_id": SB_ID,
        "revision": 3,
        "ok": True,
        "errors": [],
        "warnings": [],
        "scenes": scenes,
        "materialization": [],
        "local_preview": {
            "contract": "…",
            "viewport": {"width": 1280, "height": 800, "device_scale_factor": 2},
            "ready_timeout_ms": 10000,
            "settle_timeout_ms": 5000,
            "max_bundle_bytes": 33554432,
        },
        "view_url": f"https://app.example/storyboards/{SB_ID}",
    }


def write_settings(home: Path, origin: str, org: str = ORG, key: str = KEY) -> None:
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "settings.json").write_text(json.dumps({
        "env": {"CARDINAL_MCP_URL": f"{origin}/api/orgs/{org}/mcp", "CARDINAL_MCP_API_KEY": key},
    }))


def hermetic_env(home: Path, **extra) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CARDINAL_CHROMIUM", "PUPPETEER_EXECUTABLE_PATH", "CHROME_PATH",
                        "CARDINAL_MCP_URL", "CARDINAL_MCP_API_KEY", "TZ")}
    env["HOME"] = str(home)
    env["PATH"] = str(home / "empty-bin") + os.pathsep + "/usr/bin:/bin"
    env.update(extra)
    return env


def run_renderer(args: list, env: dict, stdin: str | None = None, timeout: int = 60):
    res = subprocess.run([sys.executable, str(RENDER_PREVIEW)] + args, input=stdin, capture_output=True,
                         text=True, env=env, timeout=timeout)
    lines = [json.loads(line) for line in res.stdout.splitlines() if line.strip()]
    return res, lines


# ---------------------------------------------------------------------------
# Discovery + launch flags
# ---------------------------------------------------------------------------

class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _exe(self, rel: str) -> str:
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("#!/bin/sh\n")
        p.chmod(0o755)
        return str(p)

    def test_cardinal_chromium_is_authoritative(self):
        good = self._exe("bin/chrome")
        found, _ = rp.find_chromium({"CARDINAL_CHROMIUM": good, "CHROME_PATH": "/x"}, "Linux", self.home,
                                    which=lambda c: None, version_of=lambda p: "Chromium 130.0.1.2")
        self.assertEqual(found["path"], good)
        self.assertEqual(found["source"], "$CARDINAL_CHROMIUM")
        # A wrong explicit override is reported, never silently replaced.
        other = self._exe("bin/other")
        found, searched = rp.find_chromium({"CARDINAL_CHROMIUM": str(self.home / "nope"), "CHROME_PATH": other},
                                           "Linux", self.home, which=lambda c: other,
                                           version_of=lambda p: "Chromium 130.0.1.2")
        self.assertIsNone(found)
        self.assertEqual(len(searched), 1)
        self.assertIn("nope", searched[0])

    def test_env_fallbacks_then_linux_path_then_caches(self):
        cache = self._exe(".cache/ms-playwright/chromium-1234/chrome-linux/chrome")
        older = self._exe(".cache/ms-playwright/chromium-1100/chrome-linux/chrome")
        on_path = self._exe("bin/chromium")
        which = {"chromium": on_path}.get
        v = lambda p: "Chromium 130.0.1.2"  # noqa: E731
        found, _ = rp.find_chromium({"PUPPETEER_EXECUTABLE_PATH": cache}, "Linux", self.home, which=which, version_of=v)
        self.assertEqual(found["source"], "$PUPPETEER_EXECUTABLE_PATH")
        found, _ = rp.find_chromium({}, "Linux", self.home, which=which, version_of=v)
        self.assertEqual(found["path"], on_path)
        found, searched = rp.find_chromium({}, "Linux", self.home, which=lambda c: None, version_of=v)
        self.assertEqual(found["path"], cache, "newest cache build first")
        self.assertNotEqual(found["path"], older)
        self.assertTrue(any("google-chrome" in s for s in searched))
        self.assertTrue(any("/snap/bin/chromium" in s for s in searched))

    def test_macos_apps_and_playwright_cache(self):
        apps = self.home / "Apps"
        brave = self._exe("Apps/Brave Browser.app/Contents/MacOS/Brave Browser")
        found, searched = rp.find_chromium({}, "Darwin", self.home, version_of=lambda p: "Brave Browser 131.1.73.1",
                                           mac_roots=[apps])
        self.assertEqual(found["path"], brave)
        self.assertTrue(any("Google Chrome.app" in s for s in searched))
        os.remove(brave)
        pw = self._exe("Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/"
                       "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing")
        found, _ = rp.find_chromium({}, "Darwin", self.home, version_of=lambda p: "Google Chrome for Testing 140.0.1.1",
                                    mac_roots=[apps])
        self.assertEqual(found["path"], pw)

    def test_too_old_chrome_is_skipped(self):
        old = self._exe("bin/chromium")
        new = self._exe(".cache/puppeteer/chrome/linux-131.0.1/chrome-linux64/chrome")
        versions = {old: "Chromium 109.0.5414.74", new: "Google Chrome for Testing 131.0.6778.85"}
        found, searched = rp.find_chromium({}, "Linux", self.home, which={"chromium": old}.get,
                                           version_of=versions.get)
        self.assertEqual(found["path"], new)
        self.assertTrue(any("too old" in s for s in searched))

    def test_launch_args_lock_the_network_and_keep_the_sandbox(self):
        args = rp.launch_args("/tmp/profile-x")
        for flag in ("--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=127.0.0.1:9",
                     "--proxy-bypass-list=<-loopback>"):
            self.assertIn(flag, args)
        self.assertEqual(rp.CANVAS_CHROMIUM_NETWORK_ARGS, (
            "--host-resolver-rules=MAP * ~NOTFOUND", "--proxy-server=127.0.0.1:9", "--proxy-bypass-list=<-loopback>"))
        for flag in ("--headless=new", "--remote-debugging-pipe", "--user-data-dir=/tmp/profile-x",
                     "--disable-extensions", "--no-first-run"):
            self.assertIn(flag, args)
        joined = " ".join(args)
        for bad in ("--no-sandbox", "--disable-setuid-sandbox", "--allow-file-access-from-files",
                    "--disable-web-security", "--remote-debugging-port", "--single-process", "--no-zygote"):
            self.assertNotIn(bad, joined)


# ---------------------------------------------------------------------------
# Bundle fetch: origin guard, integrity, status mapping
# ---------------------------------------------------------------------------

class FetchTests(unittest.TestCase):
    def setUp(self):
        self.body = b"<!doctype html><html>bundle</html>"
        self.maestro = StubMaestro({"s1": self.body})
        self.foreign = StubMaestro({"s1": self.body})
        self.conn = {"origin": self.maestro.origin, "org": ORG, "key": KEY}

    def tearDown(self):
        self.maestro.close()
        self.foreign.close()

    def test_fetches_path_on_the_connected_origin_with_the_key(self):
        data = rp.fetch_bundle(self.conn, bundle_ref("s1", self.body), 1 << 20)
        self.assertEqual(data, self.body)
        self.assertEqual(self.maestro.requests[0]["key"], KEY)
        self.assertTrue(self.maestro.requests[0]["path"].endswith("/scenes/s1/preview.html?revision=3"))

    def test_a_foreign_url_is_never_contacted(self):
        ref = bundle_ref("s1", self.body, url=self.foreign.origin + "/api/orgs/org-1/storyboards/x")
        rp.fetch_bundle(self.conn, ref, 1 << 20)
        self.assertEqual(self.foreign.requests, [], "the key must never reach another host")
        self.assertEqual(len(self.maestro.requests), 1)

    def test_path_must_be_the_bundle_route(self):
        for path in ("/api/orgs/org-1/mcp", "//evil.example/api/orgs/org-1/storyboards/" + SB_ID +
                     "/scenes/s1/preview.html?revision=3", "https://evil.example/x",
                     f"/api/orgs/org-1/storyboards/{SB_ID}/scenes/s1/preview.html?revision=3&x=1"):
            ref = dict(bundle_ref("s1", self.body), path=path)
            with self.assertRaises(rp.FetchError):
                rp.fetch_bundle(self.conn, ref, 1 << 20)
        self.assertEqual(self.maestro.requests, [])

    def test_other_org_is_refused_before_any_request(self):
        with self.assertRaises(rp.FetchError) as cm:
            rp.fetch_bundle(self.conn, bundle_ref("s1", self.body, org="org-2"), 1 << 20)
        self.assertIn("reconnect", str(cm.exception))
        self.assertEqual(self.maestro.requests, [])

    def test_redirects_are_not_followed(self):
        self.maestro.script.append((302, {"Location": self.foreign.origin + "/steal"}, b""))
        with self.assertRaises(rp.FetchError) as cm:
            rp.fetch_bundle(self.conn, bundle_ref("s1", self.body), 1 << 20)
        self.assertIn("redirect", str(cm.exception))
        self.assertEqual(self.foreign.requests, [])

    def test_sha256_and_size_mismatch_are_refused(self):
        ref = bundle_ref("s1", self.body)
        with self.assertRaises(rp.FetchError) as cm:
            rp.fetch_bundle(self.conn, dict(ref, sha256="0" * 64), 1 << 20)
        self.assertIn("sha256", str(cm.exception))
        with self.assertRaises(rp.FetchError) as cm:
            rp.fetch_bundle(self.conn, dict(ref, bytes=len(self.body) - 1), 1 << 20)
        self.assertIn("size", str(cm.exception))
        with self.assertRaises(rp.FetchError) as cm:
            rp.fetch_bundle(self.conn, dict(ref, bytes=len(self.body) + 5), 1 << 20)
        self.assertIn("size", str(cm.exception))
        with self.assertRaises(rp.FetchError):
            rp.fetch_bundle(self.conn, ref, 4)  # over the cap: never fetched
        self.assertEqual(len(self.maestro.requests), 3)

    def test_429_honours_retry_after(self):
        self.maestro.script.append((429, {"Retry-After": "7"}, b'{"error":"validation_busy"}'))
        waits = []
        data = rp.fetch_bundle(self.conn, bundle_ref("s1", self.body), 1 << 20, sleep=waits.append)
        self.assertEqual(data, self.body)
        self.assertEqual(waits, [7])

    def test_status_codes_become_plain_language(self):
        cases = [
            (409, {"error": "revision_mismatch"}, "storyboard__preview again"),
            (409, {"error": "dataset_not_materialized"}, "storyboard__preview first"),
            (403, {"error": "forbidden"}, "Member"),
            (403, {"error": "org_scope_mismatch"}, "reconnect"),
            (403, {"error": "insufficient_scope"}, "upgrade Cardinal"),
            (413, {"error": "bundle_too_large", "message": "inline data 40 MiB"}, "40 MiB"),
            (422, {"error": "scene_not_renderable", "message": "the scene has errors (x)"}, "not renderable"),
            (404, {"error": "scene_not_found"}, "not found"),
            (401, {}, "--rotate"),
        ]
        for status, body, needle in cases:
            self.maestro.script.append((status, {"Content-Type": "application/json"}, json.dumps(body).encode()))
            with self.assertRaises(rp.FetchError) as cm:
                rp.fetch_bundle(self.conn, bundle_ref("s1", self.body), 1 << 20, sleep=lambda s: None)
            self.assertIn(needle, str(cm.exception), (status, body))
            self.assertEqual(cm.exception.status, status)

    def test_connect_info_prefers_settings_then_environ(self):
        with TemporaryDirectory() as t:
            home = Path(t)
            self.assertEqual(rp.connect_info(home, {}), {})
            env = {"CARDINAL_MCP_URL": "https://app.example:8443/api/orgs/o%2D9/mcp", "CARDINAL_MCP_API_KEY": "k"}
            info = rp.connect_info(home, env)
            self.assertEqual(info, {"origin": "https://app.example:8443", "org": "o-9", "key": "k"})
            write_settings(home, "https://app.cardinalhq.io", org="o-1", key="k2")
            info = rp.connect_info(home, env)
            self.assertEqual(info, {"origin": "https://app.cardinalhq.io", "org": "o-1", "key": "k2"})


# ---------------------------------------------------------------------------
# CDP pipe framing
# ---------------------------------------------------------------------------

class PipeFramingTests(unittest.TestCase):
    def _pair(self, max_bytes=1 << 20):
        to_browser_r, to_browser_w = os.pipe()
        from_browser_r, from_browser_w = os.pipe()
        cdp = rp.PipeCDP(to_browser_w, from_browser_r, max_message_bytes=max_bytes)
        self.addCleanup(lambda: [self._close(fd) for fd in (to_browser_r, to_browser_w, from_browser_w)])
        return cdp, to_browser_r, from_browser_w

    @staticmethod
    def _close(fd):
        try:
            os.close(fd)
        except OSError:
            pass

    @staticmethod
    def _read_msg(fd) -> dict:
        buf = b""
        while not buf.endswith(b"\0"):
            buf += os.read(fd, 1)
        return json.loads(buf[:-1])

    def test_round_trip_with_split_and_coalesced_frames(self):
        cdp, rd, wr = self._pair()
        events = []
        cdp.on(events.append)
        out = {}
        t = threading.Thread(target=lambda: out.update(r=cdp.send("Browser.getVersion", timeout=5)))
        t.start()
        msg = self._read_msg(rd)
        self.assertEqual(msg["method"], "Browser.getVersion")
        frame = json.dumps({"method": "Page.loadEventFired", "params": {}}).encode() + b"\0" + \
            json.dumps({"id": msg["id"], "result": {"product": "X"}}).encode() + b"\0"
        # Split mid-frame, then the rest in one write (two frames coalesced).
        os.write(wr, frame[:7])
        os.write(wr, frame[7:])
        t.join(5)
        self.assertEqual(out["r"], {"product": "X"})
        self.assertEqual(events[0]["method"], "Page.loadEventFired")

    def test_error_response_raises(self):
        cdp, rd, wr = self._pair()
        box = {}

        def call():
            try:
                cdp.send("Nope.method", timeout=5)
            except rp.CDPError as e:
                box["e"] = e
        t = threading.Thread(target=call)
        t.start()
        msg = self._read_msg(rd)
        os.write(wr, json.dumps({"id": msg["id"], "error": {"message": "not found"}}).encode() + b"\0")
        t.join(5)
        self.assertIn("not found", str(box["e"]))

    def test_oversized_message_is_dropped_and_closes_the_canvas(self):
        cdp, rd, wr = self._pair(max_bytes=1024)
        cdp.track_session("S1")
        seen = []
        cdp.on(seen.append)
        box = {}

        def call():
            try:
                cdp.send("Runtime.evaluate", {"expression": "1"}, session="S1", timeout=5)
            except rp.CDPError as e:
                box["e"] = e
        t = threading.Thread(target=call)
        t.start()
        self._read_msg(rd)
        big = json.dumps({"method": "Page.frameNavigated", "sessionId": "S1",
                          "params": {"frame": {"name": "n" * 5000}}}).encode() + b"\0"
        for i in range(0, len(big), 700):
            os.write(wr, big[i:i + 700])
        t.join(5)
        self.assertIsInstance(box.get("e"), rp.CanvasClosed)
        self.assertEqual(cdp.oversized, 1)
        self.assertEqual(seen, [], "the oversized event is never parsed or dispatched")
        abort = self._read_msg(rd)
        self.assertEqual(abort["method"], "Runtime.evaluate")
        self.assertIn("cardinalPreview.abort", abort["params"]["expression"])
        # The stream resynchronises on the next NUL.
        os.write(wr, json.dumps({"method": "Page.loadEventFired", "params": {}}).encode() + b"\0")
        for _ in range(50):
            if seen:
                break
            threading.Event().wait(0.02)
        self.assertEqual(seen[0]["method"], "Page.loadEventFired")

    def test_closed_pipe_fails_waiters(self):
        cdp, rd, wr = self._pair()
        os.close(wr)
        with self.assertRaises(rp.CDPClosed):
            cdp.send("Browser.getVersion", timeout=5)


# ---------------------------------------------------------------------------
# End to end against the fake Chromium
# ---------------------------------------------------------------------------

class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "empty-bin").mkdir()
        self.chrome = make_fake_chromium(self.home)
        self.log = self.home / "chrome.log"
        self.body = b"<!doctype html><html data-cv-state=loading>scene</html>"
        self.maestro = StubMaestro({"rhythm": self.body})
        write_settings(self.home, self.maestro.origin)

    def tearDown(self):
        self.maestro.close()
        self.tmp.cleanup()

    def _env(self, **extra):
        return hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.chrome), FAKE_CHROME_LOG=str(self.log), **extra)

    def test_renders_every_step_and_writes_private_pngs(self):
        result = preview_result([
            {"id": "rhythm", "ok": True, "preview_bundle": bundle_ref("rhythm", self.body)},
            {"id": "broken", "ok": False, "preview_bundle": {"unavailable": "the scene has errors; fix them"}},
        ])
        src = self.home / "preview.json"
        src.write_text(json.dumps(result))
        res, lines = run_renderer(["--from-json", str(src)], self._env())
        self.assertEqual(res.returncode, 0, res.stderr)
        steps = [ln for ln in lines if ln.get("scene_id") == "rhythm"]
        self.assertEqual([s["step"] for s in steps], [0, 1])
        out_dir = self.home / ".claude" / "cardinal" / "storyboards" / SB_ID / "r3"
        for s in steps:
            self.assertEqual(s["png"], str(out_dir / f"rhythm-{s['step']}.png"))
            self.assertEqual(Path(s["png"]).read_bytes(), TINY_PNG)
            self.assertEqual(stat.S_IMODE(os.stat(s["png"]).st_mode), 0o600)
            self.assertEqual(s["state"], "settled")
            self.assertIsNone(s["error"])
        for d in (out_dir, out_dir.parent, self.home / ".claude" / "cardinal"):
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700, d)
        broken = [ln for ln in lines if ln.get("scene_id") == "broken"][0]
        self.assertIsNone(broken["png"])
        self.assertIn("the scene has errors; fix them", broken["error"])
        summary = lines[-1]["summary"]
        self.assertEqual(summary["rendered"], 2)
        self.assertEqual(summary["chromium"]["path"], str(self.chrome))
        self.assertIn("150.0.7000.1", summary["chromium"]["version"])

        log = read_log(self.log)
        argv = [e for e in log if e["kind"] == "argv"][0]
        self.assertEqual(argv["tz"], "UTC")
        for flag in rp.CANVAS_CHROMIUM_NETWORK_ARGS:
            self.assertIn(flag, argv["argv"])
        self.assertNotIn("--no-sandbox", argv["argv"])
        self.assertNotIn("--allow-file-access-from-files", argv["argv"])
        cmds = [e for e in log if e["kind"] == "cmd"]
        methods = [c["method"] for c in cmds]
        for m in ("Target.createBrowserContext", "Target.attachToTarget", "Emulation.setDeviceMetricsOverride",
                  "Emulation.setTimezoneOverride", "Emulation.setEmulatedMedia", "Fetch.enable", "Page.navigate",
                  "Page.captureScreenshot", "Target.closeTarget", "Browser.close"):
            self.assertIn(m, methods)
        self.assertLess(methods.index("Fetch.enable"), methods.index("Page.navigate"),
                        "interception is on before the page loads")
        tz = [c for c in cmds if c["method"] == "Emulation.setTimezoneOverride"][0]
        self.assertEqual(tz["params"]["timezoneId"], "UTC")
        media = [c for c in cmds if c["method"] == "Emulation.setEmulatedMedia"][0]
        self.assertIn({"name": "prefers-reduced-motion", "value": "reduce"}, media["params"]["features"])
        nav = [c for c in cmds if c["method"] == "Page.navigate"][0]
        self.assertTrue(nav["params"]["url"].startswith("file://"))
        self.assertTrue(nav["params"]["url"].endswith("rhythm.html#theme=light"))
        # Request interception: own page and data: continue, the rest fail.
        cont = [c["params"]["requestId"] for c in cmds if c["method"] == "Fetch.continueRequest"]
        fail = [c["params"]["requestId"] for c in cmds if c["method"] == "Fetch.failRequest"]
        self.assertEqual(sorted(cont), ["r-data", "r-own"])
        self.assertEqual(fail, ["r-evil"])
        shot = [c for c in cmds if c["method"] == "Page.captureScreenshot"][0]
        self.assertEqual(shot["params"]["clip"]["height"], 900)
        self.assertTrue(shot["params"]["captureBeyondViewport"])
        # The bundle and the profile are gone afterwards.
        profile = [a for a in argv["argv"] if a.startswith("--user-data-dir=")][0].split("=", 1)[1]
        self.assertFalse(Path(profile).exists())
        self.assertFalse(Path(nav["params"]["url"][len("file://"):].split("#")[0]).exists())
        # The key went to the stub maestro only.
        self.assertEqual([r["key"] for r in self.maestro.requests], [KEY])

    def test_stale_pngs_of_a_scene_are_replaced(self):
        out = self.home / "out"
        out.mkdir()
        (out / "rhythm-5.png").write_bytes(b"old")
        (out / "rhythm-2-0.png").write_bytes(b"other scene")
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        res, _ = run_renderer(["--out", str(out), "--theme", "dark"], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((out / "rhythm-0-dark.png").exists())
        self.assertTrue((out / "rhythm-5.png").exists(), "light PNGs untouched by a dark render")
        res, _ = run_renderer(["--out", str(out)], self._env(), stdin=json.dumps(result))
        self.assertFalse((out / "rhythm-5.png").exists())
        self.assertTrue((out / "rhythm-2-0.png").exists(), "another scene's PNGs are kept")
        nav = [e for e in read_log(self.log) if e.get("method") == "Page.navigate"]
        self.assertTrue(nav[0]["params"]["url"].endswith("#theme=dark"))

    def test_child_frame_closes_the_canvas(self):
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        res, lines = run_renderer([], self._env(FAKE_CHROME_MODE="grandchild"), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0, res.stderr)
        rec = [ln for ln in lines if ln.get("scene_id") == "rhythm"][0]
        self.assertIsNone(rec["png"])
        self.assertIn("child frame", rec["error"])
        self.assertTrue(any("child frame" in p for p in rec["protocol_errors"]))
        aborts = [e for e in read_log(self.log)
                  if e.get("method") == "Runtime.evaluate" and "abort(" in e["params"]["expression"]]
        self.assertEqual(len(aborts), 1)
        self.assertNotIn("contextId", aborts[0]["params"], "abort runs in the host page's main world")

    def test_reveal_error_is_reported_per_scene(self):
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        res, lines = run_renderer([], self._env(FAKE_CHROME_MODE="reveal_error"), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0)
        rec = [ln for ln in lines if ln.get("scene_id") == "rhythm"][0]
        self.assertIn("ready timeout", rec["error"])

    def test_mcp_wrappers_are_accepted(self):
        inner = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        for wrapped in ({"structuredContent": inner},
                        {"content": [{"type": "text", "text": json.dumps(inner)}]},
                        [{"type": "text", "text": json.dumps(inner)}]):
            res, lines = run_renderer([], self._env(), stdin=json.dumps(wrapped))
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual(lines[-1]["summary"]["rendered"], 2)

    def test_scene_filter(self):
        result = preview_result([
            {"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)},
            {"id": "other", "preview_bundle": bundle_ref("other", self.body)},
        ])
        res, lines = run_renderer(["--scene", "rhythm", "--scene", "ghost"], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual({ln["scene_id"] for ln in lines if "scene_id" in ln}, {"rhythm", "ghost"})
        self.assertEqual(len(self.maestro.requests), 1)

    def test_no_chromium_exits_3_and_says_keep_authoring(self):
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        env = hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.home / "no-such-chrome"))
        res, lines = run_renderer([], env, stdin=json.dumps(result))
        self.assertEqual(res.returncode, 3)
        msg = lines[-1]["summary"]["message"]
        self.assertIn("not a publish requirement", msg)
        self.assertIn("no-such-chrome", msg)
        self.assertEqual(self.maestro.requests, [], "no fetch without a renderer")

    def test_sandbox_failure_exits_3_without_retrying_unsandboxed(self):
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        res, lines = run_renderer([], self._env(FAKE_CHROME_MODE="nosandbox"), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 3)
        self.assertIn("sandbox", lines[-1]["summary"]["message"])
        launches = [e for e in read_log(self.log) if e["kind"] == "argv"]
        self.assertEqual(len(launches), 1)
        self.assertNotIn("--no-sandbox", launches[0]["argv"])

    def test_windows_is_reported_as_unsupported(self):
        page = self.home / "p.html"
        page.write_text("x")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(rp.sys, "platform", "win32"), redirect_stdout(out), redirect_stderr(err):
            code = rp.main(["--html", str(page)])
        self.assertEqual(code, 3)
        self.assertIn("not supported on Windows", out.getvalue())

    def test_not_connected_exits_2(self):
        (self.home / ".claude" / "settings.json").unlink()
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        res, lines = run_renderer([], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 2)
        self.assertIn("/cardinal:connect", lines[-1]["summary"]["message"])

    def test_forbidden_stops_after_the_first_scene(self):
        self.maestro.script.append((403, {}, b'{"error":"forbidden"}'))
        result = preview_result([
            {"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)},
            {"id": "two", "preview_bundle": bundle_ref("two", self.body)},
        ])
        res, lines = run_renderer([], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 2)
        self.assertEqual(len(self.maestro.requests), 1)
        self.assertIn("Member", [ln for ln in lines if ln.get("scene_id") == "rhythm"][0]["error"])

    def test_revision_mismatch_stops_and_says_preview_again(self):
        self.maestro.script.append((409, {}, b'{"error":"revision_mismatch","revision":4}'))
        result = preview_result([
            {"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)},
            {"id": "two", "preview_bundle": bundle_ref("two", self.body)},
        ])
        res, lines = run_renderer([], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 2)
        self.assertEqual(len(self.maestro.requests), 1, "one stale revision means all are stale")
        self.assertIn("storyboard__preview again", lines[-1]["summary"]["message"])
        self.assertEqual(read_log(self.log), [], "no browser when nothing was fetched")

    def test_pre_bundle_maestro_says_upgrade(self):
        result = preview_result([{"id": "rhythm", "render": {"images": []}}])
        res, lines = run_renderer([], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 2)
        self.assertIn("Upgrade Cardinal", lines[-1]["summary"]["message"])

    def test_all_unavailable_is_exit_0_with_the_reasons(self):
        result = preview_result([{"id": "a", "preview_bundle": {"unavailable": "inline data 40 MiB exceeds the cap"}}])
        res, lines = run_renderer([], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0)
        self.assertIn("40 MiB", lines[0]["error"])
        self.assertEqual(read_log(self.log), [], "no browser for nothing to render")

    def test_help_runs(self):
        res = subprocess.run([sys.executable, str(RENDER_PREVIEW), "--help"], capture_output=True, text=True,
                             timeout=30)
        self.assertEqual(res.returncode, 0)
        self.assertIn("Exit codes", res.stdout)


# ---------------------------------------------------------------------------
# Real Chrome (opt-in)
# ---------------------------------------------------------------------------

SMOKE_HOST = """<script>
window.cardinalPreview = {version: 1, steps: 2, state: "loading", step: null, errors: [],
  reveal(n) { return new Promise((r) => setTimeout(() => { this.state = "settled"; this.step = n; r(); }, 300)); },
  abort(reason) { this.aborted = reason; },
  report() { return {state: this.state, step: this.step, steps: 2, height: 800, error: null, frameErrors: [],
                     protocolErrors: this.aborted ? [this.aborted] : []}; }};
</script>"""


@unittest.skipUnless(os.environ.get("CARDINAL_CHROMIUM_SMOKE") == "1",
                     "real-Chrome smoke test: set CARDINAL_CHROMIUM_SMOKE=1 (and optionally "
                     "CARDINAL_PREVIEW_SMOKE_BUNDLES=<dir of real bundle pages>)")
class RealChromeSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, pages: list) -> tuple:
        args = []
        for p in pages:
            args += ["--html", str(p)]
        env = {k: v for k, v in os.environ.items() if k != "CARDINAL_CHROMIUM"}
        if os.environ.get("CARDINAL_CHROMIUM"):
            env["CARDINAL_CHROMIUM"] = os.environ["CARDINAL_CHROMIUM"]
        return run_renderer(args + ["--out", str(self.dir / "out")], env, timeout=600)

    def test_synthetic_page_and_locks(self):
        ok = self.dir / "ok.html"
        ok.write_text(f'<!doctype html><body style="margin:0">{SMOKE_HOST}<iframe id="cv" '
                      f'style="border:0;width:1280px;height:800px;background:#c00" srcdoc="hi"></iframe>'
                      f'<img src="https://example.com/x.png"></body>')
        nested = self.dir / "nested.html"
        nested.write_text(f'<!doctype html><body>{SMOKE_HOST}<iframe id="cv" srcdoc="<script>setTimeout(()=>'
                          f'document.body.appendChild(document.createElement(\'iframe\')),50)</script>"></iframe></body>')
        res, lines = self._run([ok, nested])
        self.assertEqual(res.returncode, 0, res.stderr)
        by = {}
        for ln in lines:
            if "scene_id" in ln:
                by.setdefault(ln["scene_id"], []).append(ln)
        self.assertEqual([r["step"] for r in by["ok"]], [0, 1])
        self.assertTrue(Path(by["ok"][0]["png"]).read_bytes().startswith(b"\x89PNG"))
        self.assertIn("child frame", by["nested"][-1]["error"])
        self.assertIn("blocked 1 network request", res.stderr)

    def test_real_bundles(self):
        root = os.environ.get("CARDINAL_PREVIEW_SMOKE_BUNDLES")
        if not root:
            self.skipTest("CARDINAL_PREVIEW_SMOKE_BUNDLES not set")
        pages = sorted(Path(root).glob("*.html"))
        self.assertTrue(pages)
        res, lines = self._run(pages)
        self.assertEqual(res.returncode, 0, res.stderr)
        problems = [ln for ln in lines if "scene_id" in ln and ln["error"]]
        self.assertEqual(problems, [])
        self.assertTrue(all(ln["state"] == "settled" for ln in lines if "scene_id" in ln))


# ---------------------------------------------------------------------------
# SessionStart: storyboard-session.py
# ---------------------------------------------------------------------------

def _run_storyboard_session(cwd: Path, payload: dict | None = None, env_extra: dict | None = None):
    body = payload if payload is not None else {
        "session_id": "0f6e2a9c-1b2d-4e5f-8a7b-9c0d1e2f3a4b",
        "cwd": str(cwd),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")}
    env["HOME"] = str(cwd)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SESSION_HOOK)], input=json.dumps(body), capture_output=True,
                          text=True, timeout=10, env=env, cwd=str(cwd))


class StoryboardSessionHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _context(self, res) -> str:
        self.assertEqual(res.returncode, 0, res.stderr)
        body = json.loads(res.stdout)
        self.assertEqual(body["hookSpecificOutput"]["hookEventName"], "SessionStart")
        return body["hookSpecificOutput"]["additionalContext"]

    def test_emits_session_id_outside_a_git_repo(self):
        ctx = self._context(_run_storyboard_session(self.root))
        self.assertEqual(ctx, "Cardinal session id for this session: 0f6e2a9c-1b2d-4e5f-8a7b-9c0d1e2f3a4b. "
                              "Pass it as session_id to storyboard__create.")

    def test_emits_session_id_inside_a_git_repo(self):
        subprocess.run(["git", "init", "-q"], cwd=str(self.root), check=True)
        ctx = self._context(_run_storyboard_session(self.root))
        self.assertIn("storyboard__create", ctx)

    def test_every_source_emits(self):
        for source in ("startup", "resume", "clear", "compact"):
            res = _run_storyboard_session(self.root, {"session_id": "abc_123", "source": source})
            self.assertIn("abc_123", self._context(res))

    def test_env_fallback(self):
        res = _run_storyboard_session(self.root, {}, {"CLAUDE_CODE_SESSION_ID": "from-env-1"})
        self.assertIn("from-env-1", self._context(res))
        res = _run_storyboard_session(self.root, {}, {"CLAUDE_SESSION_ID": "from-env-2"})
        self.assertIn("from-env-2", self._context(res))

    def test_silent_without_a_valid_id(self):
        for payload in ({}, {"session_id": ""}, {"session_id": "has space"}, {"session_id": "x" * 129},
                        {"session_id": 42}):
            res = _run_storyboard_session(self.root, payload)
            self.assertEqual(res.returncode, 0)
            self.assertEqual(res.stdout, "", payload)

    def test_garbage_stdin_fails_open(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")}
        res = subprocess.run([sys.executable, str(SESSION_HOOK)], input="{not json", capture_output=True, text=True,
                             timeout=10, env=env)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout, "")
        self.assertEqual(res.stderr, "")

    def test_registered_synchronously_on_session_start(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]["SessionStart"]
        entries = [h for group in hooks for h in group["hooks"] if "storyboard-session.py" in h["command"]]
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].get("async"), "additionalContext needs a synchronous hook")
        self.assertTrue(os.access(SESSION_HOOK, os.X_OK))


if __name__ == "__main__":
    unittest.main()
