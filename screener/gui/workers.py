from __future__ import annotations

import datetime as dt
import traceback

from PySide6.QtCore import QThread, Signal


class LoadWorker(QThread):
    """시세 패널 로딩 (네트워크) 을 UI 스레드 밖에서 수행."""

    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, source: str, end: dt.date, days: int, krx_id: str = "", krx_pw: str = ""):
        super().__init__()
        self.source, self.end, self.days = source, end, days
        self.krx_id, self.krx_pw = krx_id, krx_pw

    def run(self):
        try:
            if self.source == "demo":
                from ..data import demo
                panel = demo.load_panel(self.end, self.days, progress=self.progress.emit)
            else:
                from ..data import krx
                self.progress.emit(0, 1, "KRX 로그인 중…")
                krx.login(self.krx_id, self.krx_pw)
                panel = krx.load_panel(self.end, self.days, progress=self.progress.emit)
            self.done.emit(panel)
        except Exception as e:  # noqa: BLE001 — 사용자에게 그대로 보여준다
            traceback.print_exc()
            self.failed.emit(str(e))


class DartWorker(QThread):
    done = Signal(str, object)   # ticker, DataFrame
    failed = Signal(str, str)

    def __init__(self, client, ticker: str):
        super().__init__()
        self.client, self.ticker = client, ticker

    def run(self):
        try:
            self.done.emit(self.ticker, self.client.disclosures(self.ticker))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self.ticker, str(e))
