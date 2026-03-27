#!/bin/bash

echo "========================================"
echo "  車牌辨識系統 - Linux 安裝程式"
echo "========================================"
echo ""

# 檢查 Python
if ! command -v python3 &> /dev/null; then
    echo "[錯誤] Python3 未安裝"
    echo "請先安裝 Python 3.10 或更新版本"
    echo "Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    exit 1
fi

echo "[1/6] 檢查 Python 版本..."
python3 --version

echo ""
echo "[2/6] 檢查 NVIDIA 驅動..."
if command -v nvidia-smi &> /dev/null; then
    nvidia-smi --query-gpu=name --format=csv,noheader
else
    echo "[警告] NVIDIA 驅動未找到"
    echo "請先安裝 NVIDIA 驅動"
fi

echo ""
echo "[3/6] 建立虛擬環境..."
python3 -m venv venv
source venv/bin/activate

echo ""
echo "[4/6] 安裝必要套件..."
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install ultralytics==8.0.0
pip install easyocr==1.7.0
pip install flask==2.3.0
pip install opencv-python==4.8.0
pip install numpy==1.24.0
pip install werkzeug==2.3.0

echo ""
echo "[5/6] 複製模型檔案..."
if [ ! -d "models" ]; then
    mkdir -p models
fi
cp -r ../lpr-system/models/* models/ 2>/dev/null || true

echo ""
echo "[6/6] 建立資料庫..."
if [ ! -d "database" ]; then
    mkdir -p database
fi

echo ""
echo "========================================"
echo "  安裝完成！"
echo "========================================"
echo ""
echo "執行 ./start.sh 啟動系統"
echo "然後開啟瀏覽器訪問 http://localhost:5000"
echo ""
echo "預設帳號：admin"
echo "預設密碼：admin123"
echo ""
