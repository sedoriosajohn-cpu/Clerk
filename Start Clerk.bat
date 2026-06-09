@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Clerk

:: ─────────────────────────────────────────────────────────────────────────────
:: Try virtual environment first (fastest path after first run)
:: ─────────────────────────────────────────────────────────────────────────────
if exist ".venv\Scripts\python.exe" (
    echo Starting Clerk...
    ".venv\Scripts\python.exe" run_clerk.py
    goto end
)

:: ─────────────────────────────────────────────────────────────────────────────
:: First run: find Python, create venv, install deps, then launch
:: ─────────────────────────────────────────────────────────────────────────────
echo.
echo  ╔══════════════════════════════╗
echo  ║     Clerk — First-time setup ║
echo  ╚══════════════════════════════╝
echo.

set PYTHON_EXE=

:: Prefer the py launcher (installed with official Python on Windows)
where py >nul 2>nul
if not errorlevel 1 (
    set PYTHON_EXE=py -3
    goto found_python
)

where python >nul 2>nul
if not errorlevel 1 (
    set PYTHON_EXE=python
    goto found_python
)

where python3 >nul 2>nul
if not errorlevel 1 (
    set PYTHON_EXE=python3
    goto found_python
)

echo  ERROR: Python was not found.
echo.
echo  Please install Python 3.11 or newer from https://www.python.org/downloads/
echo  Make sure to check "Add Python to PATH" during installation.
echo.
pause
exit /b 1

:found_python
echo  Found Python. Creating virtual environment...
%PYTHON_EXE% -m venv .venv
if errorlevel 1 (
    echo  ERROR: Could not create virtual environment.
    echo  Try running:  python -m venv .venv
    pause
    exit /b 1
)

echo  Installing dependencies (this takes about 30 seconds the first time)...
.venv\Scripts\python.exe -m pip install -r backend\requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo  ERROR: Dependency installation failed.
    echo  Try running manually:  pip install -r backend\requirements.txt
    pause
    exit /b 1
)

echo.
echo  Setup complete! Starting Clerk...
echo.
".venv\Scripts\python.exe" run_clerk.py

:end
echo.
pause
