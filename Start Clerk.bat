@echo off
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" run_clerk.py
    goto end
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 run_clerk.py
    goto end
)

where python >nul 2>nul
if not errorlevel 1 (
    python run_clerk.py
    goto end
)

echo Python was not found. Install Python, then run this file again.

:end
echo.
pause
