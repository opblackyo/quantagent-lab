import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

export default defineConfig({
  testDir: "./tests",
  testIgnore: ["**/research-fixture.spec.ts", "**/research-live-composition.spec.ts"],
  fullyParallel: false,
  workers: 1,
  timeout: 60000,
  expect: { timeout: 15000 },
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:3001",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command:
        "uv run --no-sync uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8001",
      cwd: path.resolve(__dirname, ".."),
      url: "http://127.0.0.1:8001/api/v1/health",
      reuseExistingServer: false,
      env: {
        QUANTAGENT_DB_PATH: process.env.QUANTAGENT_E2E_DB || path.resolve(__dirname, "../artifacts/e2e.sqlite3"),
        QUANTAGENT_LOCAL_ORIGINS: "http://127.0.0.1:3001",
      },
      timeout: 60000,
    },
    {
      command: "npm run dev -- --port 3001",
      url: "http://127.0.0.1:3001",
      reuseExistingServer: false,
      env: { BACKEND_URL: "http://127.0.0.1:8001" },
      timeout: 120000,
    },
  ],
});
