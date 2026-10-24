# 測試策略

使用專案兩個 Skills 審查受影響領域，測試以可觀察的金融與契約不變條件為主。

- 資料：拒絕重複／倒序／休市列／缺交易日／NaN／Inf／非法 OHLC；manifest 雜湊不符拒絕。
- 特徵：手算 Wilder 種子與遞推、平盤 RSI、ATR gap、MA60 暖機；改動未來價格後比較過去數值特徵。
- 時間：週五假日改星期四決策、下一正常 session 開盤執行；所有策略同起點；結束邊界不強迫賣出。
- 帳本：手算買、賣、0/50/100% 配置與價格跳空；費用與滑價只計一次，100% 買入現金不為負；固定參考開盤／股數之 gross-net 差符合成本，而非對不同持倉路徑做泛化斷言。
- Agent：非法 JSON、未知 evidence、bool/NaN/超界權重、超時、預算耗盡、上游格式失敗皆不成交；原始失敗 Trace 可檢視。Replay 禁止 provider 呼叫且要求來源配對。
- 績效：含初始現金的回撤與報酬、零波動、無交易、只有損失、費用彙總。
- API/worker：202→完成、失敗可查、重啟修復、並行服務鎖、比較設定不一致拒絕、JSON 匯出可重播。
- UI：Playwright Test 啟動前後端，建立 Mock、等待完成、檢視三 Agent、行情與成交、四組比較、切換 run、Replay 與匯出；桌面／手機、錯誤與無資料狀態。

標準命令：`uv run pytest`、`npm --prefix frontend run typecheck`、`npm --prefix frontend run build`、`npm --prefix frontend run test:e2e`。驗證紀錄須附實際結果；API 測試使用 Mock 或 HTTP fixture，不發出付費模型請求。
