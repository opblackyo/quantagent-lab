import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parsePorts, assertPortFree } from "./dev_options.mjs";

let ports;
try {
  ports = parsePorts(process.argv.slice(2));
  if (ports.help) {
    console.log("node tools/dev.mjs [--backend-port 8000] [--frontend-port 3000]");
    process.exit(0);
  }
  await Promise.all([assertPortFree(ports.backendPort), assertPortFree(ports.frontendPort)]);
} catch (error) {
  console.error(error.message);
  process.exit(1);
}

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const python = path.join(
  root,
  ".venv",
  process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
);
const next = path.join(root, "frontend/node_modules/next/dist/bin/next");
if (!existsSync(python) || !existsSync(next)) {
  console.error("先執行 uv sync --locked 與 npm --prefix frontend ci。");
  process.exit(1);
}
const backend = spawn(
  python,
  [
    "-m",
    "uvicorn",
    "app.main:app",
    "--app-dir",
    "backend",
    "--host",
    "127.0.0.1",
    "--port",
    String(ports.backendPort),
  ],
  {
    cwd: root,
    stdio: "inherit",
    env: { ...process.env, QUANTAGENT_LOCAL_ORIGINS: `http://127.0.0.1:${ports.frontendPort}` },
  },
);
const frontend = spawn(
  process.execPath,
  [next, "dev", "--hostname", "127.0.0.1", "--port", String(ports.frontendPort)],
  { cwd: path.join(root, "frontend"), stdio: "inherit", env: {
    ...process.env,
    BACKEND_URL: ports.customBackend ? `http://127.0.0.1:${ports.backendPort}` : (process.env.BACKEND_URL || "http://127.0.0.1:8000"),
  } },
);
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  for (const child of [backend, frontend]) {
    if (child.exitCode !== null || !child.pid) continue;
    if (process.platform === "win32")
      spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
    else child.kill("SIGTERM");
  }
  process.exitCode = code;
}
process.on("SIGINT", () => stop());
process.on("SIGTERM", () => stop());
for (const child of [backend, frontend]) {
  child.on("error", (error) => {
    console.error(error.message);
    stop(1);
  });
  child.on("exit", (code) => {
    if (!stopping) stop(code || 0);
  });
}
console.log(
  `QuantAgent Lab: http://127.0.0.1:${ports.frontendPort}   API: http://127.0.0.1:${ports.backendPort}/docs`,
);
console.log("按 Ctrl+C 停止本次啟動的前後端。");
