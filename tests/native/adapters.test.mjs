import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import openCode from "../../dist/native/opencode/index.js";
import pi from "../../dist/native/pi/index.ts";
import { createBridge } from "../../dist/native/opencode/lib/bridge.js";

const attributes = record => Object.fromEntries(record.attributes.map(a => [a.key, Object.values(a.value)[0]]));
const flat = bodies => bodies.flatMap(b => b.resourceLogs.flatMap(r => r.scopeLogs.flatMap(s => s.logRecords)));

async function fixture(t, runtime) {
  const dir = await mkdtemp(join(tmpdir(), `cardinal-${runtime}-`));
  const key = `CARDINAL_${runtime.toUpperCase()}_HOME`;
  const prior = process.env[key]; process.env[key] = dir;
  const bodies = [];
  const server = createServer(async (req, res) => {
    assert.equal(req.headers["x-cardinalhq-api-key"], "test-ingest-key");
    let body = ""; for await (const part of req) body += part;
    bodies.push(JSON.parse(body)); res.writeHead(200); res.end("{}");
  });
  await new Promise(r => server.listen(0, "127.0.0.1", r));
  t.after(async () => {
    prior === undefined ? delete process.env[key] : process.env[key] = prior;
    await new Promise(r => server.close(r)); await rm(dir, { recursive: true, force: true });
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
  assert.equal(records.find(r => r.event_name === "cardinal.git_state").cardinal_repo, "cardinalhq/fixture");
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
  assert.deepEqual([...registered.keys()], ["cardinal_list_tools", "cardinal_call_tool"]);
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
