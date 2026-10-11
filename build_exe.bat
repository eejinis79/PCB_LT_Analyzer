@echo off
REM Windows 에서 더블클릭하면 dist\VolumeSpikeScreener.exe 가 만들어집니다.
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :error
python build_exe.py || goto :error
echo.
echo 완료: dist\VolumeSpikeScreener.exe
pause
exit /b 0
:error
echo 빌드 실패
pause
exit /b 1
