# 驗證紀錄

## 2026-10-24，第 0 輪

- `uv sync --python 3.13 --group market`：成功，Python 3.13.9；72 個解析項，安裝 70 個套件，完整版本存 uv.lock。
- `npm install --save-exact`：成功，直接依賴與 package-lock.json 已建立；使用獨立 npm cache 避開原快取缺檔。
- Python smoke：FastAPI/HTTPX TestClient `/health`、Pydantic 拒絕 NaN、LangGraph START→node→END 的 compile/invoke、XNYS Thanksgiving 休市與隔日提前收盤均通過。
- Node ESM smoke：React 19.3.1 與 Lightweight Charts 5.2.1 的 createChart/CandlestickSeries 可匯入。第一次 CommonJS `require` 失敗，已確認套件只提供 ESM，前端採標準 ESM import。
- Context7 文件查詢成功；Playwright 與 shadcn MCP 已在本次工作階段列為工具。

第 0 輪完成依據為固定來源檢查、明確 v0.2 決策、成功依賴解析與框架 smoke。前端 build、完整回測與 UI 驗收仍屬後續階段。

## 2026-10-24，初版功能驗證

- `uv run --no-sync pytest -q`：**41 passed**，涵蓋資料完整性／雜湊、Wilder 種子、未來資料不影響過去特徵、500 次調倉守恆、Good Friday／下一開盤、無效模型輸出、超時與預算、未知費用預留、Replay 零提供者呼叫、基準策略保存訊號、重啟鎖與修復、並行 idempotency、模型版本比較限制。40 個 warnings 來自 exchange-calendars 使用 pandas 已棄用介面；在固定版本上測試通過，未修改或隱藏供應商程式碼警告。
- `npm run typecheck`（frontend）：通過。依已安裝 Next.js 官方 CLI 文件，先執行 `next typegen` 再 `tsc --noEmit`；忽略自動產生的 next-env.d.ts，確保新 checkout 的 CI 也能先取得路由型別。
- `npm run build`（frontend）：通過，Next.js 16.4.3。包含 `/`、動態六頁研究路由與 icon。
- `npm run test:e2e`（frontend）：**4 passed (28.2s)**，實際啟動 8001/3001，建立→決策日期→三 Agent→原始 JSON→K 線與帳本→四組比較→Replay→下載 JSON；390px 手機操作、設定保存、遺失建立回應後重試不重複建立、服務失敗與重試提示均通過。
- E2E 先前暴露並修正：明確表單可讀名稱；已存在 run 時需等待新 run URL；切換 run 不可短暫套用上一份曲線；依 XNYS 正確預期 2024-01-16 成交（2024-01-15 為休市）。
- Playwright MCP 實際操作建立實驗與 Agent Explorer，並保存桌面截圖；E2E 保存桌面／手機 Trace 截圖。產物在忽略的 artifacts/screenshots 與 frontend/test-results。
- 真實資料下載：`uv run --no-sync --group market python tools/fetch_market_data.py` 成功。2018-01-02–2025-12-31 共 2011 個 SPY 交易日，CSV SHA-256 `79fe3c66ee6493b834160f5386be38c5f03823802de27ff8450f772d61878bdb`；來源與顯式 raw 參數保存在本機 metadata，data/cache 已由 Git 忽略。
- API 轉接器只使用 HTTP fixture 驗證。真實付費模型呼叫數為零；正式單／多模型績效實驗尚未執行。

本輪原始碼、合成資料與 lockfile 會納入本機 Git；真實 Yahoo CSV、SQLite、匯出結果、瀏覽器產物及金鑰不納入版本控制。`.gitattributes` 固定文字 LF，以免跨平台換行破壞 CSV 或 lockfile 的雜湊。

## 2026-10-24，追加中斷保存及交接準備

- 後端：**43 passed (20.58s)**；新增提供者呼叫前已落盤、重啟後中斷紀錄可匯出、完成後事件不可覆寫等驗證。44 個 warnings 為同一上游 pandas 棄用警告。
- 瀏覽器：**4 passed (53.3s)**。UI 流程使用 2024-01-01–2024-04-30 的固定區間，包含 MLK 休市與 2024-03-18 成交；完整年度數值另由真實 CSV 流程與手算驗證。加入逐次保存後，測試改為等待本次批次的新 run，避免把先前比較表當成本次已完成。
- 排除非同步完成回應覆寫使用者後續導覽的競爭；建立成功事件可刷新當前頁的 run 清單。
- SQLite 保留空閒連線並序列化所有活動交易；核對實際 sqlite_version=3.50.4，依官方 WAL 文件處理最後連線 checkpoint 與已知 WAL-reset 並行條件。
- 提交 `68a915a` 的真實 SPY 2024 四組功能驗證已完成：252 個估值日、共同首筆可成交日 2024-01-08、code_dirty=false；Buy & Hold 與固定股數、實際費用／滑價的獨立公式一致。模型全部 Mock，付費呼叫數 0；產物位於 artifacts/spy-2024-pipeline-comparison.json 與驗證摘要。
- 本輪提交前複驗：`uv run --no-sync pytest -q` 為 **43 passed, 44 warnings (24.67s)**；`npm run typecheck`、`npm run build` 通過；`npm run test:e2e` 為 **4 passed (1.1m)**。

## 2026-10-24，最終本機 checkpoint

- 程式提交 `d3d9700c45c3f06f06dd9b64c1b4702c602d9b99` 後，在乾淨工作目錄重新執行真實 CSV 的 2024 年四組 Mock 流程，四組均 COMPLETED、code_dirty=false、付費模型呼叫 0。共用資料雜湊仍為 `79fe3c66ee6493b834160f5386be38c5f03823802de27ff8450f772d61878bdb`、252 個估值日、首個成交日 2024-01-08。
- Buy & Hold 獨立公式再次通過；逐組匯出核對持久化事件：每組 252 筆估值，決策數為 1／52／52／52，Agent 呼叫為 0／0／52／156；所有呼叫均為已驗證 Mock，原始輸出、輸入與 prompt hash 完整，沒有遺留 PENDING/RUNNING 事件。完整摘要存於忽略的 `artifacts/spy-2024-validation-summary.json`。
- Orca `QuantAgent Lab Dev` 終端已重新啟動 `node tools/dev.mjs`；API health 與前端均 HTTP 200。Playwright MCP 檢視這批新結果，console 錯誤／警告為 0，桌面截圖存 `artifacts/screenshots/dashboard-desktop.png`。
- 軟體初版可操作；正式付費模型實驗、2025 保留期研究及含股息敏感度分析仍未執行。遠端 CI 尚未執行，程式未推送或發布。

## 2026-10-24，公開前隱私與重現檢查

- 新增公開內容隱私規範；移除追蹤中的本機 agent 設定與開發額度工具，保留本機副本。公開文件刪去個人 profile 路徑與帳戶方案／用量。原始 Git 歷史仍私有，未重寫。
- Gitleaks 8.30.0：四筆原始提交的完整歷史及乾淨候選副本均未偵測到金鑰；自訂隱私檢查另發現舊提交身分與本機路徑，因此不可直接公開現有 Git 歷史。
- `uv run --no-sync pytest -q`：**47 passed, 44 warnings (27.08s)**，包含四項發布負向測試。乾淨副本依 README 安裝後的前次測試為 46 passed (25.05s)，之後新增一項拒絕缺少 Git metadata 卻假成功的負向測試。
- 乾淨副本 `npm ci`、`uv sync --locked`、前端 typecheck/build 成功；瀏覽器 **4 passed (1.3m)**。測試改成等待非同步研究的狀態完成，再斷言比較畫面；未修改回測或 Agent 決策邏輯。
- 公開候選 80 份檔案的隱私掃描為零項；Gitleaks 目錄掃描為零項。Markdown 本地連結均存在。副本沒有 Git metadata、私人行情或 SQLite。
- 新增四張合成 Mock 截圖與 45.24 秒操作 WebM；已檢視截圖與影片抽樣，PNG 無文字／EXIF metadata，影片沒有作者等個資標籤。真實資料與模型結果不納入公開展示。
- README、架構、方法、貢獻、安全、Roadmap、授權提案、Release Notes 與發布清單已整理。LICENSE、匿名發布身分／歷史策略、實際 GitHub 目的地及最終發布仍待確認；遠端 CI 未執行。
