export type Dataset = {
  id: string;
  kind: "synthetic" | "historical";
  symbol: string;
  source: string;
  start_date: string;
  end_date: string;
  rows: number;
  sha256: string;
  adjustment_mode: string;
};
export type Config = {
  strategy_id: string;
  dataset_id: string;
  start_date: string;
  end_date: string;
  initial_cash: number;
  fee_bps: number;
  slippage_bps: number;
  llm_mode: string;
  max_llm_calls: number;
  max_llm_budget_usd: number;
  [key: string]: unknown;
};
export type Metrics = {
  total_return: number;
  cagr: number;
  sharpe_ratio: number | null;
  max_drawdown: number;
  final_equity: number;
  total_fees: number;
  slippage_cost: number;
  trade_count: number;
  llm_calls: number;
  llm_cost_usd: number;
  approved_decisions: number;
  skipped_decisions: number;
  exposure_fraction: number;
  [key: string]: number | null;
};
export type Run = {
  run_id: string;
  status: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED";
  progress: number;
  config: Config;
  metrics: Metrics | null;
  dataset: Dataset | null;
  created_at: string;
  code_commit: string | null;
  code_dirty: boolean;
  source_hash: string;
  error_message: string | null;
  decision_origin?: string;
};
export type Call = {
  stage: string;
  status: string;
  mode: string;
  model_id: string;
  latency_ms: number;
  cost_usd: number;
  raw_response: string | null;
  input: Record<string, unknown>;
  [key: string]: unknown;
};
export type Analysis = { regime: string; thesis: string; evidence: string[] };
export type Critic = {
  risk_level: string;
  risk_flags: string[];
  counterarguments: string[];
};
export type DecisionOutput = {
  target_weight: number;
  confidence: number;
  rationale: string;
  evidence: string[];
  acknowledged_risks: string[];
};
export type Decision = {
  decision_id: string;
  decision_session: string;
  execution_session: string | null;
  status: string;
  reason_codes: string[];
  snapshot: Record<string, string | number> | null;
  calls: Call[];
  requested_weight: number | null;
  approved_weight: number | null;
  trade_id: string | null;
  analysis_output: Analysis | null;
  critic_output: Critic | null;
  decision_output: DecisionOutput | null;
};
export type Trade = {
  trade_id: string;
  decision_id: string;
  execution_session: string;
  side: "BUY" | "SELL";
  quantity: number;
  execution_price: number;
  reference_open: number;
  fee: number;
  slippage_cost: number;
  cash_after: number;
  shares_after: number;
};
export type Equity = {
  session_date: string;
  equity: number;
  cash: number;
  shares: number;
  close: number;
  drawdown: number;
};
export type Bar = {
  session_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ma20: number | null;
  ma60: number | null;
  rsi14: number | null;
  realized_vol20: number | null;
};
export type Capabilities = {
  modes: string[];
  api: { available: boolean; provider: string | null; model_id: string | null };
  engine_version: string;
  feature_version: string;
  calendar: string;
  return_basis: string;
};
export type Comparison = {
  runs: Run[];
  curves: { run_id: string; strategy_id: string; equity: Equity[] }[];
  return_basis: string;
};

export const strategyNames: Record<string, string> = {
  buy_hold: "Buy & Hold",
  ma_crossover: "MA20 / MA60",
  single_agent: "Single-Agent",
  multi_agent: "Multi-Agent",
};
export const strategyColors: Record<string, string> = {
  buy_hold: "#7d8ba0",
  ma_crossover: "#c79043",
  single_agent: "#4788b4",
  multi_agent: "#3157c8",
};
export const money = (value: number, digits = 0) =>
  new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: digits,
  }).format(value);
export const percent = (value: number | null | undefined) =>
  value == null ? "—" : `${(value * 100).toFixed(2)}%`;

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/v1${path}`, {
    cache: "no-store",
    ...init,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = Array.isArray(error.detail)
      ? error.detail.map((item: { msg: string }) => item.msg).join("；")
      : error.detail;
    throw new Error(
      typeof detail === "string" ? detail : `請求失敗 (${response.status})`,
    );
  }
  return response.json();
}
const pendingRequestKeys = new Map<string, string>();
export async function createExperiment(config: Partial<Config>) {
  const body = JSON.stringify(config);
  const key = pendingRequestKeys.get(body) || crypto.randomUUID();
  pendingRequestKeys.set(body, key);
  const result = await api<{ run_id: string; status: string }>("/experiments", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Idempotency-Key": key },
    body,
  });
  if (pendingRequestKeys.get(body) === key) pendingRequestKeys.delete(body);
  window.dispatchEvent(new Event("quantagent:updated"));
  return result;
}
