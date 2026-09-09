import { spawn } from "node:child_process";
import { pythonArgs } from "./bridge.js";

export function cli(runtime) {
  const [command = "--help", ...args] = process.argv.slice(2);
  const child = spawn(process.env.CARDINAL_PYTHON || "python3", pythonArgs(runtime, command, args), { stdio: "inherit" });
  child.on("error", () => { console.error("Cardinal requires Python 3.9+. Set CARDINAL_PYTHON to its executable path."); process.exitCode = 1; });
  child.on("exit", (code) => { process.exitCode = code ?? 1; });
}
