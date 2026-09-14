import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { connection } from "./lib/bridge.js";

/** Lazy connection: installing the extension never initiates a remote tool call. */
export function cardinalMcp({ readConnection = () => connection("pi"), makeClient = () => new Client({ name: "cardinal-pi-plugin", version: "0.2.0" }) } = {}) {
  let current, fingerprint, connecting;
  let acquiring = Promise.resolve();
  async function close() {
    if (connecting) await connecting.catch(() => {});
    const client = current;
    current = undefined; fingerprint = undefined;
    if (client) await client.close().catch(() => {});
  }
  async function openClient() {
    const conn = await readConnection();
    if (!conn?.state.mcp_url || !conn.secrets.mcp_api_key) {
      await close();
      throw new Error("Cardinal MCP is not connected. Run cardinal-pi connect, then restart Pi.");
    }
    const identity = JSON.stringify([conn.state.mcp_url, conn.secrets.mcp_api_key]);
    if (connecting) await connecting;
    if (current && fingerprint === identity) return current;
    await close();
    connecting = (async () => {
      const client = makeClient();
      client.onclose = () => {
        if (current === client) { current = undefined; fingerprint = undefined; }
      };
      try {
        await client.connect(new StreamableHTTPClientTransport(new URL(conn.state.mcp_url), {
          requestInit: { headers: { "X-CardinalHQ-API-Key": conn.secrets.mcp_api_key }, redirect: "error" },
        }), { timeout: 15000 });
        current = client; fingerprint = identity;
        return client;
      } catch (error) { await client.close().catch(() => {}); throw error; }
    })();
    try { return await connecting; } finally { connecting = undefined; }
  }
  function getClient() {
    const result = acquiring.then(openClient);
    acquiring = result.then(() => {}, () => {});
    return result;
  }
  return {
    close,
    async list(signal) {
      const client = await getClient();
      const tools = [], cursors = new Set();
      let cursor;
      do {
        const page = await client.listTools(cursor ? { cursor } : {}, { signal, timeout: 30000 });
        tools.push(...page.tools);
        cursor = page.nextCursor;
        if (cursor && cursors.has(cursor)) throw new Error("Cardinal MCP returned a repeated pagination cursor.");
        cursors.add(cursor);
      } while (cursor);
      return tools;
    },
    async call(name, args, signal) {
      const client = await getClient();
      const result = await client.callTool({ name, arguments: args }, undefined, { signal, timeout: 60000 });
      if (result.isError) {
        throw new Error((result.content || []).filter(c => c.type === "text").map(c => c.text).join("\n") || "Cardinal tool failed");
      }
      const content = (result.content || []).map(c => c.type === "text" || c.type === "image" ? c : { type: "text", text: JSON.stringify(c) });
      if (result.structuredContent) content.push({ type: "text", text: JSON.stringify(result.structuredContent) });
      return { content: content.length ? content : [{ type: "text", text: "Tool completed." }], details: {} };
    },
  };
}
