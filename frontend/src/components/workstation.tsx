"use client";

import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import {
  Activity,
  ArrowUpRight,
  Check,
  ChevronRight,
  Clock,
  Download,
  FlaskConical,
  LayoutDashboard,
  List,
  Loader2,
  Plus,
  RefreshCw,
  Settings,
  ShieldCheck,
  Workflow,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ComparisonChart, EquityChart, MarketChart } from "@/components/charts";
import ResearchPanel from "@/components/research-panel";
import {
  api,
  createExperiment,
  money,
  percent,
  strategyColors,
  strategyNames,
  type Bar,
  type Capabilities,
  type Comparison,
  type Config,
  type Dataset,
  type Decision,
  type Equity,
  type Run,
  type Trade,
  type PilotBudget,
} from "@/lib/api";

const navigation = [
  { path: "/", label: "研究總覽", icon: LayoutDashboard },
  { path: "/market", label: "市場分析", icon: Activity },
  { path: "/agents", label: "Agent 決策", icon: Workflow },
  { path: "/backtests", label: "策略比較", icon: FlaskConical },
  { path: "/runs", label: "實驗紀錄", icon: List },
  { path: "/research", label: "合成研究", icon: ShieldCheck },
  { path: "/settings", label: "研究設定", icon: Settings },
];
const pageCopy: Record<string, { title: string; description: string }> = {
  "/": {
    title: "研究總覽",
    description: "從市場資料到模擬成交，讓每一次決策都有依據。",
  },
  "/market": {
    title: "市場分析",
    description: "查看日線、移動平均與實際模擬成交的位置。",
  },
  "/agents": {
    title: "Agent 決策探索",
    description: "回到決策當下，檢視分析、批判與最後的風控結果。",
  },
  "/backtests": {
    title: "策略比較",
    description: "在相同資料、交易期間與成本下，觀察四種研究方法。",
  },
  "/runs": {
    title: "實驗紀錄",
    description: "保存每次設定、執行狀態與完整研究產物。",
  },
  "/settings": {
    title: "研究設定",
    description: "調整新實驗的預設參數，檢查資料與模型可用狀態。",
  },
  "/research": {
    title: "合成研究",
    description: "本機合成夾具的預覽、啟動與安全重播。不是即時 API 績效。",
  },
};
const defaults = {
  initial_cash: 100000,
  fee_bps: 5,
  slippage_bps: 5,
  max_llm_calls: 2000,
  max_llm_budget_usd: 2,
};
function readDefaults() {
  try {
    return {
      ...defaults,
      ...JSON.parse(localStorage.getItem("quantagent-defaults") || "{}"),
    };
  } catch {
    return defaults;
  }
}
function message(error: unknown) {
  return error instanceof Error ? error.message : "無法完成請求";
}
const reasonNames: Record<string, string> = {
  TARGET_ALREADY_MET: "目前持倉已符合目標",
  INVALID_FEATURES: "市場特徵不足或無效",
  INVALID_DECISION: "未取得有效決策",
  INVALID_TARGET: "目標倉位未通過限制",
  NO_EXECUTION_SESSION: "期間內沒有下一成交日",
  BUDGET_EXHAUSTED: "已達呼叫次數或費用上限",
  BUDGET_EXCEEDED: "模型費用已超過預留",
  PROVIDER_TIMEOUT: "模型回應逾時",
  PROVIDER_HTTP_ERROR: "模型服務請求失敗",
  PROVIDER_NETWORK_ERROR: "模型服務無法連線",
  PROVIDER_ERROR: "模型服務未完成回應",
  INVALID_AGENT_OUTPUT: "模型輸出未通過格式驗證",
  REPLAY_MISSING_DECISION: "來源缺少這個決策日",
  REPLAY_INPUT_MISMATCH: "重播輸入與原始紀錄不同",
  REPLAY_CONTRACT_MISMATCH: "重播輸出未通過當前契約",
  PILOT_BUDGET_EXHAUSTED: "整體 Pilot 預算或呼叫次數已用盡",
  PILOT_BUDGET_EXCEEDED: "回報費用超過 Pilot 上限，停止新呼叫",
  PILOT_INPUT_BOUND_EXCEEDED: "輸入超過已凍結的預留界線",
  MODEL_VERSION_MISMATCH: "提供者回傳的模型版本與固定快照不符",
  PROVIDER_CONFIGURATION_CHANGED: "提供者、群組或模型設定已變更，停止新呼叫",
};
const reasonText = (code: string) => reasonNames[code] || code;

function Status({ status, progress }: { status: string; progress?: number }) {
  const names: Record<string, string> = {
    COMPLETED: "已完成",
    RUNNING: "執行中",
    PENDING: "排隊中",
    FAILED: "執行失敗",
    APPROVED: "通過",
    REJECTED: "已拒絕",
    SKIPPED: "已跳過",
  };
  return (
    <span className={`status status-${status.toLowerCase()}`}>
      {status === "RUNNING" ? (
        <Loader2 size={12} className="spin" />
      ) : (
        <span className="status-dot" />
      )}
      {names[status] || status}
      {status === "RUNNING" && progress != null ? ` ${progress}%` : ""}
    </span>
  );
}
export function Panel({
  title,
  subtitle,
  action,
  children,
  className = "",
}: {
  title: string;
  subtitle?: string;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <div className="panel-heading">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        {action}
      </div>
      {children}
    </section>
  );
}
function Empty({
  title,
  children,
  action,
}: {
  title: string;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-symbol">
        <FlaskConical size={28} />
      </div>
      <h2>{title}</h2>
      <p>{children}</p>
      {action}
    </div>
  );
}
function Metric({
  label,
  value,
  detail,
  positive,
}: {
  label: string;
  value: string;
  detail: string;
  positive?: boolean;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong
        className={
          positive === undefined ? "" : positive ? "positive" : "negative"
        }
      >
        {value}
      </strong>
      <small>{detail}</small>
    </div>
  );
}

export default function Workstation() {
  const pathname = usePathname();
  const router = useRouter();
  const params = useSearchParams();
  const [runs, setRuns] = useState<Run[]>([]);
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [capabilities, setCapabilities] = useState<Capabilities | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [dialog, setDialog] = useState(false);
  const [loadedDetails, setDetails] = useState<{
    run_id: string;
    decisions: Decision[];
    equity: Equity[];
    trades: Trade[];
    bars: Bar[];
  } | null>(null);
  const [detailsError, setDetailsError] = useState("");
  const selected =
    runs.find((r) => r.run_id === params.get("run")) ||
    runs.find((r) => r.status === "COMPLETED") ||
    runs[0];
  const details =
    loadedDetails?.run_id === selected?.run_id ? loadedDetails : null;
  const page = pageCopy[pathname] || pageCopy["/"];
  async function refresh() {
    try {
      const [newRuns, newDatasets, caps] = await Promise.all([
        api<Run[]>("/experiments"),
        api<Dataset[]>("/datasets"),
        api<Capabilities>("/system/capabilities"),
      ]);
      setRuns(newRuns);
      setDatasets(newDatasets);
      setCapabilities(caps);
      setError("");
    } catch (e) {
      setError(message(e));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    void refresh();
    const onUpdated = () => {
      void refresh();
    };
    window.addEventListener("quantagent:updated", onUpdated);
    return () => window.removeEventListener("quantagent:updated", onUpdated);
  }, []);
  const active = runs.some(
    (r) => r.status === "RUNNING" || r.status === "PENDING",
  );
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => void refresh(), 1200);
    return () => clearInterval(timer);
  }, [active]);
  useEffect(() => {
    setDetails(null);
    setDetailsError("");
    if (!selected || selected.status !== "COMPLETED") return;
    let ignore = false;
    const id = selected.run_id;
    const query = new URLSearchParams({
      dataset_id: selected.config.dataset_id,
      start_date: selected.config.start_date,
      end_date: selected.config.end_date,
    });
    Promise.all([
      api<Decision[]>(`/experiments/${id}/decisions`),
      api<Equity[]>(`/experiments/${id}/equity`),
      api<Trade[]>(`/experiments/${id}/trades`),
      api<{ bars: Bar[] }>(`/market/bars?${query}`),
    ])
      .then(([decisions, equity, trades, market]) => {
        if (!ignore)
          setDetails({
            run_id: id,
            decisions,
            equity,
            trades,
            bars: market.bars,
          });
      })
      .catch((e) => {
        if (!ignore) setDetailsError(message(e));
      });
    return () => {
      ignore = true;
    };
  }, [selected?.run_id, selected?.status]);
  function select(id: string) {
    router.replace(`${pathname}?run=${id}`, { scroll: false });
  }
  async function submit(config: Partial<Config>) {
    const startingLocation = window.location.href;
    setCreating(true);
    setError("");
    try {
      const result = await createExperiment(config);
      await refresh();
      if (window.location.href === startingLocation) select(result.run_id);
      setDialog(false);
    } catch (e) {
      setError(message(e));
      throw e;
    } finally {
      setCreating(false);
    }
  }
  async function replay(run: Run) {
    try {
      await submit({
        ...run.config,
        llm_mode: "replay",
        replay_source_run_id: run.run_id,
      });
    } catch {}
  }
  async function batch() {
    const startingLocation = window.location.href;
    setCreating(true);
    setError("");
    try {
      const base = selected?.config || {
        dataset_id: datasets[0]?.id,
        start_date: "2024-01-01",
        end_date: "2024-12-31",
        ...readDefaults(),
      };
      let id = "";
      for (const strategy_id of Object.keys(strategyNames)) {
        const result = await createExperiment({
          ...base,
          strategy_id,
          llm_mode: "mock",
          model_id: null,
          model_provider: null,
          replay_source_run_id: null,
        });
        id = result.run_id;
      }
      await refresh();
      if (window.location.href === startingLocation) select(id);
    } catch (e) {
      setError(message(e));
      await refresh();
    } finally {
      setCreating(false);
    }
  }
  const href = (path: string) =>
    `${path}${selected ? `?run=${selected.run_id}` : ""}`;
  const modes = selected?.config.llm_mode;
  const synthetic =
    selected?.dataset?.kind === "synthetic" ||
    datasets.find((d) => d.id === selected?.config.dataset_id)?.kind ===
      "synthetic";
  const mock =
    !!selected?.config.strategy_id.includes("agent") &&
    (modes === "mock" || selected?.decision_origin === "mock");
  return (
    <div className="workspace">
      <aside className="sidebar">
        <Link href={href("/")} className="brand">
          <span className="brand-mark">
            <i />
            <i />
            <i />
          </span>
          <span>
            QuantAgent<span className="brand-sub">量化研究工作台</span>
          </span>
        </Link>
        <nav aria-label="主要導覽">
          {navigation.map((item) => (
            <Link
              key={item.path}
              href={href(item.path)}
              className={`nav-link ${pathname === item.path ? "active" : ""}`}
            >
              <item.icon size={18} />
              <span>{item.label}</span>
              {pathname === item.path && <span className="nav-active-dot" />}
            </Link>
          ))}
        </nav>
        <div className="sidebar-foot">
          <ShieldCheck size={19} />
          <div>
            研究環境<span>歷史回測 · 模擬成交</span>
          </div>
          <span className="online-dot" />
        </div>
      </aside>
      <div className="workspace-main">
        <header className="topbar">
          <div className="breadcrumb">
            研究工作台
            <ChevronRight size={14} />
            <span>{page.title}</span>
          </div>
          <div className="topbar-actions">
            <span className="market-context">
              <span className="online-dot" />
              SPY <span>美股 ETF／日線</span>
            </span>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => void refresh()}
              aria-label="重新整理"
            >
              <RefreshCw size={16} />
            </Button>
            <Button onClick={() => setDialog(true)} disabled={!datasets.length}>
              <Plus size={16} />
              建立實驗
            </Button>
          </div>
        </header>
        <main className="content">
          <div className="page-heading">
            <div>
              <h1>{page.title}</h1>
              <p>{page.description}</p>
            </div>
            {runs.length > 0 && (
              <label className="run-picker">
                <span>目前實驗</span>
                <select
                  aria-label="目前實驗"
                  value={selected?.run_id || ""}
                  onChange={(e) => select(e.target.value)}
                >
                  {runs.map((r) => (
                    <option key={r.run_id} value={r.run_id}>
                      {strategyNames[r.config.strategy_id]} ·{" "}
                      {r.config.start_date} · {r.run_id.slice(0, 6)}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>
          {error && (
            <div role="alert" className="error-banner">
              <strong>無法完成請求</strong>
              <span>{error}</span>
              <button onClick={() => void refresh()}>重試</button>
            </div>
          )}
          {selected && (
            <div
              className={`research-banner ${synthetic || mock ? "demo-banner" : ""}`}
            >
              <FlaskConical size={16} />
              <span>
                {synthetic
                  ? "DEMO：合成行情"
                  : mock
                    ? "MOCK：固定規則 Agent"
                    : "歷史模擬研究"}
                {mock ? " · 非真實 LLM 決策" : ""}
                {modes === "replay" ? " · 已保存決策重播" : ""}
                。績效採價格報酬，不含股息。
              </span>
              <Badge variant="outline">
                {modes === "replay"
                  ? "Replay"
                  : mock
                    ? "Mock"
                    : selected.config.strategy_id.includes("agent")
                      ? "API"
                      : "Baseline"}
              </Badge>
            </div>
          )}
          {loading ? (
            <div className="loading-state">
              <Loader2 className="spin" />
              載入研究資料…
            </div>
          ) : pathname === "/research" ? (
            <ResearchPanel datasets={datasets} />
          ) : pathname === "/settings" ? (
            <SettingsPage datasets={datasets} capabilities={capabilities} />
          ) : pathname === "/runs" ? (
            <RunsPage
              runs={runs}
              selected={selected?.run_id}
              select={select}
              href={href}
              replay={replay}
              busy={creating}
            />
          ) : !selected ? (
            <Empty
              title="開始你的第一個研究實驗"
              action={
                <Button
                  onClick={() => setDialog(true)}
                  disabled={!datasets.length}
                >
                  <Plus size={16} />
                  建立第一個實驗
                </Button>
              }
            >
              選擇資料期間與策略，系統將保存分析、風控和每筆模擬成交。離線 Mock
              模式不需要 API 金鑰。
            </Empty>
          ) : selected.status !== "COMPLETED" ? (
            <Panel title={strategyNames[selected.config.strategy_id]}>
              <div className="run-progress">
                <Status status={selected.status} progress={selected.progress} />
                {selected.status === "FAILED" ? (
                  <>
                    <p role="alert">
                      {selected.error_message}。請檢查研究設定後建立新實驗。
                    </p>
                    <Button variant="outline" asChild>
                      <a href={`/api/v1/experiments/${selected.run_id}/export`}>
                        <Download size={14} />
                        匯出中斷紀錄
                      </a>
                    </Button>
                  </>
                ) : (
                  <>
                    <progress max={100} value={selected.progress} />
                    <p>
                      正在計算市場快照、決策與交易帳本。你可以切換到其他已完成實驗。
                    </p>
                  </>
                )}
              </div>
            </Panel>
          ) : detailsError ? (
            <div className="error-banner" role="alert">
              {detailsError}
            </div>
          ) : !details ? (
            <div className="loading-state">
              <Loader2 className="spin" />
              載入實驗結果…
            </div>
          ) : pathname === "/market" ? (
            <MarketPage
              key={selected.run_id}
              run={selected}
              bars={details.bars}
              trades={details.trades}
            />
          ) : pathname === "/agents" ? (
            <AgentsPage
              key={selected.run_id}
              decisions={details.decisions}
              trades={details.trades}
            />
          ) : pathname === "/backtests" ? (
            <BacktestsPage
              key={selected.run_id}
              runs={runs}
              selected={selected}
              batch={batch}
              busy={creating}
            />
          ) : (
            <Overview
              run={selected}
              details={details}
              runs={runs}
              href={href}
            />
          )}
          <footer className="page-footer">
            <span>QuantAgent Lab</span>
            <span>研究與教育用途 · 僅做多 · 無槓桿 · 下一交易日開盤成交</span>
          </footer>
        </main>
      </div>
      <ExperimentDialog
        open={dialog}
        onOpenChange={setDialog}
        datasets={datasets}
        capabilities={capabilities}
        submit={submit}
        busy={creating}
      />
    </div>
  );
}

function Overview({
  run,
  details,
  runs,
  href,
}: {
  run: Run;
  details: { decisions: Decision[]; equity: Equity[]; trades: Trade[] };
  runs: Run[];
  href: (p: string) => string;
}) {
  const [pilotBudget, setPilotBudget] = useState<PilotBudget | null>(null);
  useEffect(() => {
    let ignore = false;
    setPilotBudget(null);
    if (run.config.research_group_id) {
      api<PilotBudget>(`/pilots/${run.config.research_group_id}/budget`)
        .then((value) => { if (!ignore) setPilotBudget(value); })
        .catch(() => { if (!ignore) setPilotBudget(null); });
    }
    return () => { ignore = true; };
  }, [run.run_id, run.config.research_group_id]);
  const metrics = run.metrics!;
  const latest = details.decisions.filter((d) => d.decision_output).at(-1);
  const point = details.equity.at(-1)!;
  return (
    <>
      <div className="metrics-strip">
        <Metric
          label="期末資產淨值"
          value={money(metrics.final_equity)}
          detail={`初始資金 ${money(run.config.initial_cash)}`}
        />
        <Metric
          label="期間報酬"
          value={percent(metrics.total_return)}
          positive={metrics.total_return >= 0}
          detail="扣除手續費與滑價"
        />
        <Metric
          label="最大回撤"
          value={percent(metrics.max_drawdown)}
          detail="相對於歷史資產高點"
        />
        <Metric
          label="Sharpe Ratio"
          value={metrics.sharpe_ratio?.toFixed(2) || "—"}
          detail="252 交易日年化 · 無風險利率 0"
        />
      </div>
      <div className="overview-grid">
        <Panel
          title="資產變化"
          subtitle={`${run.config.start_date} 至 ${run.config.end_date}`}
          action={
            <span className="legend-item">
              <i style={{ background: "#3157c8" }} />
              {strategyNames[run.config.strategy_id]}
            </span>
          }
        >
          <EquityChart
            points={details.equity}
            initial={run.config.initial_cash}
          />
          <div className="chart-foot">
            <span>每日收盤估值 · 價格報酬</span>
            <span>
              手續費 {run.config.fee_bps} bps ／ 滑價 {run.config.slippage_bps}{" "}
              bps
            </span>
          </div>
        </Panel>
        <Panel title="研究摘要" subtitle="目前實驗的決策與執行概況">
          <div className="research-summary">
            <div className="strategy-identity">
              <span className="strategy-glyph">
                <Workflow size={24} />
              </span>
              <div>
                <strong>{strategyNames[run.config.strategy_id]}</strong>
                <span>
                  {!run.config.strategy_id.includes("agent")
                    ? "確定性基準策略"
                    : run.config.llm_mode === "mock"
                      ? "固定 Mock 規則流程"
                      : run.config.llm_mode === "replay"
                        ? "保存的決策重播"
                        : "模型推論流程"}
                </span>
              </div>
            </div>
            <dl>
              <div>
                <dt>通過／跳過決策</dt>
                <dd>
                  {metrics.approved_decisions} / {metrics.skipped_decisions}
                </dd>
              </div>
              <div>
                <dt>模擬成交筆數</dt>
                <dd>{metrics.trade_count}</dd>
              </div>
              <div>
                <dt>曝險時間</dt>
                <dd>{percent(metrics.exposure_fraction)}</dd>
              </div>
              <div>
                <dt>手續費／滑價成本</dt>
                <dd>
                  {money(metrics.total_fees, 2)} /{" "}
                  {money(metrics.slippage_cost, 2)}
                </dd>
              </div>
              <div>
                <dt>LLM 呼叫／用量估計費用</dt>
                <dd>
                  {metrics.llm_calls} / {money(metrics.llm_cost_usd, 4)}
                </dd>
              </div>
              {(metrics.uncertain_cost_reserve_usd ?? 0) > 0 && (
                <div>
                  <dt>待確認費用預留</dt>
                  <dd>{money(metrics.uncertain_cost_reserve_usd ?? 0, 4)}</dd>
                </div>
              )}
              {pilotBudget && (
                <div data-testid="pilot-budget">
                  <dt>Pilot 共用預算（含待確認費用）</dt>
                  <dd>{money(pilotBudget.charged_or_reserved_usd, 4)} / {money(pilotBudget.limit_usd, 2)} · {pilotBudget.attempts}/{pilotBudget.max_calls} 次</dd>
                  <dd data-testid="pilot-cost-reconciliation">
                    已核對扣款 {money(pilotBudget.confirmed_actual_usd ?? 0, 8)} · 用量估計 {money(pilotBudget.usage_estimated_usd, 8)} · 待確認預留 {money(pilotBudget.unknown_reserved_usd, 8)}
                  </dd>
                </div>
              )}
            </dl>
            <Link className="text-link" href={href("/backtests")}>
              比較其他研究策略
              <ArrowUpRight size={14} />
            </Link>
          </div>
        </Panel>
      </div>
      <div className="overview-bottom">
        <Panel
          title="最近一次決策"
          subtitle={latest?.decision_session || "尚無有效決策"}
          action={
            <Link className="text-link" href={href("/agents")}>
              完整決策歷程
              <ChevronRight size={14} />
            </Link>
          }
        >
          {latest ? (
            <div className="latest-decision">
              <div className="decision-weight">
                <span>目標倉位</span>
                <strong>{percent(latest.approved_weight)}</strong>
                <Status status={latest.status} />
              </div>
              <div>
                <h3>
                  {latest.analysis_output?.regime === "bullish"
                    ? "趨勢偏多"
                    : latest.analysis_output?.regime === "bearish"
                      ? "趨勢偏空"
                      : "依既定規則配置"}
                </h3>
                <p>{latest.decision_output?.rationale}</p>
                <div className="feature-tags">
                  {latest.decision_output?.evidence.map((e) => (
                    <span key={e}>{e}</span>
                  ))}
                </div>
              </div>
            </div>
          ) : (
            <p className="panel-note">
              此期間的訊號都已跳過，請查看決策紀錄中的原因。
            </p>
          )}
        </Panel>
        <Panel title="期末持倉" subtitle={point.session_date}>
          <div className="holding-row">
            <span className="ticker-avatar">S</span>
            <div>
              <strong>SPY</strong>
              <span>美股 ETF</span>
            </div>
            <div className="holding-value">
              <strong>{point.shares.toFixed(3)} 股</strong>
              <span>現金 {money(point.cash, 2)}</span>
            </div>
          </div>
          <div className="holding-meter">
            <span
              style={{
                width: `${((point.shares * point.close) / point.equity) * 100}%`,
              }}
            />
          </div>
          <div className="chart-foot">
            <span>持股市值</span>
            <strong>{money(point.shares * point.close, 2)}</strong>
          </div>
          <p className="panel-note">期末按收盤價估值，保留未平倉部位。</p>
        </Panel>
      </div>
      <Panel
        title="最近實驗"
        action={
          <Link className="text-link" href={href("/runs")}>
            全部紀錄
            <ChevronRight size={14} />
          </Link>
        }
      >
        <RunTable runs={runs.slice(0, 4)} selected={run.run_id} href={href} />
      </Panel>
    </>
  );
}

function MarketPage({
  run,
  bars,
  trades,
}: {
  run: Run;
  bars: Bar[];
  trades: Trade[];
}) {
  const last = bars.at(-1);
  return (
    <>
      <Panel
        title="SPY 日線"
        subtitle={`${run.config.start_date} 至 ${run.config.end_date}`}
        action={
          <div className="chart-legend">
            <span className="legend-item">
              <i style={{ background: "#bb8c46" }} />
              MA20
            </span>
            <span className="legend-item">
              <i style={{ background: "#5379c1" }} />
              MA60
            </span>
          </div>
        }
      >
        <MarketChart bars={bars} trades={trades} />
        <div className="chart-foot">
          <span>箭頭為已完成的模擬成交</span>
          <a
            href="https://www.tradingview.com/"
            target="_blank"
            rel="noreferrer"
          >
            Charts by TradingView
          </a>
        </div>
      </Panel>
      <div className="metrics-strip">
        <Metric
          label="區間末日收盤"
          value={last ? money(last.close, 2) : "—"}
          detail={last?.session_date || ""}
        />
        <Metric
          label="RSI14"
          value={last?.rsi14?.toFixed(2) || "—"}
          detail="Wilder 平滑"
        />
        <Metric
          label="20 日年化波動率"
          value={percent(last?.realized_vol20)}
          detail="日對數報酬 · 樣本標準差"
        />
        <Metric
          label="模擬成交"
          value={`${trades.length} 筆`}
          detail="下一正常交易日開盤"
        />
      </div>
      <Panel title="交易帳本" subtitle="成交價格已包含方向性滑價">
        <TradeTable trades={trades} />
      </Panel>
    </>
  );
}

export function AgentsPage({
  decisions,
  trades,
}: {
  decisions: Decision[];
  trades: Trade[];
}) {
  const [index, setIndex] = useState(0);
  useEffect(() => setIndex(0), [decisions]);
  const decision = decisions[index];
  if (!decision)
    return (
      <Empty title="這個實驗沒有決策紀錄">請選擇包含有效週決策日的實驗。</Empty>
    );
  const trade = trades.find((t) => t.trade_id === decision.trade_id);
  const values: [string, string][] = [
    ["收盤價", "last_close"],
    ["MA20", "ma20"],
    ["MA60", "ma60"],
    ["RSI14", "rsi14"],
    ["ATR14", "atr14"],
    ["年化波動率", "realized_vol20"],
    ["5 日報酬", "return_5d"],
    ["成交量比例", "volume_ratio20"],
  ];
  return (
    <>
      <div className="trace-toolbar">
        <label>
          決策日期
          <select
            aria-label="決策日期"
            value={index}
            onChange={(e) => setIndex(Number(e.target.value))}
          >
            {decisions.map((d, i) => (
              <option key={d.decision_id} value={i}>
                {d.decision_session} · {d.status}
              </option>
            ))}
          </select>
        </label>
        <div className="trace-time">
          <Clock size={15} />
          <span>收盤後分析</span>
          <ChevronRight size={14} />
          <span>{decision.execution_session || "無下一成交日"} 開盤成交</span>
        </div>
        <Status status={decision.status} />
      </div>
      <div className="trace-layout">
        <div className="agent-flow">
          <AgentCard
            step="1"
            title="Analysis"
            role="市場分析"
            call={decision.calls.find((c) => c.stage === "analysis")}
            absent="此策略未使用 Analysis Agent。"
          >
            {decision.analysis_output && (
              <>
                <Badge variant="secondary">
                  {decision.analysis_output.regime}
                </Badge>
                <p>{decision.analysis_output.thesis}</p>
                <Evidence fields={decision.analysis_output.evidence} />
              </>
            )}
          </AgentCard>
          <div className="flow-connector" />
          <AgentCard
            step="2"
            title="Critic"
            role="批判與反證"
            call={decision.calls.find((c) => c.stage === "critic")}
            absent="此策略未使用 Critic Agent。"
          >
            {decision.critic_output && (
              <>
                <div className="feature-tags">
                  {decision.critic_output.risk_flags.map((flag) => (
                    <span key={flag}>{flag}</span>
                  ))}
                </div>
                <p>
                  {decision.critic_output.counterarguments.join(" ") ||
                    "未提出額外反證。"}
                </p>
                <span className="subtle">
                  模型判斷風險：{decision.critic_output.risk_level}
                </span>
              </>
            )}
          </AgentCard>
          <div className="flow-connector" />
          <AgentCard
            step="3"
            title="Decision"
            role="整合建議"
            call={decision.calls.find((c) => c.stage === "decision")}
            absent={
              decision.decision_output
                ? "確定性基準策略，不使用 LLM。"
                : "未產生有效決策。"
            }
          >
            {decision.decision_output && (
              <>
                <div className="decision-proposal">
                  <span>建議倉位</span>
                  <strong>{percent(decision.requested_weight)}</strong>
                  <span>
                    {decision.calls.length
                      ? `模型自評 ${percent(decision.decision_output.confidence)}`
                      : "確定性規則"}
                  </span>
                </div>
                <p>{decision.decision_output.rationale}</p>
                <Evidence fields={decision.decision_output.evidence} />
              </>
            )}
          </AgentCard>
        </div>
        <div className="trace-inspector">
          <Panel title="當時市場快照" subtitle={decision.decision_session}>
            <dl className="feature-list">
              {values.map(([label, key]) => (
                <div key={key}>
                  <dt>{label}</dt>
                  <dd>
                    {decision.snapshot?.[key] == null
                      ? "—"
                      : key.includes("return") || key.includes("vol20")
                        ? percent(Number(decision.snapshot[key]))
                        : Number(decision.snapshot[key]).toFixed(3)}
                  </dd>
                </div>
              ))}
            </dl>
            <p className="panel-note">數值只使用截至當日收盤的資料。</p>
          </Panel>
          <Panel
            title="程式化風控"
            action={<ShieldCheck size={18} className="positive" />}
          >
            <div className="risk-verdict">
              <Status status={decision.status} />
              <strong>核准倉位 {percent(decision.approved_weight)}</strong>
              <p>
                {decision.reason_codes.length
                  ? decision.reason_codes.map(reasonText).join(" / ")
                  : "通過目標倉位與曝險限制檢查。"}
              </p>
            </div>
          </Panel>
          <Panel title="模擬成交結果">
            {trade ? (
              <dl className="feature-list">
                <div>
                  <dt>方向</dt>
                  <dd>{trade.side === "BUY" ? "買入" : "賣出"}</dd>
                </div>
                <div>
                  <dt>成交日期</dt>
                  <dd>{trade.execution_session}</dd>
                </div>
                <div>
                  <dt>股數／成交價</dt>
                  <dd>
                    {trade.quantity.toFixed(3)} /{" "}
                    {money(trade.execution_price, 2)}
                  </dd>
                </div>
                <div>
                  <dt>手續費</dt>
                  <dd>{money(trade.fee, 2)}</dd>
                </div>
              </dl>
            ) : (
              <p className="panel-note">
                {decision.status === "APPROVED"
                  ? "目標已滿足，本次沒有新增成交。"
                  : "本次未新增交易，保留原有持倉。"}
              </p>
            )}
          </Panel>
        </div>
      </div>
      <Panel
        title="可追溯的原始紀錄"
        subtitle="模型輸入、驗證與風控結果；私有 API 原始回應留在本機，不傳回瀏覽器"
      >
        <details className="raw-record">
          <summary>展開 JSON 紀錄</summary>
          <pre>{JSON.stringify(decision, null, 2)}</pre>
        </details>
      </Panel>
    </>
  );
}
function Evidence({ fields }: { fields: string[] }) {
  return (
    <div className="feature-tags">
      {fields.map((field) => (
        <span key={field}>{field}</span>
      ))}
    </div>
  );
}
function AgentCard({
  step,
  title,
  role,
  call,
  absent,
  children,
}: {
  step: string;
  title: string;
  role: string;
  call: Decision["calls"][number] | undefined;
  absent: string;
  children: ReactNode;
}) {
  return (
    <section
      className="agent-card"
      data-testid={`agent-${title.toLowerCase()}`}
    >
      <div className="agent-card-heading">
        <span className="step-circle">{step}</span>
        <div>
          <h2>
            {title}
            <span>{role}</span>
          </h2>
        </div>
        {call && (
          <span
            className={`agent-validation ${call.status === "VALIDATED" ? "positive" : "negative"}`}
          >
            {call.status === "VALIDATED" ? <Check size={14} /> : null}
            {call.status === "VALIDATED" ? "格式通過" : "驗證失敗"}
          </span>
        )}
      </div>
      <div className="agent-body">
        {children}
        {call?.status === "FAILED" && (
          <p className="negative">
            {reasonText(String(call.error_code || "PROVIDER_ERROR"))}
          </p>
        )}
        {!call && <p className="subtle">{absent}</p>}
      </div>
      {call && (
        <div className="agent-card-foot">
          <span>{String(call.actual_model_id || call.model_id)}</span>
          {typeof call.provider_attribution === "object" && call.provider_attribution !== null && (
            <span data-testid="provider-attribution">
              {String((call.provider_attribution as Record<string, unknown>).provider || "")}
              {(call.provider_attribution as Record<string, unknown>).provider === "kuaiapi" && (
                <> · {String((call.provider_attribution as Record<string, unknown>).group || "")} · gateway 標籤，上游未驗證</>
              )}
            </span>
          )}
          <span>
            {call.mode === "replay"
              ? "Replay"
              : `${call.latency_ms.toFixed(0)} ms`}
          </span>
        </div>
      )}
    </section>
  );
}

function BacktestsPage({
  runs,
  selected,
  batch,
  busy,
}: {
  runs: Run[];
  selected: Run;
  batch: () => void;
  busy: boolean;
}) {
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    setComparison(null);
    setError("");
    let ignore = false;
    const keys = [
      "dataset_hash",
      "start_date",
      "end_date",
      "initial_cash",
      "fee_bps",
      "slippage_bps",
      "max_exposure",
      "research_group_id",
    ];
    const compatible = runs.filter(
      (r) =>
        r.status === "COMPLETED" &&
        (r.config.llm_mode === "replay") === (selected.config.llm_mode === "replay") &&
        (!r.config.strategy_id.endsWith("agent") || !selected.config.strategy_id.endsWith("agent") ||
          r.config.llm_mode === selected.config.llm_mode) &&
        keys.every((k) => r.config[k] === selected.config[k]),
    );
    const grouped = new Map<string, Run>();
    for (const r of compatible) {
      if (!grouped.has(r.config.strategy_id))
        grouped.set(r.config.strategy_id, r);
    }
    grouped.set(selected.config.strategy_id, selected);
    if (grouped.size < 2) return;
    api<Comparison>("/experiments/compare", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        run_ids: [...grouped.values()].map((r) => r.run_id),
      }),
    })
      .then((data) => {
        if (!ignore) setComparison(data);
      })
      .catch((e) => {
        if (!ignore) setError(message(e));
      });
    return () => {
      ignore = true;
    };
  }, [runs, selected.run_id]);
  return (
    <>
      <div className="comparison-intro">
        <div>
          <h2>同一套市場，四種決策方法</h2>
          <p>
            {selected.config.start_date} 至 {selected.config.end_date} ·
            初始資金 {money(selected.config.initial_cash)} · 手續費{" "}
            {selected.config.fee_bps} bps · 滑價 {selected.config.slippage_bps}{" "}
            bps
          </p>
        </div>
        <Button onClick={batch} disabled={busy}>
          {busy ? <Loader2 className="spin" size={16} /> : <Plus size={16} />}
          執行四組 Mock 比較
        </Button>
      </div>
      {error && (
        <div className="error-banner" role="alert">
          {error}
        </div>
      )}
      {comparison ? (
        <>
          <Panel
            title="資產曲線比較"
            action={
              <div className="chart-legend">
                {comparison.curves.map((c) => (
                  <span className="legend-item" key={c.run_id}>
                    <i style={{ background: strategyColors[c.strategy_id] }} />
                    {strategyNames[c.strategy_id]}
                  </span>
                ))}
              </div>
            }
          >
            <ComparisonChart comparison={comparison} />
          </Panel>
          <Panel title="績效與研究成本">
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>研究策略</th>
                    <th>模式</th>
                    <th>報酬</th>
                    <th>最大回撤</th>
                    <th>Sharpe</th>
                    <th>成交筆數</th>
                    <th>LLM 估計成本</th>
                  </tr>
                </thead>
                <tbody>
                  {comparison.runs.map((r) => (
                    <tr key={r.run_id}>
                      <td>
                        <span
                          className="table-strategy-dot"
                          style={{
                            background: strategyColors[r.config.strategy_id],
                          }}
                        />
                        {strategyNames[r.config.strategy_id]}
                      </td>
                      <td>{r.decision_origin === "baseline" ? "Baseline" : r.config.llm_mode}</td>
                      <td
                        className={
                          r.metrics!.total_return >= 0 ? "positive" : "negative"
                        }
                      >
                        {percent(r.metrics!.total_return)}
                      </td>
                      <td>{percent(r.metrics!.max_drawdown)}</td>
                      <td>{r.metrics!.sharpe_ratio?.toFixed(2) || "—"}</td>
                      <td>{r.metrics!.trade_count}</td>
                      <td>{money(r.metrics!.llm_cost_usd, 4)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Panel>
          <Panel title="回撤曲線">
            <ComparisonChart comparison={comparison} drawdown />
          </Panel>
          <p className="method-note">
            Mock 比較展示固定規則的系統流程；不能用來判定多代理模型是否較佳。API
            研究需同時揭露模型推論預算、失敗率與歷史知識限制。
          </p>
        </>
      ) : (
        <Empty title="尚無相同設定的策略組合">
          執行四組 Mock
          比較，系統會共用目前資料、期間及交易成本，依序保存四份獨立結果。
        </Empty>
      )}
    </>
  );
}

function RunTable({
  runs,
  selected,
  href,
}: {
  runs: Run[];
  selected?: string;
  href: (p: string) => string;
}) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>研究策略</th>
            <th>期間</th>
            <th>狀態</th>
            <th>模式</th>
            <th>報酬</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr
              key={r.run_id}
              className={r.run_id === selected ? "selected-row" : ""}
            >
              <td>
                <strong>{strategyNames[r.config.strategy_id]}</strong>
                <small>{r.run_id.slice(0, 8)}</small>
              </td>
              <td>
                {r.config.start_date}
                <br />
                <span className="subtle">至 {r.config.end_date}</span>
              </td>
              <td>
                <Status status={r.status} progress={r.progress} />
              </td>
              <td>
                <Badge variant="outline">{r.config.llm_mode}</Badge>
              </td>
              <td
                className={
                  (r.metrics?.total_return || 0) >= 0 ? "positive" : "negative"
                }
              >
                {percent(r.metrics?.total_return)}
              </td>
              <td>
                <Link href={`/agents?run=${r.run_id}`} className="text-link">
                  查看
                  <ChevronRight size={14} />
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {!runs.length && <p className="panel-note">尚未建立任何實驗。</p>}
    </div>
  );
}
function RunsPage({
  runs,
  selected,
  select,
  href,
  replay,
  busy,
}: {
  runs: Run[];
  selected?: string;
  select: (id: string) => void;
  href: (p: string) => string;
  replay: (run: Run) => void;
  busy: boolean;
}) {
  const run = runs.find((r) => r.run_id === selected);
  return (
    <>
      <Panel title="所有實驗" subtitle={`${runs.length} 份最近紀錄`}>
        <RunTable runs={runs} selected={selected} href={href} />
      </Panel>
      {run && (
        <Panel
          title="目前實驗的保存資訊"
          action={
            <div className="button-row">
              {run.status === "COMPLETED" && (
                <>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => replay(run)}
                    disabled={busy}
                  >
                    <PlayIcon />
                    重播這份實驗
                  </Button>
                  <Button variant="outline" size="sm" asChild>
                    <a href={`/api/v1/experiments/${run.run_id}/export`}>
                      <Download size={14} />
                      匯出 JSON
                    </a>
                  </Button>
                </>
              )}
              {run.status === "FAILED" && (
                <Button variant="outline" size="sm" asChild>
                  <a href={`/api/v1/experiments/${run.run_id}/export`}>
                    <Download size={14} />
                    匯出中斷紀錄
                  </a>
                </Button>
              )}
            </div>
          }
        >
          <dl className="provenance-list">
            <div>
              <dt>Run ID</dt>
              <dd>{run.run_id}</dd>
            </div>
            <div>
              <dt>資料集 SHA-256</dt>
              <dd>{String(run.config.dataset_hash || "—")}</dd>
            </div>
            <div>
              <dt>程式版本</dt>
              <dd>
                {run.code_commit?.slice(0, 12) || "無 Git metadata · 請以來源雜湊核對"}
                {run.code_commit && run.code_dirty ? " · 含尚未提交修改" : ""}
              </dd>
            </div>
            <div>
              <dt>來源程式雜湊</dt>
              <dd>{run.source_hash}</dd>
            </div>
            <div>
              <dt>建立時間</dt>
              <dd>{new Date(run.created_at).toLocaleString("zh-TW")}</dd>
            </div>
          </dl>
          <details className="raw-record">
            <summary>查看完整實驗設定</summary>
            <pre>{JSON.stringify(run.config, null, 2)}</pre>
          </details>
        </Panel>
      )}
    </>
  );
}
function PlayIcon() {
  return <RefreshCw size={14} />;
}
export function TradeTable({ trades }: { trades: Trade[] }) {
  return (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>成交日期</th>
            <th>方向</th>
            <th>股數</th>
            <th>成交價</th>
            <th>手續費</th>
            <th>滑價影響</th>
            <th>交易後現金</th>
          </tr>
        </thead>
        <tbody>
          {trades.map((t) => (
            <tr key={t.trade_id}>
              <td>{t.execution_session}</td>
              <td>
                <span className={t.side === "BUY" ? "positive" : "negative"}>
                  {t.side === "BUY" ? "買入" : "賣出"}
                </span>
              </td>
              <td>{t.quantity.toFixed(4)}</td>
              <td>{money(t.execution_price, 2)}</td>
              <td>{money(t.fee, 2)}</td>
              <td>{money(t.slippage_cost, 2)}</td>
              <td>{money(t.cash_after, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {!trades.length && <p className="panel-note">本實驗沒有模擬成交。</p>}
    </div>
  );
}

function SettingsPage({
  datasets,
  capabilities,
}: {
  datasets: Dataset[];
  capabilities: Capabilities | null;
}) {
  const [values, setValues] = useState(defaults);
  const [saved, setSaved] = useState(false);
  useEffect(() => setValues(readDefaults()), []);
  function save(e: FormEvent) {
    e.preventDefault();
    localStorage.setItem("quantagent-defaults", JSON.stringify(values));
    setSaved(true);
  }
  return (
    <div className="settings-grid">
      <Panel
        title="新實驗預設值"
        subtitle="保存在這個瀏覽器，已完成的實驗保留原設定。"
      >
        <form onSubmit={save} className="settings-form">
          {[
            ["初始資金（USD）", "initial_cash"],
            ["手續費（bps）", "fee_bps"],
            ["滑價（bps）", "slippage_bps"],
            ["模型呼叫上限", "max_llm_calls"],
            ["模型費用上限（USD）", "max_llm_budget_usd"],
          ].map(([label, key]) => (
            <label key={key}>
              {label}
              <input
                type="number"
                required
                min={key === "initial_cash" ? 1 : 0}
                max={key.includes("bps") ? 100 : undefined}
                step={key === "max_llm_budget_usd" ? "0.01" : "1"}
                value={values[key as keyof typeof defaults]}
                onChange={(e) => {
                  setValues({ ...values, [key]: Number(e.target.value) });
                  setSaved(false);
                }}
              />
            </label>
          ))}
          <Button type="submit">
            {saved ? (
              <>
                <Check size={16} />
                已儲存
              </>
            ) : (
              "儲存預設值"
            )}
          </Button>
        </form>
      </Panel>
      <div>
        <Panel title="模型與執行環境">
          <dl className="feature-list">
            <div>
              <dt>離線 Mock / Replay</dt>
              <dd className="positive">可用</dd>
            </div>
            <div>
              <dt>API 模型</dt>
              <dd>
                {capabilities?.api.available
                  ? capabilities.api.model_id
                  : "尚未配置"}
              </dd>
            </div>
            <div>
              <dt>交易日曆</dt>
              <dd>{capabilities?.calendar || "—"}</dd>
            </div>
            <div>
              <dt>回測引擎</dt>
              <dd>{capabilities?.engine_version || "—"}</dd>
            </div>
          </dl>
          <p className="panel-note">
            此應用不讀取模型金鑰，也不啟用即時 API。離線 Mock / Replay 可完整使用。合成夾具會另外標示，不是已驗證的即時績效。
          </p>
        </Panel>
        <Panel title="可用資料集">
          {datasets.map((d) => (
            <div className="dataset-entry" key={d.id}>
              <div>
                <strong>{d.id}</strong>
                <Badge variant="outline">
                  {d.kind === "synthetic" ? "合成行情" : "歷史行情"}
                </Badge>
              </div>
              <p>
                {d.start_date} 至 {d.end_date} · {d.rows.toLocaleString()}{" "}
                個交易日
              </p>
              <span>{d.source}</span>
            </div>
          ))}
        </Panel>
      </div>
    </div>
  );
}

function ExperimentDialog({
  open,
  onOpenChange,
  datasets,
  capabilities,
  submit,
  busy,
}: {
  open: boolean;
  onOpenChange: (value: boolean) => void;
  datasets: Dataset[];
  capabilities: Capabilities | null;
  submit: (config: Partial<Config>) => Promise<void>;
  busy: boolean;
}) {
  const [form, setForm] = useState({
    dataset_id: "synthetic-spy-v1",
    strategy_id: "multi_agent",
    llm_mode: "mock",
    start_date: "2024-01-01",
    end_date: "2024-12-31",
    ...defaults,
  });
  const [error, setError] = useState("");
  useEffect(() => {
    if (open) {
      setForm((f) => ({ ...f, ...readDefaults() }));
      setError("");
    }
  }, [open]);
  async function send(e: FormEvent) {
    e.preventDefault();
    setError("");
    try {
      await submit(form);
    } catch (e) {
      setError(message(e));
    }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="experiment-dialog">
        <DialogHeader>
          <DialogTitle>建立研究實驗</DialogTitle>
          <DialogDescription>
            設定一份可追溯、可重播的歷史模擬。
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={send} className="experiment-form">
          <label>
            資料集
            <select
              aria-label="資料集"
              value={form.dataset_id}
              onChange={(e) => setForm({ ...form, dataset_id: e.target.value })}
            >
              {datasets.map((d) => (
                <option key={d.id} value={d.id}>
                  {d.kind === "synthetic" ? "DEMO 合成行情" : "歷史行情"} ·{" "}
                  {d.id}
                </option>
              ))}
            </select>
          </label>
          <div className="form-row">
            <label>
              研究策略
              <select
                aria-label="研究策略"
                value={form.strategy_id}
                onChange={(e) =>
                  setForm({ ...form, strategy_id: e.target.value })
                }
              >
                {Object.entries(strategyNames).map(([id, name]) => (
                  <option key={id} value={id}>
                    {name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              模型模式
              <select
                aria-label="模型模式"
                value={form.llm_mode}
                onChange={(e) => setForm({ ...form, llm_mode: e.target.value })}
              >
                <option value="mock">Mock（離線固定規則）</option>
                {capabilities?.api.available && (
                  <option value="api">API（實際模型，會產生費用）</option>
                )}
              </select>
            </label>
          </div>
          <div className="form-row">
            <label>
              開始日期
              <input
                type="date"
                required
                aria-label="開始日期"
                value={form.start_date}
                onChange={(e) =>
                  setForm({ ...form, start_date: e.target.value })
                }
              />
            </label>
            <label>
              結束日期
              <input
                type="date"
                required
                aria-label="結束日期"
                value={form.end_date}
                onChange={(e) => setForm({ ...form, end_date: e.target.value })}
              />
            </label>
          </div>
          <div className="form-row three">
            <label>
              初始資金（USD）
              <input
                type="number"
                min="1"
                max="1000000000000"
                required
                value={form.initial_cash}
                onChange={(e) =>
                  setForm({ ...form, initial_cash: Number(e.target.value) })
                }
              />
            </label>
            <label>
              手續費（bps）
              <input
                type="number"
                min="0"
                max="100"
                required
                value={form.fee_bps}
                onChange={(e) =>
                  setForm({ ...form, fee_bps: Number(e.target.value) })
                }
              />
            </label>
            <label>
              滑價（bps）
              <input
                type="number"
                min="0"
                max="100"
                required
                value={form.slippage_bps}
                onChange={(e) =>
                  setForm({ ...form, slippage_bps: Number(e.target.value) })
                }
              />
            </label>
          </div>
          {form.llm_mode === "api" && (
            <div className="form-row">
              <label>
                模型呼叫上限
                <input
                  type="number"
                  min="0"
                  value={form.max_llm_calls}
                  onChange={(e) =>
                    setForm({ ...form, max_llm_calls: Number(e.target.value) })
                  }
                />
              </label>
              <label>
                費用上限（USD）
                <input
                  type="number"
                  min="0"
                  step="0.01"
                  value={form.max_llm_budget_usd}
                  onChange={(e) =>
                    setForm({
                      ...form,
                      max_llm_budget_usd: Number(e.target.value),
                    })
                  }
                />
              </label>
            </div>
          )}
          <div className="form-note">
            <ShieldCheck size={16} />
            <p>
              僅做多、無槓桿。每週收盤決策，下一交易日開盤模擬成交。
              {form.llm_mode === "mock"
                ? "Mock 結果只供系統流程驗證。"
                : "API 呼叫將依後端設定的模型與費率執行。"}
            </p>
          </div>
          {error && (
            <p role="alert" className="negative">
              {error}
            </p>
          )}
          <div className="dialog-actions">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              取消
            </Button>
            <Button type="submit" disabled={busy}>
              {busy ? (
                <Loader2 size={16} className="spin" />
              ) : (
                <FlaskConical size={16} />
              )}
              啟動研究
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
