"""The cross-agent parity matrix (docs/parity.json) is true of the code.

docs/parity.json lists every feature of the four plugin adapters (claude,
codex, cursor, gemini) and, per adapter, "present", "limit:<citation>" or
"na:<reason>". This suite checks:

- every cell is one of the three, and every feature names all four adapters;
- every "present" cell has at least one probe, and every probe passes
  (a hook registration, a skill, a file, an MCP server, or a grep);
- every limit cites something checkable (a URL, a `*.md#anchor` that
  resolves to a heading, or a gemini-cli bundle reference);
- every Claude hook registration (event + script) is referenced by a
  parity feature or by the `_claude_internal_hooks` allowlist, so a new
  Claude-only hook forces a parity decision;
- docs/parity.md's generated matrix matches parity.json
  (`python3 build/parity_md.py` rewrites it).

Codex and Cursor register their hooks and MCP servers from code
(scripts/cardinal-connect). The probes read what that code writes: each
connect script is loaded in a child `python3 -I` with HOME pointed at a
scratch directory and its own writer functions are run against scratch
files (nothing under the real HOME is touched, nothing is sent). Claude
and Gemini register from static files (hooks/hooks.json, .mcp.json,
extension/hooks/hooks.json).

Stdlib only; Python 3.9.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PARITY_JSON = ROOT / "docs" / "parity.json"
PARITY_MD = ROOT / "docs" / "parity.md"
ADAPTERS = ("claude", "codex", "cursor", "gemini")
PROBE_KINDS = ("hook", "skill", "script", "agent", "mcp", "grep")


def _load_parity_md():
    """build/parity_md.py by path (build/ is not put on sys.path: its module
    names would shadow other suites' imports in a shared run)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("cardinal_build_parity_md", ROOT / "build" / "parity_md.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


parity_md = _load_parity_md()

# Run inside a child interpreter: loads one adapter's scripts/cardinal-connect
# (with the source core/cardinal_core, not a possibly stale vendored copy)
# and prints the hook registration and MCP server names its writers produce.
_CONNECT_PROBE = r'''
import importlib.util, json, re, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

root, adapter = Path(sys.argv[1]), sys.argv[2]
sys.path.insert(0, str(root / "core"))
import cardinal_core  # noqa: F401  (the source package wins over hooks/cardinal_core)

path = root / "adapters" / adapter / "scripts" / "cardinal-connect"
loader = SourceFileLoader("cardinal_connect_parity_probe", str(path))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)
scratch = Path(tempfile.mkdtemp())
url, key = "https://mcp.example.invalid/mcp", "parity-probe-key"
if adapter == "codex":
    mod.HOOKS_PATH = scratch / "hooks.json"
    mod.write_hooks_config()
    hooks = json.loads(mod.HOOKS_PATH.read_text())["hooks"]
    block = mod.build_managed_block(url, key)
    servers = re.findall(r'(?m)^\[mcp_servers\.(?:"([^"]+)"|([A-Za-z0-9_-]+))\]', block)
    servers = [a or b for a, b in servers]
elif adapter == "cursor":
    hooks = mod.build_hooks_config(scratch / "hooks.json")["hooks"]
    mcp = scratch / "mcp.json"
    mod.write_mcp_config(mcp, url, key)
    servers = list(json.loads(mcp.read_text())["mcpServers"])
elif adapter == "gemini":
    hooks = None  # extension/hooks/hooks.json is the registration
    servers = list(mod.merged_mcp_servers(None, url, key))
else:
    raise SystemExit("no connect probe for " + adapter)
print(json.dumps({"hooks": hooks, "mcp_servers": sorted(servers)}))
'''


def load_parity() -> dict:
    return json.loads(PARITY_JSON.read_text(encoding="utf-8"))


def features(data: dict) -> Dict[str, dict]:
    return {k: v for k, v in data.items() if not k.startswith("_")}


def flatten_hooks(hooks: dict) -> List[dict]:
    """[{event, matcher, command}] from a hooks map: Claude / Codex / Gemini
    groups ({matcher, hooks: [{command}]}) or Cursor's flat entries
    ({command, matcher?})."""
    out = []
    for event, entries in (hooks or {}).items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("hooks"), list):
                for handler in entry["hooks"]:
                    if isinstance(handler, dict):
                        out.append({"event": event, "matcher": str(entry.get("matcher") or ""),
                                    "command": str(handler.get("command") or "")})
            else:
                out.append({"event": event, "matcher": str(entry.get("matcher") or ""),
                            "command": str(entry.get("command") or "")})
    return out


_CONNECT_CACHE: Dict[str, dict] = {}


def connect_probe(adapter: str) -> dict:
    if adapter not in _CONNECT_CACHE:
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            proc = subprocess.run([sys.executable, "-I", "-c", _CONNECT_PROBE, str(ROOT), adapter],
                                  cwd=home, env=env, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise AssertionError(f"loading adapters/{adapter}/scripts/cardinal-connect failed:\n"
                                 f"{proc.stderr[-2000:]}")
        _CONNECT_CACHE[adapter] = json.loads(proc.stdout.strip().splitlines()[-1])
    return _CONNECT_CACHE[adapter]


def registrations(adapter: str) -> List[dict]:
    if adapter == "claude":
        data = json.loads((ROOT / "adapters/claude/hooks/hooks.json").read_text())
        return flatten_hooks(data.get("hooks"))
    if adapter == "gemini":
        data = json.loads((ROOT / "adapters/gemini/extension/hooks/hooks.json").read_text())
        return flatten_hooks(data.get("hooks"))
    return flatten_hooks(connect_probe(adapter)["hooks"])


def mcp_servers(adapter: str) -> List[str]:
    if adapter == "claude":
        return sorted(json.loads((ROOT / "adapters/claude/.mcp.json").read_text()))
    return connect_probe(adapter)["mcp_servers"]


def matcher_covers(matcher: str, tool: str) -> bool:
    """Whether a registered matcher fires for `tool` (an empty or `*` matcher
    fires for every tool; otherwise it is a regex searched in the name)."""
    if matcher in ("", "*"):
        return True
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return matcher == tool


def run_probe(adapter: str, probe: dict) -> Optional[str]:
    """None when the probe passes, else why it failed."""
    kind = probe.get("kind")
    base = ROOT / "adapters" / adapter
    if kind == "hook":
        regs = [r for r in registrations(adapter) if r["event"] == probe["event"]]
        if not regs:
            return f"no {probe['event']} registration"
        regs = [r for r in regs if re.search(probe["command"], r["command"])]
        if not regs:
            return f"no {probe['event']} handler whose command matches /{probe['command']}/"
        if "matcher" in probe:
            regs = [r for r in regs if re.search(probe["matcher"], r["matcher"])]
            if not regs:
                return f"the {probe['event']} handler's matcher does not match /{probe['matcher']}/"
        if "tool" in probe:
            regs = [r for r in regs if matcher_covers(r["matcher"], probe["tool"])]
            if not regs:
                return f"no {probe['event']} handler /{probe['command']}/ fires for tool {probe['tool']!r}"
        return None
    if kind == "skill":
        path = base / "skills" / probe["name"] / "SKILL.md"
        return None if path.is_file() else f"missing {path.relative_to(ROOT)}"
    if kind in ("script", "agent"):
        path = base / probe["path"]
        return None if path.is_file() else f"missing {path.relative_to(ROOT)}"
    if kind == "mcp":
        servers = mcp_servers(adapter)
        return None if probe["server"] in servers else f"MCP server {probe['server']!r} not in {servers}"
    if kind == "grep":
        path = base / probe["path"]
        if not path.is_file():
            return f"missing {path.relative_to(ROOT)}"
        text = path.read_text(encoding="utf-8", errors="replace")
        if re.search(probe["pattern"], text, re.MULTILINE) is None:
            return f"/{probe['pattern']}/ not found in {path.relative_to(ROOT)}"
        return None
    return f"unknown probe kind {kind!r}"


def probe_list(feature: dict, adapter: str) -> List[dict]:
    value = (feature.get("probes") or {}).get(adapter)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


CELL_RE = re.compile(r"^(present|limit:\S.*|na:\S.*)$", re.DOTALL)
URL_RE = re.compile(r"https?://[^\s)>\]]+")
MD_REF_RE = re.compile(r"((?:adapters|docs|common|core)/[\w./-]+\.md)#([\w-]+)")
BUNDLE_RE = re.compile(r"chunk-[A-Za-z0-9]+\.js:\d+")


def github_anchor(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def md_anchors(path: Path) -> set:
    anchors = set()
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m and not in_fence:
            anchors.add(github_anchor(m.group(1)))
    return anchors


class ParityMatrixShape(unittest.TestCase):
    def setUp(self):
        self.data = load_parity()
        self.features = features(self.data)

    def test_has_features(self):
        self.assertGreater(len(self.features), 20)

    def test_every_feature_names_all_four_adapters_with_a_valid_cell(self):
        for fid, f in self.features.items():
            with self.subTest(feature=fid):
                self.assertTrue(f.get("title"), f"{fid}: no title")
                self.assertIn(f.get("group"), parity_md.GROUPS, f"{fid}: unknown group {f.get('group')!r}")
                self.assertEqual(sorted(f.get("adapters", {})), sorted(ADAPTERS),
                                 f"{fid}: adapters must be exactly {ADAPTERS} (opencode/pi/devin are not "
                                 f"in this matrix)")
                for adapter, cell in f["adapters"].items():
                    self.assertIsInstance(cell, str, f"{fid}/{adapter}")
                    self.assertRegex(cell, CELL_RE,
                                     f"{fid}/{adapter}: cell must be 'present', 'limit:<citation>' "
                                     f"or 'na:<reason>', got {cell!r}")

    def test_probes_only_on_present_cells_and_every_present_cell_has_one(self):
        for fid, f in self.features.items():
            for adapter in ADAPTERS:
                cell = f["adapters"].get(adapter, "")
                probes = probe_list(f, adapter)
                with self.subTest(feature=fid, adapter=adapter):
                    if cell == "present":
                        self.assertTrue(probes, f"{fid}/{adapter} is present but has no probe")
                    else:
                        self.assertFalse(probes, f"{fid}/{adapter} is {cell.split(':')[0]}: a probe is "
                                                 f"only for a present cell")
                    for p in probes:
                        self.assertIn(p.get("kind"), PROBE_KINDS, f"{fid}/{adapter}: {p}")

    def test_every_limit_cites_something_checkable(self):
        for fid, f in self.features.items():
            for adapter, cell in f["adapters"].items():
                if not cell.startswith("limit:"):
                    continue
                with self.subTest(feature=fid, adapter=adapter):
                    refs = URL_RE.findall(cell) + MD_REF_RE.findall(cell) + BUNDLE_RE.findall(cell)
                    self.assertTrue(refs, f"{fid}/{adapter}: a limit must cite a doc URL, a "
                                          f"<file>.md#anchor or a gemini-cli bundle reference: {cell!r}")
                    for path, anchor in MD_REF_RE.findall(cell):
                        target = ROOT / path
                        self.assertTrue(target.is_file(), f"{fid}/{adapter}: cites missing {path}")
                        self.assertIn(anchor, md_anchors(target),
                                      f"{fid}/{adapter}: {path} has no heading for #{anchor}")

    def test_na_cells_give_a_reason(self):
        for fid, f in self.features.items():
            for adapter, cell in f["adapters"].items():
                if cell.startswith("na:"):
                    self.assertGreater(len(cell[3:].strip()), 10, f"{fid}/{adapter}: na needs a reason")


class ParityProbes(unittest.TestCase):
    def test_every_present_probe_passes(self):
        data = load_parity()
        for fid, f in features(data).items():
            for adapter in ADAPTERS:
                if f["adapters"].get(adapter) != "present":
                    continue
                for probe in probe_list(f, adapter):
                    with self.subTest(feature=fid, adapter=adapter, probe=probe):
                        why = run_probe(adapter, probe)
                        self.assertIsNone(why, f"parity feature {fid!r} on {adapter}: "
                                               f"{probe.get('kind')} probe failed: {why}")


class ClaudeHookGuard(unittest.TestCase):
    """A Claude hook nobody mapped is a parity decision nobody made."""

    @staticmethod
    def script_of(command: str) -> str:
        return command.rstrip().rsplit("/", 1)[-1]

    def test_every_claude_hook_is_referenced_by_a_parity_feature(self):
        data = load_parity()
        probes = [p for f in features(data).values() for p in probe_list(f, "claude") if p.get("kind") == "hook"]
        allow = {(a["event"], a["script"]) for a in data.get("_claude_internal_hooks", [])}
        for reg in registrations("claude"):
            script = self.script_of(reg["command"])
            covered = any(p["event"] == reg["event"] and re.search(p["command"], reg["command"]) for p in probes)
            with self.subTest(event=reg["event"], script=script):
                self.assertTrue(covered or (reg["event"], script) in allow,
                                f"Claude registers {script} on {reg['event']} but no feature in docs/parity.json "
                                f"references it: add a feature row (and wire Codex/Cursor/Gemini or declare "
                                f"limit/na), or allowlist it in _claude_internal_hooks with a reason")

    def test_allowlist_entries_are_real_and_explained(self):
        data = load_parity()
        regs = {(r["event"], self.script_of(r["command"])) for r in registrations("claude")}
        for entry in data.get("_claude_internal_hooks", []):
            with self.subTest(entry=entry):
                self.assertTrue(str(entry.get("reason") or "").strip(), f"{entry}: needs a reason")
                self.assertIn((entry.get("event"), entry.get("script")), regs,
                              f"{entry}: not registered in adapters/claude/hooks/hooks.json (stale allowlist)")


class ParityDoc(unittest.TestCase):
    def test_parity_md_matches_parity_json(self):
        current = PARITY_MD.read_text(encoding="utf-8")
        self.assertEqual(current, parity_md.update(current, load_parity()),
                         "docs/parity.md is out of date: run `python3 build/parity_md.py`")


if __name__ == "__main__":
    unittest.main()
