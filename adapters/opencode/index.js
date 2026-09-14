import { resolve } from "node:path";
import { connection, createBridge, DECISION_TOOL, decisionContext, recordDecision } from "./lib/bridge.js";

async function loadZod() {
  try { return (await import("zod")).z; } catch { return undefined; }
}

/** OpenCode tool args are a zod raw shape (`@opencode-ai/plugin` ToolDefinition). */
function decisionTool(z, onRecorded) {
  const f = DECISION_TOOL.fields;
  const ids = (description) => z.array(z.string()).optional().describe(description);
  return {
    description: DECISION_TOOL.description,
    args: {
      choice: z.string().describe(f.choice),
      question: z.string().optional().describe(f.question),
      why: z.string().optional().describe(f.why),
      alt: ids(f.alt),
      by: z.enum(["agent", "user"]).optional().describe(f.by),
      anchor: ids(f.anchor), follows: ids(f.follows), refines: ids(f.refines), supersedes: ids(f.supersedes),
      id: z.string().optional().describe(f.id),
    },
    async execute(args, toolContext) {
      try { return await recordDecision("opencode", toolContext.sessionID, toolContext.directory, args); }
      finally { onRecorded(toolContext.sessionID); }
    },
  };
}

/** OpenCode 1.18.x server plugin. Uses terminal message/part events, including errors. */
export default async function CardinalPlugin({ client, directory }, options = {}) {
  const warn = (message) => { void client.app.log({ body: { service: "cardinal", level: "warn", message } }).catch(() => {}); };
  const bridge = options.bridge || createBridge("opencode", { warn });
  const sessions = new Map();
  const readConnection = options.connection || (() => connection("opencode"));
  // One context lookup per session per user message; system.transform runs on every model request.
  const decisionContexts = new Map();
  // Without zod (an install that skipped dependencies) the prompt points at the CLI instead.
  const z = "zod" in options ? options.zod : await loadZod();
  const tool = z ? decisionTool(z, (id) => decisionContexts.delete(id)) : undefined;
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
    ...(tool ? { tool: { [DECISION_TOOL.name]: tool } } : {}),
    async "chat.message"(input) { decisionContexts.delete(input?.sessionID); },
    async "experimental.chat.system.transform"(input, output) {
      try {
        const ctx = await context(input?.sessionID);
        if (!ctx) return;
        let pending = decisionContexts.get(ctx.session_id);
        if (!pending) {
          if (decisionContexts.size >= 1024) decisionContexts.delete(decisionContexts.keys().next().value);
          pending = decisionContext("opencode", ctx.session_id, ctx.cwd, tool ? DECISION_TOOL.name : undefined);
          decisionContexts.set(ctx.session_id, pending);
        }
        const text = await pending;
        if (text && Array.isArray(output?.system)) output.system.push(text);
      } catch { /* Decision capture must never break a model request. */ }
    },
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
          decisionContexts.delete(p.info.id);
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
            const mcp = part.tool.startsWith("cardinal_") && part.tool !== DECISION_TOOL.name ? part.tool.slice(9) : undefined;
            bridge.send({ ...ctx, kind: "tool", event_id: part.callID, model_call_id: part.messageID,
              tool_name: part.tool, success: state.status === "completed", timestamp: state.time.end,
              duration_ms: state.time.end - state.time.start, command: state.input?.command,
              mcp_server_name: mcp ? "cardinal" : undefined, mcp_tool_name: mcp });
          }
        } else if (event.type === "server.instance.disposed") {
          await bridge.flush();
          sessions.clear();
          decisionContexts.clear();
        }
      } catch { /* An unfamiliar upstream event must not interrupt the agent. */ }
    },
    async dispose() { await bridge.flush(); sessions.clear(); decisionContexts.clear(); },
  };
}
