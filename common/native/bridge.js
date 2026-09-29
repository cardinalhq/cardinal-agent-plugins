import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export function agentHome(runtime, env = process.env) {
  if (env[`CARDINAL_${runtime.toUpperCase()}_HOME`]) return env[`CARDINAL_${runtime.toUpperCase()}_HOME`];
  if (runtime === "opencode") return join(env.XDG_CONFIG_HOME || join(homedir(), ".config"), "opencode");
  return env.PI_CODING_AGENT_DIR || join(homedir(), ".pi", "agent");
}

export async function connection(runtime) {
  const home = agentHome(runtime);
  try {
    const [state, secrets] = await Promise.all([
      readFile(join(home, "cardinal.json"), "utf8").then(JSON.parse),
      readFile(join(home, "cardinal-secrets.json"), "utf8").then(JSON.parse),
    ]);
    return { state, secrets };
  } catch (error) {
    if (error.code === "ENOENT") return undefined;
    throw new Error("Cardinal connection files are unreadable. Run cardinal status or reconnect.");
  }
}

export function pythonArgs(runtime, command, args = []) {
  const lib = dirname(fileURLToPath(import.meta.url));
  return ["-B", join(lib, "cardinal_native.py"), "--runtime", runtime, command, ...args];
}

/**
 * Bounded, ordered queue. Network and Python failures never reject agent hooks.
 * Bounded by count (2048 queued, 256 per batch) and, for callers that pass an
 * event's size to send(), by bytes (maxPendingBytes queued, maxBatchBytes per batch).
 */
export function createBridge(runtime, { warn = (_message) => {}, run = runPython, maxBatchBytes = Infinity,
  maxPendingBytes = Infinity, warning: warningText = "Cardinal telemetry could not run. Check Python 3.9+ and run the package's status command." } = {}) {
  let pending = [], pendingBytes = 0, timer, active, warned = false;
  const warning = () => {
    if (!warned) {
      warned = true;
      try { warn(warningText); } catch { /* never break the agent */ }
    }
  };
  async function drain() {
    if (active) return active;
    active = (async () => {
      while (pending.length) {
        let n = 0, bytes = 0;
        while (n < pending.length && n < 256 && (n === 0 || bytes + pending[n].size <= maxBatchBytes)) bytes += pending[n++].size;
        const batch = pending.splice(0, n);
        pendingBytes -= bytes;
        try { await run(runtime, batch.map((item) => item.event)); } catch { warning(); }
      }
    })();
    try { await active; } finally { active = undefined; }
  }
  return {
    send(event, size = 0) {
      if (pending.length >= 2048 || pendingBytes + size > maxPendingBytes) { warning(); return false; }
      pending.push({ event, size });
      pendingBytes += size;
      if (!timer) timer = setTimeout(() => { timer = undefined; void drain(); }, 20);
      return true;
    },
    async flush() {
      clearTimeout(timer); timer = undefined;
      await drain();
      if (pending.length) await drain();
    },
  };
}

/** Shared contract for the host-native decision tool; each host supplies its schema library. */
export const DECISION_TOOL = {
  name: "cardinal_record_decision",
  description: "Record a decision that constrains later work (a choice between approaches, a settled open question, or a call the user made) as Cardinal decision telemetry. Record choices, not progress, findings, or tool calls. Only works while Cardinal decision capture is on.",
  fields: {
    choice: "The option chosen, 2-7 words.",
    question: "The question this decision settles.",
    why: "One sentence on why this option won.",
    alt: "Options that were considered and rejected.",
    by: "Who made the call: agent (default) or user.",
    anchor: "Files, dir/, file::Symbol, or <kind>:<identifier>[@path] the decision governs.",
    follows: "Earlier decision ids this one only makes sense because of.",
    refines: "Earlier decision ids this one narrows.",
    supersedes: "Earlier decision ids this one replaces.",
    id: "Decision id; reuse an existing id to revise that decision.",
  },
};

/** Default `decision record` kill: well above cardinal_native's ~11.5s worst-case internal budget. */
export const DECISION_RECORD_TIMEOUT_MS = 20000;

/**
 * `decision` CLI invocation. Request/response, unlike the fire-and-forget telemetry queue.
 * The child leads its own process group so a timeout or host abort also stops its git/gh children.
 */
export function runDecision(runtime, args, { cwd, timeout = DECISION_RECORD_TIMEOUT_MS, signal, python = process.env.CARDINAL_PYTHON || "python3" } = {}) {
  return new Promise((resolve) => {
    let stdout = "", stderr = "", done = false, child, timer;
    const onAbort = () => stop();
    const finish = (code) => {
      if (done) return;
      done = true; clearTimeout(timer); signal?.removeEventListener?.("abort", onAbort);
      resolve({ code, stdout, stderr });
    };
    const stop = () => {
      if (child?.pid && child.exitCode === null && child.signalCode === null) {
        try { process.kill(process.platform === "win32" ? child.pid : -child.pid, "SIGKILL"); } catch { try { child.kill("SIGKILL"); } catch { /* already gone */ } }
      }
      finish(-1);
    };
    if (signal?.aborted) return finish(-1);
    try {
      child = spawn(python, pythonArgs(runtime, "decision", args), {
        cwd, stdio: ["ignore", "pipe", "pipe"], windowsHide: true, detached: process.platform !== "win32",
      });
    } catch { return finish(-1); }
    timer = setTimeout(stop, timeout);
    signal?.addEventListener?.("abort", onAbort, { once: true });
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("error", () => finish(-1));
    child.on("close", (code) => finish(code ?? -1));
  });
}

function parseOverride(value) {
  const lowered = typeof value === "string" ? value.trim().toLowerCase() : undefined;
  if (["1", "true", "on", "yes"].includes(lowered)) return true;
  if (["0", "false", "off", "no"].includes(lowered)) return false;
  return undefined;
}

/**
 * Mirrors cardinal_core.decisions.is_enabled (env override, then config.json) so the common
 * "capture off" case never spawns Python per prompt. Python re-checks before acting.
 */
export async function decisionsEnabled(runtime, env = process.env) {
  const forced = parseOverride(env.CARDINAL_DECISIONS);
  if (forced !== undefined) return forced;
  try {
    const config = JSON.parse(await readFile(join(agentHome(runtime, env), "cardinal", "decisions", "config.json"), "utf8"));
    return config?.enabled === true;
  } catch { return false; }
}

/** Decision instructions + this session's ledger, or undefined when capture is off or anything fails. */
export async function decisionContext(runtime, sessionId, cwd, tool, { timeout = 1500 } = {}) {
  try {
    if (!sessionId || !(await decisionsEnabled(runtime))) return undefined;
    const { code, stdout } = await runDecision(runtime, ["context", `--session=${sessionId}`, ...(tool ? [`--tool=${tool}`] : [])], { cwd, timeout });
    return code === 0 && stdout.trim() ? stdout.trim() : undefined;
  } catch { return undefined; }
}

/** Translate tool parameters to CLI flags. `--flag=value` keeps values that start with "-" intact. */
export function decisionRecordArgs(sessionId, params = {}) {
  const args = ["record", `--session=${sessionId}`, `--choice=${params.choice ?? ""}`];
  for (const key of ["question", "why", "by", "id"]) {
    if (typeof params[key] === "string" && params[key]) args.push(`--${key}=${params[key]}`);
  }
  for (const key of ["alt", "anchor", "follows", "refines", "supersedes"]) {
    for (const value of Array.isArray(params[key]) ? params[key] : []) args.push(`--${key}=${value}`);
  }
  return args;
}

/**
 * Runs `decision record`; resolves with { message, context } (the refreshed prompt context).
 * Throws so the host marks a failed or aborted call as an error.
 */
export async function recordDecision(runtime, sessionId, cwd, params, { tool, signal, timeout } = {}) {
  if (!sessionId) throw new Error("Cardinal could not determine the session id.");
  const args = [...decisionRecordArgs(sessionId, params), "--json", ...(tool ? [`--tool=${tool}`] : [])];
  const { code, stdout, stderr } = await runDecision(runtime, args, { cwd, signal, timeout });
  if (signal?.aborted) throw new Error("Decision recording was cancelled.");
  if (code !== 0) throw new Error((stderr || stdout).trim() || "Cardinal could not record the decision. Check Python 3.9+.");
  try {
    const result = JSON.parse(stdout);
    return { message: String(result.message ?? ""), context: typeof result.context === "string" ? result.context : undefined };
  } catch {
    return { message: stdout.trim(), context: undefined };
  }
}

async function runPython(runtime, events) {
  const conn = await connection(runtime);
  if (!conn?.state.ingest_endpoint || !conn.secrets.ingest_api_key) return;
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.CARDINAL_PYTHON || "python3", pythonArgs(runtime, "telemetry"), {
      stdio: ["pipe", "ignore", "ignore"], windowsHide: true,
    });
    const timeout = setTimeout(() => child.kill(), 10000);
    child.on("error", reject);
    child.on("close", (code) => { clearTimeout(timeout); code === 0 ? resolve() : reject(new Error("telemetry failed")); });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(events));
  });
}

// ---------------------------------------------------------------------------
// Evidence capture: every tool call, kept locally so a storyboard can cite it.
// The JS side only forwards the call (input + result) to cardinal_native.py
// `evidence-capture`, which runs the shared generic pipeline
// (cardinal_core.evidence_capture: sensitivity gate, scrub, cap, spool). No
// network, no connection needed. The id is computed here too, so the plugin
// can show it inline before Python has written the file.
// ---------------------------------------------------------------------------

const SESSION_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;
const OFF = new Set(["0", "false", "off", "no"]);

/** cardinal.evidence.v2 id: ev_ + sha256("cardinal.evidence.v2|runtime|session|tool_use_id")[:12].
 *  Pinned to the Python implementation by core/tests/testdata/evidence_id_vectors.json. */
export function evidenceId(runtime, sessionId, toolUseId) {
  if (typeof toolUseId !== "string" || !toolUseId || toolUseId.length > 256) return undefined;
  const session = typeof sessionId === "string" && SESSION_ID_RE.test(sessionId) ? sessionId : "no-session";
  const h = createHash("sha256").update(["cardinal.evidence.v2", runtime || "", session, toolUseId].join("|"), "utf8").digest("hex");
  return `ev_${h.slice(0, 12)}`;
}

/** Mirrors cardinal_core.evidence.capture_disabled so the "capture off" case spawns nothing. */
export function evidenceCaptureEnabled(env = process.env) {
  if (OFF.has(String(env.CARDINAL_EVIDENCE_CAPTURE ?? "").trim().toLowerCase())) return false;
  try { return !existsSync(join(env.HOME || homedir(), ".cardinal", "evidence", "disabled")); } catch { return false; }
}

/** CARDINAL_EVIDENCE_CONTEXT=0 keeps capturing but shows no ids inline. */
export function evidenceContextEnabled(env = process.env) {
  return !OFF.has(String(env.CARDINAL_EVIDENCE_CONTEXT ?? "").trim().toLowerCase());
}

/** The line shown to the agent beside a captured result: a hint on the first capture of a session, the bare id after. */
export function evidenceLine(runtime, id, first) {
  if (!first) return `[evidence:${id}]`;
  return `[evidence:${id}] Cardinal keeps this result on this machine. Every tool result in this session gets an id like this and can be cited in a storyboard: \`cardinal-${runtime} evidence promote --storyboard <id> ev_...\` uploads only what you promote (only what a scene cites; a repeat prints the receipt it already has); \`cardinal-${runtime} evidence find <text>\` looks an id up. A call that touched something sensitive is kept only as a withheld stub (\`cardinal-${runtime} evidence list --withheld\`).`;
}

async function runEvidencePython(runtime, events) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.CARDINAL_PYTHON || "python3", pythonArgs(runtime, "evidence-capture"), {
      stdio: ["pipe", "ignore", "ignore"], windowsHide: true,
    });
    const timeout = setTimeout(() => child.kill(), 10000);
    child.on("error", reject);
    child.on("close", (code) => { clearTimeout(timeout); code === 0 ? resolve() : reject(new Error("evidence capture failed")); });
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(events));
  });
}

// A tool result can be any size (a file read, a build log). What reaches
// Python is bounded so one huge call cannot sink a batch (Python reads at
// most 64 MiB of stdin) or the plugin's memory: each result field is cut to
// MAX_EVIDENCE_FIELD (Python keeps at most 256 KiB of it anyway, marked
// truncated); an input too large to send whole cannot be checked for secrets,
// so that call is sent as a withheld stub instead (never an unchecked one).
export const MAX_EVIDENCE_FIELD = 1 << 20;
export const MAX_EVIDENCE_EVENT = 4 << 20;
const MAX_EVIDENCE_BATCH = 16 << 20;
// Queued bytes. A stub (a few hundred bytes) is queued past it; the count
// bound (2048) still holds.
const MAX_EVIDENCE_PENDING = 64 << 20;

function jsonBytes(value) {
  try { return Buffer.byteLength(JSON.stringify(value) ?? "null", "utf8"); } catch { return Infinity; }
}

function cutText(s, max) {
  return s.length > max ? s.slice(0, max) : s;
}

function cutField(value) {
  if (typeof value === "string") return cutText(value, MAX_EVIDENCE_FIELD);
  if (Array.isArray(value) && value.every((b) => b && typeof b === "object" && typeof b.type === "string")) {
    // Content blocks (Pi): text kept up to the budget, anything else reduced to its type.
    let left = MAX_EVIDENCE_FIELD;
    return value.slice(0, 1024).map((b) => {
      if (b.type === "text" && typeof b.text === "string") {
        const text = cutText(b.text, Math.max(0, left));
        left -= text.length;
        return { type: "text", text };
      }
      return { type: b.type };
    });
  }
  let text;
  try { text = JSON.stringify(value); } catch { text = String(value); }
  return cutText(text ?? "", MAX_EVIDENCE_FIELD);
}

/** [event, bytes]: the event as sent to Python, bounded (see MAX_EVIDENCE_EVENT). */
export function boundEvidenceEvent(event) {
  const size = jsonBytes(event);
  if (size <= MAX_EVIDENCE_EVENT) return [event, size];
  const out = { ...event };
  for (const key of ["output", "content", "error"]) {
    if (key in out && jsonBytes(out[key]) > MAX_EVIDENCE_FIELD) out[key] = cutField(out[key]);
  }
  const cut = jsonBytes(out);
  if (cut <= MAX_EVIDENCE_EVENT) return [out, cut];
  const stub = evidenceStub(event, "size");
  return [stub, jsonBytes(stub)];
}

function evidenceStub(event, why) {
  return { kind: "evidence", unreadable: why, session_id: event.session_id, cwd: event.cwd,
    tool_name: event.tool_name, tool_call_id: event.tool_call_id, is_error: event.is_error === true };
}

/** Bounded (count and bytes), ordered, fire-and-forget queue of tool calls for the local evidence spool. */
export function createEvidenceSink(runtime, { warn = (_message) => {}, run = runEvidencePython } = {}) {
  const bridge = createBridge(runtime, { warn, run, maxBatchBytes: MAX_EVIDENCE_BATCH, maxPendingBytes: MAX_EVIDENCE_PENDING,
    warning: "Cardinal evidence capture could not run. Check Python 3.9+ and run the package's status command." });
  return {
    send(event) {
      try {
        const [bounded, size] = boundEvidenceEvent(event);
        // A backlog (Python slower than the tools) keeps the call as a
        // withheld stub rather than dropping it: its id was already shown.
        if (!bridge.send(bounded, size)) {
          bridge.send(evidenceStub(event, "backlog"), 0);
        }
      } catch { /* never break the agent */ }
    },
    flush: () => bridge.flush(),
  };
}
