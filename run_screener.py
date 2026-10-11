"""거래량 급증 · 가격 횡보 스크리너 실행.

    python run_screener.py          # KRX 실데이터
    python run_screener.py --demo   # 오프라인 데모 데이터로 시작
"""
import argparse
import os
import sys

# 실행파일(창 모드)에서는 stdout/stderr 가 없으므로 로그 파일로 보낸다
if sys.stdout is None or sys.stderr is None:
    from screener.paths import app_dir

    _log = open(app_dir() / "screener.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log
# pykrx 는 import 시 환경변수 KRX_ID/KRX_PW 로 자동 로그인을 시도하므로 비워 둔다 (로그인은 앱에서 수행)
os.environ.pop("KRX_ID", None)
os.environ.pop("KRX_PW", None)

from PySide6.QtWidgets import QApplication

from screener.gui.main_window import MainWindow
from screener.gui.theme import QSS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="데모 데이터 소스를 기본 선택")
    ap.add_argument("--autoload", action="store_true", help="시작하자마자 데이터 불러오기")
    ap.add_argument("--selftest", action="store_true",
                    help="데모 데이터로 창을 띄워 스크리닝까지 확인한 뒤 종료 (빌드 검증용)")
    args, qt_args = ap.parse_known_args()

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
    main()
