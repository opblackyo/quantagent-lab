import { expect, test } from "@playwright/test";
import fs from "node:fs";

const MARKERS = [
  "synthetic-offline-secret-marker",
  "synthetic-live-a-group",
  "synthetic-live-b-group",
  "synthetic-live-a-cohort",
  "synthetic-live-b-cohort",
  "synthetic-live-price-ref",
  "synthetic-live-binding-ref",
  "kuaiapi.net",
  "api.openai.com",
];

test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  page.on("response", async (response) => {
    const text = await response.text().catch(() => "");
    for (const marker of MARKERS) expect(text, response.url()).not.toContain(marker);
  });
});

test("offline composition starts only after preview and records one then three posts", async ({ page }) => {
  await page.goto("/research");
  await expect(page.getByTestId("research-banner")).toContainText("合成夾具");
  await expect(page.getByTestId("research-posts")).toContainText("夾具傳輸 0");
  await expect(page.getByTestId("research-study")).toHaveCount(0);
  await page.getByTestId("research-transport").selectOption("live");
  await page.getByTestId("research-profile").selectOption("fixture-success");
  await page.getByTestId("research-protocol").selectOption("acceptance-stage-a-v1");
  await page.getByTestId("research-start").fill("2023-01-27");
  await page.getByTestId("research-end").fill("2023-01-31");
  await page.getByTestId("research-attempts").fill("1");
  await page.getByTestId("research-deadline").fill("45");
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-live-readiness")).toContainText("不能核准花費");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await expect(page.getByTestId("research-study")).toHaveCount(0);

  await page.getByTestId("research-profile").selectOption("synthetic-live");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-live-readiness")).toContainText("離線組成");
  await expect(page.getByTestId("research-scope")).toHaveText("階段 A · 單代理 1");
  await expect(page.getByTestId("research-nominal")).toHaveText("1 / 1");
  await expect(page.getByTestId("research-start-button")).toBeEnabled();
  await expect(page.getByTestId("research-study")).toHaveCount(0);
  const stageAHash = await page.getByTestId("research-manifest").innerText();

  await page.getByTestId("research-deadline").fill("46");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await page.getByTestId("research-deadline").fill("45");
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-manifest")).toHaveText(stageAHash);
  await expect(page.getByTestId("research-start-button")).toBeEnabled();
  await page.getByTestId("research-start-button").click();
  await expect(page.getByTestId("research-state")).toContainText("COMPLETED", { timeout: 90000 });
  await expect(page.getByTestId("research-state")).toContainText("組成傳輸 1");
  await expect(page.getByTestId("coverage-single_agent")).toHaveText("COMPLETED");
  await expect(page.getByTestId("coverage-multi_agent")).toHaveCount(0);
  await expect(page.getByTestId("research-unknown")).toHaveText("0");
  await expect(page.getByTestId("research-invoice")).toHaveText("null");
  await expect(page.getByTestId("research-estimated")).not.toHaveText("—");
  await expect(page.getByTestId("research-billing-note")).toContainText("不是發票");
  await page.getByTestId("research-replay").click();
  await expect(page.getByTestId("research-replays")).toContainText("不取代來源");
  await expect(page.getByTestId("research-state")).toContainText("組成傳輸 1");
  const downloadPromise = page.waitForEvent("download");
  await page.getByTestId("research-export").click();
  const download = await downloadPromise;
  const exported = JSON.parse(fs.readFileSync((await download.path()) as string, "utf8"));
  const raw = JSON.stringify(exported);
  for (const marker of MARKERS) expect(raw).not.toContain(marker);
  expect(exported.real_spending_authorized).toBe(false);
  expect(exported.report.cost.invoiced_actual_usd).toBeNull();
  expect(exported.report.sources.map((item: { strategy: string }) => item.strategy)).toEqual(["single_agent"]);

  await page.getByTestId("research-protocol").selectOption("acceptance-stage-b-v1");
  await page.getByTestId("research-attempts").fill("3");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-scope")).toHaveText("階段 B · 多代理 3");
  await expect(page.getByTestId("research-nominal")).toHaveText("3 / 3");
  const stageBHash = await page.getByTestId("research-manifest").innerText();
  expect(stageBHash).not.toBe(stageAHash);
  await expect(page.getByTestId("research-start-button")).toBeEnabled();
  await page.getByTestId("research-start-button").click();
  await expect(page.getByTestId("research-state")).toContainText("COMPLETED", { timeout: 90000 });
  await expect(page.getByTestId("research-state")).toContainText("組成傳輸 3");
  await expect(page.getByTestId("coverage-multi_agent")).toHaveText("COMPLETED");
  await expect(page.getByTestId("coverage-single_agent")).toHaveCount(0);
});
