# 第 0 輪重用審查

查核日期：2026-10-24。深度限定為下列 LICENSE、依賴清單與指定原始碼，未聲稱完成外部專案的全面審計。

| 來源與固定版本 | 已讀證據 | 決策 |
| --- | --- | --- |
| TradingAgents `e8f5503e9b86d5c2710b0c76b1f686703968c9df` | LICENSE（Apache-2.0）、pyproject.toml、graph/setup.py、backtest.py | 參考圖狀流程與隔離各角色輸入；不引入整套依賴。其 backtest 明確是獨立決策品質評估，不含持倉、成交或現金帳本，不能代替本案回測。 |
| AI Hedge Fund `4e1657ddc689e3939c481a8f0955444b10f02dc9` | LICENSE（MIT）、pyproject.toml、risk/limits.py、backtesting/fund.py | 參考確定性風控與逐日推進設計。現有 next-close、複數標的、可正負權重與本案下一開盤、單標的、僅做多不同；不直接複製。 |
| LangGraph 1.2.17 | 官方 Graph API、Context7 的 START/END、StateGraph、compile/invoke 範例；PyPI metadata | 重用固定 Analysis → Critic → Decision 編排，不加入任意工具或自由辯論。 |
| Lightweight Charts 5.2.1 | 官方 v5 migration/getting-started/series-markers；npm metadata | 重用 K 線與交易標記。採 v5 `addSeries(CandlestickSeries)`；保留 TradingView 歸屬連結與套件授權／NOTICE。 |
| shadcn CLI 4.22.1 | 官方 MCP 說明、已完成本機啟動握手 | 用於產生基本介面元件，元件隨本專案維護；不取用外部交易產品整頁。 |
| Recharts 3.10.1 | npm peer dependencies，React 19 相容範圍 | 重用資產、回撤曲線；型別檢查與 build 為本機整合驗收。 |
| exchange-calendars 4.13.2 | 官方 README 的 get_calendar/sessions_in_range/session_close；PyPI metadata | 重用 XNYS 交易日曆，避免以行情缺漏推測休市；MVP 限美股一般日線交易時段。 |

自行實作：資料快照與時間契約、Wilder 指標種子、單標的成本帳本、共同決策日程、Agent 結構驗證、Replay 配對、實驗版本與比較、Decision Explorer。

較簡單的替代是 Python 依序呼叫三個函式；本案保留藍圖指定的 LangGraph，僅用固定圖，額外框架負擔以 smoke test 驗證。若框架不相容，先保留單一序列入口，再重新評估替代，不改動回測契約。

證據連結：[TradingAgents 原始碼](https://github.com/TauricResearch/TradingAgents/tree/e8f5503e9b86d5c2710b0c76b1f686703968c9df)、[AI Hedge Fund 原始碼](https://github.com/virattt/ai-hedge-fund/tree/4e1657ddc689e3939c481a8f0955444b10f02dc9)、[LangGraph API](https://docs.langchain.com/oss/python/langgraph/graph-api)、[Lightweight Charts v5](https://tradingview.github.io/lightweight-charts/docs/migrations/from-v4-to-v5)、[exchange-calendars](https://github.com/gerrymanoim/exchange_calendars)。
