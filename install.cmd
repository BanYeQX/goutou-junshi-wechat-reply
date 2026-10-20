@echo off
setlocal
cd /d "%~dp0"
echo Installing into .venv (first run takes a few minutes) ...
if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv 2>nul || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
  echo Python 3.12 was not found. Install it from https://www.python.org/downloads/ and run this again.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Installing the requirements failed. See the messages above.
  pause
  exit /b 1
)
echo.
echo Done. Next: double-click start.cmd for first-time setup (see README.md).
pause
