"""사용자 데이터(계정 금고, 시세 캐시, 로그) 저장 위치.

실행파일(PyInstaller)로 배포해도 쓰기 가능한 위치여야 하므로 프로젝트 폴더가 아닌
OS 별 사용자 폴더를 쓴다. 환경변수 VSS_HOME 으로 바꿀 수 있다 (테스트용).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "VolumeSpikeScreener"


def app_dir() -> Path:
    if os.environ.get("VSS_HOME"):
        base = Path(os.environ["VSS_HOME"])
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def cache_dir(*parts: str) -> Path:
    return app_dir().joinpath("cache", *parts)
