# QuantAgent Lab

**多代理量化交易研究與可視化平台** · 目標版本 `v0.1.0-alpha`

[English](README.md) · [系統架構](docs/ARCHITECTURE.md) · [研究方法](docs/RESEARCH_METHODOLOGY.md)

以 SPY 格式日線、每週收盤決策與下一交易日開盤模擬成交，呈現市場特徵、Agent 分工、確定性風控、帳本及績效。初版可在**沒有 API Key** 的情況下，以合成資料與 Mock／Replay 操作完整流程。

目前為發布準備版本，尚未公開發布。Mock 是固定規則；真實模型轉接器僅以 HTTP fixture 驗證，尚未進行付費模型研究。沒有券商下單功能。

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

合成資料有 2018–2025 年共 2,011 個交易日，**不是歷史 SPY 價格**。重播範例由本機新建的 Mock 實驗產生，不需私人資料庫。[操作短片](docs/images/walkthrough.webm)也只展示這份合成資料。

真實行情下載、API 設定、費用、備份及驗證指令詳見 [英文 README](README.md)。`.env.example` 只列設定名稱，程式不會自動讀取 `.env`；不要把金鑰放入前端、截圖、日誌或 Git。

## 研究與發布限制

本版僅計價格報酬，不含股息與現金利息，不強制期末平倉。Mock 績效不能作為多代理 LLM 較好的證據。歷史實驗不能排除模型已在預訓練見過市場事件；2025 僅是本專案開發的保留期間。

私人行情、資料庫、API 回應與開發設定不供公開散布。專案已選定 [MIT](LICENSE)，署名 QuantAgent Lab contributors；授權審查見 [紀錄](docs/LICENSE_PROPOSAL.md)；正式發布需完成 [檢查表](docs/PUBLIC_RELEASE_CHECKLIST.md)。目前沒有公開網站部署或遠端 CI 成功紀錄。
