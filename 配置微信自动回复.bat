@echo off
rem Visual settings launcher. Keep this file ASCII-only for cmd.exe.
chcp 65001 >nul
title WeChat auto reply settings
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

"%PY_EXE%" %PY_ARGS% settings_ui.py
if errorlevel 1 pause
