# 🚗 車牌辨識系統安裝程式

## 系統需求

### 硬體
- NVIDIA 顯示卡（GTX 1050 以上，建議 GTX 2060 以上）
- 8GB RAM 以上
- 50GB 硬碟空間

### 軟體
- Windows 10/11 或 Ubuntu 20.04/22.04
- Python 3.10+
- NVIDIA 驅動程式 + CUDA 11.8 或 12.x

---

## 快速安裝

### Windows
1. 下載專案壓縮檔
2. 解壓縮
3. 執行 `windows/install.bat`
4. 等待安裝完成
5. 執行 `start.bat` 啟動系統

### Linux
1. 下載專案壓縮檔
2. 解壓縮
3. 執行 `chmod +x linux/install.sh && ./linux/install.sh`
4. 執行 `./start.sh` 啟動系統

---

## 安裝後設定

### 首次設定
1. 開啟瀏覽器訪問 http://localhost:5000
2. 預設帳號：admin
3. 預設密碼：admin123
4. 建議立即修改密碼

### 攝影機設定
- 進口 RTSP URL
- 出口 RTSP URL
- 或使用 USB Webcam

### 車主白名單
- 在「車主管理」頁面新增車牌白名單
- 系統會自動比對並開門

---

## 疑難排解

### CUDA 錯誤
```
# 檢查 NVIDIA 驅動
nvidia-smi

# 檢查 CUDA 版本
nvcc --version
```

### 系統無法啟動
```
# Windows
python main.py

# 查看錯誤訊息
```

---

## 技術支援
如有問題請聯繫技術人員
