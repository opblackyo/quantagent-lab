// Record only an isolated synthetic-only, no-paid-API demonstration instance.
import { createRequire } from "node:module";
import { mkdir, copyFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const require = createRequire(path.join(root, "frontend/package.json"));
const { chromium, expect } = require("@playwright/test");
const baseURL = process.argv[2] || "http://127.0.0.1:3000";
const url = new URL(baseURL);
if (!["127.0.0.1", "localhost"].includes(url.hostname))
  throw new Error("Local demo only");
const datasets = await (await fetch(`${baseURL}/api/v1/datasets`)).json();
const caps = await (
  await fetch(`${baseURL}/api/v1/system/capabilities`)
).json();
const runs = await (await fetch(`${baseURL}/api/v1/experiments`)).json();
if (
  !datasets.length ||
  datasets.some((d) => d.kind !== "synthetic" || !d.redistributable) ||
  caps.api.available ||
  runs.length
)
  throw new Error(
    "Capture requires an empty synthetic-only instance with paid API disabled",
  );

const output = path.join(root, "docs/images");
const videoDir = path.join(root, "artifacts/demo-recording");
await mkdir(output, { recursive: true });
const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 },
  reducedMotion: "reduce",
  recordVideo: { dir: videoDir, size: { width: 1440, height: 1000 } },
});
const page = await context.newPage();
const video = page.video();
const errors = [];
page.on("pageerror", (e) => errors.push(e.message));
async function capture(name) {
  await page.getByText("DEMO：合成行情", { exact: false }).waitFor();
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(1500); // Reading time in the public demonstration video.
  await page.screenshot({
    path: path.join(output, name + ".png"),
    fullPage: true,
    animations: "disabled",
  });
}
try {
  await page.goto(baseURL);
  await page.getByRole("button", { name: "建立實驗", exact: true }).click();
  await page
    .getByLabel("研究策略", { exact: true })
    .selectOption("multi_agent");
  await page.getByLabel("結束日期", { exact: true }).fill("2024-04-30");
  await page.waitForTimeout(1200);
  await page.getByRole("button", { name: "啟動研究", exact: true }).click();
  await expect(page).toHaveURL(/[?&]run=[0-9a-f-]+/);
  await page.getByTestId("equity-chart").waitFor({ timeout: 60000 });
  await capture("dashboard");
  await page.getByRole("link", { name: "Agent 決策", exact: true }).click();
  await page.getByTestId("agent-analysis").waitFor();
  await page.getByLabel("決策日期", { exact: true }).selectOption("1");
  await capture("agents");
  await page.getByText("展開 JSON 紀錄", { exact: true }).click();
  await page.locator("pre").scrollIntoViewIfNeeded();
  await page.waitForTimeout(1500);
  await page.getByRole("link", { name: "市場分析", exact: true }).click();
  await page.getByTestId("market-chart").locator("canvas").first().waitFor();
  await capture("market");
  await page.getByRole("link", { name: "策略比較", exact: true }).click();
  const before = new URL(page.url()).searchParams.get("run");
  await page
    .getByRole("button", { name: "執行四組 Mock 比較", exact: true })
    .click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("run"), { timeout: 60000 })
    .not.toBe(before);
  await page
    .getByRole("heading", { name: "資產曲線比較", exact: true })
    .waitFor({ timeout: 60000 });
  await capture("backtests");
  await page.getByRole("link", { name: "實驗紀錄", exact: true }).click();
  const source = new URL(page.url()).searchParams.get("run");
  await page.getByRole("button", { name: "重播這份實驗", exact: true }).click();
  await expect
    .poll(() => new URL(page.url()).searchParams.get("run"))
    .not.toBe(source);
  await page.getByText("Replay", { exact: true }).first().waitFor();
  await page.waitForTimeout(2500);
  if (errors.length)
    throw new Error("Page errors occurred; do not publish media");
} finally {
  await context.close();
  await browser.close();
}
await copyFile(await video.path(), path.join(output, "walkthrough.webm"));
console.log(
  "Synthetic demo screenshots/video recorded. Visually review before publication.",
);
