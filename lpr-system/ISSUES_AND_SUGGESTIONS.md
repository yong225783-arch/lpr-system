# 車牌辨識系統 — 問題與建議統整
**系統：78車牌辨識開門系統 | 最後更新：2026-04-04**

---

## 🔴 立即問題（影響正常運作）

### 1. Relay 硬體未連線
- **現況：** `relay_modbus_ip` 是空的，但 `relay_type` 已經設成 `modbus_tcp`
- **後果：** 開門指令會直接 fail（因為嘗試連線到空 IP）
- **建議：** 確認 Modbus TCP 繼電器的 IP 位址並填入設定

### 2. OCR 引擎設為 Ollama，但 Ollama 未安裝
- **現況：** `ocr_engine = 'ollama'`、`ollama_url = http://192.168.110.14:11434`
- **後果：** 使用 Ollama 時所有辨識會失敗，降到備援 engine
- **建議：** 確認 Ollama 服務是否在 `192.168.110.14` 運行，或者把 `ocr_engine` 改成 `easyocr`

### 3. 停車場狀態不同步（邏輯問題）
- **現況：** `auto_parking_session = false`，但系統有 `parking_slots` 和 `parking_sessions` 表
- **後果：** 手動進場/離場若沒嚴格按照流程操作，車位狀態會和實際記錄不符
- **建議：** 確認系統使用情境——是否需要自動化追蹤車位？

---

## 🟡 功能缺失

### 4. 計費系統沒有發票/收據
- **現況：** `billing` 表有收費記錄，`billing_rules` 有費率設定，但沒有發票產生
- **建議：** 新增「開立收據」功能，支援列印或 PDF 匯出

### 5. 沒有實際的月租到期通知
- **現況：** 系統有演算法（`api_owners_expiring`）可查詢即將到期車牌，但沒有發送通知（無 email/LINE/簡訊）
- **建議：** 串接 email 或 LINE 推播，讓管理者能在到期前主動收到提醒

### 6. 繼電器斷線無錯誤處理
- **現況：** Relay 指令 fail 時只寫 log，不通知管理者
- **建議：** 斷線後自動發出系統 alert，並可設定是否要 email 通知

### 7. API 沒有文件
- **現況：** 49 個 API endpoints，全靠 trace code 理解
- **建議：** 使用 Flask-Swagger 或 apifrac 自動生成 API 文件

---

## 🟢 程式碼品質

### 8. ✅ 已清理 — 模型整理完成
已將閒置模型移至 `models/archived/`（共 74MB）：

**目前使用中：**
- `models/best.pt` — YOLO 車牌偵測（搭配 Ollama OCR）

**已移至 archived/：**
- `koushim_license_plate.pt`、`koushim_v2.pt`、`license_plate_yolo.pt` — 從未引用
- `taiwan_plate.onnx` — 從未引用
- `ulrixon_bbox/`、`ulrixon_bbox.pt`、`ulrixon_bbox.pt.bak` — 用戶已停用 Ulrixon，改用 Ollama
- `best.pt.v8.bak` — `best.pt` 備份，功用重複

**注意：** `yolov8s.pt`（22MB，fallback 用）仍保留在根目錄。

### 9. 備份檔案佔空間
- `lpr-system.zip` 和 `lpr-system/` 重複，浪費備份空間

### 10. `start_server.py` 未納入版本控制
- 手动创建的启动脚本，需要加入 git 或正式化

---

## 📊 系統現況摘要

| 項目 | 數值 |
|------|------|
| 車主數（owners） | 6 |
| 行車記錄（records） | 120 |
| 停車場Sessions | 10 |
| API endpoints | 49 |
| OCR 引擎設定 | ollama（需確認服務） |
| Relay 類型 | modbus_tcp（IP 未填） |
| 自動停車追蹤 | 關閉 |
| 日誌保留 | 10MB × 5 檔 |

---

## 建議優先順序

| 順序 | 項目 | 原因 |
|------|------|------|
| 1 | 填 Relay IP | 開門功能完全不能運作 |
| 2 | 確認 Ollama 服務 | 幾乎所有辨識都走這條 |
| 3 | 月租到期通知 | 實用性高，實作簡單 |
| 4 | 清理舊模型 | 長期維護更輕鬆 |
| 5 | 發票/收據功能 | 正式營運必備 |
