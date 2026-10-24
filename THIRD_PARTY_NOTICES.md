# 第三方來源與授權

直接依賴以 `pyproject.toml`、`frontend/package.json` 與兩份 lockfile 為準；完整依賴仍各自受原套件授權約束。本清單不是第三方程式碼的自有著作權聲明。

| 套件 | 授權 | 使用方式 |
| --- | --- | --- |
| Next.js、React、React DOM、Tailwind CSS | MIT | Web 框架與樣式工具 |
| TypeScript | Apache-2.0 | 型別檢查 |
| FastAPI、Pydantic、LangGraph、pytest | MIT | API、驗證、固定 Agent 圖與測試 |
| pandas、NumPy、uvicorn、HTTPX | BSD-3-Clause（NumPy 另含其發行附帶的第三方授權） | 特徵、數值、服務與 HTTP 轉接 |
| exchange-calendars | Apache-2.0 | XNYS 交易日曆 |
| Lightweight Charts | Apache-2.0 | K 線圖；圖表保留 TradingView attribution logo 與連結 |
| Recharts、Radix UI、cn | MIT | 圖表及介面基礎 |
| class-variance-authority | Apache-2.0 | 元件樣式變體 |
| Lucide React | ISC | 介面圖示 |
| Playwright | Apache-2.0 | 瀏覽器驗證 |
| yfinance（選用） | Apache-2.0 | 個人研究行情下載轉接，不賦予 Yahoo 行情再散布權利 |

[Lightweight Charts 歸屬要求](https://github.com/tradingview/lightweight-charts#license)、[TradingView](https://www.tradingview.com/)。安裝套件內的 LICENSE 與原始發行條款為準。

TradingAgents 與 AI Hedge Fund 僅參考設計，未複製其實作；固定查核 commit 與路徑見 `docs/REUSE_AUDIT.md`。

## 傳遞依賴與散布範圍

`docs/DEPENDENCY_LICENSES.json` 記錄 70 個本機 Python 套件（含選用行情群組）及 npm lockfile 的 240 個套件項目；僅為版本綁定的授權 metadata 盤點，不是完整法律認證。peewee 4.5.3 的 metadata 未宣告授權，已另讀其安裝包 LICENSE 核對 MIT 文字。

Next.js 的 sharp/libvips 選用平台套件包含 LGPL-3.0-or-later，lightningcss（開發工具）為 MPL-2.0，caniuse-lite 資料為 CC-BY-4.0。這些不是本專案自有程式碼的授權。此候選版本只散布原始碼、lockfile 與合成展示素材，不散布 node_modules、原生執行檔、容器或 Next standalone bundle；未來若散布套件或二進位，需依實際產物保留各項授權、歸屬與所需原始碼／提供方式，另做散布審查。不得把直接依賴清單當成所有依賴皆為 MIT 的證明。

`frontend/src/components/ui/` 由 shadcn CLI 產生，保留下列 MIT notice：

MIT License

Copyright (c) 2023 shadcn

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
