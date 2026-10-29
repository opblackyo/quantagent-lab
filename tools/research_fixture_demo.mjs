import { spawn } from "node:child_process";
import fs from "node:fs";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const python = process.env.QUANTAGENT_PYTHON || path.join(root, ".venv", "Scripts", "python.exe");
const nextBin = path.join(root, "frontend", "node_modules", "next", "dist", "bin", "next");

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

if (!fs.existsSync(python) || !fs.existsSync(nextBin)) {
  console.error("Fixture demo needs the existing .venv interpreter and frontend/node_modules/next. Nothing was downloaded.");
  process.exit(1);
}

const apiPort = Number(process.env.QUANTAGENT_FIXTURE_API_PORT || (await freePort()));
const webPort = Number(process.env.QUANTAGENT_FIXTURE_WEB_PORT || (await freePort()));
if (apiPort === webPort || apiPort < 1 || webPort < 1) {
  console.error("Could not allocate two distinct loopback ports.");
  process.exit(1);
}
const dbName = `research-fixture-${process.pid}-${Date.now()}.sqlite3`;
const dbPath = path.join(root, "artifacts", dbName);
fs.mkdirSync(path.dirname(dbPath), { recursive: true });
if (fs.existsSync(dbPath)) {
  console.error("Refusing to reuse an existing fixture database name.");
  process.exit(1);
}
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

function stop() {
  for (const child of [web, api]) {
    if (child.pid && !child.killed) child.kill();
  }
}
process.on("SIGINT", stop);
process.on("SIGTERM", stop);
api.on("exit", stop);
web.on("exit", stop);
console.log(`Synthetic fixture UI: ${origin}/research`);
console.log(`Loopback API: http://127.0.0.1:${apiPort}/api/v1/research/readiness`);
console.log(`Ignored database name: artifacts/${dbName}`);
console.log("Labels: SYNTHETIC_FIXTURE. This is not a live provider and not real funding.");
