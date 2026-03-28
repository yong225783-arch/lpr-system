#!/bin/bash

echo "=========================================="
echo "  LPR 車牌辨識系統啟動腳本"
echo "=========================================="
echo ""

# 檢查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 找不到 Python3，正在安裝..."
    sudo apt update && sudo apt install -y python3 python3-pip
fi

# 檢查必要套件
echo "📦 檢查依賴套件..."
pip3 install -q flask flask-cors ultralytics easyocr requests pytesseract Pillow adafruit-blinka adafruit-circuitpython-modbusbusio smbus smbus2 2>/dev/null

# 啟動
echo ""
echo "🚀 啟動 LPR 系統..."
echo "   按 Ctrl+C 停止"
echo ""
python3 main.py
