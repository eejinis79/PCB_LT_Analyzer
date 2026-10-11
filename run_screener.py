"""거래량 급증 · 가격 횡보 스크리너 실행.

    python run_screener.py          # KRX 실데이터
    python run_screener.py --demo   # 오프라인 데모 데이터로 시작

탐색기에서 더블클릭해도 된다. 필요한 패키지가 없으면 설치 여부를 묻고,
오류가 나면 창이 바로 닫히지 않고 메시지를 보여준다.
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 어느 폴더에서 실행하든 screener 패키지를 찾을 수 있게
sys.path.insert(0, str(HERE))

# import 이름 → pip 패키지 이름
REQUIRED = {
    "pandas": "pandas", "numpy": "numpy", "requests": "requests", "pykrx": "pykrx",
    "PySide6": "PySide6", "matplotlib": "matplotlib", "cryptography": "cryptography",
}


def _has_console() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _pause(msg: str = "엔터를 누르면 창이 닫힙니다..."):
    if _has_console():
        try:
            input(msg)
        except EOFError:
            pass


def _check_environment() -> bool:
    """Python 버전과 필요한 패키지를 확인하고, 없으면 설치를 제안한다."""
    if sys.version_info < (3, 10):
        print(f"[오류] Python 3.10 이상이 필요합니다. 현재: {sys.version.split()[0]}")
        print("https://www.python.org/downloads/ 에서 최신 Python 을 설치하세요.")
        return False
    missing = [pip for mod, pip in REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return True
    print("필요한 패키지가 설치되어 있지 않습니다: " + ", ".join(missing))
    if not _has_console():
        print(f'설치 명령: "{sys.executable}" -m pip install -r requirements.txt')
        return False
    ans = input("지금 자동으로 설치할까요? 몇 분 걸립니다. [Y/n] ").strip().lower()
    if ans not in ("", "y", "yes", "ㅛ"):
        print(f'직접 설치하려면: "{sys.executable}" -m pip install -r requirements.txt')
        return False
    req = HERE / "requirements.txt"
    cmd = [sys.executable, "-m", "pip", "install"] + (["-r", str(req)] if req.exists() else missing)
    print("실행:", " ".join(cmd))
    if subprocess.call(cmd) != 0:
        print("[오류] 패키지 설치에 실패했습니다. 위 메시지를 확인하세요.")
        return False
    importlib.invalidate_caches()
    print("설치 완료. 프로그램을 시작합니다.\n")
    return True

# 실행파일(창 모드)에서는 stdout/stderr 가 없으므로 로그 파일로 보낸다
if sys.stdout is None or sys.stderr is None:
    from screener.paths import app_dir

    _log = open(app_dir() / "screener.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log
# pykrx 는 import 시 환경변수 KRX_ID/KRX_PW 로 자동 로그인을 시도하므로 비워 둔다 (로그인은 앱에서 수행)
os.environ.pop("KRX_ID", None)
os.environ.pop("KRX_PW", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="데모 데이터 소스를 기본 선택")
    ap.add_argument("--autoload", action="store_true", help="시작하자마자 데이터 불러오기")
    ap.add_argument("--selftest", action="store_true",
                    help="데모 데이터로 창을 띄워 스크리닝까지 확인한 뒤 종료 (빌드 검증용)")
    args, qt_args = ap.parse_known_args()

    from PySide6.QtWidgets import QApplication

    from screener.gui.main_window import MainWindow
    from screener.gui.theme import QSS

    app = QApplication([sys.argv[0], *qt_args])
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    win = MainWindow(demo=args.demo or args.selftest)
    win.show()
    if args.selftest:
        return _selftest(app, win)
    if args.autoload:
        win.load_data()
    sys.exit(_run(app, win))


def _run(app, win) -> int:
    """이벤트 루프 실행 후 창 → 앱 순서로 정리 (순서가 어긋나면 종료 시 Qt 가 비정상 종료할 수 있음)."""
    code = app.exec()
    win.shutdown()
    win.deleteLater()
    del win
    app.processEvents()
    return code


def _selftest(app, win):
    from PySide6.QtCore import QTimer

    from screener import vault  # noqa: F401  (암호화 모듈 번들 확인)

    def check():
        rows = win.model.rowCount()
        print(f"selftest: {rows} rows")
        app.exit(0 if rows > 0 else 1)

    win.load_data()
    win._loader.done.connect(lambda _p: QTimer.singleShot(500, check))
    win._loader.failed.connect(lambda m: (print("selftest failed:", m), app.exit(1)))
    QTimer.singleShot(60_000, lambda: (print("selftest timeout"), app.exit(2)))
    code = _run(app, win)
    sys.stdout.flush()
    sys.exit(code)


if __name__ == "__main__":
    if not _check_environment():
        _pause()
        sys.exit(1)
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — 더블클릭 실행 시 창이 바로 닫히지 않도록
        print("\n[오류] 프로그램 실행 중 문제가 발생했습니다. 아래 내용을 복사해서 알려주세요.\n")
        traceback.print_exc()
        _pause()
        sys.exit(1)
