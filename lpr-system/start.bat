@echo off
chcp 65001 >nul
echo ==========================================
echo   LPR 車牌辨識系統啟動腳本
echo ==========================================
echo.

REM 檢查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 找不到 Python，正在安裝...
    echo 請先安裝 Python: https://www.python.org/downloads
    pause
    exit /b
)

REM 安裝依賴
echo 📦 檢查依賴套件...
pip install -q flask flask-cors ultralytics easyocr requests pytesseract Pillow

REM 啟動
echo.
echo 🚀 啟動 LPR 系統...
echo    按 Ctrl+C 停止
echo.
python main.py
pause
