import { expect, test } from "@playwright/test";
import fs from "node:fs";

const MARKERS = [
  "sk-syntheticfixturesecret",
  "synthetic-fixture-token",
  "fixture-success-key",
  "fixture-delayed-key",
  "fixture-timeout-key",
  "fixture-drift-key",
  "fixture-cancel-key",
  "fixture-acceptance-a-key",
  "fixture-acceptance-b-key",
  "fixture-success-cohort",
  "fixture-binding-ref",
  "fixture-price-ref",
  "kuaiapi.net",
  "api.openai.com",
  "account-secret-marker",
  "ops@example.com",
  "fixture-group-marker",
  "grant-fixture-",
];

test.describe.configure({ mode: "serial" });

test.beforeEach(async ({ page }) => {
  page.on("request", (request) => {
    const data = request.postData() || "";
    for (const marker of MARKERS) expect(data, request.url()).not.toContain(marker);
    expect(data).not.toContain('"approved"');
  });
  page.on("response", async (response) => {
    const text = await response.text().catch(() => "");
    for (const marker of MARKERS) expect(text, response.url()).not.toContain(marker);
  });
});

async function openResearch(page: import("@playwright/test").Page) {
  await page.goto("/research");
  await expect(page.getByTestId("research-banner")).toContainText("合成夾具");
  await expect(page.getByTestId("research-profile")).toContainText("fixture-success");
}

async function preview(page: import("@playwright/test").Page, profile: string) {
  await page.getByTestId("research-profile").selectOption(profile);
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-manifest")).toHaveText(/^[a-f0-9]{64}$/);
}

test("mobile preview and a changed setting do not dispatch", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await openResearch(page);
  await expect(page.getByTestId("research-posts")).toContainText("夾具傳輸 0");
  await preview(page, "fixture-success");
  await expect(page.getByTestId("research-posts")).toContainText("夾具傳輸 0");
  const width = await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1);
  expect(width).toBeTruthy();
  await page.getByTestId("research-end").fill("2023-01-15");
  await expect(page.getByText("設定已變更，請重新預覽")).toBeVisible();
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  const storage = await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage));
  for (const marker of MARKERS) expect(storage).not.toContain(marker);
});

test("four source fixture runs from preview through replay and export", async ({ page }) => {
  await openResearch(page);
  await preview(page, "fixture-success");
  await expect(page.getByTestId("research-nominal")).toContainText("16");
  await expect(page.getByTestId("research-warnings")).toBeVisible();
  await page.getByTestId("research-budget").fill("0.5");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await page.getByTestId("research-budget").fill("1");
  await expect(page.getByTestId("research-start-button")).toBeEnabled();
  const manifest = await page.getByTestId("research-manifest").innerText();
  const ids = await page.evaluate(async (hash) => {
    const datasets = await fetch("/api/v1/datasets").then((response) => response.json());
    const dataset = datasets.find((item: { id: string }) => item.id === "synthetic-spy-v1");
    const body = {
      manifest_hash: hash,
      idempotency_key: hash,
      request: {
        profile_id: "fixture-success",
        dataset_id: "synthetic-spy-v1",
        expected_dataset_hash: dataset.sha256,
        start_date: "2023-01-01",
        end_date: "2023-01-31",
        requested_budget_usd: "1",
        requested_max_attempts: 32,
      },
    };
    const send = () =>
      fetch("/api/v1/research/studies", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then((response) => response.json());
    const [left, right] = await Promise.all([send(), send()]);
    return [left.study_id, right.study_id];
  }, manifest);
  expect(ids[0]).toBeTruthy();
  expect(ids[0]).toBe(ids[1]);
  await page.getByTestId("research-start-button").click();
  await expect(page.getByTestId("research-state")).toContainText("COMPLETED", { timeout: 90000 });
  await expect(page.getByTestId("research-state")).toContainText("夾具傳輸 16");
  for (const name of ["buy_hold", "ma_crossover", "single_agent", "multi_agent"]) {
    await expect(page.getByTestId(`coverage-${name}`)).toHaveText("COMPLETED");
  }
  await page.getByTestId("research-source").selectOption("buy_hold");
  await expect(page.getByTestId("research-source-view")).toContainText("來源 COMPLETED");
  await page.getByTestId("research-source").selectOption("multi_agent");
  await expect(page.getByTestId("research-source-view")).toContainText("bullish");
  await expect(page.getByTestId("equity-chart")).toBeVisible();
  await page.getByTestId("research-replay").click();
  await expect(page.getByTestId("research-replays")).toContainText("不取代來源");
  await expect(page.getByTestId("research-state")).toContainText("夾具傳輸 16");
  const downloadPromise = page.waitForEvent("download");
  await page.getByTestId("research-export").click();
  const download = await downloadPromise;
  const file = await download.path();
  expect(file).toBeTruthy();
  const exported = fs.readFileSync(file as string, "utf8");
  const body = JSON.parse(exported);
  const report = body.report;
  expect(report.coverage.denominator).toBe(4);
  expect(report.coverage.failures_remain_in_denominator).toBe(true);
  for (const name of ["buy_hold", "ma_crossover", "single_agent", "multi_agent"]) {
    expect(report.sources.map((item: { strategy: string }) => item.strategy)).toContain(name);
  }
  expect(report.cost.invoiced_actual_usd).toBeNull();
  expect(report.cost.baseline_calls).toBe(0);
  expect(report.cost.baseline_cost_usd).toBe(0);
  expect(report.cost.per_call_actual_allocation_usd).toBeNull();
  expect(report.comparability.ok).toBe(true);
  expect(report.coverage.strategy_denominator).toBe(4);
  expect(report.coverage.session_denominator).toBeGreaterThan(4);
  expect(report.coverage.verdict).toBe("COMPLETE");
  expect(report.coverage.missing_sessions).toBe(0);
  const holds = report.coverage.observations.filter(
    (item: { strategy: string; kind: string }) => item.strategy === "buy_hold" && item.kind === "policy_hold",
  );
  expect(holds.length).toBeGreaterThan(0);
  expect(report.cost.actual_charge_scope).toBe("aggregate-matched-calls");
  await expect(page.getByTestId("research-coverage-note")).toContainText("政策持有");
  expect(report.semantic_replay.every((item: { equal: boolean }) => item.equal)).toBe(true);
  expect(report.limitations.join("\n")).toContain("Return sign is not a success criterion");
  expect(report.versions.transport).toBe("synthetic-fixture-not-live-api");
  await expect(page.getByTestId("research-limitations")).toContainText("不是成功標準");
  for (const marker of MARKERS) expect(exported).not.toContain(marker);
  const storage = await page.evaluate(() => JSON.stringify(localStorage) + JSON.stringify(sessionStorage));
  for (const marker of MARKERS) expect(storage).not.toContain(marker);
});

test("cancel before send, then reconcile and resume only the delayed fixture", async ({ page }) => {
  await openResearch(page);
  await preview(page, "fixture-cancel");
  await page.getByTestId("research-start-button").click();
  await expect(page.getByTestId("research-cancel")).toBeEnabled({ timeout: 15000 });
  await page.getByTestId("research-cancel").click();
  await expect(page.getByTestId("research-state")).toContainText("CANCELLED", { timeout: 20000 });
  await expect(page.getByTestId("research-state")).toContainText("夾具傳輸 0");

  await page.getByTestId("research-profile").selectOption("fixture-delayed");
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-start-button")).toBeEnabled();
  await page.getByTestId("research-start-button").click();
  await expect(page.getByTestId("research-state")).toContainText("PAUSED_BILLING", { timeout: 90000 });
  await expect(page.getByTestId("research-resume")).toBeDisabled();
  await page.getByTestId("research-reconcile").click();
  await expect(page.getByTestId("research-unknown")).toHaveText("0", { timeout: 15000 });
  await expect(page.getByTestId("research-resume")).toBeEnabled();
  await page.getByTestId("research-resume").click();
  await expect(page.getByTestId("research-state")).toContainText("COMPLETED", { timeout: 90000 });
  await expect(page.getByTestId("research-state")).toContainText("夾具傳輸 16");

  await page.getByTestId("research-profile").selectOption("fixture-timeout");
  await page.getByTestId("research-preview").click();
  await page.getByTestId("research-start-button").click();
  await expect(page.getByTestId("research-state")).toContainText("PAUSED_BILLING", { timeout: 30000 });
  await expect(page.getByTestId("research-state")).toContainText("夾具傳輸 1");
  await page.getByTestId("research-reconcile").click();
  await expect(page.getByTestId("research-error")).toContainText("NOT_ELIGIBLE");
  await expect(page.getByTestId("research-resume")).toBeDisabled();
  await expect(page.getByTestId("research-partial")).toContainText("不是完整結果");
  const hold = await page.getByTestId('research-unknown').innerText();
  await expect(page.getByTestId('research-cancel')).toBeEnabled();
  await page.getByTestId('research-cancel').click();
  await expect(page.getByTestId('research-state')).toContainText('CANCELLED');
  await expect(page.getByTestId('research-unknown')).toHaveText(hold);
});

test('source data stays bound to its study and ignores obsolete responses', async ({ page }) => {
  await openResearch(page);
  await preview(page, 'fixture-timeout');
  await page.getByTestId('research-start-button').click();
  await expect(page.getByTestId('research-state')).toContainText(/PAUSED_BILLING|CANCELLED/, { timeout: 30000 });
  await preview(page, 'fixture-success');
  await page.getByTestId('research-start-button').click();
  await expect(page.getByTestId('research-state')).toContainText('COMPLETED', { timeout: 90000 });
  await page.getByTestId('research-source').selectOption('buy_hold');
  await expect(page.getByTestId('research-source-view')).toContainText('來源 COMPLETED');
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let intercepted!: () => void;
  const requested = new Promise<void>((resolve) => { intercepted = resolve; });
  await page.route('**/sources/multi_agent', async (route) => {
    const response = await route.fetch();
    intercepted();
    await gate;
    await route.fulfill({ response }).catch(() => undefined);
  }, { times: 1 });
  try {
    await page.getByTestId('research-source').selectOption('multi_agent');
    await requested;
    await page.getByRole('button', { name: /fixture-timeout/ }).click();
    await expect(page.getByTestId('research-state')).toContainText(/PAUSED_BILLING|CANCELLED/, { timeout: 30000 });
    release();
    await expect(page.getByTestId('research-source-view')).toContainText('來源 NOT_RUN');
    await expect(page.getByTestId('research-source-view')).not.toContainText('bullish');
    await expect(page.getByTestId('research-source-view').locator('a[href^="/agents?run="]')).toHaveCount(0);
  } finally {
    release();
  }
});

test("acceptance stage A and stage B freeze separate one-week studies", async ({ page }) => {
  await openResearch(page);
  const postsBefore = await page.getByTestId("research-posts").innerText();
  await page.getByTestId("research-protocol").selectOption("acceptance-stage-a-v1");
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-error")).toContainText("ACCEPTANCE_REQUIRES_ONE_DECISION_WEEK");
  await expect(page.getByTestId("research-posts")).toHaveText(postsBefore);
  await expect(page.getByTestId("research-start-button")).toBeDisabled();

  await page.getByTestId("research-start").fill("2023-01-27");
  await page.getByTestId("research-end").fill("2023-01-31");
  await page.getByTestId("research-attempts").fill("1");
  await page.getByTestId("research-protocol").selectOption("acceptance-stage-b-v1");
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-scope")).toHaveText("階段 B · 多代理 3");
  await expect(page.getByTestId("research-nominal")).toHaveText("3 / 1");
  await expect(page.getByTestId("research-warnings")).toContainText("ACCEPTANCE_PROTOCOL_CAPS_INSUFFICIENT");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await expect(page.getByTestId("research-posts")).toHaveText(postsBefore);

  await page.getByTestId("research-profile").selectOption("fixture-acceptance-a");
  await page.getByTestId("research-protocol").selectOption("acceptance-stage-a-v1");
  await page.getByTestId("research-attempts").fill("32");
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-posts")).toHaveText(postsBefore);
  await expect(page.getByTestId("research-scope")).toHaveText("階段 A · 單代理 1");
  await expect(page.getByTestId("research-nominal")).toHaveText("1 / 1");
  await expect(page.getByTestId("research-sessions")).toHaveText("1");
  const stageAHash = await page.getByTestId("research-manifest").innerText();
  await page.getByTestId("research-start-button").click();
  await expect(page.getByTestId("research-state")).toContainText("COMPLETED", { timeout: 90000 });
  await expect(page.getByTestId("research-state")).toContainText("夾具傳輸 1");
  await expect(page.getByTestId("coverage-single_agent")).toHaveText("COMPLETED");
  await expect(page.getByTestId("coverage-buy_hold")).toHaveCount(0);
  await expect(page.getByTestId("research-protocol-note")).toContainText("不是四策略研究");
  const stageAId = await page.getByTestId("research-study-id").innerText();
  const downloadPromise = page.waitForEvent("download");
  await page.getByTestId("research-export").click();
  const download = await downloadPromise;
  const exported = JSON.parse(fs.readFileSync((await download.path()) as string, "utf8"));
  expect(exported.report.coverage.denominator).toBe(1);
  expect(exported.report.sources.map((item: { strategy: string }) => item.strategy)).toEqual(["single_agent"]);
  expect(exported.report.limitations.join("\n")).toContain("not four-strategy research");
  expect(exported.report.versions.transport).toBe("synthetic-fixture-not-live-api");
  expect(exported.report.cost.invoiced_actual_usd).toBeNull();
  expect(exported.real_spending_authorized).toBe(false);
  for (const marker of MARKERS) expect(JSON.stringify(exported)).not.toContain(marker);

  await page.getByTestId("research-profile").selectOption("fixture-acceptance-b");
  await page.getByTestId("research-protocol").selectOption("acceptance-stage-b-v1");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-scope")).toHaveText("階段 B · 多代理 3");
  const stageBHash = await page.getByTestId("research-manifest").innerText();
  expect(stageBHash).not.toBe(stageAHash);
  await expect(page.getByTestId("research-nominal")).toHaveText("3 / 3");
  await page.getByTestId("research-start-button").click();
  await expect(page.getByTestId("research-state")).toContainText("COMPLETED", { timeout: 90000 });
  await expect(page.getByTestId("research-state")).toContainText("夾具傳輸 3");
  await expect(page.getByTestId("coverage-multi_agent")).toHaveText("COMPLETED");
  await expect(page.getByTestId("coverage-single_agent")).toHaveCount(0);
  const stageBId = await page.getByTestId("research-study-id").innerText();
  expect(stageBId).not.toBe(stageAId);
});

test("live transport selection cannot start or post", async ({ page }) => {
  await openResearch(page);
  const postsBefore = await page.getByTestId("research-posts").innerText();
  await expect(page.getByTestId("research-transport")).toHaveValue("");
  await expect(page.getByTestId("research-live-readiness")).toContainText("不能核准花費");
  await page.getByTestId("research-transport").selectOption("live");
  await page.getByTestId("research-deadline").fill("45");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-warnings")).toContainText("LIVE_APPROVAL_REQUIRED");
  await expect(page.getByTestId("research-warnings")).toContainText("OVERALL_DEADLINE_DOES_NOT_STOP_UPSTREAM_BILLING");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await expect(page.getByTestId("research-posts")).toHaveText(postsBefore);
  await page.getByTestId("research-transport").selectOption("");
  await page.getByTestId("research-deadline").fill("");
  await expect(page.getByTestId("research-start-button")).toBeDisabled();
  await page.getByTestId("research-preview").click();
  await expect(page.getByTestId("research-warnings")).not.toContainText("LIVE_APPROVAL_REQUIRED");
  await expect(page.getByTestId("research-start-button")).toBeEnabled();
  await expect(page.getByTestId("research-posts")).toHaveText(postsBefore);
});
