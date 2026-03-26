#!/bin/bash
# 停車場管理系統啟動腳本

echo "========================================"
echo "  停車場管理系統"
echo "========================================"

# 檢查 Python
if ! command -v python3 &> /dev/null; then
    echo "錯誤: 需要 Python 3"
    exit 1
fi

# 安裝依賴
echo "正在檢查依賴..."
pip3 install -r requirements.txt -q

# 設定環境變量（可自行修改）
export CAMERA_SOURCE=${CAMERA_SOURCE:-"0"}
export SIMULATE_RELAY=${SIMULATE_RELAY:-"1"}

echo ""
echo "啟動選項："
echo "  - 攝影機: $CAMERA_SOURCE"
echo "  - 繼電器模式: $([ "$SIMULATE_RELAY" = "1" ] && echo "模擬" || echo "真實硬體")"
echo ""
echo "開啟瀏覽器前往: http://localhost:5000"
echo "預設帳號: admin"
echo "預設密碼: admin123"
echo ""
echo "========================================"
echo ""

# 啟動
python3 main.py
