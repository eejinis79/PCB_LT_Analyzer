"""백테스트 창: 과거 데이터 수집 · 매매 시뮬레이션 · 조건 최적화 · 요인 분석 · 모의투자."""
from __future__ import annotations

import json
import traceback
from dataclasses import replace

import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
from PySide6.QtCore import QDate, QSortFilterProxyModel, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableView, QTabWidget, QVBoxLayout, QWidget,
)

from .. import backtest as bt
from ..db import StockDB
from . import theme as T
from .chart import ChartWidget
from .performance_view import FrameModel
from .widgets import Card, ParamSlider, StatChip, hbox

TRADE_COLUMNS = [
    ("entry_date", "진입일", "date"), ("exit_date", "청산일", "date"), ("ticker", "코드", "str"),
    ("name", "종목명", "str"), ("market", "시장", "str"), ("entry", "진입가", "{:,.0f}"),
    ("exit", "청산가", "{:,.0f}"), ("ret", "수익률", "pct"), ("days", "보유일", "{:.0f}"),
    ("reason", "청산 사유", "str"),
]
FACTOR_COLUMNS = [
    ("label", "지표", "str"), ("ic", "수익 관련성(IC)", "{:+.3f}"), ("weight", "학습 가중치", "{:+.4f}"),
    ("days", "분석 일수", "{:.0f}"),
]


class TaskWorker(QThread):
    """fn(progress, cancelled) 을 백그라운드에서 실행."""

    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self.done.emit(self.fn(self.progress.emit, lambda: self._cancel))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(str(e))


def _table(model) -> QTableView:
    proxy = QSortFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.setSortRole(Qt.UserRole)
    t = QTableView()
    t.setModel(proxy)
    t._proxy = proxy  # 참조 유지
    t.setSortingEnabled(True)
    t.setAlternatingRowColors(True)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(26)
    t.setShowGrid(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _pct(v, signed=True):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "–"
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


class BacktestWindow(QDialog):
    def __init__(self, main):
        super().__init__(main, Qt.Window)
        self.main = main
        self.setWindowTitle("백테스트 · 최적화 · 요인 분석 · 모의투자")
        self.resize(1440, 900)
        self._worker: TaskWorker | None = None
        self._panel = None
        self._cands = None
        self._cands_key = None
        self._grid = None
        self._factor = None

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.addWidget(self._build_side())
        right = QVBoxLayout()
        right.addLayout(self._build_top())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_result_tab(), "결과")
        self.tabs.addTab(self._build_grid_tab(), "조건 최적화")
        self.tabs.addTab(self._build_factor_tab(), "요인 분석 · 학습 점수")
        right.addWidget(self.tabs, 1)
        root.addLayout(right, 1)
        self.refresh_db_info()

    # ------------------------------------------------------------------ UI
    def _build_top(self):
        lay = QHBoxLayout()
        self.db_info = QLabel("", objectName="CardHint")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(260)
        self.progress.hide()
        self.status = QLabel("", objectName="CardHint")
        self.stop_btn = QPushButton("중지", objectName="Ghost")
        self.stop_btn.clicked.connect(self._cancel)
        self.stop_btn.hide()
        lay.addWidget(self.db_info, 1)
        lay.addWidget(self.status)
        lay.addWidget(self.progress)
        lay.addWidget(self.stop_btn)
        return lay

    def _build_side(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(340)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(12)

        dc = Card("① 과거 데이터 수집", "KRX 에서 과거 전종목 시세(상장폐지 종목 포함)를 DB 에 받습니다. "
                                      "이미 받은 날짜는 건너뛰어 중간에 멈춰도 이어받습니다.")
        self.years = QSpinBox()
        self.years.setRange(1, 10)
        self.years.setValue(3)
        self.years.setSuffix(" 년")
        self.with_flows = QCheckBox("외국인·기관 순매수 포함 (시간 약 3배)")
        self.bf_btn = QPushButton("과거 데이터 수집")
        self.bf_btn.clicked.connect(self.run_backfill)
        dc.body.addLayout(hbox(QLabel("기간", objectName="ParamLabel"), None, self.years))
        dc.body.addWidget(self.with_flows)
        dc.body.addWidget(self.bf_btn)
        lay.addWidget(dc)

        P = ParamSlider
        rc = Card("② 매매 규칙", "신호 다음날 시가에 사서 아래 규칙으로 판다고 가정합니다.")
        self.r_stop = P("손절 (0=안 함)", 0, 30, 0.5, 5, "%", 1)
        self.r_target = P("익절 (0=안 함)", 0, 100, 1, 15, "%", 0)
        self.r_hold = P("최대 보유", 1, 120, 1, 20, "일", 0)
        self.r_cost = P("왕복 비용", 0, 2, 0.05, 0.25, "%", 2, "수수료 + 매도 세금 + 체결 오차(슬리피지)")
        self.r_pos = P("동시 보유 종목", 1, 20, 1, 5, "개", 0, "자금을 N 등분해서 투자")
        self.r_day = P("하루 신규 진입", 1, 10, 1, 2, "개", 0, "같은 날 신호가 많으면 점수 상위부터")
        self.r_regime = QCheckBox("하락장 진입 금지 (지수 < 이동평균)")
        self.r_regime.setChecked(True)
        self.r_ma = P("이동평균", 20, 200, 5, 60, "일", 0)
        self.r_regime.toggled.connect(self.r_ma.setEnabled)
        for w in (self.r_stop, self.r_target, self.r_hold, self.r_cost, self.r_pos, self.r_day,
                  self.r_regime, self.r_ma):
            rc.body.addWidget(w)
        lay.addWidget(rc)

        sc = Card("③ 실행", "조건은 메인 화면 왼쪽에 지금 설정된 값을 그대로 사용합니다.")
        self.source = QComboBox()
        self.source.addItem("과거 백테스트 (현재 조건으로 매일 스크리닝했다면)", "backtest")
        self.source.addItem("모의투자 (DB 에 저장된 실제 신호로 매매했다면)", "paper")
        self.start = QDateEdit(calendarPopup=True)
        self.end = QDateEdit(calendarPopup=True)
        for d in (self.start, self.end):
            d.setDisplayFormat("yyyy-MM-dd")
        self.params_lbl = QLabel("", objectName="CardHint")
        self.params_lbl.setWordWrap(True)
        self.run_btn = QPushButton("백테스트 실행", objectName="Primary")
        self.run_btn.clicked.connect(self.run_backtest)
        sc.body.addWidget(self.source)
        sc.body.addLayout(hbox(QLabel("시작", objectName="ParamLabel"), None, self.start))
        sc.body.addLayout(hbox(QLabel("종료", objectName="ParamLabel"), None, self.end))
        sc.body.addWidget(self.params_lbl)
        sc.body.addWidget(self.run_btn)
        lay.addWidget(sc)
        lay.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _build_result_tab(self):
        w = QWidget(objectName="Root")
        lay = QVBoxLayout(w)
        chips = QHBoxLayout()
        self.k = {}
        for key, label in (("trades", "거래 수"), ("win_rate", "승률"), ("avg_ret", "거래당 평균"),
                           ("total", "누적 수익"), ("cagr", "연환산"), ("mdd", "최대 낙폭"),
                           ("bench_total", "시장(전종목 평균)")):
            c = StatChip(label, color=T.TEXT)
            c.setMinimumWidth(112)
            self.k[key] = c
            chips.addWidget(c)
        chips.addStretch(1)
        lay.addLayout(chips)
        self.result_note = QLabel("", objectName="CardHint")
        self.result_note.setWordWrap(True)
        lay.addWidget(self.result_note)
        split = QSplitter(Qt.Vertical)
        self.eq_chart = ChartWidget()
        self.eq_chart.show_message("왼쪽에서 ‘백테스트 실행’")
        split.addWidget(self.eq_chart)
        self.trade_model = FrameModel(TRADE_COLUMNS, self)
        self.trade_table = _table(self.trade_model)
        for i, (c, *_r) in enumerate(TRADE_COLUMNS):
            self.trade_table.setColumnWidth(i, {"name": 130, "reason": 90}.get(c, 90))
        split.addWidget(self.trade_table)
        split.setSizes([420, 300])
        lay.addWidget(split, 1)
        return w

    def _build_grid_tab(self):
        w = QWidget(objectName="Root")
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.g_h = QComboBox()
        for h in bt.FWD_HORIZONS:
            self.g_h.addItem(f"{h}일 보유 수익", h)
        self.g_h.setCurrentIndex(1)
        self.g_top = QSpinBox()
        self.g_top.setRange(0, 20)
        self.g_top.setValue(0)
        self.g_top.setSpecialValueText("전체 신호")
        self.g_top.setPrefix("날짜별 상위 ")
        self.g_top.setToolTip("0 = 조건에 맞는 모든 신호, N = 날짜별 점수 상위 N개만 (실제 매매처럼)")
        run = QPushButton("최적화 실행", objectName="Primary")
        run.clicked.connect(self.run_grid)
        self.g_apply = QPushButton("추천 조합을 메인 화면에 적용")
        self.g_apply.clicked.connect(self.apply_grid)
        self.g_apply.setEnabled(False)
        for x in (QLabel("평가 기준"), self.g_h, self.g_top, run):
            top.addWidget(x)
        top.addStretch(1)
        top.addWidget(self.g_apply)
        lay.addLayout(top)
        self.g_note = QLabel("거래량 배수 × 허용 등락률 조합별로, 앞쪽 2/3 기간(학습)과 뒤쪽 1/3 기간(검증)의 "
                             "신호당 평균 수익(비용 차감)을 비교합니다. 학습 기간에서 고른 조합이 검증 기간에서도 "
                             "좋아야 믿을 만합니다.", objectName="CardHint")
        self.g_note.setWordWrap(True)
        lay.addWidget(self.g_note)
        self.g_chart = ChartWidget()
        self.g_chart.show_message("‘최적화 실행’ — 처음에는 후보 계산에 수십 초 걸립니다")
        lay.addWidget(self.g_chart, 1)
        return w

    def _build_factor_tab(self):
        w = QWidget(objectName="Root")
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.f_h = QComboBox()
        for h in bt.FWD_HORIZONS:
            self.f_h.addItem(f"{h}일 보유 수익", h)
        self.f_h.setCurrentIndex(1)
        run = QPushButton("분석 실행", objectName="Primary")
        run.clicked.connect(self.run_factor)
        self.f_apply = QPushButton("학습 점수를 메인 화면 점수로 사용")
        self.f_apply.clicked.connect(self.apply_factor)
        self.f_apply.setEnabled(False)
        for x in (QLabel("평가 기준"), self.f_h, run):
            top.addWidget(x)
        top.addStretch(1)
        top.addWidget(self.f_apply)
        lay.addLayout(top)
        self.f_note = QLabel("후보 신호의 각 지표가 이후 수익과 얼마나 관련 있었는지(IC: 날짜별 순위상관 평균, "
                             "±0.03 이상이면 의미 있는 편)와, 학습 기간 데이터로 정한 가중치 점수가 "
                             "검증 기간에서 기본 점수보다 나았는지 보여줍니다.", objectName="CardHint")
        self.f_note.setWordWrap(True)
        lay.addWidget(self.f_note)
        self.f_result = QLabel("")
        self.f_result.setWordWrap(True)
        self.f_result.setStyleSheet("font-size: 14px;")
        lay.addWidget(self.f_result)
        self.f_model = FrameModel(FACTOR_COLUMNS, self)
        self.f_table = _table(self.f_model)
        self.f_table.setColumnWidth(0, 260)
        self.f_table.setSortingEnabled(False)  # 관련성 절대값 순 유지
        lay.addWidget(self.f_table, 1)
        return w

    # ------------------------------------------------------------------ 공통
    @property
    def db(self) -> StockDB:
        return StockDB(self.main.db_path)

    def refresh_db_info(self):
        try:
            st = self.db.stats()
        except Exception as e:  # noqa: BLE001
            self.db_info.setText(f"DB 오류: {e}")
            return
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else "–"  # noqa: E731
        self.db_info.setText(f"DB: {fmt(st['first'])} ~ {fmt(st['last'])} · {st['days']}영업일 · "
                             f"수급 {st['flow_days']}일 · 저장 결과 {st['runs']}회 · {st['size_mb']:.0f}MB")
        if st["first"]:
            first = QDate.fromString(st["first"], "yyyyMMdd")
            last = QDate.fromString(st["last"], "yyyyMMdd")
            for d in (self.start, self.end):
                d.setDateRange(first, last)
            if self.start.date() <= first or self.start.date() > last:
                self.start.setDate(first.addDays(120))  # 지표 계산용 앞부분 여유
            self.end.setDate(last)
        p = self.main.params()
        self.params_lbl.setText(
            f"현재 조건: 거래량 ≥{p.vol_ratio_min:g}배 · 등락 ±{p.price_chg_max:g}% · 최근 {p.recent_days}일 · "
            f"거래대금 ≥{p.min_value_eok:g}억" + (" · 수급" if p.flow_enabled else "")
            + (" · 바닥권" if p.pos_enabled else "") + (" · OBV" if p.obv_enabled else "")
            + (" · 학습점수" if p.score_weights else ""))

    def rules(self) -> bt.TradeRules:
        return bt.TradeRules(
            stop_pct=self.r_stop.value(), target_pct=self.r_target.value(), max_hold=int(self.r_hold.value()),
            cost_pct=self.r_cost.value(), max_positions=int(self.r_pos.value()), per_day=int(self.r_day.value()),
            regime_filter=self.r_regime.isChecked(), regime_ma=int(self.r_ma.value()))

    def _busy(self, on: bool, msg: str = ""):
        for b in (self.run_btn, self.bf_btn):
            b.setEnabled(not on)
        self.progress.setVisible(on)
        self.stop_btn.setVisible(on)
        self.status.setText(msg)

    def _start(self, fn, on_done, msg):
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "백테스트", "다른 작업이 진행 중입니다.")
            return
        self._busy(True, msg)
        self._worker = TaskWorker(fn)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(lambda r: (self._busy(False), on_done(r)))
        self._worker.failed.connect(lambda m: (self._busy(False), QMessageBox.critical(self, "오류", m)))
        self._worker.start()

    def _on_progress(self, i, n, msg):
        self.progress.setMaximum(max(n, 1))
        self.progress.setValue(i)
        self.status.setText(msg)

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("중지하는 중…")

    def shutdown(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(10000)

    def _date_str(self, w: QDateEdit) -> str:
        return w.date().toString("yyyyMMdd")

    def _ensure_candidates(self, progress, cancelled):
        """현재 조건·기간의 후보 풀 (같은 설정이면 재사용)."""
        p = self.main.params()
        if self._panel is None:
            progress(0, 1, "DB 에서 과거 시세 읽는 중…")
            self._panel = bt.load_history(self.db)
        key = json.dumps([bt.loose_params(p).to_dict(), p.pos_days, self._date_str(self.start),
                          self.r_cost.value()], sort_keys=True, default=str)
        if self._cands is None or self._cands_key != key:
            cands = bt.build_candidates(self._panel, p, start=self._date_str(self.start), progress=progress,
                                        cancelled=cancelled, cost_pct=self.r_cost.value())
            if cancelled():
                raise RuntimeError("중지했습니다.")
            self._cands, self._cands_key = cands, key
        end = pd.Timestamp(self.end.date().toPython())
        cands = self._cands
        return cands[cands["date"] <= end] if not cands.empty else cands

    # ------------------------------------------------------------------ 과거 데이터 수집
    def run_backfill(self):
        m = self.main
        if not m.ensure_unlocked():
            return
        if not (m.creds.krx_id and m.creds.krx_pw):
            QMessageBox.information(self, "계정", "메인 화면 ‘계정’에서 KRX ID/비밀번호를 먼저 입력하세요.")
            return
        years, flows = self.years.value(), self.with_flows.isChecked()
        calls = years * 248 * 2 * (3 if flows else 1)
        if QMessageBox.question(
                self, "과거 데이터 수집",
                f"{years}년치 약 {calls:,}회 KRX 조회 (이미 받은 날짜 제외)\n"
                f"예상 시간: {calls * 0.8 / 60:.0f}~{calls * 1.5 / 60:.0f}분.\n"
                "중간에 ‘중지’하거나 창을 닫아도 다음에 이어서 받습니다. 시작할까요?") != QMessageBox.Yes:
            return
        db_path, krx_id, krx_pw = m.db_path, m.creds.krx_id, m.creds.krx_pw

        def job(progress, cancelled):
            from ..data import krx
            progress(0, 1, "KRX 로그인…")
            krx.login(krx_id, krx_pw)
            return krx.backfill(StockDB(db_path), years=years, with_flows=flows,
                                progress=progress, cancelled=cancelled)

        def done(stats):
            self._panel = self._cands = None
            self.refresh_db_info()
            QMessageBox.information(
                self, "과거 데이터 수집",
                ("중지했습니다. 다시 누르면 이어서 받습니다.\n" if stats["cancelled"] else "완료했습니다.\n")
                + f"새로 받음 {stats['fetched']:,} · 이미 있음 {stats['skipped']:,} · 오류 {stats['errors']:,}")

        self._start(job, done, "과거 데이터 수집 중…")

    # ------------------------------------------------------------------ 백테스트
    def run_backtest(self):
        self.refresh_db_info()
        rules, source = self.rules(), self.source.currentData()
        end = pd.Timestamp(self.end.date().toPython())

        def job(progress, cancelled):
            if source == "paper":
                if self._panel is None:
                    progress(0, 1, "DB 에서 시세 읽는 중…")
                    self._panel = bt.load_history(self.db)
                sig = bt.saved_signals(self.db, self._panel)
            else:
                cands = self._ensure_candidates(progress, cancelled)
                sig = bt.select(cands, self.main.params())
            sig = sig[sig["di"] < int(np.searchsorted(self._panel.dates, end, side="right"))] if len(sig) else sig
            progress(1, 1, "매매 시뮬레이션…")
            return bt.simulate(sig, self._panel, rules) + (len(sig),)

        self._start(job, self._show_result, "백테스트 실행 중…")

    def _show_result(self, res):
        trades, equity, m, n_sig = res
        if equity.empty or not m.get("trades"):
            for c in self.k.values():
                c.set("–")
            self.eq_chart.show_message("신호가 없습니다 — 기간·조건을 확인하세요" if self.source.currentData() == "backtest"
                                       else "저장된 실제 신호가 없습니다 — 메인 화면에서 ‘결과 DB 저장’ 또는 자동 실행 후 확인")
            self.trade_model.set_frame(pd.DataFrame())
            self.result_note.setText(f"신호 {n_sig:,}개 · 매매 0건")
            return
        self.k["trades"].set(f"{m['trades']:,}")
        self.k["win_rate"].set(_pct(m.get("win_rate"), signed=False))
        self.k["avg_ret"].set(_pct(m.get("avg_ret")))
        self.k["total"].set(_pct(m["total"]))
        self.k["cagr"].set(_pct(m["cagr"]))
        self.k["mdd"].set(_pct(m["mdd"]))
        self.k["bench_total"].set(_pct(m["bench_total"]))
        for key in ("avg_ret", "total", "cagr"):
            v = m.get(key, 0) or 0
            self.k[key].value_lbl.setStyleSheet(f"color: {T.UP if v > 0 else T.DOWN};")
        self.result_note.setText(
            f"{m['start']:%Y-%m-%d} ~ {m['end']:%Y-%m-%d} · 신호 {n_sig:,}개 중 {m['trades']:,}건 매매 · "
            f"평균 보유 {m.get('avg_days', 0):.1f}일 · 평균 이익 {_pct(m.get('avg_win'))} / 평균 손실 {_pct(m.get('avg_loss'))} · "
            f"평균 투자 비중 {m['exposure'] * 100:.0f}% · 비용 {self.r_cost.value():.2f}% 차감 · "
            "과거 성과가 미래를 보장하지 않습니다.")
        t = trades.copy()
        for c in ("entry_date", "exit_date"):
            t[c] = pd.to_datetime(t[c]).dt.strftime("%Y%m%d")
        self.trade_model.set_frame(t)
        self.trade_table.sortByColumn(0, Qt.DescendingOrder)
        self._plot_equity(equity)

    def _plot_equity(self, eq: pd.DataFrame):
        fig = self.eq_chart.fig
        fig.clear()
        gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08, left=0.07, right=0.98, top=0.9, bottom=0.1)
        ax = fig.add_subplot(gs[0])
        axd = fig.add_subplot(gs[1], sharex=ax)
        for a in (ax, axd):
            self.eq_chart._style(a)
        ax.plot(eq["date"], (eq["equity"] - 1) * 100, color=T.ACCENT, linewidth=1.6, label="전략")
        ax.plot(eq["date"], (eq["bench"] - 1) * 100, color=T.MUTED, linewidth=1, linestyle="--", label="시장 평균")
        ax.axhline(0, color=T.BORDER, linewidth=1)
        ax.set_ylabel("누적 수익률 (%)", color=T.MUTED, fontsize=9)
        leg = ax.legend(facecolor=T.SURFACE, edgecolor=T.BORDER, fontsize=9, loc="upper left")
        for t in leg.get_texts():
            t.set_color(T.TEXT)
        dd = (eq["equity"] / eq["equity"].cummax() - 1) * 100
        axd.fill_between(eq["date"], dd, 0, color=T.DOWN, alpha=0.35)
        axd.set_ylabel("낙폭 (%)", color=T.MUTED, fontsize=9)
        ax.tick_params(labelbottom=False)
        fig.suptitle("누적 수익률 · 최대 낙폭", color=T.TEXT, fontsize=11, x=0.07, ha="left")
        self.eq_chart.draw_idle()

    # ------------------------------------------------------------------ 최적화
    def run_grid(self):
        h, top = self.g_h.currentData(), self.g_top.value()
        p = self.main.params()

        def job(progress, cancelled):
            cands = self._ensure_candidates(progress, cancelled)
            progress(1, 1, "조합별 성과 계산…")
            return bt.grid_search(cands, p, horizon=h, top_per_day=top)

        self._start(job, self._show_grid, "최적화 중…")

    def _show_grid(self, grid: pd.DataFrame):
        self._grid = grid
        p = self.main.params()
        best = bt.best_cell(grid)
        self.g_apply.setEnabled(best is not None)
        vols, chgs = list(bt.GRID_VOL), list(bt.GRID_CHG)
        mat = grid.pivot(index="price_chg_max", columns="vol_ratio_min", values="train_mean").reindex(
            index=chgs, columns=vols).to_numpy() * 100
        fig = self.g_chart.fig
        fig.clear()
        ax = fig.add_subplot(111)
        self.g_chart._style(ax)
        ax.grid(False)
        lim = np.nanmax(np.abs(mat)) if np.isfinite(mat).any() else 1
        im = ax.imshow(mat, cmap="RdBu_r", vmin=-lim, vmax=lim, aspect="auto", origin="lower")
        ax.set_xticks(range(len(vols)), [f"{v:g}배" for v in vols])
        ax.set_yticks(range(len(chgs)), [f"±{c:g}%" for c in chgs])
        ax.set_xlabel("거래량 배수 ≥", color=T.MUTED)
        ax.set_ylabel("허용 등락률", color=T.MUTED)
        for r in grid.itertuples():
            i, j = chgs.index(r.price_chg_max), vols.index(r.vol_ratio_min)
            txt = (f"{r.train_mean * 100:+.2f}%\n검증 {r.test_mean * 100:+.2f}%\n{int(r.train_n)}건"
                   if r.train_n else "신호 없음")
            light = r.train_n and abs(r.train_mean * 100) < lim * 0.55  # 밝은 칸은 어두운 글씨
            ax.text(j, i, txt, ha="center", va="center", fontsize=7.5,
                    color="#1b2033" if light else T.TEXT)
        if best is not None:
            ax.add_patch(mpatches.Rectangle(
                (vols.index(best.vol_ratio_min) - 0.5, chgs.index(best.price_chg_max) - 0.5), 1, 1,
                fill=False, edgecolor=T.GOOD, linewidth=2.5))
        cb = fig.colorbar(im, ax=ax, fraction=0.03)
        cb.ax.tick_params(colors=T.MUTED, labelsize=8)
        fig.subplots_adjust(left=0.08, right=0.95, top=0.9, bottom=0.12)
        h = self.g_h.currentData()
        fig.suptitle(f"학습 기간 신호당 평균 {h}일 수익 (색) · 검증 기간 결과 · 초록 테두리 = 추천",
                     color=T.TEXT, fontsize=11, x=0.08, ha="left")
        self.g_chart.draw_idle()

        cur = grid[(grid.vol_ratio_min == min(vols, key=lambda v: abs(v - p.vol_ratio_min)))
                   & (grid.price_chg_max == min(chgs, key=lambda c: abs(c - p.price_chg_max)))]
        msg = f"검증 시작일 {grid.attrs['split']:%Y-%m-%d}. "
        if best is not None:
            msg += (f"추천: 거래량 ≥{best.vol_ratio_min:g}배 · 등락 ±{best.price_chg_max:g}% → 학습 "
                    f"{best.train_mean * 100:+.2f}% ({int(best.train_n)}건, 승률 {best.train_win * 100:.0f}%), "
                    f"검증 {_pct(best.test_mean)} ({int(best.test_n)}건). ")
        else:
            msg += "학습 기간 신호 30건 이상인 조합이 없어 추천하지 않습니다. "
        if not cur.empty:
            c = cur.iloc[0]
            msg += f"현재 조건과 가장 가까운 칸: 학습 {_pct(c.train_mean)}, 검증 {_pct(c.test_mean)}."
        self.g_note.setText(msg)

    def apply_grid(self):
        best = bt.best_cell(self._grid) if self._grid is not None else None
        if best is None:
            return
        p = self.main.params()
        self.main.set_params(replace(p, vol_ratio_min=float(best.vol_ratio_min),
                                     price_chg_max=float(best.price_chg_max),
                                     daily_chg_max=max(p.daily_chg_max, float(best.price_chg_max))))
        self.refresh_db_info()
        QMessageBox.information(self, "적용", "메인 화면 조건에 적용했습니다. ‘결과’ 탭에서 다시 백테스트해 보세요.")

    # ------------------------------------------------------------------ 요인 분석
    def run_factor(self):
        h = self.f_h.currentData()
        p = self.main.params()

        def job(progress, cancelled):
            cands = self._ensure_candidates(progress, cancelled)
            progress(1, 1, "요인 분석…")
            return bt.factor_analysis(cands, replace(p, score_weights=None), horizon=h)

        self._start(job, self._show_factor, "요인 분석 중…")

    def _show_factor(self, fa: dict):
        if "error" in fa:
            self.f_result.setText(fa["error"])
            self.f_apply.setEnabled(False)
            return
        self._factor = fa
        self.f_model.set_frame(fa["table"])
        t = fa["test"]
        if t:
            better = (t["learned_top"]["mean"] or 0) > (t["base_top"]["mean"] or 0)
            self.f_result.setText(
                f"검증 기간({fa['split']:%Y-%m-%d}~) 날짜별 상위 3개 평균 {fa['horizon']}일 수익 — "
                f"<b>학습 점수 {_pct(t['learned_top']['mean'], True)}</b> (승률 {_pct(t['learned_top']['win'], False)}) vs "
                f"기본 점수 {_pct(t['base_top']['mean'])} (승률 {_pct(t['base_top']['win'], False)}) · "
                f"후보 전체 평균 {_pct(t['all']['mean'])}. "
                + ("<span style='color:%s'>학습 점수가 더 나았습니다.</span>" % T.GOOD if better else
                   "<span style='color:%s'>학습 점수가 낫지 않았습니다 — 적용을 권하지 않습니다.</span>" % T.WARN))
            self.f_apply.setEnabled(True)
        else:
            self.f_result.setText("검증 기간 데이터가 없습니다.")
            self.f_apply.setEnabled(False)

    def apply_factor(self):
        if not self._factor:
            return
        self.main._set_weights(self._factor["weights"])
        self.refresh_db_info()
        QMessageBox.information(self, "적용", "메인 화면 점수를 학습 점수로 바꿨습니다. (⑥ 추가 필터에서 되돌릴 수 있음)")
