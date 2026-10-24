# 架構審查

原規格核心方向可實作。採 Next.js → 同源 `/api/v1` 代理 → FastAPI → 單工作者研究流程；定義於 `TECHNICAL_DECISIONS.md` 的 v0.2 決策優先於 v0.1 歧義。

後端資料流：CSV + manifest → 嚴格驗證及 SHA-256 → 因果特徵 → 共同週決策日曆 → baseline 或固定 LangGraph → Pydantic → 確定性風控 → 下一開盤成交 → 帳本 → 績效 → 原子保存完成結果。模型只看到數值，不持有 execution/ledger 物件。

SQLite 保存 run 設定、狀態、進度與完成結果 JSON。GET decisions 回傳 snapshot、calls、reason_codes、模型輸出及成交參照；補齊原 API 無法呈現完整 Trace 的缺口。增加 GET datasets、GET experiments 清單、Replay source 及 compare 的一致性檢查；保留藍圖既有端點。

瀏覽器只呼叫同源 API，不接觸 API 金鑰。前端提供六個頁面，從同一型別化 client 讀取後端；圖表只畫 API 回傳的行情、成交與權益。明確呈現 synthetic/mock/replay、價格報酬基礎、費率、期間與資料版本。

主要失效模式與處理：行情缺漏即拒絕；模型錯誤只跳過該次決策；無下個成交日記錄跳過；服務重啟使遺留任務失敗；鎖不允許兩個 worker 寫同一資料庫；不同期間／資料／成本的 run 不允許默認當成公平比較。

目前已完成依賴安裝、框架 smoke、43 項後端測試、Next build 與 4 項真實瀏覽器 E2E；結果見 VERIFICATION_LOG。尚未發出真實付費模型呼叫，也未宣稱已完成正式保留期間研究。

每次提供者請求前，先把輸入與費用預留寫入 SQLite；呼叫回應、決策、成交與每日估值逐步保存。若服務中斷，失敗 run 可匯出 events 診斷，尚未回覆的呼叫標示 INTERRUPTED／未知費用預留，不當成免費。完成後結果與事件不可再透過 Store 改寫。
