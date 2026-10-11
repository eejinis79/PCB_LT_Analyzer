from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget,
)


class Card(QFrame):
    """제목/설명이 있는 둥근 카드 컨테이너."""

    def __init__(self, title: str, hint: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("CardTitle")
        lay.addWidget(t)
        if hint:
            h = QLabel(hint)
            h.setObjectName("CardHint")
            h.setWordWrap(True)
            lay.addWidget(h)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        lay.addLayout(self.body)


class ParamSlider(QWidget):
    """라벨 + 슬라이더 + 숫자 입력이 연동되는 파라미터 입력기."""

    valueChanged = Signal(float)

    def __init__(self, label: str, lo: float, hi: float, step: float, value: float,
                 suffix: str = "", decimals: int = 1, tooltip: str = "", parent=None):
        super().__init__(parent)
        self._step = step
        self._lo = lo
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        name = QLabel(label)
        name.setObjectName("ParamLabel")
        self.spin = QDoubleSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setSingleStep(step)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(f" {suffix}" if suffix else "")
        self.spin.setAlignment(Qt.AlignRight)
        self.spin.setFixedWidth(96)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, round((hi - lo) / step))

        grid.addWidget(name, 0, 0)
        grid.addWidget(self.spin, 0, 1, Qt.AlignRight)
        grid.addWidget(self.slider, 1, 0, 1, 2)
        if tooltip:
            for w in (name, self.spin, self.slider):
                w.setToolTip(tooltip)

        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.setValue(value)

    def _from_slider(self, pos: int):
        v = self._lo + pos * self._step
        if abs(self.spin.value() - v) > 1e-9:
            self.spin.setValue(v)

    def _from_spin(self, v: float):
        self.slider.blockSignals(True)
        self.slider.setValue(round((v - self._lo) / self._step))
        self.slider.blockSignals(False)
        self.valueChanged.emit(v)

    def value(self) -> float:
        return self.spin.value()

    def setValue(self, v: float):
        self.spin.setValue(v)
        self._from_spin(self.spin.value())


class StatChip(QFrame):
    def __init__(self, label: str, value: str = "–", color: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Chip")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(0)
        self.value_lbl = QLabel(value)
        self.value_lbl.setObjectName("ChipValue")
        if color:
            self.value_lbl.setStyleSheet(f"color: {color};")
        lbl = QLabel(label)
        lbl.setObjectName("ChipLabel")
        lay.addWidget(self.value_lbl)
        lay.addWidget(lbl)

    def set(self, value: str):
        self.value_lbl.setText(value)


def hbox(*widgets, spacing=8, stretch_last=False) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        else:
            lay.addWidget(w)
    if stretch_last:
        lay.addStretch(1)
    return lay
