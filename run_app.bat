@echo off
setlocal

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Install Python 3.11 or newer, then run this file again.
  pause
  exit /b 1
)

echo Starting SAS TLF Assistant...
echo.
echo Open this URL in your browser:
echo http://127.0.0.1:8000
echo.
echo Keep this window open while using the app.
echo Press Ctrl+C to stop the server.
echo.

python app.py

echo.
echo Server stopped.
pause
