@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python environment missing. No changes made.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" -B calibration_tool.py --run
if errorlevel 1 pause
