import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";
import { cardinalMcp } from "../../dist/native/pi/mcp.js";

test("Pi MCP initializes, discovers schemas, calls tools, propagates errors and closes", async t => {
  const requests = [], transports = new Set();
  const http = createServer(async (req, res) => {
    requests.push(req.headers["x-cardinalhq-api-key"]);
    if (req.method !== "POST") { res.writeHead(405); res.end(); return; }
    const server = new McpServer({ name: "fixture", version: "1.0.0" });
    server.registerTool("echo", { inputSchema: { value: z.string() } }, async ({ value }) => ({ content: [{ type: "text", text: value }] }));
    server.registerTool("fail", { inputSchema: {} }, async () => ({ isError: true, content: [{ type: "text", text: "expected tool failure" }] }));
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
    transports.add(transport);
    res.on("close", () => { void transport.close(); transports.delete(transport); });
    await server.connect(transport);
    await transport.handleRequest(req, res);
  });
  await new Promise(r => http.listen(0, "127.0.0.1", r));
  const mcp = cardinalMcp({ readConnection: async () => ({ state: { mcp_url: `http://127.0.0.1:${http.address().port}/mcp` }, secrets: { mcp_api_key: "mcp-test-key" } }) });
  t.after(async () => { await mcp.close(); for (const transport of transports) await transport.close(); http.closeAllConnections(); await new Promise(r => http.close(r)); });
  const tools = await mcp.list();
  assert.equal(tools.find(tool => tool.name === "echo").inputSchema.properties.value.type, "string");
  const results = await Promise.all([mcp.call("echo", { value: "hello" }), mcp.call("echo", { value: "world" })]);
  assert.equal(results[0].content[0].text, "hello");
  await assert.rejects(mcp.call("fail", {}), /expected tool failure/);
  assert(requests.length > 3); assert(requests.every(key => key === "mcp-test-key"));
});

test("MCP follows pagination and rejects repeated cursors", async () => {
  let page = 0, clients = 0;
  const mcp = cardinalMcp({
    readConnection: async () => ({ state: { mcp_url: "https://fixture.example/mcp" }, secrets: { mcp_api_key: "fixture" } }),
    makeClient() { clients++; return { async connect() {}, async close() {}, async listTools() {
      page++; return { tools: [{ name: `tool-${page}` }], nextCursor: page === 1 ? "next" : undefined };
    } }; },
  });
  assert.equal((await mcp.list()).length, 2); assert.equal(clients, 1); await mcp.close();
});

test("MCP never forwards credentials across redirects", async t => {
  let forwarded = false;
  const target = createServer((_req, res) => { forwarded = true; res.end("{}"); });
  const redirect = createServer((_req, res) => { res.writeHead(307, { location: `http://127.0.0.1:${target.address().port}/mcp` }); res.end(); });
  await new Promise(r => target.listen(0, "127.0.0.1", r));
  await new Promise(r => redirect.listen(0, "127.0.0.1", r));
  const mcp = cardinalMcp({ readConnection: async () => ({ state: { mcp_url: `http://127.0.0.1:${redirect.address().port}/mcp` }, secrets: { mcp_api_key: "secret" } }) });
  t.after(async () => { await mcp.close(); target.closeAllConnections(); redirect.closeAllConnections(); await Promise.all([new Promise(r => target.close(r)), new Promise(r => redirect.close(r))]); });
  await assert.rejects(mcp.list()); assert.equal(forwarded, false);
});
