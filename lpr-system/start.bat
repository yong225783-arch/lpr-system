@echo off
chcp 65001 >nul
echo ==========================================
echo   LPR 車牌辨識系統啟動腳本
echo ==========================================
echo.

REM 檢查 main.py 是否存在，不存在就自動下載
if not exist "main.py" (
    echo 📥 找不到系統檔案，正在從 GitHub 下載...
    git clone https://github.com/yong225783-arch/lpr-system.git temp_lpr
    xcopy /E /Y temp_lpr\* .
    rmdir /S /Q temp_lpr
    echo ✅ 下載完成！
)

REM 檢查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo ❌ 找不到 Python，請先安裝：
    echo    https://www.python.org/downloads
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
