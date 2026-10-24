# AI Multi-Agent Quantitative Trading Research Platform

**Software Development Blueprint / PRD / Technical Specification**

文件版本：v0.1\
開發方式：Codex 分階段實作，每階段完成測試後才能進入下一階段\
產品類型：本地端優先的量化交易研究與可視化 Web 平台\
預期開發週期：12 週\
第一版目標：Research MVP，不包含實盤交易

---

## 1. 產品願景與核心定位

本專案旨在建立一套結合大型語言模型、多代理協作、量化交易研究、風險管理、歷史回測及可視化分析的 Web 平台。

使用者可以選擇歷史資料與研究策略，執行單代理或多代理 AI 決策流程，檢視每個 Agent 的輸入、分析、反對意見及最終交易建議，再透過一致的風控及回測引擎比較策略表現。

**產品必須同時滿足三個核心條件：**

- **可操作**：具備真正連接後端的 Web UI，不能只有靜態畫面或 CLI。
- **可驗證**：回測結果、交易成本、持倉、風控與績效指標必須由確定性程式計算。
- **可追溯**：每次 Agent 決策都能追溯到輸入資料、模型輸出、風控結果及實際模擬交易。

本專案不是 AI 股價預測產品，也不是實盤自動交易機器人。

## 2. MVP 範圍

### 2.1 必須完成

| 模組                 | MVP 功能                                  |
| ------------------ | --------------------------------------- |
| Market Data        | 讀取歷史日線 OHLCV、驗證資料、快取資料集                 |
| Feature Engine     | MA20、MA60、RSI14、ATR14、波動率、報酬率、成交量特徵     |
| Strategy Baselines | Buy & Hold、MA20/MA60 規則策略               |
| Single-Agent       | 一個 LLM 直接根據結構化市場特徵提出交易建議                |
| Multi-Agent        | Analysis、Critic、Decision 三個 Agent       |
| Risk Engine        | 倉位限制、禁止槓桿與放空、無效訊號攔截                     |
| Backtest Engine    | 下一交易日開盤成交、手續費、滑價、現金與持倉帳本                |
| Experiment Engine  | 執行研究、儲存設定、比較四組策略                        |
| Web UI             | Dashboard、市場圖表、Agent Trace、回測比較、執行紀錄、設定 |
| Testing            | 核心單元測試、整合測試、瀏覽器端對端測試                    |
| Documentation      | README、架構文件、實驗方法、授權清單及操作說明              |

### 2.2 第二階段擴充

增加其他 ETF、跨標的比較、Paper Trading 帳本、風險情境分析、Agent Prompt 比較、更多回測期間與自動化報告。

### 2.3 暫不開發

即時高頻交易、真實券商下單、期權、期貨、槓桿、放空、加密貨幣實盤、新聞爬蟲、多模態圖像判斷、自動調整 Prompt 的自我演化 Agent、複雜分散式微服務及使用者付費訂閱。

## 3. 技術選型

| 層級              | 技術                             | 用途               |
| --------------- | ------------------------------ | ---------------- |
| Frontend        | Next.js、TypeScript             | Web 應用           |
| UI              | Tailwind CSS、shadcn/ui         | 介面元件             |
| Financial Chart | TradingView Lightweight Charts | K 線與交易標記         |
| Analytics Chart | Recharts                       | 資產曲線、回撤、比較圖      |
| Backend         | Python、FastAPI                 | API 與研究流程        |
| Validation      | Pydantic                       | Agent 與 API 資料驗證 |
| Data Analysis   | pandas、NumPy                   | 市場資料與技術指標        |
| Agent Workflow  | LangGraph                      | 固定多代理工作流程        |
| Database        | SQLite                         | 實驗與決策紀錄          |
| Data Artifacts  | CSV、JSON                       | 行情快照及研究輸出        |
| Python Tests    | pytest                         | 單元與整合測試          |
| Browser Tests   | Playwright Test                | UI 端對端測試         |

所有套件版本由 Codex 在第一階段查證相容性後鎖定，不預先猜測版本號。

回測核心第一版採用**小型、自行實作且有完整測試的單標的交易帳本**。不直接複製大型回測框架，原因是本專案需要精確控制下一根 K 線開盤成交、Agent 決策快取、成本計算及策略比較。未來可加入外部回測引擎作交叉驗證。

## 4. 系統架構

### 4.1 分層設計

**Presentation Layer**

Next.js Dashboard、K 線圖、Agent Trace、策略比較、研究執行管理。

**API Layer**

FastAPI 負責接收實驗設定、啟動研究任務、查詢狀態、取得歷史資料及輸出結果。

**Research Orchestration Layer**

管理研究任務、資料快照、策略選擇、Agent 工作流程、快取與執行紀錄。

**Agent Layer**

Analysis Agent、Critic Agent、Decision Agent，以及單代理比較模式。

**Deterministic Quant Layer**

Feature Engine、Risk Engine、Execution Simulator、Portfolio Ledger、Performance Metrics。

**Storage Layer**

SQLite、版本化行情 CSV、研究設定 JSON、Agent 決策紀錄與實驗產物。

### 4.2 資料流程

1. 使用者從 Web UI 選擇研究策略、資料期間及執行模式。
2. Backend 載入固定版本的歷史行情。
3. Feature Engine 在每個決策時間點建立不包含未來資訊的市場快照。
4. 策略模組依設定產生目標倉位。
5. 若為 Multi-Agent，依序執行 Analysis → Critic → Decision。
6. Pydantic 驗證 LLM 輸出。
7. Risk Engine 依確定性規則審核目標倉位。
8. Backtest Engine 在下一個交易日開盤模擬成交。
9. 系統更新現金、持倉與資產淨值。
10. 儲存每次決策、交易、風控及績效。
11. Frontend 顯示研究結果與完整決策歷程。

Agent 不得直接修改交易帳本，也不得自行呼叫券商下單。

## 5. 第一版市場與資料規則

### 5.1 研究標的

第一版固定使用 **SPY（美股 ETF）** 作為主要研究標的。

資料頻率為日線，貨幣為 USD，市場時間以 `America/New_York` 為準。

### 5.2 研究期間

| 用途        | 時間          |
| --------- | ----------- |
| 資料準備與指標暖機 | 自 2018 年起   |
| 開發與策略設計   | 2019–2023 年 |
| 驗證期間      | 2024 年      |
| 最終保留測試期間  | 2025 年      |

在正式最終測試前，必須先固定 Agent Prompt、策略規則及風控參數。不得因 2025 年結果不佳而反覆調整後仍宣稱 2025 年是未使用過的測試集。

### 5.3 資料來源

系統核心必須支援從本地 CSV 讀取行情，確保不依賴外部 API 也能執行測試與展示。

可額外建立 `yfinance` 研究資料轉接器，但正式研究前須確認其資料欄位、調整方式、使用限制及授權條件。

每份資料集需保存來源、取得日期、涵蓋期間、資料筆數、調整方式與 SHA-256 雜湊值。

### 5.4 資料完整性

載入時必須檢查：

- 日期不得重複或倒序。
- OHLC 價格必須大於零。
- `high` 不得低於 `open`、`close` 或 `low`。
- `low` 不得高於 `open`、`close` 或 `high`。
- 成交量不得為負值。
- 指標暖機期間不足時不得產生交易訊號。
- 缺失交易日不得自行填入虛構價格。

第一版使用未調整價格進行價格型回測，不計入股息再投資，因此績效屬於 **price-only return**，不能宣稱等同完整總報酬。若後續支援股息與公司行動，須另行版本化並重新驗證帳本。

## 6. 技術指標規格

Feature Engine 在每個已收盤交易日計算：

| 欄位               | 定義                      |
| ---------------- | ----------------------- |
| `last_close`     | 當日收盤價                   |
| `ma20`           | 20 個交易日收盤價簡單移動平均        |
| `ma60`           | 60 個交易日收盤價簡單移動平均        |
| `rsi14`          | 14 期 RSI                |
| `atr14`          | 14 期 Average True Range |
| `atr_pct14`      | `atr14 / last_close`    |
| `realized_vol20` | 最近 20 個日對數報酬標準差乘以 √252  |
| `return_5d`      | 最近 5 個交易日價格報酬           |
| `volume_ratio20` | 當日成交量除以最近 20 日平均成交量     |

RSI 與 ATR 的平滑公式必須在程式與測試文件中固定，避免不同套件的公式差異造成不可重現的結果。

所有特徵只可使用截至該決策日收盤為止的資料。

## 7. Agent 架構與責任

### 7.1 Analysis Agent

**目的：** 根據結構化技術特徵提出市場狀態分析。

輸入：FeatureSnapshot。

輸出欄位：

| 欄位         | 型別與限制                          |
| ---------- | ------------------------------ |
| `regime`   | `bullish`、`bearish`、`sideways` |
| `thesis`   | 文字，最多 500 字元                   |
| `evidence` | FeatureSnapshot 中存在的特徵欄位名稱陣列   |

不得虛構新聞、企業財報、宏觀數據或輸入中不存在的指標。

### 7.2 Critic Agent

**目的：** 對 Analysis Agent 的論點提出反證與風險提醒。

輸入：FeatureSnapshot、AnalysisOutput。

輸出欄位：

| 欄位                 | 型別與限制                                                                 |
| ------------------ | --------------------------------------------------------------------- |
| `risk_level`       | `low`、`medium`、`high`                                                 |
| `risk_flags`       | `VOLATILITY_HIGH`、`TREND_CONFLICT`、`WEAK_EVIDENCE`、`NO_OBJECTION` 的陣列 |
| `counterarguments` | 文字陣列，最多三項                                                             |

Critic 是分析角色，不是最終風控執行者。

### 7.3 Decision Agent

**目的：** 整合市場分析與批判結果，提出目標倉位。

輸入：FeatureSnapshot、AnalysisOutput、CriticOutput。

輸出欄位：

| 欄位                   | 型別與限制                 |
| -------------------- | --------------------- |
| `target_weight`      | 僅允許 `0.0`、`0.5`、`1.0` |
| `confidence`         | 0 至 1 的模型自評分數         |
| `rationale`          | 決策理由，最多 500 字元        |
| `evidence`           | 有效特徵名稱陣列              |
| `acknowledged_risks` | 已考慮風險的代碼陣列            |

`confidence` 僅代表模型自評，不得直接視為經過校準的獲利機率。

### 7.4 Single-Agent Baseline

使用相同 FeatureSnapshot，由單一 LLM 直接輸出 DecisionOutput，不經過 Analysis 與 Critic。

必須使用相同模型系列、相同資料、相同風控與回測規則，並記錄 token 用量及成本。

多代理使用較多 LLM 呼叫，因此研究報告不得把所有績效差異直接歸因於「代理數量」；需同時揭露推論成本，第二階段可加入相同推論預算的比較實驗。

### 7.5 Agent Workflow

固定流程：

`FeatureSnapshot → Analysis → Critic → Decision → Schema Validation → Risk Engine`

不允許 Agent 自由無限對話，不允許自主搜尋外部網站，也不允許在決策過程中取得未來行情。

所有 LLM 呼叫須有超時、有限次重試、錯誤記錄與失敗後不新增交易的處理。

## 8. 回測與交易執行規格

### 8.1 決策與成交時間

- 每週最後一個有效交易日收盤後產生訊號。
- 下一個有效交易日開盤時模擬成交。
- 不允許使用當日收盤後才知道的訊號，在當日收盤前成交。
- 非決策日維持原有持倉。
- 遇到市場休市，依資料中的下一個有效交易日執行。

### 8.2 投資組合規則

| 參數   | MVP 預設值           |
| ---- | ----------------- |
| 初始資金 | USD 100,000（虛擬）   |
| 允許方向 | 僅做多               |
| 允許槓桿 | 不允許               |
| 允許放空 | 不允許               |
| 目標倉位 | 0%、50%、100%       |
| 最大曝險 | 100%              |
| 手續費  | 每次成交名目金額 5 bps    |
| 滑價   | 每次成交價格方向性調整 5 bps |
| 股數   | 模擬器允許小數股數         |
| 資產估值 | 每個交易日收盤後          |

這些數值是**研究用預設假設**，不是對真實券商費率或市場滑價的描述。所有策略必須使用相同設定。

### 8.3 成交模擬

買入成交價 = 下一交易日開盤價 × `(1 + slippage_rate)`。

賣出成交價 = 下一交易日開盤價 × `(1 - slippage_rate)`。

手續費 = 成交名目金額 × `fee_rate`。

交易數量必須根據可用現金、既有持倉及目標倉位計算，確保買入後現金不為負值、持倉不為負數、總曝險不超過 100%。

需要保存每筆模擬成交的價格、數量、手續費、滑價成本、交易後現金及交易後持倉。

### 8.4 風控引擎

Risk Engine 由確定性 Python 程式實作，負責：

- 檢查目標倉位是否在允許集合內。
- 限制最大曝險為 100%。
- 禁止負現金、負持倉、槓桿及放空。
- 檢查行情與特徵是否有效。
- 檢查 Agent 輸出是否符合 Schema。
- 將審核結果及原因保存至決策紀錄。

若資料或 Agent 輸出無效，系統應標記 `SKIPPED`，不新增交易並保留既有持倉。這與 `target_weight = 0.0` 的主動清倉必須嚴格區分。

### 8.5 回測指標

主要指標：Total Return、CAGR、Sharpe Ratio、Maximum Drawdown。

輔助指標：Sortino Ratio、Calmar Ratio、交易次數、換手率、總手續費、滑價成本、LLM 呼叫次數及成本。

Sharpe 與 Sortino 的第一版無風險利率假設為 0，年化採 252 個交易日。Win Rate 與 Profit Factor 須先明確定義完整交易回合，才能納入正式比較。

## 9. 實驗設計

### 9.1 四組比較

| ID             | 策略               | 決策方式                         |
| -------------- | ---------------- | ---------------------------- |
| `buy_hold`     | Buy & Hold       | 第一個共同可交易日進場並持有               |
| `ma_crossover` | MA20/MA60        | MA20 高於 MA60 時目標 100%，否則 0%  |
| `single_agent` | Single-Agent LLM | 一個 Agent 提出目標倉位              |
| `multi_agent`  | Multi-Agent LLM  | Analysis → Critic → Decision |

所有策略使用相同的起始交易日、行情資料、初始資金、成交規則、交易成本及風控上限。

### 9.2 研究問題

**RQ1：** Multi-Agent 是否改善風險調整後的績效？

**RQ2：** Multi-Agent 的批判流程是否改變交易決策與風險暴露？

**RQ3：** 相對於 Single-Agent，Multi-Agent 額外消耗多少 token、成本及執行時間？

**RQ4：** 透過結構化決策紀錄，是否能完整追溯交易訊號的產生原因？

### 9.3 實驗限制

LLM 即使只接收歷史數值資料，也不能完全排除預訓練知識包含歷史市場資訊的可能。因此 Agent Prompt 不提供真實歷史日期、未來價格、新聞或外部搜尋工具，並在報告中揭露此限制。

正式研究使用已固定的資料集、Prompt 版本與模型設定。API 回應不保證每次完全一致，因此須儲存原始回應及驗證後結果，提供 Replay 模式重現既有實驗。

## 10. 資料模型

以下欄位為第一版專案內部契約。Codex 必須依此建立 Pydantic Models 與資料庫結構，不得在不同模組使用互相矛盾的名稱。

### 10.1 MarketBar

- `symbol`: string
- `session_date`: ISO 8601 日期字串
- `open`: positive float
- `high`: positive float
- `low`: positive float
- `close`: positive float
- `volume`: non-negative integer
- `currency`: string，MVP 固定 USD
- `source`: string
- `adjustment_mode`: string，MVP 固定 `raw`

### 10.2 FeatureSnapshot

- `snapshot_id`: UUID
- `symbol`: string
- `decision_session`: ISO 8601 日期字串
- `last_close`: float
- `ma20`: float
- `ma60`: float
- `rsi14`: float
- `atr14`: float
- `atr_pct14`: float
- `realized_vol20`: float
- `return_5d`: float
- `volume_ratio20`: float
- `dataset_hash`: SHA-256 string
- `feature_version`: string

### 10.3 ExperimentConfig

- `strategy_id`: `buy_hold`、`ma_crossover`、`single_agent`、`multi_agent`
- `symbol`: string，MVP 固定 SPY
- `start_date`: ISO date
- `end_date`: ISO date
- `initial_cash`: float
- `fee_bps`: float
- `slippage_bps`: float
- `max_exposure`: float
- `llm_mode`: `mock`、`api`、`replay`
- `model_provider`: string 或 null
- `model_id`: string 或 null
- `prompt_version`: string 或 null
- `dataset_hash`: SHA-256 string
- `max_llm_calls`: integer
- `max_llm_budget_usd`: float

### 10.4 DecisionRecord

- `decision_id`: UUID
- `run_id`: UUID
- `decision_session`: ISO date
- `execution_session`: ISO date 或 null
- `snapshot_id`: UUID
- `analysis_output`: JSON 或 null
- `critic_output`: JSON 或 null
- `decision_output`: JSON 或 null
- `requested_weight`: float 或 null
- `approved_weight`: float 或 null
- `status`: `APPROVED`、`REJECTED`、`SKIPPED`
- `reason_codes`: string array
- `prompt_hash`: string 或 null
- `model_id`: string 或 null
- `created_at`: ISO datetime

### 10.5 TradeRecord

- `trade_id`: UUID
- `run_id`: UUID
- `execution_session`: ISO date
- `side`: `BUY` 或 `SELL`
- `quantity`: float
- `execution_price`: float
- `notional`: float
- `fee`: float
- `slippage_cost`: float
- `cash_after`: float
- `shares_after`: float

### 10.6 ExperimentRun

- `run_id`: UUID
- `status`: `PENDING`、`RUNNING`、`COMPLETED`、`FAILED`
- `progress`: 0–100
- `config_json`: JSON
- `dataset_hash`: string
- `code_commit`: Git commit hash 或 null
- `started_at`: ISO datetime
- `finished_at`: ISO datetime 或 null
- `error_message`: string 或 null

另外需保存每日 `EquityPoint`、LLM 呼叫的 token 與成本紀錄，以及研究產物路徑。

## 11. 後端 API 規格

API 前綴：`/api/v1`

| Method | Endpoint                          | 功能             |
| ------ | --------------------------------- | -------------- |
| GET    | `/health`                         | 健康檢查           |
| GET    | `/market/bars`                    | 取得歷史行情         |
| POST   | `/experiments`                    | 建立並啟動研究任務      |
| GET    | `/experiments/{run_id}`           | 查詢研究狀態與設定      |
| GET    | `/experiments/{run_id}/decisions` | 取得 Agent 決策紀錄  |
| GET    | `/experiments/{run_id}/trades`    | 取得交易紀錄         |
| GET    | `/experiments/{run_id}/equity`    | 取得資產曲線         |
| GET    | `/experiments/{run_id}/metrics`   | 取得績效指標         |
| POST   | `/experiments/compare`            | 比較多個研究結果       |
| GET    | `/experiments/{run_id}/export`    | 匯出研究資料         |
| GET    | `/system/capabilities`            | 顯示可用的研究模式與模型狀態 |

建立研究任務時回傳 `run_id` 與 `PENDING` 狀態。前端透過輪詢查詢進度，不得讓長時間 LLM 研究阻塞一般 HTTP 請求。

第一版採單機、單工作者背景任務模式；重新啟動服務後，未完成的任務須標記為失敗或提供明確恢復方式，不得顯示永久執行中。

## 12. 前端頁面與 UI 規格

### 12.1 Dashboard — `/`

顯示平台總覽、最新研究任務、主要績效指標、資產曲線、最近決策與風控狀態。

### 12.2 Market Analysis — `/market`

包含標的資訊、K 線圖、MA20/MA60、RSI、波動率與歷史交易標記。支援選擇日期區間與研究執行紀錄。

### 12.3 Agent Decision Explorer — `/agents`

本專案最重要的特色頁面。

必須提供：

- 選擇研究執行與歷史決策日期。
- 顯示當時 FeatureSnapshot。
- 分別展示 Analysis、Critic、Decision Agent 輸出。
- 清楚標示 Agent 之間的意見分歧。
- 顯示 Risk Engine 的核准、拒絕或跳過原因。
- 顯示最終目標倉位及實際模擬成交結果。
- 提供原始結構化資料檢視功能。

### 12.4 Backtest Comparison — `/backtests`

顯示四組策略比較、Equity Curve、Drawdown Curve、績效指標、交易成本及 LLM 成本。

圖表必須標示資料期間與交易成本假設，不能用假資料冒充真實回測。

### 12.5 Experiment Runs — `/runs`

顯示研究任務清單、執行狀態、進度、錯誤訊息、設定快照、決策紀錄及匯出功能。

### 12.6 Settings — `/settings`

設定研究資料期間、策略、交易成本、模型選擇及最大 LLM 呼叫預算。

API 金鑰只允許由 Backend 環境變數讀取，不得顯示於前端或寫入資料庫。

所有頁面必須處理 Loading、Empty、Error 與 Responsive 狀態。Mock 模式的畫面應明確標示 `DEMO / MOCK DATA`。

## 13. LLM 工程與成本控制

第一版提供三種模式：

**Mock Mode**：不呼叫付費模型，使用固定測試回應，適合開發及成果展示。

**API Mode**：使用實際 LLM Provider，執行研究決策並記錄模型名稱、Prompt 版本、token、延遲與成本。

**Replay Mode**：不重新呼叫 LLM，直接使用先前保存的 Agent 輸出，重新執行風控與回測。

必須實作：

- LLM Provider Adapter。
- Pydantic Structured Output Validation。
- Prompt 版本與雜湊。
- API 超時及有限次重試。
- 呼叫次數與預算上限。
- 失敗時跳過交易，不自行編造模型答案。
- Agent Input/Output 日誌。
- 不記錄 API Key 或其他機密資訊。

同一份資料與 Prompt 的快取鍵需包含資料雜湊、特徵版本、模型識別、Prompt 版本及重要模型設定。

## 14. Repository 建議結構

專案採前後端分離的單一 Repository。

| 路徑                         | 職責                      |
| -------------------------- | ----------------------- |
| `frontend/`                | Next.js 應用              |
| `frontend/src/app/`        | 頁面與路由                   |
| `frontend/src/components/` | UI、圖表、Agent Trace 元件    |
| `frontend/src/lib/`        | API Client 與前端型別        |
| `backend/`                 | Python 後端               |
| `backend/app/api/`         | FastAPI Routers         |
| `backend/app/models/`      | Pydantic 與資料模型          |
| `backend/app/data/`        | 資料來源、驗證與快取              |
| `backend/app/features/`    | 技術指標與特徵                 |
| `backend/app/agents/`      | LLM Adapter、Agent 與工作流程 |
| `backend/app/risk/`        | 確定性風控                   |
| `backend/app/backtest/`    | 成交模擬、帳本、績效              |
| `backend/app/experiments/` | 實驗執行、比較與匯出              |
| `backend/app/storage/`     | SQLite 與研究產物            |
| `backend/tests/`           | Python 測試               |
| `frontend/tests/`          | 前端與 Playwright 測試       |
| `data/sample/`             | 可公開的範例資料                |
| `data/cache/`              | 本地行情快取                  |
| `artifacts/`               | 研究結果與圖表                 |
| `docs/`                    | 規格、架構、實驗方法、授權與成果文件      |
| `AGENTS.md`                | Codex 專案開發規範            |

以上為新專案預定建立的路徑，不代表目前已有這些檔案。

## 15. 必須通過的測試

### 15.1 市場資料測試

- 缺失欄位、負價格、重複日期應被拒絕。
- 指標計算不能使用未來 K 線。
- 修改未來資料不得影響過去的 FeatureSnapshot。
- 資料雜湊與研究設定必須正確保存。

### 15.2 回測測試

- 收盤後產生的訊號只能在下一交易日開盤成交。
- 買入與賣出的滑價方向正確。
- 手續費與滑價不能被重複扣除。
- 相同交易下，加入成本後的淨績效不得高於零成本結果。
- 現金不得為負、持倉不得為負。
- 無效 Agent 輸出不得觸發交易。
- 所有策略使用相同的測試起點與成本設定。

### 15.3 Agent 測試

- 非法 JSON 被拒絕。
- 非法 `target_weight` 被拒絕。
- 引用不存在的特徵被拒絕。
- Agent 超時或 API 錯誤後不產生新交易。
- Mock Mode 不需 API Key。
- Replay Mode 不重新呼叫 LLM。

### 15.4 UI 測試

- 使用者能建立 Mock 實驗。
- 能看到實驗狀態由 PENDING 變為 COMPLETED。
- 能開啟 Agent Trace。
- 能切換不同研究結果。
- 能查看 K 線、交易標記與回測曲線。
- API 失敗時顯示清楚錯誤。
- 桌面與手機寬度下無關鍵元件無法操作的問題。

## 16. Codex 開發階段

| 輪次 | 工作內容                          | 驗收條件           |
| -- | ----------------------------- | -------------- |
| 0  | Repository 與開源套件審查            | 產出授權、依賴與重用評估   |
| 1  | 專案骨架、前後端啟動、CI 基礎              | 本地可啟動、健康檢查成功   |
| 2  | 行情資料、驗證、特徵計算                  | CSV 與指標測試通過    |
| 3  | 回測帳本、成交規則、風控                  | 時間與資金測試通過      |
| 4  | Buy & Hold、MA 策略              | 基準回測可執行        |
| 5  | Single-Agent、Mock、Schema      | 單代理決策可保存       |
| 6  | Multi-Agent、LangGraph、Trace   | 三個 Agent 可完整執行 |
| 7  | SQLite、實驗工作者、API              | 研究任務可執行及查詢     |
| 8  | Dashboard、Market、K 線圖         | 真實串接 Backend   |
| 9  | Agent Explorer、Runs、Backtests | 能追溯及比較結果       |
| 10 | 正式比較實驗、成本紀錄、匯出                | 四組研究結果可比較      |
| 11 | 全面測試、修正、文件、展示                 | 完整 MVP 驗收通過    |

每輪必須先檢查現有程式碼，再提出本輪修改計畫。完成後需執行相關測試並報告實際結果。禁止在沒有驗證的情況下宣稱完成。

## 17. 開源重用策略

第一輪先審查：

- TradingAgents：研究 Agent 角色、工作流程及決策紀錄。
- AI Hedge Fund：研究交易訊號、持倉管理及回測架構。
- LangGraph：使用正式框架建立固定工作流程。
- Lightweight Charts：直接使用金融圖表元件。
- shadcn/ui：直接使用現成介面元件。
- Recharts：直接使用績效圖表元件。

優先重用有清楚授權及維護狀態的通用程式庫。不得未經授權直接複製整套交易系統，也不得把第三方原始碼描述為自行創作。

Codex 必須先查驗實際 Repository、LICENSE、版本、API 及相依套件，再決定是否抽取程式碼。

## 18. Definition of Done

只有同時達成下列條件，MVP 才視為完成：

- 可以依 README 在本地啟動前後端。
- 沒有 API Key 也能使用 Mock Mode 操作完整 Demo。
- 可以載入固定歷史行情與產生技術指標。
- 可以執行四種比較策略。
- Agent 輸入與輸出皆經過 Schema 驗證。
- 風控引擎不依賴 LLM 自行計算交易限制。
- 回測不存在已知的未來資料洩漏及錯誤成交時間問題。
- 可以查詢完整 Agent 決策與交易紀錄。
- Dashboard、K 線、Agent Trace、回測比較均連接真實後端資料。
- 可以儲存並重播研究結果。
- 核心自動化測試及端對端測試通過。
- 有可追溯的實驗設定、資料版本與程式版本。
- 有完整 README、研究方法、授權清單與已知限制。
- 沒有實盤交易功能，也沒有暴露 API 金鑰。

**績效是否優於 Buy & Hold 不屬於軟體完成條件。** 研究成果應以可驗證的實驗及分析為準。

## 19. 必須避免的開發錯誤

1. 先做假 Dashboard，最後才發現後端資料格式無法串接。
2. 讓 LLM 自己計算持倉、手續費、報酬或 Sharpe Ratio。
3. 讓 AI 看到回測決策日期之後的行情。
4. 以當日收盤後的訊號在當日收盤前成交。
5. 為了讓 Multi-Agent 看起來更好而使用不同的交易成本或資料期間。
6. 只比較總報酬，不比較回撤、成本及研究限制。
7. 使用真實行情畫面卻搭配未標示的虛構績效。
8. 同時導入太多 Agent、模型、標的及資料來源。
9. 未檢查授權便直接複製外部專案程式碼。
10. 在缺乏測試的情況下直接進入下一輪開發。

## 20. 第一版驗收展示流程

成果展示必須可以依序完成：

1. 啟動 Web 平台並查看 Dashboard。
2. 選擇 SPY、研究期間及 Multi-Agent 模式。
3. 啟動 Mock 或已保存的 Replay 實驗。
4. 查看 Analysis、Critic、Decision 三個 Agent 的輸出。
5. 查看 Risk Engine 最終審核結果。
6. 在 K 線圖上查看模擬交易位置。
7. 開啟四種策略的回測比較。
8. 查看報酬、回撤、Sharpe Ratio、交易成本與 LLM 成本。
9. 選擇任一決策日期，追溯當時輸入及決策。
10. 匯出實驗紀錄，展示可重現性。

以上十個操作必須能在不修改原始碼的情況下完成。

---

**本文件為 Codex 的主要開發規格。遇到規格衝突、資料限制或外部套件不相容時，應先提出問題及修改建議，不得自行改變核心研究方法。**
