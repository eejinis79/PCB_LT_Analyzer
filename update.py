"""GitHub 에서 최신 프로그램을 받아 이 폴더를 업데이트한다.

    python update.py            (Windows: update_windows.bat 더블클릭)

- 저장소가 공개(public)라 git 이나 GitHub 로그인이 필요 없다.
- 덮어쓰기 전에 내용이 달라지는 기존 파일은 _backup\\날짜_시각\\ 에 복사해 둔다.
  (직접 고친 파일이 있으면 거기서 꺼내 다시 반영하면 된다)
- DB(stock_data.db), 가상환경(.venv), 백업 폴더는 건드리지 않는다. 파일을 지우지도 않는다.
"""
from __future__ import annotations

import datetime as dt
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = "eejinis79/PCB_LT_Analyzer"
BRANCH = "claude/stock-screening-gui-botl0d"   # 작업 브랜치가 main 에 합쳐지면 "main" 으로 바꾸면 된다
URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"

HERE = Path(__file__).resolve().parent
SKIP_DIRS = {".venv", "_backup", "__pycache__", ".git", ".pytest_cache", "dist", "build"}
SKIP_SUFFIXES = (".db", ".db-wal", ".db-shm")


def download(url: str = URL) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "VolumeSpikeScreener-updater"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def skipped(rel: Path) -> bool:
    return any(p in SKIP_DIRS for p in rel.parts) or rel.name.endswith(SKIP_SUFFIXES)


def apply_zip(data: bytes, target: Path = HERE, now: dt.datetime | None = None) -> dict:
    """zip 내용을 target 에 반영. {'added': [...], 'updated': [...], 'backup': 경로 또는 None}"""
    stamp = (now or dt.datetime.now()).strftime("%Y%m%d_%H%M%S")
    backup = target / "_backup" / stamp
    added, updated = [], []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            parts = Path(info.filename).parts[1:]  # 맨 앞 '저장소-브랜치/' 폴더 제거
            if not parts:
                continue
            rel = Path(*parts)
            if skipped(rel):
                continue
            new = z.read(info)
            dest = target / rel
            if dest.exists():
                if dest.read_bytes() == new:
                    continue
                (backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, backup / rel)
                updated.append(str(rel))
            else:
                added.append(str(rel))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(new)
    return {"added": added, "updated": updated, "backup": str(backup) if updated else None}


def main() -> int:
    print(f"최신 버전 내려받는 중… ({REPO} / {BRANCH})")
    try:
        data = download()
    except Exception as e:  # noqa: BLE001
        print(f"[오류] 내려받기 실패: {e}\n인터넷 연결을 확인하세요.")
        return 1
    res = apply_zip(data)
    if not res["added"] and not res["updated"]:
        print("이미 최신 버전입니다.")
        return 0
    for f in res["updated"]:
        print(f"  변경  {f}")
    for f in res["added"]:
        print(f"  추가  {f}")
    print(f"\n완료: 변경 {len(res['updated'])}개, 추가 {len(res['added'])}개")
    if res["backup"]:
        print(f"바뀌기 전 파일은 여기에 백업했습니다: {res['backup']}")
    if any(f.replace("\\", "/") == "requirements.txt" for f in res["updated"] + res["added"]):
        print("필요한 패키지 목록이 바뀌었습니다. 다음 실행 때 설치를 물으면 Y 를 누르세요.")
        flag = HERE / ".venv" / "installed.flag"
        if flag.exists():
            flag.unlink()  # run_windows.bat 이 다음 실행 때 다시 설치하도록
    return 0


if __name__ == "__main__":
    code = main()
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\n엔터를 누르면 창이 닫힙니다...")
        except EOFError:
            pass
    sys.exit(code)
