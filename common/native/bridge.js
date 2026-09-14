import { spawn } from "node:child_process";
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

/** Bounded, ordered queue. Network and Python failures never reject agent hooks. */
export function createBridge(runtime, { warn = (_message) => {}, run = runPython } = {}) {
  let pending = [], timer, active, warned = false;
  const warning = () => {
    if (!warned) {
      warned = true;
      try { warn("Cardinal telemetry could not run. Check Python 3.9+ and run the package's status command."); } catch { /* never break the agent */ }
    }
  };
  async function drain() {
    if (active) return active;
    active = (async () => {
      while (pending.length) {
        const batch = pending.splice(0, 256);
        try { await run(runtime, batch); } catch { warning(); }
      }
    })();
    try { await active; } finally { active = undefined; }
  }
  return {
    send(event) {
      if (pending.length >= 2048) { warning(); return; }
      pending.push(event);
      if (!timer) timer = setTimeout(() => { timer = undefined; void drain(); }, 20);
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
