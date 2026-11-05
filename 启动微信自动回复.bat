@echo off
rem ---------------------------------------------------------------
rem  WeChat auto reply launcher.
rem  NOTE: this file must stay ASCII-only. cmd.exe parses .bat files with the
rem  OEM codepage, so Chinese text here turns into mojibake and can cut the
rem  commands in half. All Chinese output comes from bot.py (UTF-8).
rem ---------------------------------------------------------------
chcp 65001 >nul
title WeChat auto reply
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "PY_EXE="
set "PY_ARGS="
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_EXE=.venv\Scripts\python.exe"
)

if not defined PY_EXE (
    python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 set "PY_EXE=python"
)
if not defined PY_EXE (
    py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "PY_EXE=py"
        set "PY_ARGS=-3"
    )
)
if not defined PY_EXE (
    echo Python 3.10+ was not found. Install Python and run again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating local Python environment ...
    "%PY_EXE%" %PY_ARGS% -m venv .venv
    if not errorlevel 1 (
        set "PY_EXE=.venv\Scripts\python.exe"
        set "PY_ARGS="
    ) else (
        echo Could not create .venv; using the system Python.
    )
)

"%PY_EXE%" %PY_ARGS% -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Python 3.10+ is required. Upgrade Python and run again.
    pause
    exit /b 1
)

"%PY_EXE%" %PY_ARGS% -c "import uiautomation, comtypes, psutil, PIL" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies ...
    "%PY_EXE%" %PY_ARGS% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Dependency installation failed.
        pause
        exit /b 1
    )
    echo.
)

if not exist "config_secret.bin" if not defined DEEPSEEK_API_KEY (
    echo No local API key found. Opening settings...
    "%PY_EXE%" %PY_ARGS% settings_ui.py
)

if not exist "config_secret.bin" if not defined DEEPSEEK_API_KEY (
    echo API key setup was cancelled. Run the settings shortcut and try again.
    pause
    exit /b 1
)

"%PY_EXE%" %PY_ARGS% bot.py %*

echo.
echo ------------------------------------------------------------
echo Script finished. Close this window.
pause
