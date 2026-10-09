"""Storyboard skills: the local preview renderer (skills/canvas/scripts/
render_preview.py), the SessionStart session-id hook
(hooks/storyboard-session.py) and the PostToolUse auto-preview hook
(hooks/storyboard-preview.py).

The renderer is driven end to end against a fake "chromium" that speaks the
--remote-debugging-pipe protocol (fd 3 in, fd 4 out, NUL-framed JSON) and a
stub maestro serving the preview-bundle route. A real-Chrome smoke test runs
only with CARDINAL_CHROMIUM_SMOKE=1 (see RealChromeSmokeTests).
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
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
PREVIEW_HOOK = PLUGIN_ROOT / "hooks" / "storyboard-preview.py"
TESTDATA = Path(__file__).resolve().parent / "testdata"

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
            err = None
            events = []
            ev = lambda method, params, s=sid: events.append({{"method": method, "params": params, "sessionId": s}})
            if sid == "S2":
                # The Canvas frame: an out-of-process iframe with its own
                # session, as in real Chrome (IsolateSandboxedIframes).
                if m == "Fetch.enable" and MODE == "child_lock_error":
                    err = {{"code": -32000, "message": "Fetch.enable failed"}}
                elif m == "Page.getFrameTree":
                    frame = {{"id": "CV", "parentId": "MAIN", "url": "about:srcdoc"}}
                    tree = {{"frame": frame}}
                    if MODE == "early_grandchild":
                        tree["childFrames"] = [{{"frame": {{"id": "G", "parentId": "CV", "url": "about:blank"}}}}]
                    if MODE == "early_hash":
                        frame["urlFragment"] = "#x"
                    res = {{"frameTree": tree}}
                elif m == "Runtime.runIfWaitingForDebugger":
                    ev("Fetch.requestPaused", {{"requestId": "r-child-evil", "request": {{"url": "https://evil-child.example/leak"}}}})
                    ev("Fetch.requestPaused", {{"requestId": "r-child-data", "request": {{"url": "data:image/png;base64,AA"}}}})
            elif m == "Browser.getVersion":
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
                ev("Fetch.requestPaused", {{"requestId": "r-own", "request": {{"url": page_url}}}})
                ev("Fetch.requestPaused", {{"requestId": "r-evil", "request": {{"url": "https://evil.example/leak"}}}})
                ev("Fetch.requestPaused", {{"requestId": "r-data", "request": {{"url": "data:image/png;base64,AA"}}}})
                ev("Page.frameAttached", {{"frameId": "CV", "parentFrameId": "MAIN"}})
                ev("Page.frameNavigated", {{"frame": {{"id": "CV", "parentId": "MAIN", "url": "about:srcdoc"}}}})
                ev("Target.attachedToTarget", {{"sessionId": "S2", "waitingForDebugger": True, "targetInfo": {{
                    "targetId": "CV", "type": "iframe", "url": "about:srcdoc", "parentFrameId": "MAIN"}}}})
                ev("Page.frameDetached", {{"frameId": "CV", "reason": "swap"}})
                ev("Page.domContentEventFired", {{}})
                ev("Page.loadEventFired", {{}})
            elif m == "Runtime.evaluate":
                expr = p.get("expression", "")
                if "abort(" in expr:
                    res = {{"result": {{"type": "undefined"}}}}
                elif "reveal(" in expr:
                    # Hostile Canvas moves: each arrives the way real Chrome
                    # sends it (on the Canvas frame's own session, S2, except
                    # the in-process variant), and reveal never answers, so
                    # only the lock can stop the wait.
                    hostile = {{
                        "grandchild": ("S2", "Page.frameAttached", {{"frameId": "G", "parentFrameId": "CV"}}),
                        "grandchild_inproc": ("S1", "Page.frameAttached", {{"frameId": "G", "parentFrameId": "CV"}}),
                        "hashnav": ("S2", "Page.navigatedWithinDocument", {{"frameId": "CV", "url": "about:srcdoc#x"}}),
                        "nested_target": ("S2", "Target.attachedToTarget", {{"sessionId": "S3", "waitingForDebugger": True,
                                          "targetInfo": {{"targetId": "G", "type": "iframe", "url": "about:blank"}}}}),
                    }}.get(MODE)
                    if hostile or MODE in ("early_grandchild", "early_hash", "child_lock_error", "hang"):
                        if hostile:
                            send({{"method": hostile[1], "params": hostile[2], "sessionId": hostile[0]}})
                        continue
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
            reply = {{"id": msg["id"], **({{"error": err}} if err else {{"result": res}})}}
            send({{**reply, **({{"sessionId": sid}} if sid else {{}})}})
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

    def test_429_waits_never_pass_the_run_deadline(self):
        self.maestro.script.append((429, {"Retry-After": "30"}, b'{"error":"validation_busy"}'))
        waits = []
        now = [100.0]
        with self.assertRaises(rp.FetchError) as cm:
            rp.fetch_bundle(self.conn, bundle_ref("s1", self.body), 1 << 20, sleep=waits.append,
                            deadline=120.0, clock=lambda: now[0])
        self.assertIn("busy", str(cm.exception))
        self.assertEqual(waits, [], "a wait past the deadline is not taken")
        with self.assertRaises(rp.FetchError) as cm:
            rp.fetch_bundle(self.conn, bundle_ref("s1", self.body), 1 << 20, deadline=100.5,
                            clock=lambda: now[0])
        self.assertIn("time budget", str(cm.exception))
        self.assertEqual(len(self.maestro.requests), 1, "no request once the budget is spent")

    def test_revision_must_be_an_int_matching_the_path(self):
        ref = bundle_ref("s1", self.body)
        for bad in (4, "3", "/../../x", None, True):
            with self.assertRaises(rp.FetchError, msg=repr(bad)) as cm:
                rp.fetch_bundle(self.conn, dict(ref, revision=bad), 1 << 20)
            self.assertIn("revision", str(cm.exception))
        self.assertEqual(self.maestro.requests, [])

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

    def test_a_truncated_or_garbled_response_is_a_fetch_error_not_a_crash(self):
        # http.client.HTTPException is not an OSError: it must still become
        # FetchError (exit 2), not a traceback (exit 1).
        import http.client

        class Opener:
            def __init__(self, exc):
                self.exc = exc

            def open(self, req, timeout=None):
                raise self.exc

        for exc in (http.client.IncompleteRead(b"<!doc", 30), http.client.BadStatusLine("garbage"),
                    http.client.RemoteDisconnected("closed")):
            with self.assertRaises(rp.FetchError, msg=repr(exc)) as cm:
                rp.fetch_bundle(self.conn, bundle_ref("s1", self.body), 1 << 20, opener=Opener(exc))
            self.assertIn(self.maestro.origin, str(cm.exception))

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

    def test_connection_env_mode_never_reads_settings(self):
        # Dogfood: a session pointed at a local stack (CARDINAL_CONNECTION=env)
        # on a machine whose settings.json holds the production key fetched
        # the bundles with the production key. Env mode is the environment
        # only, as in hooks/_storyboard_discovery.connection.
        with TemporaryDirectory() as t:
            home = Path(t)
            write_settings(home, "https://app.cardinalhq.io", org="o-prod", key="prod-key")
            local = {"CARDINAL_CONNECTION": "env", "CARDINAL_MCP_URL": "http://localhost:4210/api/orgs/o-dev/mcp",
                     "CARDINAL_MCP_API_KEY": "dev-key"}
            self.assertEqual(rp.connect_info(home, local),
                             {"origin": "http://localhost:4210", "org": "o-dev", "key": "dev-key"})
            # Env mode with nothing in the environment: not connected, never the settings key.
            self.assertEqual(rp.connect_info(home, {"CARDINAL_CONNECTION": "env"}), {})
            # /cardinal:disconnect wins over env mode too.
            (home / ".claude" / "cardinal-disconnected").write_text("{}\n")
            self.assertEqual(rp.connect_info(home, local), {})
            # Without env mode: settings first (unchanged); with no settings
            # connection, the disconnect marker beats a stale environment key.
            self.assertEqual(rp.connect_info(home, {k: v for k, v in local.items() if k != "CARDINAL_CONNECTION"})
                             ["key"], "prod-key")
            (home / ".claude" / "settings.json").write_text("{}")
            self.assertEqual(rp.connect_info(home, {k: v for k, v in local.items() if k != "CARDINAL_CONNECTION"}),
                             {})

    def test_connect_info_matches_the_hooks_resolver(self):
        if str(PLUGIN_ROOT / "hooks") not in sys.path:
            sys.path.insert(0, str(PLUGIN_ROOT / "hooks"))
        import _storyboard_discovery as disc
        with TemporaryDirectory() as t:
            home = Path(t)
            local = {"CARDINAL_MCP_URL": "http://localhost:4210/api/orgs/o-dev/mcp", "CARDINAL_MCP_API_KEY": "dev"}
            cases = [({}, False, False), (local, False, False), (local, True, False),
                     (dict(local, CARDINAL_CONNECTION="env"), True, False),
                     (dict(local, CARDINAL_CONNECTION="env"), True, True),
                     ({"CARDINAL_CONNECTION": "env"}, True, False), (local, False, True)]
            for environ, settings, disconnected in cases:
                s = home / ".claude" / "settings.json"
                m = home / ".claude" / "cardinal-disconnected"
                for p in (s, m):
                    if p.exists():
                        p.unlink()
                if settings:
                    write_settings(home, "https://app.cardinalhq.io", org="o-prod", key="prod-key")
                if disconnected:
                    m.parent.mkdir(parents=True, exist_ok=True)
                    m.write_text("{}\n")
                want = disc.connection(home, environ)
                self.assertEqual(rp.connect_info(home, environ), want if want.get("key") else {},
                                 (environ, settings, disconnected))


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
        # Request interception: own page and data: continue, the rest fail,
        # on the page AND on the Canvas frame's own (out-of-process) session.
        cont = sorted((c["session"], c["params"]["requestId"]) for c in cmds if c["method"] == "Fetch.continueRequest")
        fail = sorted((c["session"], c["params"]["requestId"]) for c in cmds if c["method"] == "Fetch.failRequest")
        self.assertEqual(cont, [("S1", "r-data"), ("S1", "r-own"), ("S2", "r-child-data")])
        self.assertEqual(fail, [("S1", "r-evil"), ("S2", "r-child-evil")])
        # The page auto-attaches frame targets before it loads; the Canvas
        # frame gets the filter and the locks before it is resumed.
        auto = [i for i, c in enumerate(cmds) if c["method"] == "Target.setAutoAttach" and c["session"] == "S1"]
        self.assertTrue(auto and auto[0] < methods.index("Page.navigate"))
        self.assertEqual(cmds[auto[0]]["params"], {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        child = [c["method"] for c in cmds if c["session"] == "S2"]
        for m in ("Fetch.enable", "Page.enable", "Target.setAutoAttach", "Emulation.setEmulatedMedia",
                  "Page.getFrameTree"):
            self.assertLess(child.index(m), child.index("Runtime.runIfWaitingForDebugger"), m)
        # abort() only ever runs in the host page, never in the Canvas frame.
        self.assertFalse([c for c in cmds if c["session"] == "S2" and c["method"] == "Runtime.evaluate"])
        shot = [c for c in cmds if c["method"] == "Page.captureScreenshot"][0]
        self.assertEqual(shot["params"]["clip"]["height"], 900)
        self.assertTrue(shot["params"]["captureBeyondViewport"])
        # The bundle and the profile are gone afterwards.
        profile = [a for a in argv["argv"] if a.startswith("--user-data-dir=")][0].split("=", 1)[1]
        self.assertFalse(Path(profile).exists())
        self.assertFalse(Path(nav["params"]["url"][len("file://"):].split("#")[0]).exists())
        # The key went to the stub maestro only.
        self.assertEqual([r["key"] for r in self.maestro.requests], [KEY])

    def test_a_non_int_revision_never_names_the_output_directory(self):
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        result["revision"] = "/../../../../Desktop"
        del result["scenes"][0]["preview_bundle"]["revision"]
        res, lines = run_renderer([], self._env(), stdin=json.dumps(result))
        self.assertIn("revision", [ln for ln in lines if ln.get("scene_id") == "rhythm"][0]["error"])
        self.assertEqual(self.maestro.requests, [])
        self.assertFalse((self.home / "Desktop").exists())
        # A good ref and a bad top-level revision: the ref's revision wins.
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        result["revision"] = "/../../../../Desktop"
        res, lines = run_renderer([], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(lines[-1]["summary"]["out_dir"],
                         str(self.home / ".claude" / "cardinal" / "storyboards" / SB_ID / "r3"))

    def test_explicit_out_keeps_its_mode_and_dpr_is_clamped(self):
        out = self.home / "shared-out"
        out.mkdir(mode=0o755)
        os.chmod(str(out), 0o755)
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        res, _ = run_renderer(["--out", str(out), "--dpr", "3"], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o755, "an existing --out is not chmod'ed")
        metrics = [e for e in read_log(self.log) if e.get("method") == "Emulation.setDeviceMetricsOverride"][0]
        self.assertEqual(metrics["params"]["deviceScaleFactor"], rp.MAX_DPR)

    def test_sigterm_removes_the_temp_dir(self):
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        env = dict(self._env(FAKE_CHROME_MODE="hang"), TMPDIR=str(self.home / "tmp"))
        (self.home / "tmp").mkdir()
        proc = subprocess.Popen([sys.executable, str(RENDER_PREVIEW)], stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        proc.stdin.write(json.dumps(result))
        proc.stdin.close()
        for _ in range(200):  # until the page is fetched and the reveal hangs
            if any(e.get("method") == "Runtime.evaluate" for e in read_log(self.log)):
                break
            threading.Event().wait(0.05)
        self.assertTrue(list((self.home / "tmp").glob("cardinal-preview-*")))
        proc.terminate()
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()
        self.assertEqual(list((self.home / "tmp").glob("cardinal-preview-*")), [], "temp dir removed on SIGTERM")

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

    def _closed(self, mode: str, needle: str) -> list:
        """Render one scene in `mode`; assert the Canvas was closed for `needle`."""
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        res, lines = run_renderer([], self._env(FAKE_CHROME_MODE=mode), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0, res.stderr)
        rec = [ln for ln in lines if ln.get("scene_id") == "rhythm"][0]
        self.assertIsNone(rec["png"], mode)
        self.assertIn(needle, rec["error"], mode)
        self.assertTrue(any(needle in p for p in rec["protocol_errors"]), mode)
        cmds = [e for e in read_log(self.log) if e["kind"] == "cmd"]
        aborts = [c for c in cmds if c["method"] == "Runtime.evaluate" and "abort(" in c["params"]["expression"]]
        self.assertEqual(len(aborts), 1, mode)
        self.assertEqual(aborts[0]["session"], "S1", "abort runs in the host page, not the Canvas frame")
        self.assertNotIn("contextId", aborts[0]["params"], "abort runs in the host page's main world")
        return cmds

    def test_child_frame_in_the_canvas_process_closes_the_canvas(self):
        # Real Chrome reports the Canvas's grandchild on the Canvas frame's
        # own session (the frame is out of process), not the page's.
        self._closed("grandchild", "child frame")

    def test_child_frame_of_an_in_process_canvas_closes_the_canvas(self):
        self._closed("grandchild_inproc", "child frame")

    def test_hash_navigation_in_the_canvas_process_closes_the_canvas(self):
        self._closed("hashnav", "navigated within its document")

    def test_frames_made_before_the_canvas_session_attached_close_the_canvas(self):
        # Chrome runs a srcdoc OOPIF's first parse before its session exists
        # (waitingForDebugger is false): the frame tree catches it.
        self._closed("early_grandchild", "child frame")
        self.log.unlink()
        self._closed("early_hash", "navigated within its document")

    def test_a_target_spawned_by_the_canvas_closes_it_and_stays_paused(self):
        cmds = self._closed("nested_target", "child frame")
        self.assertIn("Fetch.enable", [c["method"] for c in cmds if c["session"] == "S3"])
        self.assertNotIn("Runtime.runIfWaitingForDebugger", [c["method"] for c in cmds if c["session"] == "S3"])

    def test_a_canvas_frame_the_locks_cannot_reach_is_closed(self):
        self._closed("child_lock_error", "could not put its request filter on the Canvas frame")

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
                        [{"type": "text", "text": json.dumps(inner)}],
                        {"content": json.dumps(inner)}):
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

    def test_no_chromium_exits_3_and_keeps_storyboard_unpublished(self):
        result = preview_result([{"id": "rhythm", "preview_bundle": bundle_ref("rhythm", self.body)}])
        env = hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.home / "no-such-chrome"))
        res, lines = run_renderer([], env, stdin=json.dumps(result))
        self.assertEqual(res.returncode, 3)
        msg = lines[-1]["summary"]["message"]
        self.assertIn("Leave the storyboard unpublished", msg)
        self.assertNotIn("not a publish requirement", msg)
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

    def _canvas_page(self, name: str, srcdoc: str, extra: str = "") -> Path:
        """A host page whose Canvas frame is sandboxed like the real bundle's
        (sandbox="allow-scripts" makes it an out-of-process iframe in current
        Chrome, so these exercise the auto-attached Canvas session)."""
        page = self.dir / f"{name}.html"
        page.write_text(f'<!doctype html><body style="margin:0">{SMOKE_HOST}<iframe id="cv" sandbox="allow-scripts" '
                        f'style="border:0;width:1280px;height:800px;background:#c00" srcdoc="{srcdoc}"></iframe>'
                        f'{extra}</body>')
        return page

    def test_synthetic_page_and_locks(self):
        later = "<script>setTimeout(()=>{%s},50)</script>x"
        pages = [
            self._canvas_page("ok", "hi", '<img src="https://example.com/x.png">'),
            self._canvas_page("net", later % "new Image().src='https://host-b.example/child.png'"),
            self._canvas_page("nested", later % "document.body.appendChild(document.createElement('iframe'))"),
            self._canvas_page("nested-early",
                              "<script>document.documentElement.appendChild(document.createElement('iframe'))</script>"),
            self._canvas_page("hash", later % "location.hash='abc'"),
            self._canvas_page("hash-early", "<script>location.hash='abc'</script>"),
            self._canvas_page("blanknav", later % "location='about:blank'"),
            self._canvas_page("worker", later % "new Worker(URL.createObjectURL(new Blob(['1'])))"),
        ]
        res, lines = self._run(pages)
        self.assertEqual(res.returncode, 0, res.stderr)
        by = {}
        for ln in lines:
            if "scene_id" in ln:
                by.setdefault(ln["scene_id"], []).append(ln)
        self.assertEqual([r["step"] for r in by["ok"]], [0, 1])
        self.assertTrue(Path(by["ok"][0]["png"]).read_bytes().startswith(b"\x89PNG"))
        self.assertIn("ok: blocked 1 network request", res.stderr)
        # The Canvas frame's own request is paused and failed on its session.
        self.assertIsNone(by["net"][-1]["error"])
        self.assertIn("net: blocked 1 network request", res.stderr)
        for name in ("nested", "nested-early"):
            self.assertIn("child frame", by[name][-1]["error"] or "", name)
        for name in ("hash", "hash-early"):
            self.assertIn("navigated within its document", by[name][-1]["error"] or "", name)
        self.assertIn("a frame navigated", by["blanknav"][-1]["error"] or "")
        self.assertIn("started a worker", by["worker"][-1]["error"] or "")

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
# canvas SKILL.md: how Claude finds the renderer, and the shipped exemplars
# ---------------------------------------------------------------------------

CANVAS_SKILL = PLUGIN_ROOT / "skills" / "canvas" / "SKILL.md"


class CanvasSkillTests(unittest.TestCase):
    def test_renderer_locator_never_searches_the_working_directory(self):
        text = CANVAS_SKILL.read_text()
        self.assertNotRegex(text, r"find [^\n]*\s\.\s", "the renderer is never looked up under the cwd")
        self.assertNotIn("ls -t", text, "never pick the renderer by mtime")
        self.assertIn("<this skill's base directory>/scripts/render_preview.py", text)

    def test_locator_fallback_picks_the_newest_plugin_version_not_a_planted_copy(self):
        m = re.search(r"^(RENDER=\$\(python3 -I -c .*?'\))$", CANVAS_SKILL.read_text(), re.S | re.M)
        self.assertIsNotNone(m, "fallback locator snippet not found in SKILL.md")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            for v in ("0.9.0", "0.32.0", "0.10.1"):
                d = home / ".claude/plugins/cache/mkt place/cardinal" / v / "skills/canvas/scripts"
                d.mkdir(parents=True)
                (d / "render_preview.py").write_text("")
            # The oldest version is the most recently touched: mtime must not win.
            old = home / ".claude/plugins/cache/mkt place/cardinal/0.9.0/skills/canvas/scripts/render_preview.py"
            os.utime(old, (2_000_000_000, 2_000_000_000))
            planted = root / "repo/tools/canvas/scripts"
            planted.mkdir(parents=True)
            (planted / "render_preview.py").write_text("")
            # The snippet runs in the user's repo: stdlib names planted there
            # must not be imported (python3 -c puts the cwd on sys.path).
            for mod in ("glob", "os", "re"):
                (root / "repo" / f"{mod}.py").write_text("raise SystemExit('PLANTED ' + __name__)\n")
            res = subprocess.run(["bash", "-c", m.group(1) + '\nprintf %s "$RENDER"'], cwd=str(root / "repo"),
                                 env={"HOME": str(home), "PATH": os.environ.get("PATH", "")},
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertNotIn("PLANTED", res.stdout + res.stderr)
            self.assertEqual(res.stdout, str(home / ".claude/plugins/cache/mkt place/cardinal/0.32.0/skills/canvas/"
                                                    "scripts/render_preview.py"))

    def test_every_python_invocation_in_the_skill_is_isolated(self):
        # A bare `python3 -c` / `python3 <script>` imports from the cwd or
        # honours PYTHONPATH; the skill runs inside the user's repo.
        text = CANVAS_SKILL.read_text()
        calls = re.findall(r"python3\b[^\n]*", text)
        self.assertTrue(calls)
        for call in calls:
            self.assertRegex(call, r"^python3 -I\b", call)

    def test_skill_steers_large_populations_to_canvas(self):
        # The full rule ("Past a few thousand marks draw on a <canvas>") is
        # served by describe_grammar canvas.design.rules_that_bite (conductor
        # #1975); the skill keeps the pointer where it names the row exemplar.
        text = " ".join(CANVAS_SKILL.read_text().split())
        self.assertIn("For thousands of rows, draw on a `<canvas>` instead.", text)

    def test_exemplars_ship_with_the_skill_and_follow_the_frame_rules(self):
        text = CANVAS_SKILL.read_text()
        names = re.findall(r"`exemplars/([a-z-]+\.js)`", text)
        self.assertGreaterEqual(len(set(names)), 2)
        for name in set(names):
            src = (CANVAS_SKILL.parent / "exemplars" / name).read_text()
            self.assertLessEqual(len(src.encode()), 64 * 1024, name)
            for banned in (r"\bimport\b", r"\beval\b", r"\bfetch\b", r"\bparent\b", r"postMessage",
                           r"\bFunction\(", r"\bXMLHttpRequest\b", r"\bWebSocket\b", r"localStorage"):
                self.assertNotRegex(src, banned, f"{name}: {banned}")
            # The batch cv.data resolves to an object keyed by binding key.
            self.assertNotRegex(src, r"const\s*\[[^\]]*\]\s*=\s*await\s+cv\.data", name)
            self.assertIn("cv.mark(", src, name)


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
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")
           and not k.startswith(("CARDINAL_MCP_", "OTEL_"))}
    env["HOME"] = str(cwd)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(SESSION_HOOK)], input=json.dumps(body), capture_output=True,
                          text=True, timeout=10, env=env, cwd=str(cwd))


class StoryboardSessionHookTests(unittest.TestCase):
    """The session-id sentence, on a connected machine (connect state file).
    The unconnected connect hint: test_unconnected_mode."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / ".claude").mkdir()
        (self.root / ".claude" / "cardinal.json").write_text("{}")

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
                              "Pass it as session_id to storyboard__create, storyboard__find and "
                              "storyboard__add_act.")

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
        env["HOME"] = str(self.root)  # connected (setUp): no connect hint
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


# ---------------------------------------------------------------------------
# PostToolUse: storyboard-preview.py (auto-preview)
# ---------------------------------------------------------------------------

def _load_preview_hook():
    spec = importlib.util.spec_from_file_location("storyboard_preview_hook_under_test", PREVIEW_HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The PostToolUse payload of a real storyboard__preview call (first real e2e
# run, 2026-09-27). tool_response has the shape Claude Code 2.1.283 sends (the
# object the transcript stores as tool_use_result, the same in all 7 previews
# of run.jsonl): {content: "<result text>", structuredContent: {...}}, both
# verbatim with the org id replaced. The envelope keys and shapes (effort, mcp_server,
# prompt_id, no scratchpad_dir) follow a payload captured from Claude Code
# 2.1.283. The preview was scoped to three scenes: two unavailable (derive
# over a string) and `shipping` with a bundle.
CAPTURED = json.loads((TESTDATA / "storyboard_preview_post_tool_use.json").read_text())
CAPTURED_ORG = "00000000-0000-4000-8000-00000000c0de"
CAPTURED_SB = "sb_29ec61d324cb7c3bad43c6dc"


class StoryboardPreviewHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "empty-bin").mkdir()
        self.chrome = make_fake_chromium(self.home)
        self.log = self.home / "chrome.log"
        self.body = b"<!doctype html><html data-cv-state=loading>shipping</html>"
        self.maestro = StubMaestro({"shipping": self.body})
        write_settings(self.home, self.maestro.origin, org=CAPTURED_ORG)
        self.out_dir = self.home / ".claude" / "cardinal" / "storyboards" / CAPTURED_SB / "r17"

    def tearDown(self):
        self.maestro.close()
        self.tmp.cleanup()

    def _result(self) -> dict:
        """The captured preview result, with the shipping bundle's size and
        digest pointed at the page the stub maestro serves."""
        result = copy.deepcopy(CAPTURED["tool_response"]["structuredContent"])
        for scene in result["scenes"]:
            if scene["id"] == "shipping":
                scene["preview_bundle"].update(bytes=len(self.body), sha256=hashlib.sha256(self.body).hexdigest())
        return result

    def _real_shape(self) -> dict:
        """tool_response as Claude Code sends it: {content: text, structuredContent}."""
        result = self._result()
        return {"content": json.dumps(result), "structuredContent": result}

    def _payload(self, tool_response=None, **over) -> dict:
        body = dict(CAPTURED)
        body["tool_response"] = self._real_shape() if tool_response is None else tool_response
        body["cwd"] = str(self.home)
        body.update(over)
        return body

    def _env(self, **extra):
        return hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.chrome), FAKE_CHROME_LOG=str(self.log), **extra)

    def _run(self, payload=None, env=None, raw: str | None = None, timeout: int = 60):
        stdin = raw if raw is not None else json.dumps(payload if payload is not None else self._payload())
        res = subprocess.run([sys.executable, str(PREVIEW_HOOK)], input=stdin, capture_output=True, text=True,
                             timeout=timeout, env=env or self._env(), cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        return res

    def _context(self, res) -> str:
        body = json.loads(res.stdout)
        self.assertEqual(body["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        ctx = body["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(ctx), 6000)
        return ctx

    def _assert_rendered(self, ctx: str):
        self.assertIn(f"storyboard {CAPTURED_SB}, revision 17", ctx)
        self.assertIn(f"PNGs are in {self.out_dir}/", ctx)
        self.assertIn("- shipping: shipping-0.png, shipping-1.png", ctx)
        for step in (0, 1):
            self.assertEqual((self.out_dir / f"shipping-{step}.png").read_bytes(), TINY_PNG)
        self.assertIn("- sawtooth: not rendered (unavailable: the scene has errors; fix them and preview again)", ctx)
        self.assertIn("- cause: not rendered (unavailable:", ctx)
        self.assertIn("Read every PNG, first and last step included", ctx)
        self.assertIn("needs a fix in its source or spec", ctx)
        # scene_ids scoped the preview, so earlier scenes' PNGs live elsewhere.
        self.assertIn("Only the scenes in this preview were rendered", ctx)
        self.assertEqual([r["key"] for r in self.maestro.requests], [KEY])
        argv = [e for e in read_log(self.log) if e["kind"] == "argv"][0]["argv"]
        self.assertNotIn("--no-sandbox", argv)

    def test_fixture_is_the_real_claude_code_shape(self):
        resp = CAPTURED["tool_response"]
        self.assertEqual(set(resp), {"content", "structuredContent"})
        self.assertIsInstance(resp["content"], str)
        self.assertEqual(json.loads(resp["content"]), resp["structuredContent"])
        self.assertIsInstance(resp["structuredContent"]["scenes"], list)

    def test_captured_real_shape_renders_and_reports_png_paths(self):
        ctx = self._context(self._run())
        self._assert_rendered(ctx)
        # Scenes in the preview's order; nothing raw from the renderer.
        self.assertLess(ctx.index("- sawtooth:"), ctx.index("- cause:"))
        self.assertLess(ctx.index("- cause:"), ctx.index("- shipping:"))
        self.assertNotIn('"summary"', ctx)
        self.assertNotIn(KEY, ctx)

    def test_structured_content_alone_is_enough(self):
        # structuredContent is read first: garbage content does not matter.
        payload = self._payload(tool_response={"content": "not json", "structuredContent": self._result()})
        self._assert_rendered(self._context(self._run(payload)))

    def test_string_content_without_structured_content(self):
        payload = self._payload(tool_response={"content": json.dumps(self._result())})
        self._assert_rendered(self._context(self._run(payload)))

    def test_bare_result_string(self):
        self._assert_rendered(self._context(self._run(self._payload(tool_response=json.dumps(self._result())))))

    def test_list_of_content_blocks(self):
        blocks = [{"type": "text", "text": json.dumps(self._result())}]
        self._assert_rendered(self._context(self._run(self._payload(tool_response=blocks))))

    def test_unscoped_preview_has_no_scope_note(self):
        payload = self._payload(tool_input={"storyboard_id": CAPTURED_SB})
        self.assertNotIn("Only the scenes in this preview", self._context(self._run(payload)))

    def _spill(self, where: Path) -> str:
        # The spill follower is cardinal_core.evidence (vendored next to the hook).
        if not (PLUGIN_ROOT / "hooks" / "cardinal_core" / "evidence.py").exists():
            self.skipTest("cardinal_core not vendored — run: python3 build/vendor.py claude")
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(json.dumps(self._result()))
        return (f"Error: result (71,204 characters) exceeds maximum allowed tokens. Output has been saved to "
                f"{where}.\nFormat: JSON with schema: {{error_count: number, scenes: [...]}}\nUse offset and "
                f"limit parameters to read specific portions of the file.")

    def test_spilled_result_under_claude_projects_is_read(self):
        spill = (self.home / ".claude" / "projects" / "-work-demo" / "sess" / "tool-results" /
                 "mcp-plugin_cardinal_cardinal-storyboard__preview-1790494777541.txt")
        text = self._spill(spill)
        self._assert_rendered(self._context(self._run(self._payload(tool_response=text))))
        # The same notice inside a content block.
        self.maestro.requests.clear()
        blocks = [{"type": "text", "text": text}]
        self._assert_rendered(self._context(self._run(self._payload(tool_response=blocks))))
        # And as the string `content` of Claude Code's tool_response object.
        self.maestro.requests.clear()
        self._assert_rendered(self._context(self._run(self._payload(tool_response={"content": text}))))

    def test_spill_path_with_a_space_in_home(self):
        home = self.home / "John Doe"
        (home / ".claude").mkdir(parents=True)
        shutil.copy(self.home / ".claude" / "settings.json", home / ".claude" / "settings.json")
        spill = home / ".claude" / "projects" / "-work-demo" / "sess" / "tool-results" / "preview 1.txt"
        text = self._spill(spill)
        env = hermetic_env(home, CARDINAL_CHROMIUM=str(self.chrome), FAKE_CHROME_LOG=str(self.log))
        ctx = self._context(self._run(self._payload(tool_response=text), env=env))
        self.assertIn("- shipping: shipping-0.png, shipping-1.png", ctx)
        # The older one-line notice, text after the path on the same line.
        hook = _load_preview_hook()
        one_line = f"Output has been saved to {spill}. Use offset and limit parameters to read it."
        self.assertIn(str(spill), hook.evidence.spill_candidates(one_line))

    def test_spilled_path_outside_claude_projects_is_refused(self):
        outside = self.home / "elsewhere" / "tool-results" / "x.txt"
        res = self._run(self._payload(tool_response=self._spill(outside)))
        self.assertEqual(res.stdout, "")
        # A link planted under projects/ that resolves outside it, too.
        link = self.home / ".claude" / "projects" / "p" / "link.txt"
        link.parent.mkdir(parents=True)
        link.symlink_to(outside)
        res = self._run(self._payload(tool_response=f"Output has been saved to {link}."))
        self.assertEqual(res.stdout, "")
        res = self._run(self._payload(tool_response="Output has been saved to ../../etc/passwd."))
        self.assertEqual(res.stdout, "")
        self.assertEqual(read_log(self.log), [], "no browser without a preview result")
        self.assertEqual(self.maestro.requests, [])

    def test_error_and_non_preview_results_are_silent(self):
        for text in ('Error: MCP error 429: {"error":"validation_busy","retry_after_s":3}',
                     '{"error":"invalid_request","issues":[{"path":"scene_ids","message":"unknown scene"}]}',
                     '{"error":"published","message":"storyboard is published"}',
                     "", [], {"content": []}, 42, None):
            res = self._run(self._payload(tool_response=text) if text is not None else
                            {k: v for k, v in self._payload().items() if k != "tool_response"})
            self.assertEqual(res.stdout, "", text)
            self.assertEqual(res.stderr, "", text)
        self.assertEqual(read_log(self.log), [])

    def test_other_tools_are_ignored(self):
        for name in ("mcp__plugin_cardinal_cardinal__storyboard__publish", "Bash", "", 7):
            res = self._run(self._payload(tool_name=name))
            self.assertEqual(res.stdout, "", name)
        self.assertEqual(read_log(self.log), [])

    def test_hand_registered_cardinal_server_name_works(self):
        payload = self._payload(tool_name="mcp__cardinal__storyboard__preview")
        self._assert_rendered(self._context(self._run(payload)))

    def test_garbage_stdin_fails_open(self):
        for raw in ("{not json", "[1, 2]", "null", ""):
            res = self._run(raw=raw)
            self.assertEqual(res.stdout, "", raw)
            self.assertEqual(res.stderr, "", raw)

    def test_no_chromium_is_said_once_per_session(self):
        env = hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.home / "no-such-chrome"))
        ctx = self._context(self._run(env=env))
        self.assertIn("not rendered locally", ctx)
        self.assertIn("Leave the storyboard unpublished", ctx)
        self.assertNotIn("not a publish requirement", ctx)
        self.assertEqual(self._run(env=env).stdout, "", "second preview in the session stays quiet")
        # Chromium present but its sandbox cannot start: the same notice, once,
        # for a new session; the sandbox is never turned off.
        env = self._env(FAKE_CHROME_MODE="nosandbox")
        ctx = self._context(self._run(self._payload(session_id="other-session-2"), env=env))
        self.assertIn("sandbox", ctx)
        self.assertEqual(self._run(self._payload(session_id="other-session-2"), env=env).stdout, "")
        for launch in [e for e in read_log(self.log) if e["kind"] == "argv"]:
            self.assertNotIn("--no-sandbox", launch["argv"])

    def test_not_connected_relays_the_renderer_message(self):
        (self.home / ".claude" / "settings.json").unlink()
        ctx = self._context(self._run())
        self.assertIn("could not be rendered locally", ctx)
        self.assertIn("/cardinal:connect", ctx)
        self.assertEqual(self.maestro.requests, [])

    def test_another_servers_storyboard_preview_never_reaches_the_renderer(self):
        # The result names the pages to fetch with the Cardinal key: only
        # Cardinal's own server (`cardinal` / `plugin_cardinal_cardinal`) counts.
        for name in ("mcp__evil__storyboard__preview", "mcp__plugin_other_cardinal__storyboard__preview",
                     "mcp__plugin_cardinal_cardinal__x__storyboard__preview", "storyboard__preview"):
            res = self._run(self._payload(tool_name=name))
            self.assertEqual(res.stdout, "", name)
        self.assertEqual(read_log(self.log), [])
        self.assertEqual(self.maestro.requests, [])

    def test_every_scene_unavailable_relays_the_message_once(self):
        result = self._result()
        result["scenes"] = [s for s in result["scenes"] if s["id"] != "shipping"]
        ctx = self._context(self._run(self._payload(tool_response=json.dumps(result))))
        self.assertIn("nothing was rendered locally", ctx)
        self.assertEqual(ctx.count("every selected scene is unavailable"), 1)
        self.assertNotIn("Read every PNG", ctx)
        self.assertEqual(read_log(self.log), [])

    def test_render_errors_are_reported_per_scene(self):
        ctx = self._context(self._run(env=self._env(FAKE_CHROME_MODE="reveal_error")))
        self.assertIn("- shipping: ERROR", ctx)
        self.assertIn("ready timeout", ctx)
        self.assertIn("needs a fix in its source or spec", ctx)

    def test_a_crashed_renderer_is_reported_without_its_message(self):
        hook = _load_preview_hook()
        stderr = ("Traceback (most recent call last):\n  File \"render_preview.py\", line 9\n"
                  "KeyError: 'Authorization: Bearer ck_live_secret'\n")
        ctx = hook.crash_context(1, stderr)
        self.assertIn("local renderer failed (exit 1, KeyError)", ctx)
        self.assertIn("Leave the storyboard unpublished", ctx)
        self.assertNotIn("not a publish requirement", ctx)
        self.assertIn("render_preview.py", ctx)
        self.assertNotIn("ck_live_secret", ctx)
        self.assertIsNone(hook.crash_context(0, ""))
        self.assertIsNone(hook.crash_context(None, ""))
        self.assertIn("(exit 1)", hook.crash_context(1, ""))
        # End to end: a renderer that dies before printing anything.
        fake = self.home / "fake-hook"
        (fake / "skills" / "canvas" / "scripts").mkdir(parents=True)
        (fake / "hooks").mkdir()
        shutil.copy(PREVIEW_HOOK, fake / "hooks" / PREVIEW_HOOK.name)
        (fake / "skills" / "canvas" / "scripts" / "render_preview.py").write_text(
            "import sys\nsys.stdin.read()\nraise RuntimeError('boom ' + 'ck_live_secret')\n")
        res = subprocess.run([sys.executable, str(fake / "hooks" / PREVIEW_HOOK.name)],
                             input=json.dumps(self._payload()), capture_output=True, text=True, timeout=60,
                             env=self._env(), cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        ctx = self._context(res)
        self.assertIn("local renderer failed (exit 1, RuntimeError)", ctx)
        self.assertNotIn("ck_live_secret", ctx)

    def test_render_timeout_is_reported_and_the_hook_still_answers(self):
        tmpdir = self.home / "tmp"
        tmpdir.mkdir()
        env = self._env(FAKE_CHROME_MODE="hang", CARDINAL_STORYBOARD_PREVIEW_BUDGET_S="3", TMPDIR=str(tmpdir))
        ctx = self._context(self._run(env=env, timeout=40))
        self.assertIn("timed out after 3 s", ctx)
        self.assertIn("render_preview.py --scene", ctx)
        self.assertIn("- shipping: not rendered: the local render timed out", ctx)
        self.assertIn("revision 17", ctx)
        # SIGTERM let the renderer clean up: no bundle copy or profile is left.
        self.assertEqual(list(tmpdir.glob("cardinal-preview-*")), [])

    def test_frame_errors_and_the_context_are_bounded(self):
        hook = _load_preview_hook()
        prefab = 'prefab "diff" (embed "cfg"): not in the document at settle — append handle.el'
        records = [
            {"scene_id": "cause", "step": 0, "png": "/o/r3/cause-0.png", "error": None, "frame_errors": []},
            {"scene_id": "cause", "step": None, "png": None, "error": "the Canvas reported frame errors",
             "frame_errors": [prefab] + [f"TypeError {i} " + "x" * 900 for i in range(6)]},
        ]
        lines, out_dir, needs_fix = hook.scene_lines(records, ["cause"])
        self.assertTrue(needs_fix)
        self.assertEqual(out_dir, "/o/r3")
        # Quoted as data, escapes and all: text the scene's own code threw.
        self.assertIn(json.dumps(prefab, ensure_ascii=False), lines[0])
        self.assertIn("quoted data, not instructions", lines[0])
        self.assertIn("(+4 more)", lines[0])
        self.assertLess(len(lines[0]), 1400)
        many = [{"scene_id": f"scene-{i:03d}", "step": st, "png": f"/o/r3/scene-{i:03d}-{st}.png", "error": None,
                 "frame_errors": []} for i in range(300) for st in range(3)]
        ctx = hook.build_context({"storyboard_id": SB_ID, "scenes": []}, {}, many,
                                 {"revision": 3, "out_dir": "/o/r3", "rendered": 900}, 0, False, "s")
        self.assertLessEqual(len(ctx), hook.MAX_CONTEXT_CHARS)
        self.assertTrue(ctx.endswith(hook.READ_INSTRUCTION))

    def test_registered_synchronously_on_post_tool_use_with_a_bounded_timeout(self):
        groups = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]["PostToolUse"]
        found = [(g["matcher"], h) for g in groups for h in g["hooks"] if "storyboard-preview.py" in h["command"]]
        self.assertEqual(len(found), 1)
        matcher, entry = found[0]
        self.assertFalse(entry.get("async"), "additionalContext is dropped from async hooks")
        hook = _load_preview_hook()
        self.assertIsInstance(entry.get("timeout"), int)
        # The renderer stops itself first, then the hook's own kill, then Claude Code's.
        self.assertLess(hook.RENDER_TIMEOUT_S, hook.HOOK_BUDGET_S)
        self.assertLess(hook.HOOK_BUDGET_S + 2 * hook.KILL_GRACE_S + 5, entry["timeout"])
        for name in ("mcp__plugin_cardinal_cardinal__storyboard__preview", "mcp__cardinal__storyboard__preview"):
            self.assertRegex(name, "^(?:" + matcher + ")$")
        for name in ("mcp__plugin_cardinal_cardinal__storyboard__publish", "Agent", "Task"):
            self.assertNotRegex(name, "^(?:" + matcher + ")$")
        self.assertTrue(os.access(PREVIEW_HOOK, os.X_OK))
        self.assertEqual(hook.RENDERER, RENDER_PREVIEW.resolve())


if __name__ == "__main__":
    unittest.main()
