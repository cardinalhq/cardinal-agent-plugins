import { resolve } from "node:path";
import { connection, createBridge } from "./lib/bridge.js";

/** OpenCode 1.18.x server plugin. Uses terminal message/part events, including errors. */
export default async function CardinalPlugin({ client, directory }, options = {}) {
  const warn = (message) => { void client.app.log({ body: { service: "cardinal", level: "warn", message } }).catch(() => {}); };
  const bridge = options.bridge || createBridge("opencode", { warn });
  const sessions = new Map();
  const readConnection = options.connection || (() => connection("opencode"));
  async function context(id) {
    if (!id) return undefined;
    let info = sessions.get(id);
    if (!info) {
      try { info = (await client.session.get({ path: { id } })).data; } catch { return undefined; }
      if (info) {
        if (sessions.size >= 1024) sessions.delete(sessions.keys().next().value);
        sessions.set(id, info);
      }
    }
    // A server event may concern another workspace. Never attribute it to ours.
    if (!info?.directory || resolve(info.directory) !== resolve(directory)) return undefined;
    return { session_id: id, cwd: info.directory, parent_session_id: info.parentID };
  }
  return {
    async config(config) {
      try {
        const conn = await readConnection();
        if (!conn?.state.mcp_url || !conn.secrets.mcp_api_key) return;
        config.mcp ||= {};
        if (config.mcp.cardinal) { warn("Cardinal MCP is already configured; keeping the existing server entry."); return; }
        config.mcp.cardinal = { type: "remote", url: conn.state.mcp_url, enabled: true, oauth: false,
          headers: { "X-CardinalHQ-API-Key": conn.secrets.mcp_api_key } };
      } catch { warn("Cardinal connection could not be loaded. Run cardinal-opencode status."); }
    },
    async event({ event }) {
      try {
        const p = event.properties;
        if (event.type === "session.created" || event.type === "session.updated") {
          const info = p.info;
          if (sessions.size >= 1024) sessions.delete(sessions.keys().next().value);
          sessions.set(info.id, info);
          const ctx = await context(info.id);
          if (ctx) bridge.send({ ...ctx, kind: "session", event_id: info.id });
        } else if (event.type === "session.deleted") {
          sessions.delete(p.info.id);
        } else if (event.type === "message.updated") {
          const info = p.info;
          const ctx = await context(info.sessionID);
          if (!ctx) return;
          if (info.role === "user") {
            bridge.send({ ...ctx, kind: "user", event_id: info.id, timestamp: info.time.created });
          } else if (info.role === "assistant" && info.time.completed !== undefined) {
            bridge.send({ ...ctx, kind: "model", event_id: info.id, model_call_id: info.id,
              timestamp: info.time.completed, model: info.modelID, provider: info.providerID,
              usage: { input_tokens: info.tokens?.input, output_tokens: info.tokens?.output,
                cache_read_input_tokens: info.tokens?.cache?.read, cache_creation_input_tokens: info.tokens?.cache?.write,
                reasoning_tokens: info.tokens?.reasoning, cost_usd: info.cost } });
          }
        } else if (event.type === "message.part.updated") {
          const part = p.part;
          const ctx = await context(part.sessionID);
          if (!ctx) return;
          if (part.type === "step-start") {
            bridge.send({ ...ctx, kind: "model_start", event_id: part.messageID, model_call_id: part.messageID });
          } else if (part.type === "tool" && ["completed", "error"].includes(part.state.status)) {
            const state = part.state;
            // Only Cardinal's known MCP prefix is split; underscores in arbitrary
            // server/tool names are otherwise ambiguous in OpenCode's tool IDs.
            const mcp = part.tool.startsWith("cardinal_") ? part.tool.slice(9) : undefined;
            bridge.send({ ...ctx, kind: "tool", event_id: part.callID, model_call_id: part.messageID,
              tool_name: part.tool, success: state.status === "completed", timestamp: state.time.end,
              duration_ms: state.time.end - state.time.start, command: state.input?.command,
              mcp_server_name: mcp ? "cardinal" : undefined, mcp_tool_name: mcp });
          }
        } else if (event.type === "server.instance.disposed") {
          await bridge.flush();
          sessions.clear();
        }
      } catch { /* An unfamiliar upstream event must not interrupt the agent. */ }
    },
    async dispose() { await bridge.flush(); sessions.clear(); },
  };
}
