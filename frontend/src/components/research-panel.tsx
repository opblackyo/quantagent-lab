"use client";

import { useEffect, useState } from "react";
import { EquityChart } from "@/components/charts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { AgentsPage, TradeTable } from "@/components/workstation";
import {
  researchCancel,
  researchPreview,
  researchReadiness,
  researchReconcile,
  researchReplay,
  researchResume,
  researchSource,
  researchStart,
  researchStudies,
  researchStudy,
  strategyNames,
  type Dataset,
  type ResearchPreview,
  type ResearchReadiness,
  type ResearchSource,
  type ResearchStudy,
} from "@/lib/api";

const ACTIVE = new Set(["QUEUED", "RUNNING", "CANCEL_REQUESTED"]);
const PAUSED = new Set(["PAUSED_BILLING", "PAUSED_UNKNOWN", "PAUSED_DRIFT"]);
const STRATEGIES = ["buy_hold", "ma_crossover", "single_agent", "multi_agent"] as const;

type FormState = {
  dataset_id: string;
  profile_id: string;
  start_date: string;
  end_date: string;
  requested_budget_usd: string;
  requested_max_attempts: string;
  study_protocol: "" | "acceptance-stage-a-v1" | "acceptance-stage-b-v1";
  transport_intent: "" | "live";
  overall_deadline_seconds: string;
};

function requestBody(form: FormState, datasetHash: string) {
  const body: Record<string, unknown> = {
    profile_id: form.profile_id,
    dataset_id: form.dataset_id,
    expected_dataset_hash: datasetHash,
    start_date: form.start_date,
    end_date: form.end_date,
    requested_budget_usd: form.requested_budget_usd,
    requested_max_attempts: Number(form.requested_max_attempts),
  };
  if (form.study_protocol) body.study_protocol = form.study_protocol;
  if (form.transport_intent === "live") body.transport_intent = "live";
  if (form.overall_deadline_seconds) body.overall_deadline_seconds = Number(form.overall_deadline_seconds);
  return body;
}

export default function ResearchPanel({ datasets }: { datasets: Dataset[] }) {
  const [readiness, setReadiness] = useState<ResearchReadiness | null>(null);
  const [form, setForm] = useState<FormState>({
    dataset_id: "synthetic-spy-v1",
    profile_id: "",
    start_date: "2023-01-01",
    end_date: "2023-01-31",
    requested_budget_usd: "1",
    requested_max_attempts: "32",
    study_protocol: "",
    transport_intent: "",
    overall_deadline_seconds: "",
  });
  const [preview, setPreview] = useState<ResearchPreview | null>(null);
  const [previewKey, setPreviewKey] = useState("");
  const [studyId, setStudyId] = useState("");
  const [studyState, setStudy] = useState<ResearchStudy | null>(null);
  const [studies, setStudies] = useState<{ study_id: string; state: string; profile_id: string }[]>([]);
  const [loadedSource, setSource] = useState<{ studyId: string; strategy: string; data: ResearchSource } | null>(null);
  const [strategy, setStrategy] = useState<(typeof STRATEGIES)[number]>("multi_agent");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [nonce, setNonce] = useState(0);
  const study = studyState?.study_id === studyId ? studyState : null;
  const source = loadedSource?.studyId === studyId && loadedSource?.strategy === strategy ? loadedSource.data : null;

  useEffect(() => {
    let stop = false;
    researchReadiness()
      .then((value) => {
        if (stop) return;
        setReadiness(value);
        setForm((current) => ({
          ...current,
          profile_id: current.profile_id || value.profiles[0]?.profile_id || "",
          dataset_id: current.dataset_id || value.profiles[0]?.dataset_id || "synthetic-spy-v1",
        }));
      })
      .catch((exc: Error) => setError(exc.message));
    researchStudies()
      .then((value) => {
        if (!stop) setStudies(value.studies);
      })
      .catch(() => undefined);
    return () => {
      stop = true;
    };
  }, []);

  useEffect(() => {
    if (!studyId) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = () => {
      researchStudy(studyId)
        .then((value) => {
          if (stop) return;
          setStudy(value);
          setStudies((items) => items.map((item) => item.study_id === value.study_id ? { ...item, state: value.state } : item));
          if (ACTIVE.has(value.state) || value.worker_busy) timer = setTimeout(tick, 400);
        })
        .catch((exc: Error) => {
          if (!stop) setError(exc.message);
        });
    };
    tick();
    return () => {
      stop = true;
      if (timer) clearTimeout(timer);
    };
  }, [studyId, nonce]);

  const plannedStrategies = study?.plan.strategy_ids?.length ? study.plan.strategy_ids : [...STRATEGIES];

  useEffect(() => {
    if (!plannedStrategies.includes(strategy)) setStrategy(plannedStrategies[0] as (typeof STRATEGIES)[number]);
  }, [plannedStrategies, strategy]);

  useEffect(() => {
    setSource(null);
    if (!studyId) return;
    if (study?.plan.strategy_ids?.length && !study.plan.strategy_ids.includes(strategy)) return;
    const controller = new AbortController();
    researchSource(studyId, strategy, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) setSource({ studyId, strategy, data: value });
      })
      .catch((exc: Error) => {
        if (!controller.signal.aborted) setError(exc.message);
      });
    return () => controller.abort();
  }, [studyId, strategy, study?.state, study?.worker_busy]);

  const dataset = datasets.find((item) => item.id === form.dataset_id);
  const currentKey = JSON.stringify(form);
  const previewMatches = Boolean(preview) && previewKey === currentKey;
  const acceptance = form.study_protocol !== "";
  const capsFit = preview?.plan.estimate.all_attempts_fit_requested_caps === true;
  const liveBlocked = form.transport_intent === "live" && preview?.server_live_ready !== true;
  const enabled = readiness?.service === "synthetic-fixture";
  const unknown = Number(study?.cost?.unknown_reserved_usd || 0);
  const resumeAllowed = Boolean(study && PAUSED.has(study.state) && unknown === 0 && !study.worker_busy);
  const cancelAllowed = Boolean(study && (study.state === "QUEUED" || study.state === "RUNNING" || PAUSED.has(study.state)));

  function update(patch: Partial<FormState>) {
    setForm((current) => ({ ...current, ...patch }));
    setError("");
  }

  async function previewStudy() {
    if (!dataset || !form.profile_id) return;
    setBusy("preview");
    setError("");
    try {
      const value = await researchPreview(requestBody(form, dataset.sha256));
      setPreview(value);
      setPreviewKey(JSON.stringify(form));
    } catch (exc) {
      setPreview(null);
      setError(exc instanceof Error ? exc.message : "REQUEST_REJECTED");
    } finally {
      setBusy("");
    }
  }

  async function startStudy() {
    if (!dataset || !preview || !previewMatches || busy) return;
    setBusy("start");
    setError("");
    try {
      const started = await researchStart({
        manifest_hash: preview.plan.manifest_hash,
        idempotency_key: preview.plan.manifest_hash,
        request: requestBody(form, dataset.sha256),
      });
      setStudyId(started.study_id);
      setStudy(started);
      setNonce((value) => value + 1);
      const listed = await researchStudies();
      setStudies(listed.studies);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "REQUEST_REJECTED");
    } finally {
      setBusy("");
    }
  }

  async function act(name: "cancel" | "reconcile" | "resume" | "replay") {
    if (!studyId || busy) return;
    setBusy(name);
    setError("");
    try {
      const action = { cancel: researchCancel, reconcile: researchReconcile, resume: researchResume, replay: researchReplay }[name];
      const value = await action(studyId);
      setStudy(value);
      setNonce((current) => current + 1);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "REQUEST_REJECTED");
      setNonce((current) => current + 1);
    } finally {
      setBusy("");
    }
  }

  function loadSource(next: (typeof STRATEGIES)[number]) {
    setStrategy(next);
    setError("");
  }

  const labels = readiness?.labels || [];
  return (
    <div className="research-panel">
      <div className="fixture-banner" data-testid="research-banner" role="note">
        <strong>合成夾具 · 不是即時已驗證 API</strong>
        <p>
          價格報酬，不含股息。閘道標籤不是上游模型身分。請求金額上限不是撥款，按鍵也不會授權真實花費。
          {labels.map((label) => (
            <span key={label}> {label}</span>
          ))}
        </p>
      </div>
      {error && (
        <p className="error-banner" role="alert" data-testid="research-error">
          {error}
        </p>
      )}
      {!enabled && (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>研究服務未啟用</h2>
              <p>預設應用只提供 Mock / Replay。這個畫面不會建立供應商、讀取金鑰或送出請求。</p>
            </div>
          </div>
        </section>
      )}
      {enabled && (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>合成研究預覽</h2>
              <p>選擇允許的資料與不透明設定檔。預覽不會排隊，也不會送出。</p>
            </div>
            <Badge variant="outline" data-testid="research-posts">
              夾具傳輸 {readiness?.fixture_transport_posts ?? 0}
            </Badge>
          </div>
          <form
            className="settings-form"
            onSubmit={(event) => {
              event.preventDefault();
              void previewStudy();
            }}
          >
            <label>
              資料集
              <select data-testid="research-dataset" value={form.dataset_id} onChange={(event) => update({ dataset_id: event.target.value })}>
                {datasets.filter((item) => readiness?.profiles.some((profile) => profile.dataset_id === item.id)).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.id} · {item.kind === "synthetic" ? "合成" : item.kind}
                  </option>
                ))}
              </select>
            </label>
            <label>
              伺服器設定檔
              <select data-testid="research-profile" value={form.profile_id} onChange={(event) => update({ profile_id: event.target.value })}>
                {readiness?.profiles.map((profile) => (
                  <option key={profile.profile_id} value={profile.profile_id}>
                    {profile.profile_id} · {profile.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              開始日期
              <input data-testid="research-start" type="date" value={form.start_date} onChange={(event) => update({ start_date: event.target.value })} />
            </label>
            <label>
              結束日期
              <input data-testid="research-end" type="date" value={form.end_date} onChange={(event) => update({ end_date: event.target.value })} />
            </label>
            <label>
              研究範圍
              <select data-testid="research-protocol" value={form.study_protocol} onChange={(event) => update({ study_protocol: event.target.value as FormState["study_protocol"] })}>
                <option value="">標準研究（四策略）</option>
                <option value="acceptance-stage-a-v1">驗收階段 A：一週單代理（1 次）</option>
                <option value="acceptance-stage-b-v1">驗收階段 B：一週多代理（3 次）</option>
              </select>
            </label>
            <label>
              傳輸
              <select data-testid="research-transport" value={form.transport_intent} onChange={(event) => update({ transport_intent: event.target.value as FormState["transport_intent"] })}>
                <option value="">夾具（預設，不送真實請求）</option>
                <option value="live">真實傳輸（伺服器未核准）</option>
              </select>
            </label>
            <label>
              整體截止秒數（空白則不凍結）
              <input data-testid="research-deadline" value={form.overall_deadline_seconds} onChange={(event) => update({ overall_deadline_seconds: event.target.value })} />
            </label>
            <p className="panel-note" data-testid="research-live-readiness">
              {preview?.server_live_ready
                ? "伺服器持有與這份清單相符的離線組成。啟動仍要另按一次。這不是撥款、不是發票，也不能證明上游已停止計費。"
                : "真實傳輸與金鑰只由伺服器核准。此畫面不能核准花費。整體截止只停止本地後續讀取，不能保證上游停止計費。"}
            </p>
            <label>
              請求上限 USD（不是撥款）
              <input data-testid="research-budget" value={form.requested_budget_usd} onChange={(event) => update({ requested_budget_usd: event.target.value })} />
            </label>
            <label>
              請求呼叫上限
              <input data-testid="research-attempts" value={form.requested_max_attempts} onChange={(event) => update({ requested_max_attempts: event.target.value })} />
            </label>
            <Button type="submit" disabled={busy === "preview" || !form.profile_id} data-testid="research-preview">
              預覽計畫
            </Button>
          </form>
          {preview && (
            <div className="feature-list" data-testid="research-plan">
              <div>
                <dt>範圍</dt>
                <dd data-testid="research-scope">
                  {preview.plan.study_protocol === "acceptance-stage-a-v1"
                    ? "階段 A · 單代理 1"
                    : preview.plan.study_protocol === "acceptance-stage-b-v1"
                      ? "階段 B · 多代理 3"
                      : "標準研究 · 四策略"}
                </dd>
              </div>
              <div>
                <dt>合格決策週</dt>
                <dd data-testid="research-sessions">{preview.plan.sessions.length}</dd>
              </div>
              <div>
                <dt>名目呼叫 / 上限</dt>
                <dd data-testid="research-nominal">
                  {preview.plan.estimate.total_nominal_calls} / {preview.plan.estimate.capped_attempt_ceiling}
                </dd>
              </div>
              <div>
                <dt>保守預留</dt>
                <dd data-testid="research-reservation">{String(preview.plan.estimate.capped_reservation_usd)}</dd>
              </div>
              <div>
                <dt>基線呼叫</dt>
                <dd>{preview.plan.estimate.baseline_calls}</dd>
              </div>
              <div>
                <dt>失敗政策</dt>
                <dd data-testid="research-warnings">{preview.plan.warnings.join(" ") || "保留部位並暫停，不自動重送"}</dd>
              </div>
              <div>
                <dt>清單雜湊</dt>
                <dd data-testid="research-manifest">{preview.plan.manifest_hash}</dd>
              </div>
              <p className="panel-note">
                {preview.plan.return_basis} · {preview.plan.identity_scope} · {preview.plan.model_id} · 執行授權 {String(preview.execution_authorized)} · 預覽不是撥款
                {!previewMatches ? " · 設定已變更，請重新預覽" : ""}
              </p>
            </div>
          )}
          <div className="dialog-actions">
            <Button type="button" data-testid="research-start-button" disabled={!previewMatches || busy !== "" || (acceptance && !capsFit) || liveBlocked} onClick={() => void startStudy()}>
              明確啟動一次
            </Button>
          </div>
        </section>
      )}
      {study && (
        <section className="panel" data-testid="research-study">
          <div className="panel-heading">
            <div>
              <h2>研究進度</h2>
              <p data-testid="research-study-id">{study.study_id}</p>
              <p data-testid="research-state">
                {study.state}
                {study.reason ? ` · ${study.reason}` : ""} · {study.plan.transport_intent === "live" ? "組成傳輸" : "夾具傳輸"} {study.fixture_transport_posts} · 槽 {study.progress?.recorded_slots ?? 0}/{study.progress?.total_slots ?? 0}
              </p>
            </div>
          </div>
          <dl className="feature-list">
            {(study.plan.strategy_ids?.length ? study.plan.strategy_ids : STRATEGIES).map((name) => (
              <div key={name}>
                <dt>{strategyNames[name] || name}</dt>
                <dd data-testid={`coverage-${name}`}>{study.coverage?.[name] || "NOT_RUN"}</dd>
              </div>
            ))}
            <div>
              <dt>估計合計</dt>
              <dd data-testid="research-estimated">{study.cost?.usage_estimated_usd ?? "—"}</dd>
            </div>
            <div>
              <dt>未知保留</dt>
              <dd data-testid="research-unknown">{study.cost?.unknown_reserved_usd ?? "—"}</dd>
            </div>
            <div>
              <dt>確認合計</dt>
              <dd data-testid="research-confirmed">{study.cost?.confirmed_actual_usd ?? "—"}</dd>
            </div>
            <div>
              <dt>發票實付</dt>
              <dd data-testid="research-invoice">{study.cost?.invoiced_actual_usd ?? "null"}</dd>
            </div>
          </dl>
          <p className="panel-note" data-testid="research-billing-note">
            估計合計與確認合計都不是發票，也不是已核准的實際花費。未知保留在失敗後仍全額留下。
          </p>
          {study.plan.study_protocol && (
            <p className="panel-note" data-testid="research-protocol-note">
              這份驗收只計算凍結的策略與一個決策週。它不是四策略研究，也不是單代理與多代理的等量推論比較。合成結果不是即時花費。
            </p>
          )}
          <p className="panel-note" data-testid="research-coverage-note">
            上列是各策略執行狀態，與決策時段覆蓋、可比性分開計算。Buy & Hold 只在首次核准進場時記錄決策，其後計畫日期是政策持有（HOLD / NOT_APPLICABLE），不是已記錄決策、實際呼叫或傳輸失敗。缺漏的決策時段仍留在時段分母，執行完成也不代表覆蓋完整。
          </p>
          <p className="panel-note" data-testid="research-limitations">
            價格報酬不含股息與利息。預訓練模型可能帶有歷史知識。單代理與多代理的角色工作量不相等。閘道標籤未經獨立身分驗證。報酬正負不是成功標準。合成夾具不是即時 API 結果。
          </p>
          {(study.state.startsWith("PAUSED") || study.coverage && Object.values(study.coverage).some((item) => item !== "COMPLETED")) && (
            <p className="panel-note" data-testid="research-partial">
              未完成或 NOT_RUN 不是完整結果，也不是零績效。
            </p>
          )}
          <div className="dialog-actions">
            <Button type="button" variant="outline" data-testid="research-cancel" disabled={!cancelAllowed || busy !== ""} onClick={() => void act("cancel")}>
              取消
            </Button>
            <Button type="button" variant="outline" data-testid="research-reconcile" disabled={!study || !PAUSED.has(study.state) || busy !== ""} onClick={() => void act("reconcile")}>
              伺服器對帳
            </Button>
            <Button type="button" variant="outline" data-testid="research-resume" disabled={!resumeAllowed || busy !== ""} onClick={() => void act("resume")}>
              續跑已驗證未送出
            </Button>
            <Button type="button" variant="outline" data-testid="research-replay" disabled={busy !== "" || study.worker_busy || ACTIVE.has(study.state)} onClick={() => void act("replay")}>
              Replay（不再呼叫）
            </Button>
            <Button variant="outline" asChild>
              <a data-testid="research-export" href={`/api/v1/research/studies/${study.study_id}/export`}>
                下載安全 JSON
              </a>
            </Button>
          </div>
          <div className="settings-form">
            <label>
              檢視來源
              <select data-testid="research-source" value={strategy} onChange={(event) => void loadSource(event.target.value as (typeof STRATEGIES)[number])}>
                {(study.plan.strategy_ids?.length ? study.plan.strategy_ids : STRATEGIES).map((name) => (
                  <option key={name} value={name}>
                    {strategyNames[name] || name} · 來源
                  </option>
                ))}
              </select>
            </label>
          </div>
          {source && (
            <div data-testid="research-source-view">
              <p className="panel-note">
                來源 {source.status} · {source.origin}
                {source.complete ? "" : " · 未完成，不是零績效"}
              </p>
              {source.complete && source.run_id && (
                <p className="panel-note">
                  <a href={`/agents?run=${source.run_id}`}>在 Agent 決策頁看這個來源</a>
                  {" · "}
                  <a href={`/market?run=${source.run_id}`}>市場</a>
                  {" · "}
                  <a href={`/backtests?run=${source.run_id}`}>策略比較</a>
                </p>
              )}
              {source.equity.length > 0 && <EquityChart points={source.equity} initial={100000} />}
              <AgentsPage decisions={source.decisions} trades={source.trades} />
              <TradeTable trades={source.trades} />
            </div>
          )}
          {study.replay_runs && Object.keys(study.replay_runs).length > 0 && (
            <ul data-testid="research-replays">
              {Object.entries(study.replay_runs).map(([name, run]) => (
                <li key={name}>
                  Replay {strategyNames[name] || name} · {run.run_id} · 不取代來源 {study.source_runs?.[name]?.run_id || "—"}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
      {studies.length > 0 && (
        <section className="panel">
          <div className="panel-heading">
            <h2>這次夾具的研究</h2>
          </div>
          <ul>
            {studies.map((item) => (
              <li key={item.study_id}>
                <button type="button" onClick={() => setStudyId(item.study_id)}>
                  {item.profile_id} · {item.state}
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
