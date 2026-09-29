"""Seeded fuzz of the generic capture pipeline.

Random tool names from a hostile alphabet, random JSON trees for tool_input
and the response, planted hazards (secrets, surrogates, NULs, PEM blocks,
base64 runs, huge strings, deep nesting, spill notices pointing outside the
root), random success/failure and runtime. Deterministic by default;
CARDINAL_FUZZ_ITERS / CARDINAL_FUZZ_SEED for long runs.

Invariants, for every call:
  (a) capture_call never raises;
  (b) the file is valid UTF-8 JSON, mode 0600;
  (c) args <= 64 KiB and result <= 256 KiB (or the truncation envelope);
  (d) no planted secret substring anywhere in the file;
  (e) wire_item(entry) passes the port of conductor's EvidenceItemSchema, or
      the entry is a withheld stub (refused locally);
  (f) one call stays well inside the hook budget;
  (g) the withheld verdict does not depend on key order.

Run with: cd core && python3 -m unittest tests.test_evidence_fuzz -v
"""

from __future__ import annotations

import json
import os
import random
import stat
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cardinal_core import evidence
from cardinal_core import evidence_capture as cap
from cardinal_core import evidence_gate as gate
from cardinal_core import evidence_promote as promote

try:  # `python3 -m unittest discover tests` imports tests as top-level modules
    from test_evidence_capture_call import schema_errors
except ImportError:  # `python3 -m unittest tests.test_evidence_fuzz`
    from tests.test_evidence_capture_call import schema_errors

ITERS = int(os.environ.get("CARDINAL_FUZZ_ITERS", "3000"))
SEED = int(os.environ.get("CARDINAL_FUZZ_SEED", "20260929"))

SECRETS = {
    "pem": ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAfuzzPEMsecretBODYzzq\n"
            "-----END RSA PRIVATE KEY-----", "fuzzPEMsecretBODYzzq"),
    "pem_nofooter": ("-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANfuzzNOFOOTERsecretq", "fuzzNOFOOTERsecretq"),
    # Token shapes are assembled at run time so the test file itself holds
    # nothing a secret scanner flags.
    "jwt": ("eyJ" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiJmdXp6In0.fuzzJWTsig123456", "fuzzJWTsig123456"),
    "ghp": ("gh" + "p_" + "F" * 36, "gh" + "p_" + "F" * 36),
    "akia": ("AK" + "IAFUZZSECRET000001", "AK" + "IAFUZZSECRET000001"),
    "xox": ("xo" + "xb-1234567890-fuzzSLACKsecret", "fuzzSLACKsecret"),
    "kv": ("DB_PASSWORD=fuzzKVsecret99", "fuzzKVsecret99"),
    "kv_colon": ("api_key: fuzzCOLONsecret77", "fuzzCOLONsecret77"),
    "bearer": ("Authorization: Bearer fuzzBEARERsecret0123", "fuzzBEARERsecret0123"),
    "userinfo": ("postgres://admin:fuzzURLsecret55@db:5432/app", "fuzzURLsecret55"),
}

HOSTILE_NAMES = ["Bash", "Read", "mcp__srv__tool", "mcp__a__b__c", "mcp__", "__", "", " ", "x:y", "‮tool",
                 "télé", "\U0001f600", "a b  c", "mcp__srv with space__t!", "a" * 300, "\x00nul",
                 "FrobnicateWidgets_v9", "mcp__cardinal-dev__q", "Agent", "Edit"]
RUNTIMES = ["claude-code", "cursor", "gemini", "codex", "pi", "opencode", "future-agent"]
KEYS = ["a", "b", "command", "file_path", "content", "headers", "password", "name", "value", "stdout", "stderr",
        "structuredPatch", "text", "type", "é", "", "x" * 40, "api_key", "items", "url"]


class Fuzzer:
    def __init__(self, rng: random.Random, spill_outside: str):
        self.rng = rng
        self.spill_outside = spill_outside
        self.planted = []

    def hazard(self) -> str:
        r = self.rng.random()
        if r < 0.35:
            name = self.rng.choice(sorted(SECRETS))
            text, needle = SECRETS[name]
            self.planted.append(needle)
            return self.rng.choice(["", "log: ", "x "]) + text + self.rng.choice(["", " tail", "\n"])
        if r < 0.45:
            return self.rng.choice(["lone \ud800 hi", "nul \x00 x", "\udcff\udc80", "esc \x1b[31m", " "])
        if r < 0.55:
            import base64
            return base64.b64encode(self.rng.randbytes(self.rng.choice([100, 5000]))).decode()
        if r < 0.6:
            return "Output has been saved to " + self.spill_outside + "."
        if r < 0.63:
            return "z" * self.rng.choice([70000, 300000])
        if r < 0.65:
            return ("line " + "y" * 60 + "\n") * self.rng.choice([10000, 50000])
        return "".join(self.rng.choice("abc /.:=-_\n\"'{}[]") for _ in range(self.rng.randint(0, 40)))

    def tree(self):
        self.nodes = 0
        return self.value()

    def value(self, depth: int = 0):
        self.nodes += 1
        r = self.rng.random()
        if depth >= 10 or r < 0.3 or self.nodes > 300:
            return self.rng.choice([None, True, False, 0, -1, 3.5, 10 ** 12, self.hazard(), self.hazard()])
        if r < 0.65:
            return {self.rng.choice(KEYS): self.value(depth + 1) for _ in range(self.rng.randint(0, 8))}
        if r < 0.9:
            return [self.value(depth + 1) for _ in range(self.rng.randint(0, 8))]
        if r < 0.93:
            deep = cur = {}
            for _ in range(self.rng.choice([100, 5000])):
                cur["d"] = {}
                cur = cur["d"]
            return deep
        return {"name": "DB_PASSWORD", "value": self.hazard()}


def _reorder(v, rng, depth=0):
    if depth > 64:
        return v
    if isinstance(v, dict):
        items = list(v.items())
        rng.shuffle(items)
        return {k: _reorder(e, rng, depth + 1) for k, e in items}
    if isinstance(v, list):
        return [_reorder(e, rng, depth + 1) for e in v]
    return v


class FuzzTests(unittest.TestCase):
    def test_fuzz(self):
        rng = random.Random(SEED)
        with TemporaryDirectory() as t:
            home = Path(os.path.realpath(t))
            outside = home / "outside-secret.txt"
            outside.write_text("fuzzOUTSIDEspill")
            f = Fuzzer(rng, str(outside))
            root = evidence.default_root(home)
            slowest = 0.0
            for i in range(ITERS):
                f.planted = []
                name = rng.choice(HOSTILE_NAMES)
                runtime = rng.choice(RUNTIMES)
                tool_input = f.tree()
                response = f.tree()
                error = f.hazard() if rng.random() < 0.25 else None
                source, tool = cap.classify_mcp_name(name, runtime, ("cardinal",))
                call = cap.ToolCall(runtime=runtime, tool_name=name, source=source, tool=tool,
                                    tool_input=tool_input, response=None if error and rng.random() < 0.5 else response,
                                    error=error, session_id=rng.choice(["s1", "s2", None, "bad id!"]),
                                    tool_use_id=rng.choice([None, f"t{i}", "\U0001f600" * 3]), cwd=str(home / "w"),
                                    spill_root=str(home / ".claude" / "projects"), client=f"{runtime}/1.0")
                t0 = time.monotonic()
                try:
                    got = cap.capture_call(call, home, env={}, rules=gate.Rules())
                except Exception as e:  # (a)
                    self.fail(f"iter {i}: capture_call raised {type(e).__name__}: {e}")
                slowest = max(slowest, time.monotonic() - t0)
                if got is None:
                    self.assertEqual(source["kind"], "cardinal")
                    continue
                e = got.entry
                path = root / evidence.session_dir_name(e["session_id"]) / f"{e['evidence_id']}.json"
                raw = path.read_bytes()
                self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)  # (b)
                stored = json.loads(raw.decode("utf-8"))
                if not got.withheld:  # (c)
                    self.assertLessEqual(len(evidence.encode_json(stored["args"]).encode()),
                                         evidence.MAX_ARGS_BYTES, f"iter {i}")
                    self.assertLessEqual(len(evidence.encode_json(stored["result"]).encode()),
                                         evidence.MAX_RESULT_BYTES, f"iter {i}")
                text = raw.decode("utf-8")
                for needle in f.planted + ["fuzzOUTSIDEspill"]:  # (d)
                    self.assertNotIn(needle, text, f"iter {i}: planted secret on disk ({name!r})")
                if got.withheld:  # (e)
                    with self.assertRaises(promote.WithheldEntry):
                        promote.wire_item(stored, "c/1")
                else:
                    item = promote.wire_item(stored, "fuzz-client/1.0")
                    self.assertEqual(schema_errors(item), [], f"iter {i}: {item!r}"[:500])
                # (g)
                v1 = gate.check(name, tool_input, cwd=str(home / "w"), home=str(home))
                v2 = gate.check(name, _reorder(tool_input, rng), cwd=str(home / "w"), home=str(home))
                self.assertEqual(v1 is None, v2 is None, f"iter {i}")
            self.assertLess(slowest, 1.5, "one capture exceeded the hook budget")  # (f)


if __name__ == "__main__":
    unittest.main()
