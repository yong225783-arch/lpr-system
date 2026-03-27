@echo off
echo ========================================
echo   車牌辨識系統啟動中...
echo ========================================
echo.

:: 啟動虛擬環境
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

:: 設定環境變數
set SIMULATE_RELAY=1

echo 啟動 Flask 伺服器...
echo 系統運行於 http://localhost:5000
echo 按 Ctrl+C 停止系統
echo.

:: 啟動
python main.py

pause
