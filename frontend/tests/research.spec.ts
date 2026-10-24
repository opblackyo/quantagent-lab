import { test, expect } from "@playwright/test";
import { readFile } from "node:fs/promises";

test.describe.serial("research workstation", () => {
  let runId = "";
  test("create, inspect, compare, replay and export through the UI", async ({
    page,
  }, testInfo) => {
    test.setTimeout(120000);
    async function waitForResearch(id: string) {
      await expect
        .poll(
          async () => {
            const response = await page.request.get(
              `/api/v1/experiments/${id}`,
            );
            expect(response.ok()).toBeTruthy();
            return (await response.json()).status;
          },
          {
            timeout: 60000,
            message: "The asynchronous research job must complete",
          },
        )
        .toBe("COMPLETED");
    }
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto("/");
    await page.getByRole("button", { name: "建立實驗", exact: true }).click();
    await page
      .getByLabel("研究策略", { exact: true })
      .selectOption("multi_agent");
    await page.getByLabel("結束日期", { exact: true }).fill("2024-04-30");
    await page.getByRole("button", { name: "啟動研究", exact: true }).click();
    await expect(page).toHaveURL(/[?&]run=[0-9a-f-]+/);
    await waitForResearch(new URL(page.url()).searchParams.get("run")!);
    await expect(page.getByTestId("equity-chart")).toBeVisible();
    runId = new URL(page.url()).searchParams.get("run")!;
    expect(runId).toBeTruthy();
    await expect(
      page.getByText("DEMO：合成行情", { exact: false }),
    ).toBeVisible();
    await page.getByRole("link", { name: "Agent 決策", exact: true }).click();
    for (const stage of ["analysis", "critic", "decision"]) {
      await expect(page.getByTestId(`agent-${stage}`)).toBeVisible();
    }
    await page.getByLabel("決策日期", { exact: true }).selectOption("1");
    await expect(page.getByText("2024-01-16 開盤成交")).toBeVisible();
    await page.getByText("展開 JSON 紀錄", { exact: true }).click();
    await expect(page.locator("pre")).toContainText('"prompt_hash"');
    await page.screenshot({
      path: testInfo.outputPath("agents-desktop.png"),
      fullPage: true,
    });
    await page.getByRole("link", { name: "市場分析", exact: true }).click();
    await expect(
      page.getByTestId("market-chart").locator("canvas").first(),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "交易帳本", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("table").locator("tbody tr").first(),
    ).toBeVisible();
    await page.getByRole("link", { name: "策略比較", exact: true }).click();
    const beforeBatch = new URL(page.url()).searchParams.get("run");
    await page
      .getByRole("button", { name: "執行四組 Mock 比較", exact: true })
      .click();
    await expect
      .poll(() => new URL(page.url()).searchParams.get("run"))
      .not.toBe(beforeBatch);
    await waitForResearch(new URL(page.url()).searchParams.get("run")!);
    await expect(
      page.getByRole("button", { name: "執行四組 Mock 比較", exact: true }),
    ).toBeEnabled();
    await expect(
      page.getByRole("heading", { name: "資產曲線比較", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(4);
    await page.getByRole("link", { name: "實驗紀錄", exact: true }).click();
    const before = new URL(page.url()).searchParams.get("run");
    await page
      .getByRole("button", { name: "重播這份實驗", exact: true })
      .click();
    await expect
      .poll(() => new URL(page.url()).searchParams.get("run"))
      .not.toBe(before);
    await expect(
      page.getByText("Replay", { exact: true }).first(),
    ).toBeVisible();
    const replayId = new URL(page.url()).searchParams.get("run")!;
    await waitForResearch(replayId);
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("link", { name: "匯出 JSON", exact: true }).click();
    const download = await downloadPromise;
    const file = await download.path();
    const exported = JSON.parse(await readFile(file!, "utf-8"));
    expect(exported.result.metrics.llm_calls).toBe(0);
    expect(exported.result.metrics.llm_cost_usd).toBe(0);
    expect(exported.config.llm_mode).toBe("replay");
    expect(errors).toEqual([]);
  });

  test("mobile navigation, charts, trace and settings stay usable", async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`/?run=${runId}`);
    await expect(page.getByTestId("equity-chart")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
    await page.getByRole("link", { name: "Agent 決策", exact: true }).click();
    await expect(page.getByTestId("agent-decision")).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath("agents-mobile.png"),
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBeTruthy();
    await page.getByRole("link", { name: "研究設定", exact: true }).click();
    await page.getByRole("button", { name: "儲存預設值", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "已儲存", exact: true }),
    ).toBeVisible();
  });

  test("a lost create response retries the same experiment", async ({
    page,
  }) => {
    const before = await (await page.request.get("/api/v1/experiments")).json();
    const keys: string[] = [];
    let loseFirst = true;
    await page.route("**/api/v1/experiments", async (route) => {
      if (route.request().method() !== "POST") {
        await route.continue();
        return;
      }
      keys.push(route.request().headers()["x-idempotency-key"]);
      if (loseFirst) {
        loseFirst = false;
        await route.fetch();
        await route.abort();
      } else await route.continue();
    });
    await page.goto("/");
    await page.getByRole("button", { name: "建立實驗", exact: true }).click();
    await page
      .getByRole("spinbutton", { name: "初始資金（USD）", exact: true })
      .fill("222222");
    await page.getByLabel("結束日期", { exact: true }).fill("2024-04-30");
    await page.getByRole("button", { name: "啟動研究", exact: true }).click();
    await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
    await page.getByRole("button", { name: "啟動研究", exact: true }).click();
    await expect(page).toHaveURL(/[?&]run=[0-9a-f-]+/);
    await expect(page.getByTestId("equity-chart")).toBeVisible();
    expect(keys).toHaveLength(2);
    expect(keys[0]).toBe(keys[1]);
    const after = await (await page.request.get("/api/v1/experiments")).json();
    expect(after.length - before.length).toBe(1);
  });

  test("backend errors give a visible retry path", async ({ page }) => {
    await page.route("**/api/v1/experiments", (route) =>
      route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({ detail: "研究服務暫時無法連線" }),
      }),
    );
    await page.goto("/");
    await expect(
      page.getByRole("alert").filter({ hasText: "研究服務暫時無法連線" }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "重試", exact: true }),
    ).toBeVisible();
  });
});
