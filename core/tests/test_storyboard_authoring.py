"""cardinal_core.storyboard_authoring: the storyboard work around Cardinal's
tool calls, for claude, codex, cursor and gemini. Each runtime's MCP tool
naming (is_cardinal_tool), its own render / state directories under
~/.<runtime>, the preview render (a fake renderer), the hero upload, the
evidence token, the edit lookup's local gates, the discovery re-look's gates,
the stray-key warning and the connect hint. Claude's hooks are wrappers over
this module; adapters/claude/tests/test_storyboard_*.py drive them end to end."""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from cardinal_core import evidence
from cardinal_core import storyboard_authoring as sa
from cardinal_core.paths import AgentPaths

SB = "sb_0123456789abcdef01234567"
ORG = "org-1"
KEY = "cardinal-mcp-key-123"
SID = "11111111-2222-3333-4444-555555555555"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
CONN = {"origin": "https://maestro.test", "org": ORG, "key": KEY}
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzYiI6InNiIn0.c2ln"

# A stand-in for skills/storyboard/scripts/render_preview.py: logs its argv
# and stdin, then answers like the renderer in the mode FAKE_RENDER_MODE says.
FAKE_RENDERER = textwrap.dedent('''\
    import json, os, sys
    log = os.environ["FAKE_RENDER_LOG"]
    data = sys.stdin.read()
    with open(log, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "stdin": data}) + "\\n")
    mode = os.environ.get("FAKE_RENDER_MODE", "ok")
    rt = sys.argv[sys.argv.index("--runtime") + 1]
    out = os.path.join(os.environ["HOME"], "." + rt, "cardinal", "storyboards", "%s", "r7")
    if mode == "crash":
        raise RuntimeError("boom " + "ck_live_secret")
    if mode == "nochrome":
        print(json.dumps({"summary": {"message": "No usable local Chromium.", "exit": 3}}))
        sys.exit(3)
    if "--static-html" in sys.argv:
        png = sys.argv[sys.argv.index("--png") + 1]
        open(png, "wb").write(b"png")
        print(json.dumps({"static_html": sys.argv[sys.argv.index("--static-html") + 1], "png": png}))
        print(json.dumps({"summary": {"out_dir": out}}))
        sys.exit(0)
    if "--cover" in sys.argv:
        scene = sys.argv[sys.argv.index("--cover") + 1]
        os.makedirs(out, exist_ok=True)
        png = os.path.join(out, scene + "-cover.png")
        open(png, "wb").write(b"cover")
        print(json.dumps({"scene_id": scene, "step": "cover", "png": png}))
        print(json.dumps({"summary": {"out_dir": out}}))
        sys.exit(0)
    os.makedirs(out, exist_ok=True)
    for step in (0, 1):
        png = os.path.join(out, "shipping-%%d.png" %% step)
        open(png, "wb").write(b"png")
        print(json.dumps({"scene_id": "shipping", "step": step, "png": png, "error": None, "frame_errors": []}))
    print(json.dumps({"summary": {"storyboard_id": "%s", "revision": 7, "out_dir": out, "rendered": 2}}))
''' % (SB, SB))


def preview_result(card=None) -> dict:
    scene = {"id": "shipping", "preview_bundle": {"path": f"/api/orgs/{ORG}/storyboards/{SB}/scenes/shipping/bundle"
                                                          f"?revision=7", "revision": 7, "bytes": 10,
                                                  "sha256": "0" * 64}}
    out = {"storyboard_id": SB, "revision": 7, "scenes": [scene], "evidence_token": TOKEN}
    if card is not None:
        out["card"] = card
    return out


def mcp_shape(result: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(result)}], "structuredContent": result}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.environ = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "")}

    def tearDown(self):
        self.tmp.cleanup()

    def paths(self, runtime: str) -> AgentPaths:
        return sa.agent_paths(runtime, self.home)


class ToolNameTests(unittest.TestCase):
    def test_claude_takes_both_cardinal_server_names(self):
        for name in ("mcp__cardinal__storyboard__preview", "mcp__plugin_cardinal_cardinal__storyboard__preview"):
            self.assertTrue(sa.is_cardinal_tool("claude", name, "storyboard__preview"), name)
        self.assertFalse(sa.is_cardinal_tool("claude", "mcp__evil__storyboard__preview", "storyboard__preview"))
        self.assertFalse(sa.is_cardinal_tool("claude", "mcp__cardinal__storyboard__publish", "storyboard__preview"))
        self.assertFalse(sa.is_cardinal_tool("claude", "mcp_cardinal_storyboard__preview", "storyboard__preview"))

    def test_codex_names_its_server_cardinal(self):
        # codex/scripts/cardinal-connect: [mcp_servers.cardinal]; its hooks
        # match ^mcp__cardinal__storyboard__…$.
        self.assertTrue(sa.is_cardinal_tool("codex", "mcp__cardinal__storyboard__create", "storyboard__create"))
        self.assertFalse(sa.is_cardinal_tool("codex", "mcp__plugin_cardinal_cardinal__storyboard__create",
                                             "storyboard__create"))
        self.assertFalse(sa.is_cardinal_tool("codex", "mcp__other__storyboard__create", "storyboard__create"))

    def test_cursor_names_and_its_serverless_spelling(self):
        self.assertTrue(sa.is_cardinal_tool("cursor", "mcp__cardinal__storyboard__preview", "storyboard__preview"))
        self.assertFalse(sa.is_cardinal_tool("cursor", "mcp__grafana__storyboard__preview", "storyboard__preview"))
        # MCP:<tool> names no server: only with the server the payload names.
        self.assertFalse(sa.is_cardinal_tool("cursor", "MCP:storyboard__preview", "storyboard__preview"))
        self.assertTrue(sa.is_cardinal_tool("cursor", "MCP:storyboard__preview", "storyboard__preview",
                                            server="cardinal"))
        self.assertFalse(sa.is_cardinal_tool("cursor", "MCP:storyboard__preview", "storyboard__preview",
                                             server="other"))
        self.assertFalse(sa.is_cardinal_tool("cursor", "mcp__cardinal__storyboard__preview", "storyboard__preview",
                                             server="other"))

    def test_gemini_flattened_name_and_mcp_context(self):
        # gemini-cli 0.50: generateValidName("cardinal_storyboard__preview").
        self.assertTrue(sa.is_cardinal_tool("gemini", "mcp_cardinal_storyboard__preview", "storyboard__preview"))
        self.assertEqual(sa.mcp_tool("gemini", "mcp_cardinal_storyboard__preview"),
                         ("cardinal", "storyboard__preview"))
        self.assertFalse(sa.is_cardinal_tool("gemini", "mcp_other_storyboard__preview", "storyboard__preview"))
        self.assertFalse(sa.is_cardinal_tool("gemini", "mcp__cardinal__storyboard__preview", "storyboard__preview"))
        self.assertFalse(sa.is_cardinal_tool("gemini", "storyboard__preview", "storyboard__preview"))
        # mcp_context.server_name wins over the ambiguous flattened name.
        ctx_name = "cardinal"
        self.assertTrue(sa.is_cardinal_tool("gemini", "mcp_cardinal_storyboard__preview", "storyboard__preview",
                                            server=ctx_name))
        self.assertTrue(sa.is_cardinal_tool("gemini", "storyboard__preview", "storyboard__preview", server=ctx_name))
        self.assertFalse(sa.is_cardinal_tool("gemini", "mcp_cardinal_storyboard__preview", "storyboard__preview",
                                             server="cardinal_storyboard"))
        self.assertFalse(sa.is_cardinal_tool("gemini", "mcp_other_storyboard__preview", "storyboard__preview",
                                             server="cardinal"))

    def test_op_forms_and_unknown_input(self):
        tools = ("storyboard__create", "storyboard__add_act")
        self.assertTrue(sa.is_cardinal_tool("codex", "mcp__cardinal__storyboard__add_act", tools))
        self.assertFalse(sa.is_cardinal_tool("codex", "mcp__cardinal__storyboard__preview", tools))
        self.assertTrue(sa.is_cardinal_tool("codex", "mcp__cardinal__lakerunner__list_services"))
        for runtime in sa.RUNTIMES:
            for name in (None, "", 7, "Bash", "mcp__", "mcp__cardinal__", "mcp____x"):
                self.assertFalse(sa.is_cardinal_tool(runtime, name), (runtime, name))
        self.assertFalse(sa.is_cardinal_tool("devin", "mcp__cardinal__storyboard__create"))
        self.assertEqual(set(sa.CARDINAL_SERVERS), set(sa.RUNTIMES))


class LayoutTests(Base):
    def test_each_runtime_renders_and_keeps_state_under_its_own_dir(self):
        for runtime in sa.RUNTIMES:
            p = self.paths(runtime)
            base = self.home / f".{runtime}" / "cardinal"
            self.assertEqual(sa.storyboards_dir(p), base / "storyboards")
            self.assertEqual(sa.edit_state_dir(p), base / "storyboard-edit-lookup")
            self.assertEqual(sa.discovery_state_dir(p), base / "storyboard-discovery")
            self.assertEqual(sa.caps_path(p), base / "server-caps.json")
        self.assertEqual(sa.spill_root(self.paths("claude"), "claude"), self.home / ".claude" / "projects")
        for runtime in ("codex", "cursor", "gemini"):
            self.assertIsNone(sa.spill_root(self.paths(runtime), runtime))

    def test_default_renderer_is_the_vendoring_plugins_storyboard_script(self):
        here = Path(sa.__file__).resolve()
        self.assertEqual(sa.default_renderer(),
                         here.parents[2] / "skills" / "storyboard" / "scripts" / "render_preview.py")


class UnwrapTests(Base):
    def test_result_shapes(self):
        result = preview_result()
        want = sa._has_scenes
        for shape in (result, mcp_shape(result), {"content": json.dumps(result)}, json.dumps(result),
                      [{"type": "text", "text": json.dumps(result)}],
                      json.dumps({"content": [{"type": "text", "text": json.dumps(result)}]})):  # Cursor tool_output
            self.assertEqual(sa.unwrap(shape, want), result)
        for bad in (None, 3, "not json", {"content": [{"type": "image"}]}, {"isError": True}):
            self.assertIsNone(sa.unwrap(bad, want))

    def test_a_spill_notice_is_followed_only_for_claude_and_only_under_its_projects_dir(self):
        result = preview_result()
        projects = self.home / ".claude" / "projects" / "p" / "s" / "tool-results"
        projects.mkdir(parents=True)
        spilled = projects / "x.txt"
        spilled.write_text(json.dumps([{"type": "text", "text": json.dumps(result)}]))
        notice = {"content": [{"type": "text", "text": f"Error: too big. Output has been saved to {spilled}."}]}
        self.assertEqual(sa.preview_result(self.paths("claude"), "claude", notice), result)
        self.assertIsNone(sa.preview_result(self.paths("codex"), "codex", notice))
        outside = self.home / "elsewhere.txt"
        outside.write_text(json.dumps(result))
        self.assertIsNone(sa.preview_result(self.paths("claude"), "claude",
                                            f"Output has been saved to {outside}."))


class PreviewTests(Base):
    def setUp(self):
        super().setUp()
        self.renderer = self.home / "render_preview.py"
        self.renderer.write_text(FAKE_RENDERER)
        self.log = self.home / "render.log"
        self._saved = {k: os.environ.get(k) for k in ("HOME", "FAKE_RENDER_LOG", "FAKE_RENDER_MODE", sa.BUDGET_ENV)}
        os.environ.update(HOME=str(self.home), FAKE_RENDER_LOG=str(self.log))
        os.environ.pop("FAKE_RENDER_MODE", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def calls(self) -> list:
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def preview(self, runtime: str, tool_result, **kw):
        return sa.on_preview(self.paths(runtime), runtime, tool_result, renderer=self.renderer, **kw)

    def test_each_runtime_renders_into_its_own_dir_with_its_own_connection(self):
        for runtime in sa.RUNTIMES:
            with self.subTest(runtime=runtime):
                ctx = self.preview(runtime, mcp_shape(preview_result()), session_id=SID)
                out = self.home / f".{runtime}" / "cardinal" / "storyboards" / SB / "r7"
                self.assertIn(f"PNGs are in {out}/", ctx)
                self.assertIn("- shipping: shipping-0.png, shipping-1.png", ctx)
                self.assertIn(sa.READ_INSTRUCTION, ctx)
                argv = self.calls()[-1]["argv"]
                self.assertEqual(argv[argv.index("--runtime") + 1], runtime)
                self.assertIn("--from-json", argv)
                self.assertEqual(json.loads(self.calls()[-1]["stdin"]), preview_result())

    def test_the_card_renders_cover_and_mock_in_the_runtimes_dir(self):
        card = {"cover_render": {"scene_id": "shipping", "width": 1200, "height": 630},
                "member_preview": {"image": "summary"}, "summary_svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
                "og": {"title": "<b>t</b>", "description": "d"}}
        ctx = self.preview("gemini", mcp_shape(preview_result(card)), session_id=SID)
        out = self.home / ".gemini" / "cardinal" / "storyboards" / SB / "r7"
        self.assertIn(f"Cover render: {out / 'shipping-cover.png'}", ctx)
        self.assertIn(f"Link preview mock: {out / sa.MOCK_PNG_NAME}", ctx)
        page = (out / sa.MOCK_HTML_NAME).read_text()
        self.assertIn("&lt;b&gt;t&lt;/b&gt;", page)
        self.assertEqual(oct((out / sa.MOCK_HTML_NAME).stat().st_mode & 0o777), "0o600")
        kinds = [c["argv"] for c in self.calls()]
        self.assertEqual(len(kinds), 3)
        self.assertIn("--cover", kinds[1])
        self.assertIn("--static-html", kinds[2])
        self.assertTrue(all(a[a.index("--runtime") + 1] == "gemini" for a in kinds))

    def test_no_chromium_is_said_once_per_session_per_runtime(self):
        os.environ["FAKE_RENDER_MODE"] = "nochrome"
        first = self.preview("codex", preview_result(), session_id=SID)
        self.assertIn("not rendered locally. No usable local Chromium.", first)
        self.assertIsNone(self.preview("codex", preview_result(), session_id=SID))
        self.assertTrue((self.home / ".codex" / "cardinal" / "storyboards" / f".no-chromium-{SID}").exists())
        self.assertIn("not rendered locally", self.preview("cursor", preview_result(), session_id=SID))

    def test_a_crash_names_only_the_exception_class(self):
        os.environ["FAKE_RENDER_MODE"] = "crash"
        ctx = self.preview("codex", preview_result())
        self.assertIn("local renderer failed (exit 1, RuntimeError)", ctx)
        self.assertNotIn("ck_live_secret", ctx)

    def test_silent_without_scenes_or_renderer(self):
        self.assertIsNone(self.preview("codex", {"content": [{"type": "text", "text": "error: nope"}]}))
        self.assertIsNone(sa.on_preview(self.paths("codex"), "codex", preview_result(),
                                        renderer=self.home / "missing.py"))
        self.assertEqual(self.calls(), [])

    def test_the_budget_env_only_lowers_it(self):
        self.assertEqual(sa.preview_budget({sa.BUDGET_ENV: "3"}), 3.0)
        self.assertEqual(sa.preview_budget({sa.BUDGET_ENV: "9999"}), float(sa.PREVIEW_BUDGET_S))
        self.assertEqual(sa.preview_budget({sa.BUDGET_ENV: "x"}), float(sa.PREVIEW_BUDGET_S))
        self.assertLess(sa.PREVIEW_BUDGET_S + 2 * sa.KILL_GRACE_S + 5, sa.PREVIEW_HOOK_TIMEOUT_S)


class _Resp:
    def __init__(self, status: int, body: dict):
        self.status, self._body = status, json.dumps(body).encode()

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Opener:
    def __init__(self, status=200, body=None):
        self.requests: list = []
        self.answer = (status, body if body is not None else {"stored": True})

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _Resp(*self.answer)


class PublishTests(Base):
    def published(self, **card) -> dict:
        base = {"hero_scene_id": "shipping", "designated_cover": False,
                "hero_upload": {"method": "PUT", "path": f"/api/orgs/{ORG}/storyboards/{SB}/acts/1/hero?revision=7"
                                                         f"&scene=shipping", "max_bytes": 2097152}}
        base.update(card)
        return {"storyboard_id": SB, "published": True, "revision": 7, "card": base}

    def render(self, runtime: str) -> None:
        d = sa.storyboards_dir(self.paths(runtime)) / SB / "r7"
        d.mkdir(parents=True)
        (d / "shipping-0.png").write_bytes(PNG)

    def test_the_hero_comes_from_the_runtimes_own_render_dir(self):
        self.render("codex")
        opener = _Opener()
        text = sa.on_publish(self.paths("codex"), "codex", mcp_shape(self.published()), connection=lambda: CONN,
                             client="codex/1.0", environ=self.environ, opener=opener)
        self.assertIn("public links use your rendered shipping (revision 7)", text)
        req = opener.requests[0]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.get_header("X-cardinal-client"), "codex/1.0")
        self.assertEqual(req.data, PNG)
        # Nothing rendered under ~/.cursor: the summary card.
        text = sa.on_publish(self.paths("cursor"), "cursor", mcp_shape(self.published()), connection=lambda: CONN,
                             client=lambda: "cursor/2.0", environ=self.environ, opener=_Opener())
        self.assertIn("no local render of shipping at revision 7", text)

    def test_gates(self):
        self.render("gemini")
        opener = _Opener()
        args = dict(connection=lambda: CONN, opener=opener)
        p = self.paths("gemini")
        self.assertIsNone(sa.on_publish(p, "gemini", self.published(), environ={sa.HERO_DISABLE_ENV: "off"}, **args))
        self.assertIsNone(sa.on_publish(p, "gemini", {**self.published(), "published": False}, environ={}, **args))
        self.assertIsNone(sa.on_publish(p, "gemini", self.published(), environ={}, connection=lambda: {},
                                        opener=opener))
        self.assertEqual(opener.requests, [])
        # Claude reads the opt-out from settings.json too; others do not.
        claude = self.paths("claude")
        claude.home.mkdir(parents=True)
        (claude.home / "settings.json").write_text(json.dumps({"env": {sa.HERO_DISABLE_ENV: "0"}}))
        self.assertTrue(sa.hero_disabled(claude, "claude", {}))
        (p.home / "settings.json").write_text(json.dumps({"env": {sa.HERO_DISABLE_ENV: "0"}}))
        self.assertFalse(sa.hero_disabled(p, "gemini", {}))

    def test_an_http_error_is_reported(self):
        self.render("codex")
        text = sa.on_publish(self.paths("codex"), "codex", self.published(), connection=lambda: CONN,
                             environ={}, opener=_Opener(409, {"error": "stale_revision", "message": "x"}))
        self.assertIn("not uploaded (HTTP 409 stale_revision: x)", text)

    def test_without_a_connection_callable_the_agents_own_files_are_used(self):
        p = self.paths("codex")
        p.home.mkdir(parents=True)
        p.state_path.write_text(json.dumps({"mcp_url": f"https://maestro.test/api/orgs/{ORG}/mcp"}))
        p.secrets_path.write_text(json.dumps({"mcp_api_key": KEY}))
        self.assertEqual(sa._connection(p, "codex", None, self.environ),
                         {"origin": "https://maestro.test", "org": ORG, "key": KEY})
        self.assertEqual(sa._connection(self.paths("claude"), "claude", None, self.environ), {})


class TokenTests(Base):
    def created(self) -> dict:
        return {"storyboard_id": SB, "evidence_token": TOKEN, "evidence_token_expires_at": "2099-01-01T00:00:00Z",
                "evidence_upload": {"method": "POST", "path": f"/api/orgs/{ORG}/storyboards/{SB}/evidence"}}

    def tokens(self) -> dict:
        return evidence.read_tokens(evidence.default_root(self.home), SID)

    def test_each_runtimes_cardinal_create_is_kept_and_another_servers_never(self):
        names = {"claude": "mcp__plugin_cardinal_cardinal__storyboard__create",
                 "codex": "mcp__cardinal__storyboard__create",
                 "cursor": "mcp__cardinal__storyboard__create",
                 "gemini": "mcp_cardinal_storyboard__create"}
        for runtime, name in names.items():
            with self.subTest(runtime=runtime):
                bad = name.replace("cardinal__storyboard", "evil__storyboard").replace(
                    "mcp_cardinal_", "mcp_evil_")
                sa.observe_cardinal_result(self.paths(runtime), runtime, bad, mcp_shape(self.created()),
                                           session_id=SID, home=self.home, environ=self.environ)
                self.assertEqual(self.tokens(), {})
                sa.observe_cardinal_result(self.paths(runtime), runtime, name, mcp_shape(self.created()),
                                           session_id=SID, home=self.home, environ=self.environ)
                self.assertEqual(self.tokens()[SB]["org"], ORG)
                self.assertEqual(self.tokens()[SB]["evidence_token"], TOKEN)
                evidence.default_root(self.home).joinpath(SID, evidence.TOKEN_FILE).unlink()

    def test_preview_takes_the_org_from_a_bundle_path_and_capture_off_keeps_nothing(self):
        sa.observe_cardinal_result(self.paths("codex"), "codex", "mcp__cardinal__storyboard__preview",
                                   mcp_shape(preview_result()), session_id=SID, home=self.home,
                                   environ={"CARDINAL_EVIDENCE_CAPTURE": "0"})
        self.assertEqual(self.tokens(), {})
        sa.observe_cardinal_result(self.paths("codex"), "codex", "mcp__cardinal__storyboard__preview",
                                   mcp_shape(preview_result()), session_id=SID, home=self.home, environ={})
        self.assertEqual(self.tokens()[SB]["org"], ORG)
        sa.observe_cardinal_result(self.paths("codex"), "codex", "mcp__cardinal__storyboard__publish",
                                   mcp_shape(self.created()), session_id="other-session", home=self.home,
                                   environ={})
        self.assertEqual(evidence.read_tokens(evidence.default_root(self.home), "other-session"), {})

    def test_a_successful_mcp_checkpoint_holds_the_sessions_evidence(self):
        root = evidence.default_root(self.home)
        answer = {"investigation_id": "inv_" + "a" * 24, "events": [{"seq": 4, "type": "finding.proposed"}],
                  "first_seq": 4, "last_seq": 4}
        names = {"claude": "mcp__plugin_cardinal_cardinal__storyboard__checkpoint",
                 "codex": "mcp__cardinal__storyboard__checkpoint",
                 "cursor": "mcp__cardinal__storyboard__checkpoint",
                 "gemini": "mcp_cardinal_storyboard__checkpoint"}
        hold = root / SID / evidence.HOLD_FILE
        for runtime, name in names.items():
            with self.subTest(runtime=runtime):
                (root / SID).mkdir(parents=True, exist_ok=True)
                # Another server's tool, an error answer, capture off: no hold.
                bad = name.replace("cardinal__storyboard", "evil__storyboard").replace("mcp_cardinal_", "mcp_evil_")
                sa.observe_cardinal_result(self.paths(runtime), runtime, bad, mcp_shape(answer), session_id=SID,
                                           home=self.home, environ=self.environ)
                sa.observe_cardinal_result(self.paths(runtime), runtime, name,
                                           mcp_shape({"error": "checkpoint_requires_investigation_author"}),
                                           session_id=SID, home=self.home, environ=self.environ)
                sa.observe_cardinal_result(self.paths(runtime), runtime, name, mcp_shape(answer), session_id=SID,
                                           home=self.home, environ={**self.environ, "CARDINAL_EVIDENCE_CAPTURE": "0"})
                self.assertFalse(hold.exists())
                sa.observe_cardinal_result(self.paths(runtime), runtime, name, mcp_shape(answer), session_id=SID,
                                           home=self.home, environ=self.environ)
                self.assertEqual(evidence.read_hold(root, SID)["reason"], "checkpoint")
                self.assertEqual(oct(hold.stat().st_mode & 0o777), "0o600")
                hold.unlink()
        # No spool directory: nothing to protect, nothing written; a hold that
        # cannot be written never raises.
        sa.observe_cardinal_result(self.paths("codex"), "codex", names["codex"], mcp_shape(answer),
                                   session_id="no-such-session", home=self.home, environ=self.environ)
        self.assertFalse((root / "no-such-session").exists())
        hold.mkdir()   # the rename onto it fails
        sa.observe_cardinal_result(self.paths("codex"), "codex", names["codex"], mcp_shape(answer),
                                   session_id=SID, home=self.home, environ=self.environ)
        self.assertTrue(hold.is_dir())
        self.assertEqual([p.name for p in (root / SID).iterdir()], [evidence.HOLD_FILE])


class EditLookupTests(Base):
    def test_unconnected_marks_the_runtimes_session_ineligible_without_network(self):
        for runtime in sa.RUNTIMES:
            with self.subTest(runtime=runtime):
                seen = []
                p = self.paths(runtime)
                out = sa.edit_lookup(p, runtime, SID, "a.py", str(self.home), connection=lambda: {},
                                     environ=self.environ, debug=seen.append)
                self.assertIsNone(out)
                self.assertTrue((sa.edit_state_dir(p) / f"{SID}.skip").exists())
                sa.edit_lookup(p, runtime, SID, "b.py", str(self.home), connection=lambda: CONN,
                               environ=self.environ, debug=seen.append)
                self.assertEqual(seen, ["ineligible"])

    def test_caps_below_1_never_sends(self):
        p = self.paths("codex")
        calls = []
        out = sa.edit_lookup(p, "codex", SID, "a.py", str(self.home), connection=lambda: calls.append(1) or CONN,
                             environ=self.environ)
        self.assertIsNone(out)
        self.assertEqual(calls, [1])
        self.assertTrue((sa.edit_state_dir(p) / f"{SID}.skip").exists())

    def test_dedupe_fast_path_and_opt_out(self):
        p = self.paths("gemini")
        d = sa.edit_state_dir(p)
        d.mkdir(parents=True)
        (d / f"{SID}.json").write_text(json.dumps({"dirs": [], "count": sa.EDIT_MAX_LOOKUPS}))
        seen = []
        never = lambda: self.fail("no connection on the fast path")  # noqa: E731
        self.assertIsNone(sa.edit_lookup(p, "gemini", SID, "a.py", str(self.home), connection=never,
                                         environ=self.environ, debug=seen.append))
        self.assertEqual(seen, ["dedupe"])
        self.assertIsNone(sa.edit_lookup(p, "gemini", SID, "a.py", str(self.home), connection=never,
                                         environ={sa.DISCOVERY_DISABLE_ENV: "0"}))
        self.assertIsNone(sa.edit_lookup(p, "gemini", None, "a.py", str(self.home), connection=never))
        self.assertIsNone(sa.edit_lookup(p, "gemini", SID, "a\0.py", str(self.home), connection=never))

    def test_a_new_secrets_file_lifts_the_marker_for_the_other_runtimes(self):
        p = self.paths("cursor")
        sa.edit_lookup(p, "cursor", SID, "a.py", str(self.home), connection=lambda: {}, environ=self.environ)
        self.assertTrue(sa._ineligible(p, "cursor", SID))
        time.sleep(0.01)
        p.home.mkdir(parents=True, exist_ok=True)
        p.secrets_path.write_text("{}")
        self.assertFalse(sa._ineligible(p, "cursor", SID))


class RelookTests(Base):
    def test_gates_need_no_network(self):
        p = self.paths("codex")
        never = lambda: self.fail("no connection while discovery is off")  # noqa: E731
        self.assertIsNone(sa.relook_if_moved(p, "codex", SID, str(self.home), connection=never,
                                             environ={sa.DISCOVERY_DISABLE_ENV: "off"}))
        self.assertIsNone(sa.relook_if_moved(p, "codex", SID, str(self.home), connection=lambda: {},
                                             environ=self.environ))
        # Not a git repo: nothing to look for, and nothing raised.
        self.assertIsNone(sa.relook_if_moved(p, "codex", SID, str(self.home), connection=lambda: CONN,
                                             environ=self.environ, deadline=time.monotonic() + 0.5))

    def test_claude_reads_the_opt_out_from_settings(self):
        claude = self.paths("claude")
        claude.home.mkdir(parents=True)
        (claude.home / "settings.json").write_text(json.dumps({"env": {sa.DISCOVERY_DISABLE_ENV: "no"}}))
        self.assertTrue(sa.discovery_disabled(claude, "claude", {}))
        self.assertTrue(sa.edit_lookup_disabled(claude, "claude", {}))
        # The edit lookup takes the environment's value when it is set.
        self.assertFalse(sa.edit_lookup_disabled(claude, "claude", {sa.DISCOVERY_DISABLE_ENV: "1"}))
        self.assertTrue(sa.discovery_disabled(claude, "claude", {sa.DISCOVERY_DISABLE_ENV: "1"}))


class StrayKeyAndHintTests(Base):
    def test_claude_key_without_url_and_its_warning(self):
        p = self.paths("claude")
        self.assertFalse(sa.key_without_url(p, "claude", {}))
        self.assertTrue(sa.key_without_url(p, "claude", {"CARDINAL_MCP_API_KEY": "ck"}))
        self.assertFalse(sa.key_without_url(p, "claude", {"CARDINAL_MCP_API_KEY": "ck", "CARDINAL_MCP_URL": "u"}))
        w = sa.stray_key_warning(p, "claude", sid=SID, home=self.home, environ={"CARDINAL_MCP_API_KEY": "ck"})
        self.assertEqual(w, "Tell the user, briefly: " + sa.stray_key_text("claude"))
        self.assertIn("/cardinal:connect", w)
        self.assertIsNone(sa.stray_key_warning(p, "claude", sid=SID, home=self.home,
                                               environ={"CARDINAL_MCP_API_KEY": "ck"}), "once per session")

    def test_other_runtimes_read_their_own_files(self):
        p = self.paths("codex")
        p.home.mkdir(parents=True)
        p.secrets_path.write_text(json.dumps({"mcp_api_key": "ck"}))
        self.assertTrue(sa.key_without_url(p, "codex", {}))
        self.assertIsNone(sa.mcp_url(p, "codex", {}))
        w = sa.stray_key_warning(p, "codex", sid=None, source="startup", home=self.home, environ={})
        self.assertIn("~/.codex/cardinal-secrets.json", w)
        self.assertIn("scripts/cardinal-connect", w)
        self.assertNotIn("/cardinal:connect", w)
        self.assertIsNone(sa.stray_key_warning(p, "codex", sid=None, source="resume", home=self.home, environ={}))
        p.state_path.write_text(json.dumps({"mcp_url": "https://maestro.test/api/orgs/o/mcp"}))
        self.assertFalse(sa.key_without_url(p, "codex", {}))
        self.assertEqual(sa.mcp_url(p, "codex", {}), "https://maestro.test/api/orgs/o/mcp")

    def test_connect_hint_once_per_runtime(self):
        env = {"HOME": str(self.home)}
        for runtime in ("codex", "cursor", "gemini"):
            with self.subTest(runtime=runtime):
                p = self.paths(runtime)
                first = sa.connect_hint(p, runtime, source="startup", environ=env)
                self.assertTrue(first.startswith("Tell the user once, briefly: Cardinal is not connected"))
                self.assertIn("scripts/cardinal-connect", first)
                later = sa.connect_hint(p, runtime, source="startup", environ=env)
                self.assertTrue(later.endswith("Mention it only if the user asks about Cardinal or storyboards."))
                self.assertTrue(sa.connect_hint_marker(self.home, runtime).exists())
        p = self.paths("codex")
        p.home.mkdir(parents=True, exist_ok=True)
        p.secrets_path.write_text(json.dumps({"mcp_api_key": "ck"}))
        self.assertIsNone(sa.connect_hint(p, "codex", source="startup", environ=env))

    def test_claude_hint_needs_the_adapters_connected_answer(self):
        p = self.paths("claude")
        self.assertIsNone(sa.connect_hint(p, "claude", source="startup", home=self.home))
        self.assertIsNone(sa.connect_hint(p, "claude", connected=True, home=self.home))
        hint = sa.connect_hint(p, "claude", source="startup", connected=False, home=self.home)
        self.assertIn("/mcp lists it as missing CARDINAL_MCP_URL", hint)
        self.assertTrue((self.home / ".cardinal" / "connect-hint").exists())
        self.assertTrue(sa.connect_hint(p, "claude", source="resume", connected=False, home=self.home)
                        .endswith("Mention it only if the user asks about Cardinal or storyboards."))


class ImportCostTests(unittest.TestCase):
    def test_the_module_imports_nothing_heavy(self):
        # A hook's fast path imports this module: subprocess, signal and the
        # paths module (tempfile, dataclasses) load only when used.
        import subprocess as _sp  # noqa: F401
        code = ("import sys; import cardinal_core.storyboard_authoring; "
                "print(','.join(m for m in ('subprocess', 'cardinal_core.paths', 'tempfile') if m in sys.modules))")
        res = _sp.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                      cwd=str(Path(sa.__file__).resolve().parents[1]))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
