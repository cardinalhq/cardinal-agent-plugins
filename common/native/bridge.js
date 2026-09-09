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
