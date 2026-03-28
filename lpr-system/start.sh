#!/bin/bash

echo "=========================================="
echo "  LPR 車牌辨識系統啟動腳本"
echo "=========================================="
echo ""

# 取得腳本所在目錄
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 檢查 main.py 是否存在，不存在就自動下載
if [ ! -f "main.py" ]; then
    echo "📥 找不到系統檔案，正在從 GitHub 下載..."
    git clone https://github.com/yong225783-arch/lpr-system.git temp_lpr
    cp -r temp_lpr/* .
    rm -rf temp_lpr
    echo "✅ 下載完成！"
fi

# 檢查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 找不到 Python3，正在安裝..."
    sudo apt update && sudo apt install -y python3 python3-pip
fi

# 安裝依賴
echo "📦 檢查依賴套件..."
pip3 install -q flask flask-cors ultralytics easyocr requests pytesseract Pillow adafruit-blinka adafruit-circuitpython-modbus smbus smbus2 2>/dev/null

# 啟動
echo ""
echo "🚀 啟動 LPR 系統..."
echo "   按 Ctrl+C 停止"
echo ""
python3 main.py
