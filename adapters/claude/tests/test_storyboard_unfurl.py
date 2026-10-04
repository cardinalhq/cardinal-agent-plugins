"""The link-preview half of the storyboard preview loop (rich unfurls design
§5.2): render_preview.py --cover / --static-html, hooks/_storyboard_unfurl.py
(the Slack-like mock) and storyboard-preview.py's card steps after the scene
render.

Driven against the same fake Chromium and stub maestro as
test_storyboard_preview.py.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_storyboard_preview as tsp  # noqa: E402

PLUGIN_ROOT = tsp.PLUGIN_ROOT
HOOKS = PLUGIN_ROOT / "hooks"
PREVIEW_HOOK = tsp.PREVIEW_HOOK
TINY_PNG = tsp.TINY_PNG
SB_ID = tsp.SB_ID
CAPTURED_SB = tsp.CAPTURED_SB


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


unfurl = _load("storyboard_unfurl_under_test", HOOKS / "_storyboard_unfurl.py")

SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630" viewBox="0 0 1200 630">'
       '<rect width="1200" height="630" fill="#fff"/><text x="40" y="80">Why did checkout slow down?</text></svg>')
HOSTILE_TITLE = '<script>alert(1)</script> Why did "checkout" slow down?'


def card_block(**over) -> dict:
    card = {
        "act": 1,
        "finding_source": "authored",
        "og": {
            "site_name": "Cardinal Storyboards",
            "title": HOSTILE_TITLE,
            "description": "Shipping retries tripled p99 · 2 established · 1 ruled out · 0 open",
            "image_alt": "Summary of: Why did checkout slow down?",
        },
        "member_preview": {"image": "summary", "on_by_default": True, "enabled": True},
        "public_link": {"image": "hero"},
        "cover_render": None,
        "errors": [],
        "warnings": [],
        "summary_svg": SVG,
    }
    card.update(over)
    return card


def svg_uri() -> str:
    return "data:image/svg+xml;base64," + base64.b64encode(SVG.encode()).decode()


def png_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(TINY_PNG).decode()


# ---------------------------------------------------------------------------
# The mock page
# ---------------------------------------------------------------------------

class MockHtmlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.cover = Path(self.tmp.name) / "shipping-cover.png"
        self.cover.write_bytes(TINY_PNG)

    def tearDown(self):
        self.tmp.cleanup()

    def test_every_string_is_escaped_and_the_page_runs_nothing(self):
        page, label = unfurl.mock_html(card_block(), None)
        self.assertEqual(label, "summary card")
        self.assertNotIn("<script", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt; Why did &quot;checkout&quot; slow down?", page)
        self.assertIn("Shipping retries tripled p99 · 2 established · 1 ruled out · 0 open", page)
        self.assertIn("Cardinal Storyboards", page)
        self.assertIn(unfurl.MOCK_FOOTER, page)
        self.assertIn("240 px thumbnail", page)
        self.assertIn("-webkit-line-clamp:3", page)
        self.assertIn("width:520px", page)
        self.assertIn('width="360" height="189"', page)
        self.assertIn("default-src 'none'; img-src data:", page)
        # No external reference: every src is a data: URI.
        for chunk in page.split('src="')[1:]:
            self.assertTrue(chunk.startswith("data:"), chunk[:40])
        self.assertIn(svg_uri(), page)
        # Escaped attributes too.
        evil = card_block(og={"title": "t", "description": "d", "image_alt": '"><img src=x onerror=1>'})
        page, _ = unfurl.mock_html(evil, None)
        self.assertNotIn("<img src=x", page)
        self.assertIn("&quot;&gt;&lt;img src=x onerror=1&gt;", page)

    def test_the_cover_render_is_the_image_when_member_previews_show_the_cover(self):
        card = card_block(member_preview={"image": "cover", "on_by_default": True, "enabled": True})
        page, label = unfurl.mock_html(card, self.cover)
        self.assertEqual(label, "cover")
        self.assertIn(png_uri(), page)
        self.assertNotIn(svg_uri(), page)

    def test_a_summary_member_preview_ignores_the_cover_file(self):
        page, label = unfurl.mock_html(card_block(), self.cover)
        self.assertEqual(label, "summary card")
        self.assertIn(svg_uri(), page)
        self.assertNotIn(png_uri(), page)

    def test_a_missing_cover_falls_back_to_the_summary_card(self):
        card = card_block(member_preview={"image": "cover"})
        page, label = unfurl.mock_html(card, Path(self.tmp.name) / "nope.png")
        self.assertEqual(label, "summary card")
        # Not a PNG: never inlined as one.
        self.cover.write_bytes(b"<html>")
        self.assertEqual(unfurl.mock_html(card, self.cover)[1], "summary card")

    def test_no_image_means_no_mock(self):
        self.assertIsNone(unfurl.mock_html(card_block(summary_svg=None), None))
        self.assertIsNone(unfurl.mock_html(card_block(summary_svg=None), self.cover))  # summary member image
        self.assertIsNone(unfurl.mock_html("nope", None))


# ---------------------------------------------------------------------------
# render_preview.py --cover / --static-html
# ---------------------------------------------------------------------------

class RendererCardModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "empty-bin").mkdir()
        self.chrome = tsp.make_fake_chromium(self.home)
        self.log = self.home / "chrome.log"
        self.body = b"<!doctype html><html data-cv-state=loading>scene</html>"
        self.maestro = tsp.StubMaestro({"rhythm": self.body})
        tsp.write_settings(self.home, self.maestro.origin)
        self.out_dir = self.home / ".claude" / "cardinal" / "storyboards" / SB_ID / "r3"

    def tearDown(self):
        self.maestro.close()
        self.tmp.cleanup()

    def _env(self, **extra):
        return tsp.hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.chrome), FAKE_CHROME_LOG=str(self.log), **extra)

    def _cmds(self) -> list:
        return [e for e in tsp.read_log(self.log) if e["kind"] == "cmd"]

    def test_cover_renders_the_last_step_at_the_card_size(self):
        result = tsp.preview_result([{"id": "rhythm", "ok": True, "preview_bundle": tsp.bundle_ref("rhythm", self.body)}])
        result["card"] = card_block(cover_render={"scene_id": "rhythm", "width": 1200, "height": 630})
        self.out_dir.mkdir(parents=True)
        (self.out_dir / "rhythm-0.png").write_bytes(b"step0")  # an earlier step render stays
        src = self.home / "preview.json"
        src.write_text(json.dumps(result))
        res, lines = tsp.run_renderer(["--from-json", str(src), "--cover", "rhythm", "--dpr", "2"], self._env())
        self.assertEqual(res.returncode, 0, res.stderr)
        shots = [ln for ln in lines if ln.get("scene_id") == "rhythm"]
        self.assertEqual(len(shots), 1)
        self.assertEqual(shots[0]["step"], "cover")
        cover = self.out_dir / "rhythm-cover.png"
        self.assertEqual(shots[0]["png"], str(cover))
        self.assertEqual(cover.read_bytes(), TINY_PNG)
        self.assertEqual(stat.S_IMODE(os.stat(cover).st_mode), 0o600)
        self.assertEqual((self.out_dir / "rhythm-0.png").read_bytes(), b"step0")
        self.assertFalse((self.out_dir / "rhythm-1.png").exists())
        cmds = self._cmds()
        metrics = [c["params"] for c in cmds if c["method"] == "Emulation.setDeviceMetricsOverride"]
        self.assertEqual(metrics, [{"width": 1200, "height": 630, "deviceScaleFactor": 1.0, "mobile": False}])
        reveals = [c["params"]["expression"] for c in cmds
                   if c["method"] == "Runtime.evaluate" and "reveal(" in c["params"]["expression"]]
        self.assertEqual(len(reveals), 2, "steps through every reveal")
        self.assertIn("reveal(1)", reveals[-1])
        shots = [c["params"] for c in cmds if c["method"] == "Page.captureScreenshot"]
        self.assertEqual(len(shots), 1, "only the last step is shot")
        self.assertEqual(shots[0]["clip"], {"x": 0, "y": 0, "width": 1200, "height": 630, "scale": 1})

    def test_cover_viewport_is_clamped_and_defaults_to_the_card(self):
        result = tsp.preview_result([{"id": "rhythm", "ok": True, "preview_bundle": tsp.bundle_ref("rhythm", self.body)}])
        result["card"] = {"cover_render": {"scene_id": "rhythm", "width": 99999, "height": 1}}
        res, _ = tsp.run_renderer(["--cover", "rhythm"], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 0, res.stderr)
        metrics = [c["params"] for c in self._cmds() if c["method"] == "Emulation.setDeviceMetricsOverride"]
        self.assertEqual((metrics[0]["width"], metrics[0]["height"]), (4096, 200))
        self.log.unlink()
        del result["card"]
        res, _ = tsp.run_renderer(["--cover", "rhythm"], self._env(), stdin=json.dumps(result))
        metrics = [c["params"] for c in self._cmds() if c["method"] == "Emulation.setDeviceMetricsOverride"]
        self.assertEqual((metrics[0]["width"], metrics[0]["height"]), (1200, 630))
        res, lines = tsp.run_renderer(["--cover", "../x"], self._env(), stdin=json.dumps(result))
        self.assertEqual(res.returncode, 2)

    def test_static_html_runs_no_script_and_no_network(self):
        page = self.home / "mock.html"
        page.write_text(unfurl.mock_html(card_block(), None)[0])
        out = self.home / "out" / "mock.png"
        res, lines = tsp.run_renderer(["--static-html", str(page), "--png", str(out), "--viewport", "560x640",
                                       "--dpr", "2"], self._env())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(lines[0], {"static_html": str(page), "png": str(out), "error": None})
        self.assertEqual(lines[-1]["summary"]["rendered"], 1)
        self.assertEqual(out.read_bytes(), TINY_PNG)
        self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
        self.assertEqual(self.maestro.requests, [], "nothing is fetched from Cardinal")
        argv = [e for e in tsp.read_log(self.log) if e["kind"] == "argv"][0]["argv"]
        for flag in tsp.rp.CANVAS_CHROMIUM_NETWORK_ARGS:
            self.assertIn(flag, argv)
        for flag in tsp.rp.FORBIDDEN_CHROMIUM_ARGS:
            self.assertFalse(any(a.split("=", 1)[0] == flag for a in argv), flag)
        cmds = self._cmds()
        methods = [c["method"] for c in cmds]
        noscript = [c for c in cmds if c["method"] == "Emulation.setScriptExecutionDisabled"]
        self.assertEqual([c["params"] for c in noscript], [{"value": True}])
        self.assertLess(methods.index("Emulation.setScriptExecutionDisabled"), methods.index("Page.navigate"))
        self.assertLess(methods.index("Fetch.enable"), methods.index("Page.navigate"))
        nav = [c for c in cmds if c["method"] == "Page.navigate"][0]
        self.assertEqual(nav["params"]["url"], page.resolve().as_uri())
        metrics = [c["params"] for c in cmds if c["method"] == "Emulation.setDeviceMetricsOverride"][0]
        self.assertEqual(metrics, {"width": 560, "height": 640, "deviceScaleFactor": 2.0, "mobile": False})
        cont = sorted(c["params"]["requestId"] for c in cmds if c["method"] == "Fetch.continueRequest")
        fail = sorted(c["params"]["requestId"] for c in cmds if c["method"] == "Fetch.failRequest")
        self.assertEqual(cont, ["r-data", "r-own"])
        self.assertEqual(fail, ["r-evil"])
        self.assertNotIn("Runtime.evaluate", methods)
        shot = [c["params"] for c in cmds if c["method"] == "Page.captureScreenshot"][0]
        self.assertEqual(shot["clip"], {"x": 0, "y": 0, "width": 560, "height": 640, "scale": 1})

    def test_static_html_input_errors(self):
        res, lines = tsp.run_renderer(["--static-html", str(self.home / "nope.html"), "--png", "x.png"], self._env())
        self.assertEqual(res.returncode, 2)
        page = self.home / "p.html"
        page.write_text("<p>x</p>")
        res, lines = tsp.run_renderer(["--static-html", str(page)], self._env())
        self.assertEqual(res.returncode, 2)
        res, lines = tsp.run_renderer(["--static-html", str(page), "--png", str(self.home / "o.png"),
                                       "--viewport", "big"], self._env())
        self.assertEqual(res.returncode, 2)
        env = tsp.hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.home / "no-chrome"))
        res, lines = tsp.run_renderer(["--static-html", str(page), "--png", str(self.home / "o.png")], env)
        self.assertEqual(res.returncode, 3)
        self.assertEqual(tsp.read_log(self.log), [])


# ---------------------------------------------------------------------------
# storyboard-preview.py card steps
# ---------------------------------------------------------------------------

class PreviewHookCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "empty-bin").mkdir()
        self.chrome = tsp.make_fake_chromium(self.home)
        self.log = self.home / "chrome.log"
        self.body = b"<!doctype html><html data-cv-state=loading>shipping</html>"
        self.maestro = tsp.StubMaestro({"shipping": self.body})
        tsp.write_settings(self.home, self.maestro.origin, org=tsp.CAPTURED_ORG)
        self.out_dir = self.home / ".claude" / "cardinal" / "storyboards" / CAPTURED_SB / "r17"

    def tearDown(self):
        self.maestro.close()
        self.tmp.cleanup()

    def _result(self, card=None) -> dict:
        result = copy.deepcopy(tsp.CAPTURED["tool_response"]["structuredContent"])
        for scene in result["scenes"]:
            if scene["id"] == "shipping":
                scene["preview_bundle"].update(bytes=len(self.body), sha256=hashlib.sha256(self.body).hexdigest())
        if card is not None:
            result["card"] = card
        return result

    def _payload(self, result: dict, **over) -> dict:
        body = dict(tsp.CAPTURED)
        body["tool_response"] = {"content": json.dumps(result), "structuredContent": result}
        body["cwd"] = str(self.home)
        body.update(over)
        return body

    def _env(self, **extra):
        return tsp.hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.chrome), FAKE_CHROME_LOG=str(self.log), **extra)

    def _run(self, payload: dict, env=None, hook: Path = PREVIEW_HOOK) -> str:
        res = subprocess.run([sys.executable, str(hook)], input=json.dumps(payload), capture_output=True, text=True,
                             timeout=120, env=env or self._env(), cwd=str(self.home))
        self.assertEqual(res.returncode, 0, res.stderr)
        if not res.stdout:
            return ""
        ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(ctx), 6000)
        return ctx

    def _launches(self) -> list:
        return [e["argv"] for e in tsp.read_log(self.log) if e["kind"] == "argv"]

    def test_no_card_means_no_mock(self):
        for card in (None, card_block(summary_svg=None)):
            ctx = self._run(self._payload(self._result(card)))
            self.assertIn("- shipping: shipping-0.png, shipping-1.png", ctx)
            self.assertNotIn("Link preview mock", ctx)
            self.assertNotIn("Cover", ctx)
            self.assertFalse((self.out_dir / "unfurl-mock.html").exists())
            self.assertFalse((self.out_dir / "unfurl-mock.png").exists())
        self.assertEqual(len(self._launches()), 2, "one Chromium per preview: the scene render only")

    def test_summary_svg_renders_an_escaped_mock(self):
        ctx = self._run(self._payload(self._result(card_block())))
        mock_png = self.out_dir / "unfurl-mock.png"
        mock_html = self.out_dir / "unfurl-mock.html"
        self.assertIn(f"Link preview mock: {mock_png} (member link; image: summary card).", ctx)
        self.assertIn(self._critique(), ctx)
        self.assertLess(ctx.index("Read every PNG"), ctx.index("Link preview mock"))
        page = mock_html.read_text()
        self.assertNotIn("<script", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn(svg_uri(), page)
        self.assertEqual(mock_png.read_bytes(), TINY_PNG)
        for p in (mock_html, mock_png):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600, p)
        launches = self._launches()
        self.assertEqual(len(launches), 2, "scene render, then the mock")
        for argv in launches:
            for flag in tsp.rp.CANVAS_CHROMIUM_NETWORK_ARGS:
                self.assertIn(flag, argv)
            self.assertNotIn("--no-sandbox", argv)
        cmds = [e for e in tsp.read_log(self.log) if e["kind"] == "cmd"]
        navs = [c["params"]["url"] for c in cmds if c["method"] == "Page.navigate"]
        self.assertEqual(navs[-1], mock_html.resolve().as_uri())
        self.assertIn({"value": True}, [c["params"] for c in cmds if c["method"] == "Emulation.setScriptExecutionDisabled"])
        metrics = [c["params"] for c in cmds if c["method"] == "Emulation.setDeviceMetricsOverride"][-1]
        self.assertEqual((metrics["width"], metrics["height"], metrics["deviceScaleFactor"]), (560, 640, 2.0))
        # Members-off is said, not hidden.
        card = card_block(member_preview={"image": "summary", "on_by_default": True, "enabled": False})
        ctx = self._run(self._payload(self._result(card)))
        self.assertIn("Member link previews are currently off", ctx)

    @staticmethod
    def _critique() -> str:
        return ("Read it: is the title or description cut off? does the first line of the description state the "
                "conclusion the last scene reaches? is the cover legible at the 240 px thumbnail?")

    def test_cover_render_feeds_the_mock(self):
        card = card_block(member_preview={"image": "cover", "on_by_default": True, "enabled": True},
                          cover_render={"scene_id": "shipping", "width": 1200, "height": 630})
        ctx = self._run(self._payload(self._result(card)))
        cover = self.out_dir / "shipping-cover.png"
        self.assertIn(f"Cover render: {cover} (1200x630, the last reveal step of shipping).", ctx)
        self.assertIn(f"Link preview mock: {self.out_dir / 'unfurl-mock.png'} (member link; image: cover).", ctx)
        self.assertEqual(cover.read_bytes(), TINY_PNG)
        page = (self.out_dir / "unfurl-mock.html").read_text()
        self.assertIn(png_uri(), page)
        self.assertNotIn(svg_uri(), page)
        self.assertEqual(len(self._launches()), 3, "scene render, cover, mock")
        # The step PNGs stay next to the cover.
        self.assertTrue((self.out_dir / "shipping-1.png").is_file())
        cmds = [e for e in tsp.read_log(self.log) if e["kind"] == "cmd"]
        metrics = [c["params"] for c in cmds if c["method"] == "Emulation.setDeviceMetricsOverride"]
        self.assertIn({"width": 1200, "height": 630, "deviceScaleFactor": 1.0, "mobile": False}, metrics)

    def test_a_summary_member_preview_ignores_the_cover_render(self):
        # cover_scene binds raw evidence: member previews show the summary card.
        card = card_block(cover_render={"scene_id": "shipping", "width": 1200, "height": 630})
        ctx = self._run(self._payload(self._result(card)))
        self.assertIn("Cover render:", ctx)
        self.assertIn("(member link; image: summary card)", ctx)
        page = (self.out_dir / "unfurl-mock.html").read_text()
        self.assertIn(svg_uri(), page)
        self.assertNotIn(png_uri(), page)

    def test_no_chromium_means_no_mock_and_one_notice(self):
        # Every scene unavailable: the scene render exits before looking for
        # Chromium, so the mock's render is the one that finds none.
        result = self._result(card_block())
        result["scenes"] = [s for s in result["scenes"] if s["id"] != "shipping"]
        env = tsp.hermetic_env(self.home, CARDINAL_CHROMIUM=str(self.home / "no-such-chrome"))
        ctx = self._run(self._payload(result), env=env)
        self.assertNotIn("Link preview mock:", ctx)
        self.assertIn("link preview was not rendered locally", ctx)
        self.assertFalse((self.out_dir / "unfurl-mock.png").exists())
        ctx = self._run(self._payload(result), env=env)
        self.assertNotIn("not rendered locally", ctx, "said once per session")

    def test_cover_exit_3_stops_the_card_steps(self):
        fake = self.home / "fake-plugin"
        (fake / "skills" / "canvas" / "scripts").mkdir(parents=True)
        (fake / "hooks").mkdir()
        for name in (PREVIEW_HOOK.name, "_storyboard_unfurl.py"):
            shutil.copy(HOOKS / name, fake / "hooks" / name)
        calls = self.home / "calls.log"
        (fake / "skills" / "canvas" / "scripts" / "render_preview.py").write_text(
            "import json, sys\n"
            f"open({str(calls)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "sys.stdin.read()\n"
            "if '--cover' in sys.argv or '--static-html' in sys.argv:\n"
            "    print(json.dumps({'summary': {'message': 'No usable local Chromium.', 'exit': 3}}))\n"
            "    sys.exit(3)\n"
            f"print(json.dumps({{'scene_id': 'shipping', 'step': 0, 'png': {str(self.out_dir / 'shipping-0.png')!r}}}))\n"
            f"print(json.dumps({{'summary': {{'revision': 17, 'out_dir': {str(self.out_dir)!r}}}}}))\n")
        card = card_block(cover_render={"scene_id": "shipping", "width": 1200, "height": 630})
        ctx = self._run(self._payload(self._result(card)), hook=fake / "hooks" / PREVIEW_HOOK.name)
        self.assertIn("- shipping: shipping-0.png", ctx)
        self.assertNotIn("Link preview mock:", ctx)
        self.assertIn("link preview was not rendered locally. No usable local Chromium.", ctx)
        argvs = calls.read_text().splitlines()
        self.assertEqual(len(argvs), 2, argvs)
        self.assertIn("--cover shipping", argvs[1])
        self.assertFalse((self.out_dir / "unfurl-mock.html").exists())

    def test_the_hook_budget_is_respected(self):
        card = card_block(member_preview={"image": "cover", "on_by_default": True, "enabled": True},
                          cover_render={"scene_id": "shipping", "width": 1200, "height": 630})
        hook = _load("storyboard_preview_hook_budget", PREVIEW_HOOK)
        self.assertLess(hook.MIN_COVER_S + hook.MIN_MOCK_S, hook.HOOK_BUDGET_S)
        started = time.monotonic()
        ctx = self._run(self._payload(self._result(card)), env=self._env(CARDINAL_STORYBOARD_PREVIEW_BUDGET_S="8"))
        self.assertLess(time.monotonic() - started, 8 + 2 * hook.KILL_GRACE_S + 5)
        self.assertIn("- shipping: shipping-0.png, shipping-1.png", ctx)
        self.assertIn("Cover of shipping not rendered: the preview's time budget is spent.", ctx)
        self.assertIn("render_preview.py --cover shipping", ctx)
        self.assertIn("Link preview mock not rendered: the preview's time budget is spent.", ctx)
        self.assertEqual(len(self._launches()), 1, "no Chromium past the budget")

    def test_a_long_scene_list_keeps_the_card_lines(self):
        hook = _load("storyboard_preview_hook_reserve", PREVIEW_HOOK)
        many = [{"scene_id": f"scene-{i:03d}", "step": st, "png": f"/o/r3/scene-{i:03d}-{st}.png", "error": None,
                 "frame_errors": []} for i in range(300) for st in range(3)]
        ctx = hook.build_context({"storyboard_id": SB_ID, "scenes": []}, {}, many,
                                 {"revision": 3, "out_dir": "/o/r3"}, 0, False, "s", reserve=600)
        self.assertLessEqual(len(ctx), hook.MAX_CONTEXT_CHARS - 600)


def _png_size(path: Path) -> tuple:
    data = path.read_bytes()[:24]
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


@unittest.skipUnless(os.environ.get("CARDINAL_CHROMIUM_SMOKE") == "1",
                     "real-Chrome smoke test: set CARDINAL_CHROMIUM_SMOKE=1")
class RealChromeCardSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cover_is_1200x630_and_the_mock_renders(self):
        page = self.dir / "cover.html"
        page.write_text(f'<!doctype html><body style="margin:0">{tsp.SMOKE_HOST}<iframe id="cv" '
                        'sandbox="allow-scripts" style="border:0;width:1280px;height:800px" srcdoc="hi"></iframe></body>')
        env = dict(os.environ)
        res, lines = tsp.run_renderer(["--html", str(page), "--cover", "cover", "--out", str(self.dir / "out")], env,
                                      timeout=300)
        self.assertEqual(res.returncode, 0, res.stderr)
        shot = [ln for ln in lines if ln.get("step") == "cover"][0]
        self.assertEqual(_png_size(Path(shot["png"])), (1200, 630))
        mock = self.dir / "mock.html"
        html_text, _ = unfurl.mock_html(card_block(), None)
        mock.write_text(html_text.replace("</body>", "<script>document.title='ran'</script></body>"))
        out = self.dir / "mock.png"
        res, lines = tsp.run_renderer(["--static-html", str(mock), "--png", str(out), "--viewport", "560x640",
                                       "--dpr", "2"], env, timeout=300)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(_png_size(out), (1120, 1280))


if __name__ == "__main__":
    unittest.main()
