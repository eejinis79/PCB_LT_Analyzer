"""실행파일 빌드 (PyInstaller).

    pip install -r requirements.txt pyinstaller
    python build_exe.py            # → dist/VolumeSpikeScreener(.exe) 한 개 파일

Windows 에서 실행하면 .exe, macOS/Linux 에서 실행하면 해당 OS 용 실행파일이 만들어진다.
"""
import sys

import PyInstaller.__main__

args = [
    "run_screener.py",
    "--name=VolumeSpikeScreener",
    "--onefile",
    "--windowed",                 # 콘솔 창 없이 실행 (로그는 사용자 폴더의 screener.log)
    "--noconfirm",
    "--clean",
    "--collect-data=pykrx",       # pykrx 에 포함된 한글 폰트 등 데이터 파일
    "--hidden-import=cryptography.fernet",
    # 쓰지 않는 대형 Qt 모듈 제외 → 용량 축소
    "--exclude-module=PySide6.QtWebEngineCore",
    "--exclude-module=PySide6.QtWebEngineWidgets",
    "--exclude-module=PySide6.Qt3DCore",
    "--exclude-module=PySide6.QtMultimedia",
    "--exclude-module=PySide6.QtPdf",
    "--exclude-module=tkinter",
    "--exclude-module=pytest",
]
args += sys.argv[1:]
PyInstaller.__main__.run(args)
