import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.FIXTURE_WEB_URL || "http://127.0.0.1:9";

export default defineConfig({
  testDir: "./tests",
  testMatch: "research-fixture.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 180000,
  expect: { timeout: 20000 },
  reporter: "list",
  use: {
    baseURL,
    trace: "off",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
