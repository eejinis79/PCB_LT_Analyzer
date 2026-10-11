@echo off
chcp 65001 >nul
REM 더블클릭으로 실행: 처음 한 번은 필요한 패키지를 설치합니다 (수 분 소요).
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [오류] Python 이 설치되어 있지 않습니다.
  echo https://www.python.org/downloads/ 에서 Python 3.10 이상을 설치하세요.
  echo 설치 화면에서 "Add python.exe to PATH" 를 꼭 체크하세요.
  pause
  exit /b 1
)

if not exist .venv (
  echo [1/2] 가상환경 생성 중...
  python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat

if not exist .venv\installed.flag (
  echo [2/2] 패키지 설치 중... 처음 한 번만 합니다.
  python -m pip install --upgrade pip >nul
  python -m pip install -r requirements.txt || goto :error
  echo ok> .venv\installed.flag
)

echo 프로그램을 시작합니다. 데이터 없이 화면만 보려면: run_windows.bat --demo
python run_screener.py %*
if errorlevel 1 pause
exit /b 0

:error
echo [오류] 설치/실행 중 문제가 발생했습니다. 위 메시지를 확인하세요.
pause
exit /b 1
