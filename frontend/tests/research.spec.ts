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
    const previousIds = new Set(before.map((run: { run_id: string }) => run.run_id));
    expect(after.filter((run: { run_id: string }) => !previousIds.has(run.run_id))).toHaveLength(1);
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

  test("comparison keeps a pilot group isolated and labels deterministic baselines", async ({ page }) => {
    test.setTimeout(120000);
    const group = `offline-ui-${crypto.randomUUID()}`;
    const ids: string[] = [];
    for (const strategy of ["buy_hold", "ma_crossover", "single_agent", "multi_agent"]) {
      const response = await page.request.post("/api/v1/experiments", {
        data: { strategy_id: strategy, start_date: "2023-01-01", end_date: "2023-01-31", llm_mode: "mock", research_group_id: group },
      });
      expect(response.ok()).toBeTruthy();
      ids.push((await response.json()).run_id);
    }
    for (const id of ids) {
      await expect.poll(async () => (await (await page.request.get(`/api/v1/experiments/${id}`)).json()).status, { timeout: 60000 }).toBe("COMPLETED");
    }
    // A newer zero-cost Replay must not replace its source in a source comparison.
    const single = await (await page.request.get(`/api/v1/experiments/${ids[2]}`)).json();
    const replayResponse = await page.request.post("/api/v1/experiments", {
      data: { ...single.config, llm_mode: "replay", replay_source_run_id: ids[2] },
    });
    expect(replayResponse.ok()).toBeTruthy();
    const replayId = (await replayResponse.json()).run_id;
    await expect.poll(async () => (await (await page.request.get(`/api/v1/experiments/${replayId}`)).json()).status, { timeout: 60000 }).toBe("COMPLETED");
    // A newer, otherwise matching run must not replace a strategy from this group.
    const other = await page.request.post("/api/v1/experiments", {
      data: { strategy_id: "single_agent", start_date: "2023-01-01", end_date: "2023-01-31", llm_mode: "mock", research_group_id: "another-group" },
    });
    expect(other.ok()).toBeTruthy();
    await page.goto(`/backtests?run=${ids[3]}`);
    await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(4);
    await expect(page.getByRole("cell", { name: "Baseline", exact: true })).toHaveCount(2);
    await expect(page.getByRole("cell", { name: "mock", exact: true })).toHaveCount(2);
    await expect(page.getByRole("cell", { name: "replay", exact: true })).toHaveCount(0);
    const compared = await page.request.post("/api/v1/experiments/compare", { data: { run_ids: ids } });
    expect(compared.ok()).toBeTruthy();
    expect((await compared.json()).runs.every((r: { config: { research_group_id: string } }) => r.config.research_group_id === group)).toBeTruthy();
    // Next's route announcer also has role=alert; scope errors to app content.
    await expect(page.locator('main [role="alert"]')).toHaveCount(0);
    for (const id of ids) {
      await page.goto(`/?run=${id}`);
      await expect(page.getByLabel("目前實驗", { exact: true })).toHaveValue(id);
      await expect(page.getByTestId("equity-chart")).toBeVisible();
    }
    await page.getByRole("link", { name: "Agent 決策", exact: true }).click();
    for (const role of ["analysis", "critic", "decision"]) {
      await expect(page.getByTestId(`agent-${role}`)).toContainText("mock-rules-v1");
    }
    await page.getByRole("link", { name: "市場分析", exact: true }).click();
    await expect(page.getByRole("heading", { name: "交易帳本", exact: true })).toBeVisible();
    await page.getByRole("link", { name: "實驗紀錄", exact: true }).click();
    const downloadEvent = page.waitForEvent("download");
    await page.getByRole("link", { name: "匯出 JSON", exact: true }).click();
    const downloaded = await downloadEvent;
    const exported = JSON.parse(await readFile((await downloaded.path())!, "utf-8"));
    expect(exported.config.research_group_id).toBe(group);
    expect(exported.result.metrics.llm_calls).toBe(0);
    expect(exported.result.decision_origin).toBe("mock");
  });
  test("pilot costs distinguish confirmed deductions, estimates and unknown reservations", async ({ page }) => {
    // Synthetic UI billing fixture only; never load a project key or call a gateway.
    await page.route("**/api/v1/experiments", async (route) => {
      const response = await route.fetch();
      const runs = await response.json();
      const run = runs.find((value: { run_id: string }) => value.run_id === runId);
      run.config.research_group_id = "fixture-billing-pilot";
      await route.fulfill({ response, json: runs });
    });
    await page.route("**/api/v1/pilots/fixture-billing-pilot/budget", async (route) => {
      await route.fulfill({ json: {
        attempts: 2, max_calls: 32, limit_usd: 2,
        confirmed_actual_usd: 0.00054, usage_estimated_usd: 0.0001,
        unknown_reserved_usd: 0.01, charged_or_reserved_usd: 0.01054,
        invoiced_actual_usd: null,
      } });
    });
    await page.goto(`/?run=${runId}`);
    const costs = page.getByTestId("pilot-cost-reconciliation");
    await expect(costs).toContainText("已核對扣款 $0.00054");
    await expect(costs).toContainText("用量估計 $0.0001");
    await expect(costs).toContainText("待確認預留 $0.01");
    await expect(page.getByTestId("pilot-budget")).toContainText("2/32 次");
  });

  test("gateway attribution explicitly identifies the unverified upstream label", async ({ page }) => {
    // UI-only synthetic fixture. No gateway credential or request is used.
    await page.route(`**/api/v1/experiments/${runId}/decisions`, async (route) => {
      const response = await route.fetch();
      const decisions = await response.json();
      for (const decision of decisions) {
        for (const call of decision.calls) {
          call.provider_attribution = { provider: "kuaiapi", group: "offline-ui-fixture", identity_scope: "gateway-reported-label-only" };
        }
      }
      await route.fulfill({ response, json: decisions });
    });
    await page.goto(`/agents?run=${runId}`);
    await expect(page.getByText("DEMO：合成行情", { exact: false })).toBeVisible();
    for (const role of ["analysis", "critic", "decision"]) {
      await expect(page.getByTestId(`agent-${role}`).getByTestId("provider-attribution")).toContainText("gateway 標籤，上游未驗證");
      await expect(page.getByTestId(`agent-${role}`)).toContainText("offline-ui-fixture");
    }
  });
  test("source archives show unavailable Git metadata without claiming local edits", async ({ page }) => {
    await page.route("**/api/v1/experiments", async (route) => {
      const response = await route.fetch();
      const runs = await response.json();
      const run = runs.find((value: { run_id: string }) => value.run_id === runId);
      run.code_commit = null;
      run.code_dirty = true; // Existing backend conservatively records unknown Git state.
      await route.fulfill({ response, json: runs });
    });
    await page.goto(`/runs?run=${runId}`);
    await expect(page.getByText("無 Git metadata · 請以來源雜湊核對", { exact: true })).toBeVisible();
    await expect(page.getByText("含尚未提交修改", { exact: false })).toHaveCount(0);
    await expect(page.getByText("來源程式雜湊", { exact: true })).toBeVisible();
  });
});
