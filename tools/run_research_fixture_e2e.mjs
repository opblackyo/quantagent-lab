import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const python = process.env.QUANTAGENT_PYTHON || path.join(root, ".venv", "Scripts", "python.exe");
const nextBin = path.join(root, "frontend", "node_modules", "next", "dist", "bin", "next");
const playwright = path.join(root, "frontend", "node_modules", "@playwright", "test", "cli.js");

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close(() => resolve(port));
    });
  });
}

function childEnv(extra) {
  const env = { ...process.env, ...extra };
  for (const key of Object.keys(env)) {
    if (key.startsWith("OPENAI_") || key.startsWith("KUAIAPI_") || key.startsWith("LLM_") || key === "QUANTAGENT_DB_PATH")
      delete env[key];
  }
  return env;
}

async function waitFor(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let last = "not-ready";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return;
      last = String(response.status);
    } catch (error) {
      last = error instanceof Error ? error.name : "fetch-failed";
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`timed out waiting for ${url} (${last})`);
}

if (!fs.existsSync(python) || !fs.existsSync(nextBin) || !fs.existsSync(playwright)) {
  console.error("Fixture e2e needs the existing interpreter, Next, and Playwright CLI. Nothing was downloaded.");
  process.exit(1);
}

const apiPort = await freePort();
const webPort = await freePort();
const dbName = `research-fixture-e2e-${process.pid}-${Date.now()}.sqlite3`;
const dbPath = path.join(root, "artifacts", dbName);
fs.mkdirSync(path.dirname(dbPath), { recursive: true });
const origin = `http://127.0.0.1:${webPort}`;
const api = spawn(python, ["-m", "uvicorn", "app.research_runtime:create_from_environ", "--factory", "--host", "127.0.0.1", "--port", String(apiPort), "--app-dir", "backend"], {
  cwd: root,
  env: childEnv({ QUANTAGENT_FIXTURE_DB: dbPath, QUANTAGENT_LOCAL_ORIGINS: origin }),
  stdio: "inherit",
});
const web = spawn(process.execPath, [nextBin, "dev", "--hostname", "127.0.0.1", "--port", String(webPort)], {
  cwd: path.join(root, "frontend"),
  env: childEnv({ BACKEND_URL: `http://127.0.0.1:${apiPort}` }),
  stdio: "inherit",
});
let stopping = false;
function stop() {
  if (stopping) return;
  stopping = true;
  for (const child of [web, api]) {
    if (child.pid && !child.killed) child.kill();
  }
}
process.on("SIGINT", stop);
process.on("SIGTERM", stop);

try {
  await waitFor(`http://127.0.0.1:${apiPort}/api/v1/research/readiness`, 30000);
  await waitFor(`${origin}/research`, 90000);
  const runner = spawn(process.execPath, [playwright, "test", "-c", "playwright.fixture.config.ts", ...process.argv.slice(2)], {
    cwd: path.join(root, "frontend"),
    env: childEnv({ FIXTURE_WEB_URL: origin }),
    stdio: "inherit",
  });
  const code = await new Promise((resolve) => runner.on("exit", resolve));
  stop();
  process.exit(code ?? 1);
} catch (error) {
  stop();
  console.error(error instanceof Error ? error.message : "fixture e2e failed");
  process.exit(1);
}
