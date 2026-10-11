"""DB 위치·상태 화면."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout,
)

from ..db import DB_FILENAME, StockDB, default_db_path
from .widgets import Card


class DbDialog(QDialog):
    """현재 DB 경로와 저장된 데이터 범위를 보여주고, 위치를 바꿀 수 있게 한다."""

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("데이터 저장소 (DB)")
        self.setMinimumWidth(560)
        self.path = path
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        card = Card("SQLite DB", "받아온 시세 · 외국인/기관 순매수 · 저장한 스크리닝 결과가 이 파일 하나에 쌓입니다. "
                                 "이미 받은 날짜는 다시 받지 않습니다.")
        self.form = QFormLayout()
        self.form.setSpacing(8)
        self.lbl_path = QLabel(wordWrap=True)
        self.lbl_path.setTextInteractionFlags(self.lbl_path.textInteractionFlags() |
                                              self.lbl_path.textInteractionFlags().TextSelectableByMouse)
        self.lbl_range = QLabel()
        self.lbl_rows = QLabel()
        self.lbl_runs = QLabel()
        self.lbl_size = QLabel()
        for k, w in (("위치", self.lbl_path), ("시세 기간", self.lbl_range), ("데이터", self.lbl_rows),
                     ("저장한 결과", self.lbl_runs), ("파일 크기", self.lbl_size)):
            self.form.addRow(k, w)
        card.body.addLayout(self.form)
        lay.addWidget(card)

        row = QHBoxLayout()
        change = QPushButton("위치 변경…")
        change.clicked.connect(self._change)
        reset = QPushButton("기본 위치로", objectName="Ghost")
        reset.clicked.connect(lambda: self._set(str(default_db_path())))
        opendir = QPushButton("폴더 열기", objectName="Ghost")
        opendir.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.path).parent))))
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.button(QDialogButtonBox.Ok).setText("닫기")
        bb.accepted.connect(self.accept)
        for w in (change, reset, opendir):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(bb)
        lay.addLayout(row)
        self._refresh()

    def _refresh(self):
        try:
            st = StockDB(self.path).stats()
        except Exception as e:  # noqa: BLE001
            self.lbl_path.setText(f"{self.path}\n⚠ 열 수 없음: {e}")
            return
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else "–"  # noqa: E731
        self.lbl_path.setText(st["path"])
        self.lbl_range.setText(f"{fmt(st['first'])} ~ {fmt(st['last'])}  ({st['days']}영업일)")
        self.lbl_rows.setText(f"시세 {st['ohlcv_rows']:,}행 · 수급 {st['flow_days']}영업일")
        self.lbl_runs.setText(f"{st['runs']}회")
        self.lbl_size.setText(f"{st['size_mb']:.1f} MB")

    def _set(self, path: str):
        self.path = path
        self._refresh()

    def _change(self):
        start = str(Path(self.path).parent / DB_FILENAME)
        path, _ = QFileDialog.getSaveFileName(
            self, "DB 파일 위치 (기존 파일을 고르면 그 DB 를 사용)", start, "SQLite DB (*.db)",
            options=QFileDialog.DontConfirmOverwrite)
        if not path:
            return
        if not path.lower().endswith(".db"):
            path += ".db"
        try:
            StockDB(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "DB", f"이 위치에 DB 를 만들 수 없습니다:\n{e}")
            return
        self._set(path)
