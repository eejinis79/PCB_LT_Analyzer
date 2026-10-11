from __future__ import annotations

import math

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont

from . import theme as T

# (컬럼, 헤더, 포맷, 툴팁)
COLUMNS = [
    ("ticker", "코드", "{}", ""),
    ("name", "종목명", "{}", ""),
    ("market", "시장", "{}", ""),
    ("close", "종가", "{:,.0f}", "기준일 종가"),
    ("chg_pct", "기간등락%", "{:+.2f}", "최근 구간 누적 등락률 (구간 직전 종가 대비)"),
    ("max_daily_chg", "최대일등락%", "{:.2f}", "최근 구간 일별 등락률 절대값의 최대"),
    ("max_range", "일중변동%", "{:.2f}", "최근 구간 (고가-저가)/전일종가 최대"),
    ("vol_ratio", "거래량배수", "{:.1f}x", "최근 구간 평균 거래량 / 평소 거래량"),
    ("recent_vol", "최근거래량", "{:,.0f}", ""),
    ("base_vol", "평소거래량", "{:,.0f}", ""),
    ("value_eok", "거래대금(억)", "{:,.1f}", "최근 구간 평균 거래대금"),
    ("mcap_eok", "시총(억)", "{:,.0f}", ""),
    ("box_pct", "박스폭%", "{:.1f}", "기준 구간 (최고-최저 종가)/평균"),
    ("score", "점수", "{:.1f}", "log2(거래량배수)×10 / (1 + |기간등락| + 0.5×일중변동)"),
]
NUMERIC = {c for c, *_ in COLUMNS if c not in ("ticker", "name", "market")}


class ResultsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.df = pd.DataFrame(columns=[c for c, *_ in COLUMNS])
        self._bold = QFont()
        self._bold.setBold(True)

    def set_frame(self, df: pd.DataFrame):
        self.beginResetModel()
        self.df = df.reset_index(drop=True)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.df)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return COLUMNS[section][1]
        if role == Qt.ToolTipRole:
            return COLUMNS[section][3] or None
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col, _, fmt, _ = COLUMNS[index.column()]
        v = self.df.iat[index.row(), index.column()]
        if role == Qt.DisplayRole:
            if isinstance(v, float) and math.isnan(v):
                return "–"
            return fmt.format(v)
        if role == Qt.UserRole:  # 정렬용 원값 (numpy 스칼라는 QVariant 로 변환되지 않음)
            return v.item() if hasattr(v, "item") else v
        if role == Qt.TextAlignmentRole:
            return int((Qt.AlignRight if col in NUMERIC else Qt.AlignLeft) | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if col == "chg_pct" and isinstance(v, float):
                return QColor(T.UP if v > 0 else T.DOWN if v < 0 else T.MUTED)
            if col == "vol_ratio":
                return QColor(T.WARN if v >= 5 else T.ACCENT)
            if col == "market":
                return QColor(T.MUTED)
            if col == "score":
                return QColor(T.GOOD)
        if role == Qt.FontRole and col in ("name", "vol_ratio", "score"):
            return self._bold
        return None

    def row(self, r: int) -> pd.Series:
        return self.df.iloc[r]


class ResultsProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(-1)  # 모든 컬럼(코드/종목명)에서 검색
