@echo off
title 深圳住区形态分析工具 - 本地计算引擎
chcp 65001 >nul 2>nul
cd /d "%~dp0"

echo ============================================================
echo   深圳住区形态分析工具  -  本地计算引擎
echo ============================================================
echo.

REM ---------- 1. 定位 Python ----------
set "PY="
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%USERPROFILE%\miniconda3\python.exe"
    "%ProgramData%\miniconda3\python.exe"
    "%ProgramData%\Anaconda3\python.exe"
    "%USERPROFILE%\Anaconda3\python.exe"
) do (
    if exist "%%~P" set "PY=%%~P"
)
if not defined PY (
    where python >nul 2>nul
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [错误] 没有找到 Python。
    echo.
    echo   本工具需要 Python 3.10 或更高版本。
    echo   请前往 https://www.python.org/downloads/ 下载安装，
    echo   安装时记得勾选 "Add Python to PATH"，然后重新运行本文件。
    echo.
    pause
    exit /b 1
)

echo [1/4] Python: %PY%
"%PY%" -c "import sys; print('      版本:', sys.version.split()[0])"

REM ---------- 2. 检查依赖 ----------
echo [2/4] 检查依赖库...
"%PY%" -c "import geopandas, osmnx, sklearn, matplotlib, fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
    echo       缺少依赖，正在安装（首次运行需要几分钟）...
    echo.
    "%PY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo [错误] 依赖安装失败，请检查网络后重试。
        echo        如果在国内网络较慢，可以修改 requirements.txt 的安装源，
        echo        或在命令行先执行：pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple
        pause
        exit /b 1
    )
) else (
    echo       依赖完整
)

REM ---------- 3. 检查端口 ----------
echo [3/4] 检查端口 8765 ...
netstat -ano | findstr ":8765" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
    echo.
    echo [提示] 8765 端口已有程序在运行，引擎可能已启动。
    echo        请直接打开 http://127.0.0.1:8765 使用。
    echo.
    start "" http://127.0.0.1:8765
    pause
    exit /b 0
)

REM ---------- 4. 启动服务 ----------
echo [4/4] 启动服务...
echo.
echo ------------------------------------------------------------
echo   引擎启动中，约 5 秒后浏览器会自动打开。
echo.
echo    ^>^>^> 请勿关闭本窗口！关闭窗口 = 停止服务 ^<^<^
echo ------------------------------------------------------------
echo.

start "" /min cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:8765"

cd /d "%~dp0server"
"%PY%" -m uvicorn app:app --host 127.0.0.1 --port 8765

echo.
echo ============================================================
echo   引擎已停止。
echo ============================================================
pause
