import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

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
    "8000",
  ],
  { cwd: root, stdio: "inherit" },
);
const frontend = spawn(
  process.execPath,
  [next, "dev", "--hostname", "127.0.0.1", "--port", "3000"],
  { cwd: path.join(root, "frontend"), stdio: "inherit" },
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
  "QuantAgent Lab: http://127.0.0.1:3000   API: http://127.0.0.1:8000/docs",
);
console.log("按 Ctrl+C 停止本次啟動的前後端。");
