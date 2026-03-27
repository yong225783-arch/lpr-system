#!/bin/bash

echo "========================================"
echo "  車牌辨識系統啟動中..."
echo "========================================"
echo ""

# 啟動虛擬環境
if [ -f "venv/bin/activate" ]; then
    source venv/bin/activate
fi

# 設定環境變數
export SIMULATE_RELAY=1

echo "啟動 Flask 伺服器..."
echo "系統運行於 http://localhost:5000"
echo "按 Ctrl+C 停止系統"
echo ""

# 啟動
python3 main.py
