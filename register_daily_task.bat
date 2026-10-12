@echo off
chcp 65001 >nul
REM 평일 18:30 에 daily_run.py 를 자동 실행하도록 Windows 작업 스케줄러에 등록합니다.
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY for /f "delims=" %%i in ('where python 2^>nul') do if not defined PY set "PY=%%i"
if not defined PY (
  echo [오류] Python 을 찾을 수 없습니다.
  pause
  exit /b 1
)

echo.
echo 자동 실행 때 계정 잠금을 풀기 위해 '계정 비밀번호'를 Windows 사용자 환경변수(VSS_PASSWORD)에 저장합니다.
echo 이 PC 의 같은 사용자 계정으로 로그인한 사람은 볼 수 있으니 공용 PC 에서는 사용하지 마세요.
echo.
set /p "VSSPW=계정 비밀번호 입력: "
if "%VSSPW%"=="" (
  echo 취소했습니다.
  pause
  exit /b 1
)
setx VSS_PASSWORD "%VSSPW%" >nul
set "VSSPW="

schtasks /Create /F /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 18:30 /TN "VolumeSpikeScreener Daily" /TR "\"%PY%\" \"%~dp0daily_run.py\""
if errorlevel 1 (
  echo [오류] 작업 등록 실패
  pause
  exit /b 1
)
echo.
echo 등록 완료: 평일 18:30 자동 실행 (작업 이름: VolumeSpikeScreener Daily)
echo 지금 한 번 시험 실행하려면: schtasks /Run /TN "VolumeSpikeScreener Daily"
echo 실행 기록: %%APPDATA%%\VolumeSpikeScreener\daily_run.log
pause
