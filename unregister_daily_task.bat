@echo off
chcp 65001 >nul
schtasks /Delete /F /TN "VolumeSpikeScreener Daily"
REG delete "HKCU\Environment" /F /V VSS_PASSWORD >nul 2>nul
echo 자동 실행을 해제하고 저장된 비밀번호 환경변수를 삭제했습니다.
pause
