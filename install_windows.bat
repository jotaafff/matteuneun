@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [마뜨는구간삭제] Python 가상환경 및 패키지를 설치합니다.
where py >nul 2>nul
if errorlevel 1 (
  echo Python Launcher(py)를 찾을 수 없습니다. Python 3.11 이상을 설치하고 PATH 옵션을 켜주세요.
  pause
  exit /b 1
)
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 (
  echo Python 3.11 이상을 찾을 수 없습니다. Python 3.11 이상을 설치한 뒤 다시 실행하세요.
  pause
  exit /b 1
)
py -3 -m venv .venv
if errorlevel 1 (
  echo 가상환경을 만들지 못했습니다.
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 (
  echo 패키지 설치에 실패했습니다. 인터넷 연결과 Python 버전을 확인하세요.
  pause
  exit /b 1
)
echo 설치 완료. FFmpeg가 아직 설치되지 않았다면 README_KO.md를 참고하세요.
pause
