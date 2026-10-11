"""선택 종목 캔들 + 거래량 차트 (matplotlib)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import matplotlib

try:
    matplotlib.use("QtAgg")
except ImportError:  # pyplot 이 다른 백엔드로 먼저 로드된 경우 — Qt 캔버스를 직접 쓰므로 무관
    pass
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager as fm  # noqa: E402
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

from . import theme as T  # noqa: E402


def _setup_korean_font():
    # pykrx 에 포함된 나눔바른고딕을 우선 등록 (OS 에 한글 폰트가 없어도 동작)
    spec = importlib.util.find_spec("pykrx")
    if spec and spec.origin:
        ttf = Path(spec.origin).parent / "NanumBarunGothic.ttf"
        if ttf.exists():
            fm.fontManager.addfont(str(ttf))
    available = {f.name for f in fm.fontManager.ttflist}
    prefs = ["Malgun Gothic", "AppleGothic", "NanumBarunGothic", "NanumGothic",
             "Noto Sans CJK KR", "DejaVu Sans"]
    matplotlib.rcParams["font.family"] = [f for f in prefs if f in available] or ["sans-serif"]
    matplotlib.rcParams["axes.unicode_minus"] = False


_setup_korean_font()


class ChartWidget(FigureCanvasQTAgg):
    def __init__(self, parent=None):
        self.fig = Figure(figsize=(6, 4), facecolor=T.SURFACE)
        super().__init__(self.fig)
        self.setParent(parent)
        self.setStyleSheet(f"background: {T.SURFACE}; border-radius: 10px;")
        self.show_message("종목을 선택하면 차트가 표시됩니다")

    def _style(self, ax):
        ax.set_facecolor(T.SURFACE)
        for s in ax.spines.values():
            s.set_color(T.BORDER)
        ax.tick_params(colors=T.MUTED, labelsize=8)
        ax.grid(True, color=T.BORDER, alpha=0.5, linewidth=0.6)

    def show_message(self, text: str):
        self.fig.clear()
        self.fig.text(0.5, 0.5, text, ha="center", va="center", color=T.MUTED, fontsize=11)
        self.draw_idle()

    def plot(self, hist: pd.DataFrame, title: str, recent_days: int, base_days: int,
             base_vol: float | None = None):
        hist = hist.dropna(subset=["close"])
        if hist.empty:
            self.show_message("데이터 없음")
            return
        self.fig.clear()
        gs = self.fig.add_gridspec(2, 1, height_ratios=[2.2, 1], hspace=0.05,
                                   left=0.08, right=0.98, top=0.9, bottom=0.1)
        ax = self.fig.add_subplot(gs[0])
        axv = self.fig.add_subplot(gs[1], sharex=ax)
        for a in (ax, axv):
            self._style(a)

        x = np.arange(len(hist))
        o, h, l, c = (hist[k].to_numpy() for k in ("open", "high", "low", "close"))
        up = c >= o
        colors = np.where(up, T.UP, T.DOWN)
        ax.vlines(x, l, h, color=colors, linewidth=1)
        ax.bar(x, np.maximum(np.abs(c - o), c * 0.0008), bottom=np.minimum(o, c),
               color=colors, width=0.65)

        n = len(hist)
        r0 = n - recent_days - 0.5
        b0 = max(-0.5, r0 - base_days)
        for a in (ax, axv):
            a.axvspan(b0, r0, color=T.ACCENT, alpha=0.06)
            a.axvspan(r0, n - 0.5, color=T.WARN, alpha=0.12)

        vol = hist["volume"].to_numpy()
        vcolors = [T.WARN if i >= n - recent_days else T.MUTED for i in range(n)]
        axv.bar(x, vol, color=vcolors, width=0.65, alpha=0.9)
        if base_vol:
            axv.axhline(base_vol, color=T.ACCENT, linewidth=1, linestyle="--")
            axv.text(0, base_vol, " 평소 거래량", color=T.ACCENT, fontsize=8, va="bottom")
        axv.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_human))

        ticks = np.linspace(0, n - 1, min(6, n)).astype(int)
        axv.set_xticks(ticks)
        axv.set_xticklabels([hist.index[i].strftime("%m/%d") for i in ticks])
        ax.tick_params(labelbottom=False)
        ax.set_xlim(-0.8, n - 0.2)
        self.fig.suptitle(title, color=T.TEXT, fontsize=12, x=0.08, ha="left", fontweight="bold")
        self.draw_idle()


def _human(v, _pos=None):
    for unit, div in (("억", 1e8), ("만", 1e4)):
        if abs(v) >= div:
            return f"{v / div:.0f}{unit}"
    return f"{v:.0f}"
