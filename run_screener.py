"""거래량 급증 · 가격 횡보 스크리너 실행.

    python run_screener.py          # KRX 실데이터
    python run_screener.py --demo   # 오프라인 데모 데이터로 시작
"""
import argparse
import sys

from PySide6.QtWidgets import QApplication

from screener.gui.main_window import MainWindow
from screener.gui.theme import QSS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="데모 데이터 소스를 기본 선택")
    ap.add_argument("--autoload", action="store_true", help="시작하자마자 데이터 불러오기")
    args, qt_args = ap.parse_known_args()

    app = QApplication([sys.argv[0], *qt_args])
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    win = MainWindow(demo=args.demo)
    win.show()
    if args.autoload:
        win.load_data()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
