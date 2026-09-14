import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdir, mkdtemp, readFile, writeFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import openCode from "../../dist/native/opencode/index.js";
import pi from "../../dist/native/pi/index.ts";
import { createBridge, decisionsEnabled, runDecision } from "../../dist/native/opencode/lib/bridge.js";
import { z } from "zod";

const attributes = record => Object.fromEntries(record.attributes.map(a => [a.key, Object.values(a.value)[0]]));
const flat = bodies => bodies.flatMap(b => b.resourceLogs.flatMap(r => r.scopeLogs.flatMap(s => s.logRecords)));

async function fixture(t, runtime) {
  const dir = await mkdtemp(join(tmpdir(), `cardinal-${runtime}-`));
  const key = `CARDINAL_${runtime.toUpperCase()}_HOME`;
  const prior = process.env[key]; process.env[key] = dir;
  // A fake `gh` keeps PR resolution offline and deterministic.
  const bin = await mkdtemp(join(tmpdir(), "cardinal-gh-"));
  await writeFile(join(bin, "gh"), `#!/bin/sh\necho '{"number":42,"url":"https://github.com/cardinalhq/fixture/pull/42"}'\n`, { mode: 0o755 });
  const priorPath = process.env.PATH; process.env.PATH = `${bin}:${priorPath}`;
  const priorDecisions = process.env.CARDINAL_DECISIONS; delete process.env.CARDINAL_DECISIONS;
  const bodies = [];
  const server = createServer(async (req, res) => {
    assert.equal(req.headers["x-cardinalhq-api-key"], "test-ingest-key");
    let body = ""; for await (const part of req) body += part;
    bodies.push(JSON.parse(body)); res.writeHead(200); res.end("{}");
  });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    prior === undefined ? delete process.env[key] : process.env[key] = prior;
    process.env.PATH = priorPath;
    if (priorDecisions !== undefined) process.env.CARDINAL_DECISIONS = priorDecisions;
    await new Promise(r => server.close(r)); await rm(dir, { recursive: true, force: true }); await rm(bin, { recursive: true, force: true });
  });
  await writeFile(join(dir, "cardinal.json"), JSON.stringify({ ingest_endpoint: `http://127.0.0.1:${server.address().port}`, user_email: "test@example.com", org_slug: "fixture" }));
  await writeFile(join(dir, "cardinal-secrets.json"), JSON.stringify({ ingest_api_key: "test-ingest-key" }), { mode: 0o600 });
  const git = (...args) => execFileSync("git", args, { cwd: dir, stdio: "pipe" });
  git("init", "-b", "feat/native-adapters");
  git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.com", "commit", "--allow-empty", "-m", "fixture");
  git("remote", "add", "origin", "https://github.com/cardinalhq/fixture.git");
  return { dir, bodies };
}

function verifyContract(bodies, runtime) {
  const records = flat(bodies).map(attributes);
  assert.deepEqual(new Set(records.map(r => r.event_name)), new Set(["cardinal.git_state", "cardinal.turn_usage", "api_request", "cardinal.turn_tool", "tool_result"]));
  const usage = records.filter(r => r.event_name === "cardinal.turn_usage");
  assert.equal(usage.length, 1, "one completed model call, despite repeated events");
  assert.equal(usage[0].input_tokens, "10");
  assert.equal(usage[0].cache_read_input_tokens, "3");
  assert.equal(usage[0].cost_usd, 0.012);
  assert.equal(usage[0].agent_runtime, runtime);
  assert.equal(usage[0].turn_seq, "1");
  const tools = records.filter(r => r.event_name === "tool_result");
  assert.equal(tools.length, 2);
  assert.deepEqual(tools.map(r => r.success), [true, false]);
  assert.equal(tools[0].bash_class, "test");
  assert.equal(tools[0].turn_seq, "1");
  assert.equal(tools[1].tool_seq, "2");
  const gitState = records.find(r => r.event_name === "cardinal.git_state");
  assert.equal(gitState.cardinal_repo, "cardinalhq/fixture");
  assert.equal(gitState.cardinal_pr_number, "42");
  assert.equal(gitState.cardinal_pr_url, "https://github.com/cardinalhq/fixture/pull/42");
  assert(!JSON.stringify(bodies).includes("PRIVATE_SENTINEL"), "prompts, command arguments and tool output are not emitted");
}

test("OpenCode published event shapes -> Python -> OTLP; failures, dedup, workspace isolation", async t => {
  const { dir, bodies } = await fixture(t, "opencode");
  const warnings = [];
  const client = { app: { log: async x => warnings.push(x) }, session: { get: async ({ path }) => ({ data: { id: path.id, directory: path.id === "foreign" ? "/elsewhere" : dir } }) } };
  const plugin = await openCode({ client, directory: dir });
  const event = async (type, properties) => plugin.event({ event: { type, properties } });
  await event("session.created", { info: { id: "s1", directory: dir } });
  await event("message.updated", { info: { id: "u1", sessionID: "s1", role: "user", time: { created: 1000 } } });
  await event("message.part.updated", { part: { type: "step-start", id: "p1", messageID: "m1", sessionID: "s1" } });
  const info = { id: "m1", sessionID: "s1", role: "assistant", time: { created: 1001 }, modelID: "test-model", providerID: "test", tokens: { input: 10, output: 5, reasoning: 1, cache: { read: 3, write: 2 } }, cost: 0.012 };
  await event("message.updated", { info }); // incomplete streaming update
  for (const [id, status] of [["t1", "completed"], ["t2", "error"]]) {
    const part = { type: "tool", id, callID: id, messageID: "m1", sessionID: "s1", tool: "bash", state: { status, input: { command: "pytest PRIVATE_SENTINEL" }, output: "PRIVATE_SENTINEL", time: { start: 1002, end: 1003 } } };
    await event("message.part.updated", { part });
    await event("message.part.updated", { part });
  }
  info.time.completed = 1004;
  await event("message.updated", { info });
  await event("message.updated", { info });
  await event("message.updated", { info: { ...info, sessionID: "foreign", id: "foreign-m" } });
  await plugin.dispose();
  // Restarting the plugin and replaying a terminal event must not double bill.
  const reloaded = await openCode({ client, directory: dir });
  await reloaded.event({ event: { type: "message.updated", properties: { info } } });
  await reloaded.dispose();
  verifyContract(bodies, "opencode");
  assert.equal(warnings.length, 0);
});

test("OpenCode injects native MCP in memory and preserves existing configuration", async () => {
  const plugin = await openCode({ client: { app: { log: async () => {} } }, directory: "/repo" }, {
    connection: async () => ({ state: { mcp_url: "https://example.com/mcp" }, secrets: { mcp_api_key: "secret" } }),
    bridge: { send() {}, async flush() {} },
  });
  const config = { mcp: { other: { type: "remote", url: "https://other.example" } } };
  await plugin.config(config);
  assert.equal(config.mcp.cardinal.oauth, false);
  assert.equal(config.mcp.cardinal.headers["X-CardinalHQ-API-Key"], "secret");
  const foreign = { type: "remote", url: "https://custom.example" };
  config.mcp.cardinal = foreign;
  await plugin.config(config);
  assert.equal(config.mcp.cardinal, foreign);
  assert(config.mcp.other);
});

test("Pi lifecycle -> Python -> OTLP with tools tied to the model call", async t => {
  const { dir, bodies } = await fixture(t, "pi");
  const handlers = new Map(), registered = new Map();
  pi({ on: (name, fn) => handlers.set(name, fn), registerTool: tool => registered.set(tool.name, tool) });
  const ctx = { cwd: dir, sessionManager: { getSessionId: () => "pi-session" } };
  const fire = (name, event = {}) => handlers.get(name)(event, ctx);
  await fire("session_start");
  await fire("before_agent_start", { prompt: "PRIVATE_SENTINEL" });
  await fire("turn_start", { turnIndex: 0, timestamp: 1001 });
  const message = { role: "assistant", model: "test-model", provider: "test", timestamp: 1002, content: [{ type: "text", text: "PRIVATE_SENTINEL" }], usage: { input: 10, output: 5, cacheRead: 3, cacheWrite: 2, cost: { total: 0.012 } } };
  await fire("message_end", { message }); await fire("message_end", { message });
  for (const [id, isError] of [["p1", false], ["p2", true]]) {
    await fire("tool_execution_start", { toolCallId: id, toolName: "bash", args: { command: "pytest PRIVATE_SENTINEL" } });
    await fire("tool_execution_end", { toolCallId: id, toolName: "bash", isError, result: "PRIVATE_SENTINEL" });
  }
  await fire("session_shutdown");
  verifyContract(bodies, "pi");
  assert.deepEqual([...registered.keys()], ["cardinal_list_tools", "cardinal_call_tool", "cardinal_record_decision"]);
});

const decisionEvent = bodies => flat(bodies).map(attributes).filter(r => r.event_name === "cardinal.decision");

test("Pi decision capture: opt-in system prompt + native tool -> cardinal.decision with PR", async t => {
  const { dir, bodies } = await fixture(t, "pi");
  const handlers = new Map(), registered = new Map();
  pi({ on: (name, fn) => handlers.set(name, fn), registerTool: tool => registered.set(tool.name, tool) });
  const ctx = { cwd: dir, sessionManager: { getSessionId: () => "pi-session" } };
  const start = () => handlers.get("before_agent_start")({ prompt: "PRIVATE_SENTINEL", systemPrompt: "BASE" }, ctx);
  const tool = registered.get("cardinal_record_decision");
  assert.equal(await start(), undefined, "capture is off by default");
  await assert.rejects(tool.execute("c0", { choice: "Use SQLite" }, undefined, undefined, ctx), /cardinal-pi decision on/);

  assert.equal((await runDecision("pi", ["on"])).code, 0);
  const first = await start();
  assert(first.systemPrompt.startsWith("BASE\n\n"), "extends the chained system prompt");
  assert.match(first.systemPrompt, /`cardinal_record_decision` tool/);
  assert.match(first.systemPrompt, /\(none yet\)/);
  const result = await tool.execute("c1", { choice: "Use SQLite", question: "Which store?", why: "Zero ops", alt: ["-Postgres"], by: "user", anchor: ["README.md"] }, undefined, undefined, ctx);
  assert.match(result.content[0].text, /^Recorded decision use-sqlite: Use SQLite \(PR #42\)/);
  assert.match((await start()).systemPrompt, /- use-sqlite: Use SQLite/);
  await handlers.get("session_shutdown")();

  const [decision] = decisionEvent(bodies);
  assert.equal(decision.session_id, "pi-session");
  assert.equal(decision.agent_runtime, "pi");
  assert.equal(decision["cardinal.pr_number"], "42");
  assert.equal(decision["cardinal.repo"], "cardinalhq/fixture");
  assert.deepEqual(JSON.parse(decision["cardinal.decision.alternatives"]), ["-Postgres"]);
  assert.equal(JSON.parse(decision["cardinal.decision.anchors"])[0].identifier, "README.md");
  assert(!JSON.stringify(bodies).includes("PRIVATE_SENTINEL"));
});

test("OpenCode decision capture: chat-turn message transform + native tool -> cardinal.decision with PR", async t => {
  const { dir, bodies } = await fixture(t, "opencode");
  const client = { app: { log: async () => {} }, session: { get: async ({ path }) => ({ data: { id: path.id, directory: path.id === "foreign" ? "/elsewhere" : dir } }) } };
  const plugin = await openCode({ client, directory: dir });
  // Shape of the chat loop's messages.transform output: history with the latest user message last.
  const chat = async (target, sessionID) => {
    const messages = [
      { info: { id: "u0", sessionID, role: "user" }, parts: [{ type: "text", text: "earlier" }] },
      { info: { id: "a0", sessionID, role: "assistant" }, parts: [] },
      { info: { id: "u1", sessionID, role: "user" }, parts: [{ type: "text", text: "PRIVATE_SENTINEL" }] },
    ];
    await target["experimental.chat.messages.transform"]({}, { messages });
    assert.equal(messages[0].parts.length, 1, "only the latest user message is extended");
    return messages[2].parts.slice(1);
  };
  const tool = plugin.tool.cardinal_record_decision;
  // OpenCode wraps plugin args with z.object and serializes them for the model.
  assert.equal(z.object(tool.args).safeParse({}).success, false);
  assert.deepEqual(z.toJSONSchema(z.object(tool.args)).required, ["choice"]);
  // system.transform also fires for title/agent generation, so it must stay unused.
  assert.equal(plugin["experimental.chat.system.transform"], undefined);

  assert.deepEqual(await chat(plugin, "s1"), [], "capture is off by default");
  assert.equal((await runDecision("opencode", ["on"])).code, 0);
  assert.deepEqual(await chat(plugin, "s1"), [], "cached until the next user message");
  await plugin["chat.message"]({ sessionID: "s1" }, { message: {}, parts: [] });
  const [part] = await chat(plugin, "s1");
  assert.equal(part.synthetic, true);
  assert.equal(part.messageID, "u1");
  assert.match(part.text, /`cardinal_record_decision` tool/);
  assert.match(part.text, /\(none yet\)/);
  assert.deepEqual(await chat(plugin, "foreign"), [], "other workspaces are untouched");

  // Compaction's summarization request (no tools) gets nothing; the next chat step does.
  await plugin["experimental.session.compacting"]({ sessionID: "s1" }, { context: [], prompt: undefined });
  assert.deepEqual(await chat(plugin, "s1"), [], "compaction input is left alone");
  assert.equal((await chat(plugin, "s1")).length, 1);

  const text = await tool.execute({ choice: "Use SQLite", why: "Zero ops" }, { sessionID: "s1", directory: dir, abort: new AbortController().signal });
  assert.match(text, /^Recorded decision use-sqlite: Use SQLite \(PR #42\)/);
  // The cache is refreshed from the record result: a disabled Python must not be needed.
  const python = process.env.CARDINAL_PYTHON; process.env.CARDINAL_PYTHON = "/nonexistent/python";
  try { assert.match((await chat(plugin, "s1"))[0].text, /- use-sqlite: Use SQLite/, "recording refreshes the ledger"); }
  finally { python === undefined ? delete process.env.CARDINAL_PYTHON : process.env.CARDINAL_PYTHON = python; }
  const aborted = new AbortController(); aborted.abort();
  await assert.rejects(tool.execute({ choice: "Never" }, { sessionID: "s1", directory: dir, abort: aborted.signal }), /cancelled/);

  const fallback = await openCode({ client, directory: dir }, { zod: undefined, bridge: { send() {}, async flush() {} } });
  assert.equal(fallback.tool, undefined);
  assert.match((await chat(fallback, "s1"))[0].text, /cardinal-opencode\.js" decision record --session s1/);
  await plugin.dispose();

  const [decision] = decisionEvent(bodies);
  assert.equal(decision.session_id, "s1");
  assert.equal(decision.agent_runtime, "opencode");
  assert.equal(decision["cardinal.pr_url"], "https://github.com/cardinalhq/fixture/pull/42");
  assert.equal(decision["cardinal.decision.rationale"], "Zero ops");
});

test("decision fast path reads the enabled state, not just the config file's existence", async t => {
  const dir = await mkdtemp(join(tmpdir(), "cardinal-enabled-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const env = { CARDINAL_PI_HOME: dir };
  assert.equal(await decisionsEnabled("pi", env), false, "no config");
  await mkdir(join(dir, "cardinal", "decisions"), { recursive: true });
  const config = join(dir, "cardinal", "decisions", "config.json");
  await writeFile(config, '{"enabled":false}');
  assert.equal(await decisionsEnabled("pi", env), false, "left behind by `decision off`");
  await writeFile(config, '{"enabled":true}');
  assert.equal(await decisionsEnabled("pi", env), true);
  assert.equal(await decisionsEnabled("pi", { ...env, CARDINAL_DECISIONS: "0" }), false, "env override wins");
  assert.equal(await decisionsEnabled("pi", { ...env, CARDINAL_DECISIONS: "maybe" }), true, "unparseable override falls back to config");
  await writeFile(config, '{"enabled":false}');
  assert.equal(await decisionsEnabled("pi", { ...env, CARDINAL_DECISIONS: " ON " }), true);
});

test("decision recorder is killed with its whole process group on abort and timeout", async t => {
  const dir = await mkdtemp(join(tmpdir(), "cardinal-kill-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const python = join(dir, "python");
  // Stands in for Python blocked on a `gh`/`git` grandchild.
  await writeFile(python, `#!/bin/sh\nsleep 30 &\necho $! > "${dir}/$1.pid"\nwait\n`, { mode: 0o755 });
  const alive = pid => { try { process.kill(pid, 0); return true; } catch { return false; } };
  const grandchild = async name => {
    for (let i = 0; i < 100; i++) {
      try { return Number(await readFile(join(dir, `${name}.pid`), "utf8")); } catch { await new Promise(r => setTimeout(r, 20)); }
    }
    throw new Error("grandchild never started");
  };
  const settle = async pid => { for (let i = 0; i < 50 && alive(pid); i++) await new Promise(r => setTimeout(r, 20)); return alive(pid); };

  const controller = new AbortController();
  // pythonArgs puts the script path first; name the pid file after a unique arg instead.
  const aborted = runDecision("pi", [], { python, signal: controller.signal, timeout: 60000 });
  const abortPid = await grandchild("-B");
  controller.abort();
  assert.equal((await aborted).code, -1);
  assert.equal(await settle(abortPid), false, "abort kills the grandchild");

  await rm(join(dir, "-B.pid"));
  const started = Date.now();
  const timedOut = runDecision("pi", [], { python, timeout: 300 });
  const timeoutPid = await grandchild("-B");
  assert.equal((await timedOut).code, -1);
  assert(Date.now() - started < 5000);
  assert.equal(await settle(timeoutPid), false, "timeout kills the grandchild");
});

test("bridge serializes batches, drains on shutdown, and isolates failures", async () => {
  let running = 0, max = 0, warnings = 0;
  const received = [];
  const bridge = createBridge("opencode", {
    warn() { warnings++; },
    async run(_runtime, batch) {
      running++; max = Math.max(max, running);
      await new Promise(r => setTimeout(r, 2));
      received.push(...batch); running--;
      if (batch[0].id === 0) throw new Error("simulated failure");
    },
  });
  for (let id = 0; id < 600; id++) bridge.send({ id });
  await Promise.all([bridge.flush(), bridge.flush()]);
  assert.equal(max, 1); assert.equal(received.length, 600); assert.equal(warnings, 1);
  assert.equal(received[599].id, 599);
});

test("Pi's real extension loader accepts the built package", async t => {
  const { DefaultResourceLoader } = await import("@earendil-works/pi-coding-agent");
  const dir = await mkdtemp(join(tmpdir(), "cardinal-pi-loader-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const loader = new DefaultResourceLoader({ cwd: dir, agentDir: dir,
    additionalExtensionPaths: [resolve("dist/native/pi/index.ts")], noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true });
  await loader.reload();
  const result = loader.getExtensions();
  assert.deepEqual(result.errors, []);
  assert.equal(result.extensions.length, 1);
});
