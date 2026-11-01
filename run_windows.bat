@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo 먼저 install_windows.bat을 실행하세요.
  pause
  exit /b 1
)
.venv\Scripts\python.exe matteuneun.py
if errorlevel 1 pause
