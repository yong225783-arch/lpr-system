@echo off
echo ========================================
echo   車牌辨識系統 - Windows 安裝程式
echo ========================================
echo.

:: 檢查 Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [錯誤] Python 未安裝
    echo 請先安裝 Python 3.10 或更新版本
    echo 下載網址：https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/6] 檢查 Python 版本...
python --version

echo.
echo [2/6] 安裝 NVIDIA CUDA（如果還沒有的話）...
echo 如果還沒安裝 NVIDIA 驅動和 CUDA，請先下載：
echo https://developer.nvidia.com/cuda-downloads

echo.
echo [3/6] 建立虛擬環境...
python -m venv venv
call venv\Scripts\activate

echo.
echo [4/6] 安裝必要套件...
pip install --upgrade pip
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install ultralytics==8.0.0
pip install easyocr==1.7.0
pip install flask==2.3.0
pip install opencv-python==4.8.0
pip install numpy==1.24.0
pip install werkzeug==2.3.0

echo.
echo [5/6] 複製模型檔案...
if not exist "models" mkdir models
xcopy /E /Q "..\lpr-system\models\*" "models\" 2>nul

echo.
echo [6/6] 建立資料庫...
if not exist "database" mkdir database

echo.
echo ========================================
echo   安裝完成！
echo ========================================
echo.
echo 執行 start.bat 啟動系統
echo 然後開啟瀏覽器訪問 http://localhost:5000
echo.
echo 預設帳號：admin
echo 預設密碼：admin123
echo.
pause
