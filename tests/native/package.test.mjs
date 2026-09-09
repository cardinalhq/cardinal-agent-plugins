import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

const exec = promisify(execFile);

test("packed artifacts install outside the checkout and contain their Python runtime", async t => {
  const dir = await mkdtemp(join(tmpdir(), "cardinal-pack-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  for (const runtime of ["opencode", "pi"]) {
    const { stdout } = await exec("npm", ["pack", `./dist/native/${runtime}`, "--pack-destination", dir, "--json"]);
    const [manifest] = JSON.parse(stdout);
    assert(manifest.files.some(f => f.path === "lib/cardinal_core/otlp.py"));
    assert(manifest.files.some(f => f.path === "lib/cardinal_native.py"));
    assert(manifest.files.some(f => f.path === "LICENSE"));
    assert(!manifest.files.some(f => f.path.includes("__pycache__") || f.path.startsWith("tests/")));
    await exec("npm", ["install", "--prefix", dir, "--ignore-scripts", "--no-audit", "--no-fund", join(dir, manifest.filename)]);
    const installed = join(dir, "node_modules", "@cardinalhq", `${runtime}-plugin`);
    const env = { ...process.env, [`CARDINAL_${runtime.toUpperCase()}_HOME`]: join(dir, "empty-home") };
    const help = await exec(process.execPath, [join(installed, "bin", `cardinal-${runtime}.js`), "connect", "--help"], { env });
    assert(help.stdout.includes("--telemetry-only"));
    const py = await exec(process.env.CARDINAL_PYTHON || "python3", [join(installed, "lib", "cardinal_native.py"), "--runtime", runtime, "--help"], { env });
    assert(py.stdout.includes("disconnect"));
    if (runtime === "opencode") {
      const plugin = await import(pathToFileURL(join(installed, "index.js")));
      assert.equal(typeof plugin.default, "function");
    } else {
      const { DefaultResourceLoader } = await import("@earendil-works/pi-coding-agent");
      const loader = new DefaultResourceLoader({ cwd: dir, agentDir: join(dir, "pi-home"),
        additionalExtensionPaths: [join(installed, "index.ts")], noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true });
      await loader.reload();
      assert.deepEqual(loader.getExtensions().errors, []);
      assert.equal(loader.getExtensions().extensions.length, 1);
    }
    const secrets = await readFile(join(installed, "lib", "cardinal_native.py"), "utf8");
    assert(!secrets.includes(resolve("core")), "artifact has no absolute checkout dependency");
  }
});
