@echo off
cd /d "%~dp0"
if exist "dist\GoutouChat\GoutouChat.exe" (
  start "" "dist\GoutouChat\GoutouChat.exe"
  exit /b
)
if exist ".venv\Scripts\pythonw.exe" (
  start "" ".venv\Scripts\pythonw.exe" "app.py"
  exit /b
)
echo Please build the app first. See README.md.
pause
