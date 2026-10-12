@echo off
chcp 65001 >nul
REM 더블클릭: GitHub 에서 최신 버전을 받아 이 폴더를 업데이트합니다. (DB 는 그대로 유지)
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo [오류] Python 이 설치되어 있지 않습니다.
  pause
  exit /b 1
)
python update.py
