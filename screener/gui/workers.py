from __future__ import annotations

import datetime as dt
import traceback

from PySide6.QtCore import QThread, Signal


class LoadWorker(QThread):
    """시세 패널 로딩 (네트워크) 을 UI 스레드 밖에서 수행."""

    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, source: str, end: dt.date, days: int, krx_id: str = "", krx_pw: str = "",
                 db_path: str | None = None):
        super().__init__()
        self.source, self.end, self.days = source, end, days
        self.krx_id, self.krx_pw = krx_id, krx_pw
        self.db_path = db_path

    def run(self):
        try:
            if self.source == "demo":
                from ..data import demo
                panel = demo.load_panel(self.end, self.days, progress=self.progress.emit)
            else:
                from ..data import krx
                from ..db import StockDB
                db = StockDB(self.db_path)
                if db.stats()["days"] == 0:  # 예전 버전 캐시(.pkl)가 있으면 DB 로 옮겨 재다운로드 방지
                    self.progress.emit(0, 1, "이전 캐시를 DB 로 옮기는 중…")
                    n = db.import_pickle_cache(krx.LEGACY_CACHE_DIR)
                    if n:
                        print(f"이전 캐시 {n}개 파일을 DB 로 옮김")
                self.progress.emit(0, 1, "KRX 로그인 중…")
                krx.login(self.krx_id, self.krx_pw)
                panel = krx.load_panel(self.end, self.days, progress=self.progress.emit, db=db)
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
