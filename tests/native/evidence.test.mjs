// Generic evidence capture in the native adapters (Pi, OpenCode): every tool
// call goes to the local evidence spool through cardinal_native.py
// `evidence-capture` (the shared Python pipeline), and its id is shown inline.
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import openCode from "../../dist/native/opencode/index.js";
import pi from "../../dist/native/pi/index.ts";
import { boundEvidenceEvent, createEvidenceSink, evidenceId, evidenceCaptureEnabled, MAX_EVIDENCE_EVENT, MAX_EVIDENCE_FIELD } from "../../dist/native/opencode/lib/bridge.js";

const VECTORS = resolve(import.meta.dirname, "../../core/tests/testdata/evidence_id_vectors.json");

async function home(t, extra = {}) {
  const dir = await mkdtemp(join(tmpdir(), "cardinal-evidence-"));
  const saved = {};
  for (const [k, v] of Object.entries({ HOME: dir, CARDINAL_PI_HOME: dir, CARDINAL_OPENCODE_HOME: dir, ...extra })) {
    saved[k] = process.env[k]; process.env[k] = v;
  }
  for (const k of ["CARDINAL_EVIDENCE_CAPTURE", "CARDINAL_EVIDENCE_CONTEXT"]) {
    if (!(k in extra)) { saved[k] = process.env[k]; delete process.env[k]; }
  }
  t.after(async () => {
    for (const [k, v] of Object.entries(saved)) v === undefined ? delete process.env[k] : process.env[k] = v;
    await rm(dir, { recursive: true, force: true });
  });
  return dir;
}

async function entry(dir, session, id) {
  return JSON.parse(await readFile(join(dir, ".cardinal", "evidence", session, `${id}.json`), "utf8"));
}

async function entries(dir) {
  const root = join(dir, ".cardinal", "evidence");
  const out = [];
  for (const s of await readdir(root).catch(() => [])) {
    for (const f of await readdir(join(root, s)).catch(() => [])) if (f.startsWith("ev_")) out.push(f);
  }
  return out;
}

test("evidence ids match the Python implementation (shared vectors)", async () => {
  const { vectors } = JSON.parse(await readFile(VECTORS, "utf8"));
  assert(vectors.length >= 5);
  for (const v of vectors) assert.equal(evidenceId(v.runtime, v.session_id, v.tool_use_id), v.evidence_id, JSON.stringify(v));
  assert.equal(evidenceId("pi", "s", undefined), undefined);
  assert.equal(evidenceId("pi", "s", "x".repeat(257)), undefined);
});

test("Pi: every tool result is captured and its id appended; Cardinal's own tools and secrets are not kept", async t => {
  const dir = await home(t);
  const handlers = new Map();
  pi({ on: (name, fn) => handlers.set(name, fn), registerTool: () => {} });
  const ctx = { cwd: dir, sessionManager: { getSessionId: () => "pi-session" } };
  const fire = (name, event) => handlers.get(name)(event, ctx);
  const content = [{ type: "text", text: "12 passed\nDB_PASSWORD=hunter2" }];
  const out = await fire("tool_result", { type: "tool_result", toolCallId: "call-1", toolName: "bash", input: { command: "npm test" }, content, isError: false, details: undefined });
  const id = evidenceId("pi", "pi-session", "call-1");
  assert.equal(out.content.length, 2);
  assert.deepEqual(out.content[0], content[0]);
  assert.match(out.content[1].text, new RegExp(`^\\[evidence:${id}\\] Cardinal keeps this result`));
  assert.match(out.content[1].text, /cardinal-pi evidence promote/);
  const second = await fire("tool_result", { toolCallId: "call-2", toolName: "read", input: { path: "a.py" }, content: [{ type: "text", text: "x = 1" }], isError: false });
  assert.equal(second.content[1].text, `[evidence:${evidenceId("pi", "pi-session", "call-2")}]`);
  assert.equal(await fire("tool_result", { toolCallId: "call-3", toolName: "cardinal_call_tool", input: { name: "x" }, content: [], isError: false }), undefined);
  await fire("tool_result", { toolCallId: "call-4", toolName: "bash", input: { command: "cat .env" }, content: [{ type: "text", text: "TOKEN=SECRETzz9" }], isError: false });
  await fire("tool_result", { toolCallId: "call-5", toolName: "some_extension_tool", input: [1, 2], content: [{ type: "text", text: "boom" }], isError: true });
  await handlers.get("session_shutdown")();

  const e = await entry(dir, "pi-session", id);
  assert.equal(e.schema, "cardinal.evidence.v2");
  assert.deepEqual(e.source, { kind: "builtin", runtime: "pi" });
  assert.equal(e.server, "builtin:pi");
  assert.equal(e.tool, "bash");
  assert.deepEqual(e.args, { command: "npm test" });
  assert.deepEqual(e.result, { text: ["12 passed\nDB_PASSWORD=[redacted]"] });
  const withheld = await entry(dir, "pi-session", evidenceId("pi", "pi-session", "call-4"));
  assert.equal(withheld.withheld.rule, "path.dotenv");
  assert.equal(withheld.result, null);
  const failed = await entry(dir, "pi-session", evidenceId("pi", "pi-session", "call-5"));
  assert.equal(failed.status, "error");
  const all = await entries(dir);
  assert.equal(all.length, 4, "cardinal_call_tool is not captured");
  for (const f of all) assert(!(await readFile(join(dir, ".cardinal", "evidence", "pi-session", f), "utf8")).includes("SECRETzz9"));
});

test("OpenCode: completed and failed calls are captured; the id is appended to the output", async t => {
  const dir = await home(t);
  const client = { app: { log: async () => {} }, session: { get: async ({ path }) => ({ data: { id: path.id, directory: dir } }) } };
  const plugin = await openCode({ client, directory: dir }, { bridge: { send() {}, async flush() {} } });
  await plugin["tool.execute.before"]({ tool: "bash", sessionID: "ses_1", callID: "c1" }, { args: { command: "make test" } });
  const output = { title: "make test", output: "ok 3 tests", metadata: {} };
  await plugin["tool.execute.after"]({ tool: "bash", sessionID: "ses_1", callID: "c1", args: { command: "make test" } }, output);
  const id = evidenceId("opencode", "ses_1", "c1");
  assert.match(output.output, new RegExp(`^ok 3 tests\\n\\n\\[evidence:${id}\\] Cardinal keeps this result`));
  const cardinal = { title: "", output: "x", metadata: {} };
  await plugin["tool.execute.after"]({ tool: "cardinal_lakerunner_list_services", sessionID: "ses_1", callID: "c2", args: {} }, cardinal);
  assert.equal(cardinal.output, "x");
  await plugin.event({ event: { type: "message.part.updated", properties: { part: { type: "tool", id: "p3", callID: "c3", messageID: "m1", sessionID: "ses_1", tool: "webfetch",
    state: { status: "error", input: { url: "https://example.com" }, error: "HTTP 500", time: { start: 1, end: 2 } } } } } });
  await plugin.dispose();

  const e = await entry(dir, "ses_1", id);
  assert.deepEqual(e.source, { kind: "tool", runtime: "opencode" });
  assert.equal(e.server, "tool:opencode");
  assert.deepEqual(e.result, { text: ["ok 3 tests"] });
  const failed = await entry(dir, "ses_1", evidenceId("opencode", "ses_1", "c3"));
  assert.equal(failed.status, "error");
  assert.deepEqual(failed.result, { text: ["HTTP 500"] });
  assert.equal((await entries(dir)).length, 2);
});

test("capture off: nothing is spawned or written, no id is shown", async t => {
  const dir = await home(t, { CARDINAL_EVIDENCE_CAPTURE: "0" });
  assert.equal(evidenceCaptureEnabled(), false);
  const handlers = new Map();
  pi({ on: (name, fn) => handlers.set(name, fn), registerTool: () => {} });
  const ctx = { cwd: dir, sessionManager: { getSessionId: () => "pi-off" } };
  assert.equal(await handlers.get("tool_result")({ toolCallId: "c", toolName: "bash", input: {}, content: [], isError: false }, ctx), undefined);
  await handlers.get("session_shutdown")();
  assert.deepEqual(await entries(dir), []);
});

test("context off: captured, but the result is not changed", async t => {
  const dir = await home(t, { CARDINAL_EVIDENCE_CONTEXT: "0" });
  const handlers = new Map();
  pi({ on: (name, fn) => handlers.set(name, fn), registerTool: () => {} });
  const ctx = { cwd: dir, sessionManager: { getSessionId: () => "pi-quiet" } };
  assert.equal(await handlers.get("tool_result")({ toolCallId: "c", toolName: "bash", input: { command: "ls" }, content: [{ type: "text", text: "a" }], isError: false }, ctx), undefined);
  await handlers.get("session_shutdown")();
  assert.equal((await entries(dir)).length, 1);
});

test("huge results are cut, not dropped; one huge call never sinks the batch", async t => {
  const dir = await home(t);
  const handlers = new Map();
  pi({ on: (name, fn) => handlers.set(name, fn), registerTool: () => {} });
  const ctx = { cwd: dir, sessionManager: { getSessionId: () => "pi-big" } };
  const fire = (event) => handlers.get("tool_result")(event, ctx);
  // 40 calls of ~3 MiB each (a build log, a big file read): ~120 MiB raw,
  // more than Python reads in one batch. Every one must still land.
  const big = "L".repeat(3 << 20);
  for (let i = 0; i < 40; i++) {
    await fire({ toolCallId: `big-${i}`, toolName: "read", input: { path: `/tmp/f${i}` }, content: [{ type: "text", text: big }], isError: false });
  }
  // An input too large to send whole: kept as a withheld stub, never unchecked.
  await fire({ toolCallId: "huge-input", toolName: "write", input: { path: "/tmp/x", content: "W".repeat(5 << 20) }, content: [{ type: "text", text: "ok" }], isError: false });
  await fire({ toolCallId: "small", toolName: "some_future_tool", input: { q: 1 }, content: [{ type: "text", text: "fine" }], isError: false });
  await handlers.get("session_shutdown")();
  assert.equal((await entries(dir)).length, 42);
  const e = await entry(dir, "pi-big", evidenceId("pi", "pi-big", "big-7"));
  assert.equal(e.truncated, true);
  assert.equal(e.tool, "read");
  // Past the queue's byte bound the rest are stubs (reason "unreadable",
  // rule "backlog"), not dropped: every id the agent was shown resolves.
  const last = await entry(dir, "pi-big", evidenceId("pi", "pi-big", "big-39"));
  assert.equal(last.tool, "read");
  if (last.withheld) assert.equal(last.withheld.rule, "backlog");
  const stub = await entry(dir, "pi-big", evidenceId("pi", "pi-big", "huge-input"));
  assert.equal(stub.withheld.reason, "unreadable");
  assert.equal(stub.args, null);
  assert.equal(stub.result, null);
  const small = await entry(dir, "pi-big", evidenceId("pi", "pi-big", "small"));
  assert.deepEqual(small.result, { text: ["fine"] });
});

test("boundEvidenceEvent: small events pass through; fields are cut; oversize input becomes a stub", () => {
  const small = { kind: "evidence", tool_name: "x", input: { a: 1 }, output: "o" };
  assert.equal(boundEvidenceEvent(small)[0], small);
  const [cut, size] = boundEvidenceEvent({ kind: "evidence", tool_name: "x", input: {}, output: "o".repeat(MAX_EVIDENCE_EVENT + 10),
    content: [{ type: "text", text: "t".repeat(MAX_EVIDENCE_EVENT) }, { type: "image", data: "i".repeat(MAX_EVIDENCE_EVENT) }] });
  assert(size <= MAX_EVIDENCE_EVENT);
  assert.equal(cut.output.length, MAX_EVIDENCE_FIELD);
  assert.equal(cut.content[0].text.length, MAX_EVIDENCE_FIELD);
  assert.deepEqual(cut.content[1], { type: "image" });
  const [stub] = boundEvidenceEvent({ kind: "evidence", tool_name: "x", tool_call_id: "c", input: { big: "b".repeat(MAX_EVIDENCE_EVENT) }, output: "o" });
  assert.equal(stub.unreadable, "size");
  assert.equal(stub.input, undefined);
  assert.equal(stub.output, undefined);
});

test("evidence sink batches by bytes", async () => {
  const batches = [];
  const sink = createEvidenceSink("pi", { run: async (_rt, batch) => { batches.push(batch.length); } });
  for (let i = 0; i < 20; i++) sink.send({ kind: "evidence", tool_name: "t", tool_call_id: `c${i}`, input: {}, output: "x".repeat(3 << 20) });
  await sink.flush();
  assert.equal(batches.reduce((a, b) => a + b, 0), 20);
  assert(Math.max(...batches) <= 16, JSON.stringify(batches));
  assert(batches.length > 1);
});
