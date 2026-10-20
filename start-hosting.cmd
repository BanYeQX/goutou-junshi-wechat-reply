@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" -B "hosting_window.py"
  exit /b
)
echo Project Python is missing. The hosting loop was NOT started.
pause
