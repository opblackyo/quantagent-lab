# QuantAgent Lab

**多代理量化交易研究與可視化平台** · Research alpha 軟體結案（2026-10-29）

[English](README.md) · [系統架構](docs/ARCHITECTURE.md) · [研究方法](docs/RESEARCH_METHODOLOGY.md)

以 SPY 格式日線、每週收盤決策與下一交易日開盤模擬成交，呈現市場特徵、Agent 分工、確定性風控、帳本及績效。初版可在**沒有 API Key** 的情況下，以合成資料與 Mock／Replay 操作完整流程。

本機研究／教育軟體範圍已結案，原始碼[早已公開](https://github.com/opblackyo/quantagent-lab)；本次提供經審查的更新快照。預設以合成資料與固定規則 Mock／Replay 操作，不需金鑰。最終收尾的真實 UI gateway 驗收因結算單位／費用驗證未解，在送出推論前停止，不能稱為 live acceptance 通過。初步證據尚未建立可靠交易獲利或產品營收；私人歷史研究不隨本快照散布。沒有券商下單或公開網站部署。詳見[操作指南](docs/OPERATOR_GUIDE.md)及[結案限制](docs/RESEARCH_CLOSEOUT.md)。

![研究總覽：合成資料與 Mock 決策](docs/images/dashboard.png)

## 已完成

- 研究總覽、市場分析、Agent 決策、策略比較、實驗紀錄及研究設定六頁。
- Buy & Hold、MA20/MA60、Single-Agent 與 Multi-Agent 四組策略。
- 資料完整性、交易日曆與 SHA-256；均線、Wilder RSI/ATR、波動率等特徵。
- 下一開盤成交、手續費、滑價、現金／持股帳本、資產與回撤曲線。
- 模型輸入、原始回應、驗證、風控、成交與費用的追蹤；Replay 和 JSON 匯出。
- 呼叫／預算限制、背景工作者、重複提交防護及中斷診斷保存。

## 多 Agent 怎麼運作

每週最後交易日收盤，由程式建立當時可取得的特徵。LangGraph 依序執行 **Analysis → Critic → Decision**：市場分析提出觀點，批判角色檢查反證，決策角色綜合前序結果並建議 0%、50% 或 100% 倉位。三者不是自由辯論，也不必使用不同模型。

建議經格式驗證與程式化風控後，才在下一有效交易日開盤模擬成交。Agent 不能修改帳本。模型失敗或預算用盡會跳過新交易並保留原持倉；核准的 0% 倉位則是主動清倉。

![Agent 決策探索：合成資料與固定 Mock 規則](docs/images/agents.png)

## 本機啟動

需要 Python 3.13、uv、Node.js 22、npm。在專案根目錄執行：

```sh
uv sync --locked
npm --prefix frontend ci
node tools/dev.mjs
```

開啟 [研究工作台](http://127.0.0.1:3000)。按「建立實驗」，保留 `synthetic-spy-v1` 與 Mock，選擇 Multi-Agent。之後從「Agent 決策」切換日期，查看三角色與原始 JSON。「策略比較」可執行四組 Mock；「實驗紀錄」可重播已完成實驗。Ctrl+C 停止前後端。

若預設埠已被占用，保留其他服務，改選空閒埠：

```sh
node tools/dev.mjs --backend-port 18411 --frontend-port 13411
```

此時開啟 `http://127.0.0.1:13411`；指定後端埠也會更新前端 proxy。Launcher 會在啟動子程序前拒絕已占用的埠，不會停止其他服務。選項測試：`node --test tools/dev_options.test.mjs`。

合成資料有 2018–2025 年共 2,011 個交易日，**不是歷史 SPY 價格**。重播範例由本機新建的 Mock 實驗產生，不需私人資料庫。[操作短片](docs/images/walkthrough.webm)也只展示這份合成資料。

Source ZIP 刻意不含 `.git`，其實驗匯出沒有 Git commit metadata；請用畫面的來源程式雜湊及交付附帶的外部檔案／commit manifest 核對版本。UI 會區分 Git metadata 不可用與實際偵測到修改的 checkout。

真實行情下載、API 設定、費用、備份及驗證指令詳見 [英文 README](README.md)。`.env.example` 只列設定名稱，程式不會自動讀取 `.env`；不要把金鑰放入前端、截圖、日誌或 Git。

## 研究與發布限制

本版僅計價格報酬，不含股息與現金利息，不強制期末平倉。Mock 績效不能作為多代理 LLM 較好的證據。歷史實驗不能排除模型已在預訓練見過市場事件；2025 僅是本專案開發的保留期間。

私人行情、資料庫、API 回應與開發設定不供公開散布。專案已選定 [MIT](LICENSE)，署名 QuantAgent Lab contributors；授權審查見 [紀錄](docs/LICENSE_PROPOSAL.md)；正式發布需完成 [檢查表](docs/PUBLIC_RELEASE_CHECKLIST.md)。初次發布的 [GitHub CI](https://github.com/opblackyo/quantagent-lab/actions/runs/40076415541) 已通過 48 個 Python 測試、4 個 Chromium E2E、型別檢查與建置；此紀錄不涵蓋後續本機變更。目前沒有公開網站部署。

一般 HTTP 路由只公布 Mock／Replay 能力，會拒絕 direct 與 gateway 的付費 Agent 啟動；adapter 實作與後端環境變數並不代表該路由已開放付費模式。另有實驗性 `tools/run_live_operator.py`，以伺服器端核准 bundle、HTTPS client 與延遲私密金鑰載入接合有界 UI 路徑。正式 HTTP 路徑缺少獨立真實扣款證據時會保留完整預留，不把合成 usage 或 token estimate 當成 actual。這不是可直接投入使用的 live billing 整合，本次也未完成付費 UI 驗收。UI 支援本機保存結果、Replay 與 JSON 匯出，尚無瀏覽器上傳匯入；無金鑰 Mock／Replay 為支援的展示路徑。

HTTPX 的 connect/read/write/pool 預設各 30 秒，屬階段／資料塊等待限制，不是總執行時限。私人 runner 可另凍結有限的伺服器端 `timeout_policy`，不送入模型 JSON；逾時保留費用預留與分類診斷，不自動重送。

有界私人研究見[設定與重現說明](docs/REAL_MODEL_PILOT.md)。任何新的付費實驗均須具備該次明確模型、價格、總預算批准與私人後端金鑰設定；新程式、研究結果及行情資料未獲自動發布授權。

本次結案不新增功能／研究里程碑。當前快照的完整檢查以 [GitHub Actions](https://github.com/opblackyo/quantagent-lab/actions/workflows/ci.yml) 與對應 release 為準；上面的初次 CI 數量是歷史紀錄。
