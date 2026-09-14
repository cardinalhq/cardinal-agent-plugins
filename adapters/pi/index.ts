import { randomUUID } from "node:crypto";
import { Type } from "typebox";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createBridge, DECISION_TOOL, decisionContext, recordDecision } from "./lib/bridge.js";
import { cardinalMcp } from "./mcp.js";

const ids = (description: string) => Type.Optional(Type.Array(Type.String(), { description }));

export default function cardinal(pi: ExtensionAPI) {
  let notify = (_message: string) => {};
  const bridge = createBridge("pi", { warn: (message: string) => notify(message) });
  const mcp = cardinalMcp();
  let run = randomUUID();
  let modelCall = "";
  const tools = new Map<string, { command?: string; mcpTool?: string; start: number; modelCall: string }>();
  const context = (ctx: ExtensionContext) => ({ session_id: ctx.sessionManager.getSessionId(), cwd: ctx.cwd });

  pi.on("session_start", async (_event, ctx) => {
    notify = (message) => ctx.ui?.notify(message, "warning");
    run = randomUUID(); modelCall = ""; tools.clear();
    bridge.send({ ...context(ctx), kind: "session", event_id: ctx.sessionManager.getSessionId() });
  });
  pi.on("before_agent_start", async (event, ctx) => {
    run = randomUUID(); modelCall = "";
    bridge.send({ ...context(ctx), kind: "user", event_id: run });
    // Pi applies a returned systemPrompt for this turn only and chains extensions.
    const decisions = await decisionContext("pi", ctx.sessionManager.getSessionId(), ctx.cwd, DECISION_TOOL.name);
    return decisions ? { systemPrompt: `${event.systemPrompt ?? ""}\n\n${decisions}` } : undefined;
  });
  pi.on("turn_start", async (event, ctx) => {
    modelCall = `${run}:${event.turnIndex}`;
    bridge.send({ ...context(ctx), kind: "model_start", event_id: modelCall, model_call_id: modelCall, timestamp: event.timestamp });
  });
  pi.on("message_end", async (event, ctx) => {
    const message = event.message;
    if (message.role !== "assistant") return;
    bridge.send({ ...context(ctx), kind: "model", event_id: `${modelCall}:${message.timestamp}`,
      model_call_id: modelCall, model: message.model, provider: message.provider, timestamp: message.timestamp,
      usage: { input_tokens: message.usage?.input, output_tokens: message.usage?.output,
        cache_read_input_tokens: message.usage?.cacheRead, cache_creation_input_tokens: message.usage?.cacheWrite,
        cost_usd: message.usage?.cost?.total } });
  });
  pi.on("tool_execution_start", async (event) => {
    tools.set(event.toolCallId, { start: Date.now(), modelCall,
      command: event.toolName === "bash" ? event.args?.command : undefined,
      mcpTool: event.toolName === "cardinal_call_tool" ? event.args?.name : undefined });
  });
  pi.on("tool_execution_end", async (event, ctx) => {
    const prior = tools.get(event.toolCallId);
    tools.delete(event.toolCallId);
    bridge.send({ ...context(ctx), kind: "tool", event_id: event.toolCallId, model_call_id: prior?.modelCall || modelCall,
      tool_name: event.toolName, success: !event.isError, timestamp: Date.now(), command: prior?.command,
      duration_ms: prior ? Date.now() - prior.start : undefined,
      mcp_server_name: prior?.mcpTool ? "cardinal" : undefined, mcp_tool_name: prior?.mcpTool });
  });
  pi.on("session_shutdown", async () => { await bridge.flush(); await mcp.close(); tools.clear(); });

  pi.registerTool({
    name: "cardinal_list_tools", label: "Cardinal tools",
    description: "Discover the Cardinal MCP tools available to the connected account, including each tool's description and JSON input schema. Call before cardinal_call_tool.",
    promptSnippet: "Discover available Cardinal observability and operations tools.",
    parameters: Type.Object({}),
    async execute(_id, _params, signal) {
      return { content: [{ type: "text", text: JSON.stringify(await mcp.list(signal)) }], details: {} };
    },
  });
  pi.registerTool({
    name: "cardinal_call_tool", label: "Call Cardinal",
    description: "Call a Cardinal MCP tool by its exact name and arguments matching the input schema returned by cardinal_list_tools. Tool calls execute against the connected Cardinal account.",
    promptSnippet: "Execute a discovered Cardinal tool.",
    parameters: Type.Object({ name: Type.String(), arguments: Type.Record(Type.String(), Type.Unknown()) }),
    async execute(_id, params, signal) { return await mcp.call(params.name, params.arguments, signal); },
  });
  const f = DECISION_TOOL.fields;
  pi.registerTool({
    name: DECISION_TOOL.name, label: "Record decision", description: DECISION_TOOL.description,
    parameters: Type.Object({
      choice: Type.String({ description: f.choice }),
      question: Type.Optional(Type.String({ description: f.question })),
      why: Type.Optional(Type.String({ description: f.why })),
      alt: ids(f.alt),
      by: Type.Optional(Type.Union([Type.Literal("agent"), Type.Literal("user")], { description: f.by })),
      anchor: ids(f.anchor), follows: ids(f.follows), refines: ids(f.refines), supersedes: ids(f.supersedes),
      id: Type.Optional(Type.String({ description: f.id })),
    }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      const text = await recordDecision("pi", ctx.sessionManager.getSessionId(), ctx.cwd, params);
      return { content: [{ type: "text", text }], details: {} };
    },
  });
}
