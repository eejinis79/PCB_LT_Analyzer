"""저장한 신호의 이후 수익률 화면."""
from __future__ import annotations

import json
import math

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSplitter, QTableView, QVBoxLayout,
)

from .. import performance
from ..db import StockDB
from . import theme as T
from .chart import ChartWidget
from .widgets import StatChip

# (컬럼, 헤더, 형식) — 형식 "pct" 는 수익률(색 표시)
DETAIL_COLUMNS = [
    ("asof", "신호일", "date"), ("ticker", "코드", "str"), ("name", "종목명", "str"),
    ("market", "시장", "str"), ("vol_ratio", "거래량배수", "{:.1f}x"), ("entry", "진입가", "{:,.0f}"),
    ("r1", "+1일", "pct"), ("r5", "+5일", "pct"), ("r10", "+10일", "pct"), ("r20", "+20일", "pct"),
    ("max_gain", "20일 최고", "pct"), ("max_loss", "20일 최저", "pct"),
    ("last_ret", "현재까지", "pct"), ("held", "경과일", "{:.0f}"),
]


def _params_summary(raw: str) -> str:
    try:
        p = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    s = f"거래량≥{p.get('vol_ratio_min', 0):g}배 · 등락±{p.get('price_chg_max', 0):g}% · 최근{p.get('recent_days', 1)}일"
    if p.get("flow_enabled"):
        s += " · 수급"
    return s


class FrameModel(QAbstractTableModel):
    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.df = pd.DataFrame()

    def set_frame(self, df: pd.DataFrame):
        self.beginResetModel()
        self.df = df.reset_index(drop=True)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.df)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.columns[section][1]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col, _, fmt = self.columns[index.column()]
        v = self.df[col].iat[index.row()] if col in self.df else None
        missing = v is None or (isinstance(v, float) and math.isnan(v))
        if role == Qt.DisplayRole:
            if missing:
                return "–"
            if fmt == "pct":
                return f"{v * 100:+.2f}%"
            if fmt == "date":
                return f"{v[:4]}-{v[4:6]}-{v[6:]}"
            if fmt == "str":
                return str(v)
            return fmt.format(v)
        if role == Qt.UserRole:
            if missing:
                return -1e18  # 정렬 시 맨 아래로
            return v.item() if hasattr(v, "item") else v
        if role == Qt.TextAlignmentRole:
            return int((Qt.AlignLeft if fmt in ("str", "date") else Qt.AlignRight) | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if fmt == "pct" and not missing:
                return QColor(T.UP if v > 0 else T.DOWN if v < 0 else T.MUTED)
            if missing or col == "market":
                return QColor(T.MUTED)
        return None


class PerformanceDialog(QDialog):
    def __init__(self, db_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("신호 성과 — 저장한 스크리닝 결과의 이후 수익률")
        self.resize(1280, 820)
        self.db = StockDB(db_path)
        self.detail = pd.DataFrame()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self.run_combo = QComboBox()
        self.run_combo.setMinimumWidth(420)
        self.entry_combo = QComboBox()
        for k, v in performance.ENTRY_MODES.items():
            self.entry_combo.addItem(v, k)
        refresh = QPushButton("새로고침")
        refresh.clicked.connect(self.reload)
        export = QPushButton("CSV 내보내기", objectName="Ghost")
        export.clicked.connect(self.export_csv)
        top.addWidget(QLabel("대상"))
        top.addWidget(self.run_combo)
        top.addWidget(QLabel("진입가"))
        top.addWidget(self.entry_combo)
        top.addStretch(1)
        top.addWidget(refresh)
        top.addWidget(export)
        lay.addLayout(top)

        self.hint = QLabel("", objectName="CardHint")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        chips = QHBoxLayout()
        chips.setSpacing(10)
        self.chip_n = StatChip("신호 수", color=T.TEXT)
        chips.addWidget(self.chip_n)
        self.chips = {}
        for h in performance.HORIZONS:
            c = StatChip(f"+{h}일 평균 · 승률 · 시장대비", color=T.TEXT)
            self.chips[h] = c
            chips.addWidget(c)
        chips.addStretch(1)
        lay.addLayout(chips)

        split = QSplitter(Qt.Vertical)
        split.setHandleWidth(10)
        self.model = FrameModel(DETAIL_COLUMNS, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, (c, *_r) in enumerate(DETAIL_COLUMNS):
            self.table.setColumnWidth(i, {"asof": 96, "ticker": 70, "name": 130, "market": 70}.get(c, 86))
        self.table.doubleClicked.connect(self._open_naver)
        self.table.setToolTip("더블클릭: 네이버 금융")
        split.addWidget(self.table)
        self.chart = ChartWidget()
        split.addWidget(self.chart)
        split.setSizes([480, 260])
        lay.addWidget(split, 1)

        self._fill_runs()
        self.run_combo.currentIndexChanged.connect(self.reload)
        self.entry_combo.currentIndexChanged.connect(self.reload)
        self.reload()

    def _fill_runs(self):
        self.run_combo.clear()
        runs = self.db.list_runs()
        self.run_combo.addItem(f"전체 저장 결과 ({len(runs)}회, 같은 날 같은 종목은 1번만)", None)
        for r in runs.itertuples():
            d = r.asof
            self.run_combo.addItem(
                f"{d[:4]}-{d[4:6]}-{d[6:]} · {r.n_hits}종목 · {_params_summary(r.params)}", int(r.run_id))

    def reload(self):
        run_id = self.run_combo.currentData()
        entry = self.entry_combo.currentData() or "close"
        detail, summary, path = performance.from_db(
            self.db, run_ids=None if run_id is None else [run_id], entry=entry)
        self.detail = detail
        self.model.set_frame(detail)
        self.table.sortByColumn(0, Qt.DescendingOrder)
        self.chip_n.set(f"{len(detail):,}")
        if summary.empty:
            for c in self.chips.values():
                c.set("–")
            self.hint.setText("저장된 결과가 없습니다. 메인 화면에서 KRX 데이터로 스크리닝한 뒤 "
                              "‘결과 DB 저장’을 누르면, 이후 매일 ‘데이터 불러오기’를 할 때마다 수익률이 채워집니다.")
            self.chart.show_message("저장된 신호가 없습니다")
            return
        for row in summary.itertuples():
            if row.n == 0:
                self.chips[row.horizon].set("데이터 대기")
                continue
            self.chips[row.horizon].set(
                f"{row.mean * 100:+.1f}% · {row.win_rate * 100:.0f}% · {row.excess * 100:+.1f}%p")
        last = self.db.stats()["last"] or ""
        self.hint.setText(
            "수익률 = N영업일 뒤 종가 ÷ 진입가 − 1. 시장대비 = 같은 기간 전종목(KOSPI·KOSDAQ) 동일가중 평균 수익률과의 차이. "
            f"DB 시세 마지막 날짜: {last[:4]}-{last[4:6]}-{last[6:]} — 이후 날짜는 ‘데이터 불러오기’를 하면 채워집니다. "
            "수수료·세금 미반영.")
        self._plot(path)

    def _plot(self, path: pd.DataFrame):
        p = path[path["n"] > 0]
        if p.empty:
            self.chart.show_message("신호 이후 시세가 아직 없습니다 — 다음 거래일 이후 확인하세요")
            return
        fig = self.chart.fig
        fig.clear()
        ax = fig.add_subplot(111)
        self.chart._style(ax)
        ax.axhline(0, color=T.BORDER, linewidth=1)
        ax.plot(p["day"], p["signal"] * 100, color=T.ACCENT, marker="o", markersize=3, label="신호 평균")
        ax.plot(p["day"], p["bench"] * 100, color=T.MUTED, linestyle="--", label="시장 평균")
        ax.fill_between(p["day"], p["signal"] * 100, p["bench"] * 100,
                        where=p["signal"] >= p["bench"], color=T.UP, alpha=0.12, interpolate=True)
        ax.fill_between(p["day"], p["signal"] * 100, p["bench"] * 100,
                        where=p["signal"] < p["bench"], color=T.DOWN, alpha=0.12, interpolate=True)
        ax.set_xlabel("신호 이후 영업일", color=T.MUTED, fontsize=9)
        ax.set_ylabel("평균 누적수익률 (%)", color=T.MUTED, fontsize=9)
        ax.set_xticks(list(p["day"]))
        leg = ax.legend(facecolor=T.SURFACE, edgecolor=T.BORDER, fontsize=9)
        for t in leg.get_texts():
            t.set_color(T.TEXT)
        fig.subplots_adjust(left=0.07, right=0.98, top=0.9, bottom=0.18)
        fig.suptitle("신호 이후 평균 누적수익률 vs 시장", color=T.TEXT, fontsize=11, x=0.07, ha="left")
        self.chart.draw_idle()

    def _open_naver(self, idx):
        ticker = self.proxy.index(idx.row(), 1).data()
        QDesktopServices.openUrl(QUrl(f"https://finance.naver.com/item/main.naver?code={ticker}"))

    def export_csv(self):
        if self.detail.empty:
            return
        path, _ = QFileDialog.getSaveFileName(self, "CSV 저장", "signal_performance.csv", "CSV (*.csv)")
        if path:
            cols = [c for c, *_ in DETAIL_COLUMNS if c in self.detail]
            out = self.detail[cols].copy()
            for c, _h, f in DETAIL_COLUMNS:
                if f == "pct" and c in out:
                    out[c] = (out[c] * 100).round(2)
            out.rename(columns={c: h for c, h, _ in DETAIL_COLUMNS}).to_csv(
                path, index=False, encoding="utf-8-sig")

