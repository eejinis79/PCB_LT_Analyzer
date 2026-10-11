from __future__ import annotations

import datetime as dt
import json

import pandas as pd
from PySide6.QtCore import QDate, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSplitter, QTableView, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..core import FLOW_DAYS, FLOW_MODES, Panel, ScreenParams, run_screen
from ..db import StockDB, default_db_path
from ..vault import Credentials, Vault
from . import account
from . import theme as T
from .chart import ChartWidget
from .db_dialog import DbDialog
from .models import COLUMNS, ResultsModel, ResultsProxy
from .widgets import Card, ParamSlider, StatChip, hbox
from .workers import DartWorker, LoadWorker

PRESETS: dict[str, ScreenParams] = {
    "기본 · 1일 거래량 폭발 + 보합": ScreenParams(),
    "매집 의심 · 3일 누적 급증 + 횡보": ScreenParams(
        recent_days=3, vol_ratio_min=2.5, price_chg_max=3.0, daily_chg_max=3.0, range_max=5.0),
    "강한 급증 · 5배 이상 + ±1%": ScreenParams(
        vol_ratio_min=5.0, price_chg_max=1.0, daily_chg_max=1.5, range_max=4.0),
    "매집 확인 · 급증 + 보합 + 외국인/기관 순매수": ScreenParams(
        recent_days=1, vol_ratio_min=3.0, price_chg_max=2.0,
        flow_enabled=True, flow_mode="either", flow_days=3, flow_min_eok=1.0, flow_min_buy_days=1),
    "박스권 내 거래량 급증": ScreenParams(
        base_days=40, vol_ratio_min=3.0, price_chg_max=2.5, use_box=True, box_range_max=12.0),
}



class MainWindow(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.setWindowTitle("Volume Spike Screener — 거래량 급증 · 가격 횡보 스크리너")
        self.resize(1480, 900)
        self.settings = QSettings("PCB_LT_Analyzer", "VolumeSpikeScreener")
        self.vault = Vault()
        self.creds: Credentials | None = None   # 잠금 해제 전에는 None
        self.session_pw: str | None = None
        self.panel: Panel | None = None
        self.result = pd.DataFrame()
        self._loader: LoadWorker | None = None
        self._dart_workers: list[DartWorker] = []
        self._dart_client = None
        self._selected: str | None = None
        self._panel_source = ""
        self.db_path = self.settings.value("db_path") or str(default_db_path())

        self._debounce = QTimer(self, singleShot=True, interval=150)
        self._debounce.timeout.connect(self.rescreen)

        root = QWidget(objectName="Root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_header(demo))

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(10)
        split.addWidget(self._build_sidebar())
        split.addWidget(self._build_main())
        split.setStretchFactor(1, 1)
        split.setSizes([360, 1100])
        wrap = QHBoxLayout()
        wrap.setContentsMargins(12, 12, 12, 12)
        wrap.addWidget(split)
        v.addLayout(wrap, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("기준일과 데이터 소스를 고르고 ‘데이터 불러오기’를 누르세요.")

        self._restore_params()
        export = QAction("CSV 내보내기", self, shortcut=QKeySequence("Ctrl+S"), triggered=self.export_csv)
        self.addAction(export)

    def shutdown(self):
        """백그라운드 스레드가 끝날 때까지 기다린다 (종료 시 크래시 방지)."""
        for w in [self._loader, *self._dart_workers]:
            if w is not None and w.isRunning():
                w.wait(5000)
        self.chart.fig.clear()

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)

    # ---------------------------------------------------------------- 레이아웃
    def _build_header(self, demo: bool) -> QFrame:
        h = QFrame(objectName="Header")
        lay = QHBoxLayout(h)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(QLabel("Volume Spike Screener", objectName="Title"))
        titles.addWidget(QLabel("평소보다 거래량이 터졌는데 가격은 그대로인 종목 · KOSPI / KOSDAQ",
                                objectName="Subtitle"))
        lay.addLayout(titles)
        lay.addStretch(1)

        self.source = QComboBox()
        self.source.addItem("KRX 실데이터", "krx")
        self.source.addItem("데모 데이터(오프라인)", "demo")
        self.source.setCurrentIndex(1 if demo else 0)
        self.date_edit = QDateEdit(QDate.currentDate(), calendarPopup=True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setToolTip("기준일 (휴장일이면 직전 영업일까지 사용)")

        self.load_btn = QPushButton("데이터 불러오기", objectName="Primary")
        self.load_btn.clicked.connect(self.load_data)
        acct = QPushButton("계정", objectName="Ghost")
        acct.clicked.connect(self.edit_account)
        export = QPushButton("CSV 내보내기", objectName="Ghost")
        export.clicked.connect(self.export_csv)
        save = QPushButton("결과 DB 저장", objectName="Ghost")
        save.setToolTip("현재 스크리닝 결과와 조건을 DB 에 기록 (나중에 신호 이후 수익률 추적용)")
        save.clicked.connect(self.save_results)
        db_btn = QPushButton("DB", objectName="Ghost")
        db_btn.setToolTip("데이터 저장 위치 · 저장된 기간 보기 / 위치 변경")
        db_btn.clicked.connect(self.open_db_dialog)
        for w in (QLabel("소스"), self.source, QLabel("기준일"), self.date_edit,
                  self.load_btn, acct, db_btn, save, export):
            lay.addWidget(w)
        return h

    def _build_sidebar(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(330)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(12)

        # 프리셋
        pc = Card("프리셋", "자주 쓰는 조건 조합을 불러오거나 현재 설정을 저장합니다.")
        self.preset_combo = QComboBox()
        self._reload_presets()
        self.preset_combo.activated.connect(self._apply_preset)
        save = QPushButton("현재 설정 저장")
        save.clicked.connect(self._save_preset)
        pc.body.addWidget(self.preset_combo)
        pc.body.addWidget(save)
        lay.addWidget(pc)

        P = ParamSlider
        vc = Card("① 거래량 조건", "‘평소’(기준 구간) 대비 ‘최근’ 구간 거래량이 몇 배인지 봅니다.")
        self.p_base_days = P("기준 구간 (평소)", 5, 120, 1, 20, "일", 0,
                             "평소 거래량을 계산할 과거 영업일 수 (최근 구간 제외)")
        self.p_recent_days = P("최근 구간 (급증)", 1, 10, 1, 1, "일", 0,
                               "거래량 급증을 확인할 최근 영업일 수. 1 = 기준일 하루")
        self.p_vol_min = P("거래량 배수 ≥", 1, 30, 0.1, 3, "배", 1)
        self.p_vol_max = P("거래량 배수 ≤", 2, 200, 1, 100, "배", 0, "이상치(신규상장·거래재개) 제거용 상한")
        self.base_stat = QComboBox()
        self.base_stat.addItem("중앙값 (이상치에 강함)", "median")
        self.base_stat.addItem("평균", "mean")
        for w in (self.p_base_days, self.p_recent_days, self.p_vol_min, self.p_vol_max):
            vc.body.addWidget(w)
        vc.body.addLayout(hbox(QLabel("평소 거래량 계산", objectName="ParamLabel"), None, self.base_stat))
        lay.addWidget(vc)

        prc = Card("② 가격 조건", "거래량이 터진 동안 가격이 얼마나 ‘안 움직였는지’를 제한합니다.")
        self.p_chg = P("기간 등락률 |±|", 0, 15, 0.1, 2, "%", 1, "최근 구간 직전 종가 대비 기준일 종가")
        self.p_daily = P("일별 등락률 |±| 최대", 0, 30, 0.1, 3, "%", 1)
        self.p_range = P("일중 변동폭 최대", 0, 30, 0.1, 6, "%", 1, "(고가-저가)/전일종가 — 장중 흔들림 제한")
        self.use_box = QCheckBox("기준 구간 박스권(횡보) 조건 사용")
        self.p_box = P("박스 폭 최대", 2, 60, 0.5, 15, "%", 1, "기준 구간 (최고종가-최저종가)/평균종가")
        for w in (self.p_chg, self.p_daily, self.p_range, self.use_box, self.p_box):
            prc.body.addWidget(w)
        lay.addWidget(prc)

        lq = Card("③ 유동성 · 규모", "잡주/초소형주의 착시를 거릅니다. 0 = 제한 없음")
        self.p_value = P("거래대금 ≥", 0, 500, 0.5, 5, "억", 1, "최근 구간 평균 거래대금")
        self.p_price = P("주가 ≥", 0, 50000, 100, 1000, "원", 0)
        self.p_mcap_min = P("시가총액 ≥", 0, 50000, 50, 0, "억", 0)
        self.p_mcap_max = P("시가총액 ≤", 0, 500000, 500, 0, "억", 0)
        for w in (self.p_value, self.p_price, self.p_mcap_min, self.p_mcap_max):
            lq.body.addWidget(w)
        lay.addWidget(lq)

        mk = Card("④ 시장 · 제외")
        self.kospi = QCheckBox("KOSPI", checked=True)
        self.kosdaq = QCheckBox("KOSDAQ", checked=True)
        self.ex_pref = QCheckBox("우선주 제외", checked=True)
        self.ex_spac = QCheckBox("스팩 제외", checked=True)
        mk.body.addLayout(hbox(self.kospi, self.kosdaq, stretch_last=True))
        mk.body.addLayout(hbox(self.ex_pref, self.ex_spac, stretch_last=True))
        lay.addWidget(mk)

        fc = Card("⑤ 수급 · 외국인/기관 순매수",
                  f"기준일 포함 최근 N일(최대 {FLOW_DAYS}일)의 순매수 금액 합계와 순매수한 날 수로 거릅니다.")
        self.flow_on = QCheckBox("수급 조건 사용")
        self.flow_mode = QComboBox()
        for key, label in FLOW_MODES.items():
            self.flow_mode.addItem(label, key)
        self.p_flow_days = P("집계 기간", 1, FLOW_DAYS, 1, 3, "일", 0)
        self.p_flow_eok = P("순매수 금액 ≥", 0, 1000, 0.5, 0, "억", 1,
                            "기간 순매수 합계 하한. 0 이면 순매수(+)이기만 하면 통과")
        self.p_flow_buy_days = P("순매수한 날 ≥", 0, FLOW_DAYS, 1, 1, "일", 0,
                                 "집계 기간 중 순매수(+)였던 날의 수")
        self.flow_warn = QLabel("", objectName="CardHint")
        self.flow_warn.setStyleSheet(f"color: {T.WARN};")
        self.flow_warn.setWordWrap(True)
        self.flow_warn.hide()
        fc.body.addWidget(self.flow_on)
        fc.body.addWidget(self.flow_mode)
        for w in (self.p_flow_days, self.p_flow_eok, self.p_flow_buy_days, self.flow_warn):
            fc.body.addWidget(w)
        lay.addWidget(fc)

        reset = QPushButton("기본값으로 초기화", objectName="Ghost")
        reset.clicked.connect(lambda: self.set_params(ScreenParams()))
        lay.addWidget(reset)
        lay.addStretch(1)

        for s in self._sliders():
            s.valueChanged.connect(self._schedule)
        for c in (self.use_box, self.kospi, self.kosdaq, self.ex_pref, self.ex_spac, self.flow_on):
            c.toggled.connect(self._schedule)
        self.base_stat.currentIndexChanged.connect(self._schedule)
        self.flow_mode.currentIndexChanged.connect(self._schedule)
        for w in (self.flow_mode, self.p_flow_days, self.p_flow_eok, self.p_flow_buy_days):
            self.flow_on.toggled.connect(w.setEnabled)
            w.setEnabled(False)
        self.use_box.toggled.connect(self.p_box.setEnabled)
        self.p_box.setEnabled(False)
        scroll.setWidget(inner)
        return scroll

    def _build_main(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 0, 0, 0)
        lay.setSpacing(10)

        self.chip_total = StatChip("검색 종목", color=T.TEXT)
        self.chip_hits = StatChip("조건 충족", color=T.GOOD)
        self.chip_kospi = StatChip("KOSPI", color=T.ACCENT)
        self.chip_kosdaq = StatChip("KOSDAQ", color=T.ACCENT_2)
        self.chip_asof = StatChip("기준일 · 데이터 기간", color=T.TEXT)
        chips = QHBoxLayout()
        chips.setSpacing(10)
        for c in (self.chip_total, self.chip_hits, self.chip_kospi, self.chip_kosdaq, self.chip_asof):
            chips.addWidget(c)
        chips.addStretch(1)
        self.search = QLineEdit(placeholderText="종목명 / 코드 검색")
        self.search.setFixedWidth(220)
        chips.addWidget(self.search)
        lay.addLayout(chips)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.setHandleWidth(10)

        self.model = ResultsModel(self)
        self.proxy = ResultsProxy(self)
        self.proxy.setSourceModel(self.model)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        hh.setHighlightSections(False)
        for i, (c, *_rest) in enumerate(COLUMNS):
            self.table.setColumnWidth(i, {"ticker": 70, "name": 130, "market": 70}.get(c, 92))
        self.table.selectionModel().currentRowChanged.connect(self._on_select)
        self.table.doubleClicked.connect(self._open_naver)
        self.table.setToolTip("더블클릭: 네이버 금융 종목 페이지 열기")
        vsplit.addWidget(self.table)

        bottom = QSplitter(Qt.Horizontal)
        bottom.setHandleWidth(10)
        self.chart = ChartWidget()
        bottom.addWidget(self.chart)
        self.disc = QTreeWidget()
        self.disc.setHeaderLabels(["일자", "공시 (DART)"])
        self.disc.setRootIsDecorated(False)
        self.disc.setColumnWidth(0, 82)
        self.disc.itemDoubleClicked.connect(
            lambda it, _c: it.data(0, Qt.UserRole) and QDesktopServices.openUrl(QUrl(it.data(0, Qt.UserRole))))
        self.disc.setToolTip("빨강=희석/위험 공시, 초록=호재성 공시. 더블클릭하면 DART 원문을 엽니다.")
        bottom.addWidget(self.disc)
        bottom.setSizes([700, 380])
        vsplit.addWidget(bottom)
        vsplit.setSizes([480, 340])
        lay.addWidget(vsplit, 1)
        return w

    # ---------------------------------------------------------------- 파라미터
    def _sliders(self) -> list[ParamSlider]:
        return [self.p_base_days, self.p_recent_days, self.p_vol_min, self.p_vol_max, self.p_chg,
                self.p_daily, self.p_range, self.p_box, self.p_value, self.p_price,
                self.p_mcap_min, self.p_mcap_max, self.p_flow_days, self.p_flow_eok,
                self.p_flow_buy_days]

    def params(self) -> ScreenParams:
        markets = tuple(m for m, cb in (("KOSPI", self.kospi), ("KOSDAQ", self.kosdaq)) if cb.isChecked())
        return ScreenParams(
            base_days=int(self.p_base_days.value()), recent_days=int(self.p_recent_days.value()),
            base_stat=self.base_stat.currentData(),
            vol_ratio_min=self.p_vol_min.value(), vol_ratio_max=self.p_vol_max.value(),
            price_chg_max=self.p_chg.value(), daily_chg_max=self.p_daily.value(),
            range_max=self.p_range.value(), use_box=self.use_box.isChecked(),
            box_range_max=self.p_box.value(), min_value_eok=self.p_value.value(),
            min_price=int(self.p_price.value()), mcap_min_eok=self.p_mcap_min.value(),
            mcap_max_eok=self.p_mcap_max.value(), markets=markets,
            exclude_preferred=self.ex_pref.isChecked(), exclude_spac=self.ex_spac.isChecked(),
            flow_enabled=self.flow_on.isChecked(), flow_mode=self.flow_mode.currentData(),
            flow_days=int(self.p_flow_days.value()), flow_min_eok=self.p_flow_eok.value(),
            flow_min_buy_days=int(self.p_flow_buy_days.value()),
        )

    def set_params(self, p: ScreenParams):
        self._debounce.blockSignals(True)
        pairs = [(self.p_base_days, p.base_days), (self.p_recent_days, p.recent_days),
                 (self.p_vol_min, p.vol_ratio_min), (self.p_vol_max, p.vol_ratio_max),
                 (self.p_chg, p.price_chg_max), (self.p_daily, p.daily_chg_max),
                 (self.p_range, p.range_max), (self.p_box, p.box_range_max),
                 (self.p_value, p.min_value_eok), (self.p_price, p.min_price),
                 (self.p_mcap_min, p.mcap_min_eok), (self.p_mcap_max, p.mcap_max_eok),
                 (self.p_flow_days, p.flow_days), (self.p_flow_eok, p.flow_min_eok),
                 (self.p_flow_buy_days, p.flow_min_buy_days)]
        for w, val in pairs:
            w.setValue(val)
        self.base_stat.setCurrentIndex(max(0, self.base_stat.findData(p.base_stat)))
        self.use_box.setChecked(p.use_box)
        self.kospi.setChecked("KOSPI" in p.markets)
        self.kosdaq.setChecked("KOSDAQ" in p.markets)
        self.ex_pref.setChecked(p.exclude_preferred)
        self.ex_spac.setChecked(p.exclude_spac)
        self.flow_on.setChecked(p.flow_enabled)
        self.flow_mode.setCurrentIndex(max(0, self.flow_mode.findData(p.flow_mode)))
        self._debounce.blockSignals(False)
        self._schedule()

    def _restore_params(self):
        raw = self.settings.value("params")
        if raw:
            try:
                self.set_params(ScreenParams.from_dict(json.loads(raw)))
            except (ValueError, TypeError):
                pass

    def _custom_presets(self) -> dict:
        try:
            return json.loads(self.settings.value("presets", "{}"))
        except ValueError:
            return {}

    def _reload_presets(self):
        self.preset_combo.clear()
        self.preset_combo.addItem("— 프리셋 선택 —", None)
        for name in PRESETS:
            self.preset_combo.addItem(name, ("builtin", name))
        for name in self._custom_presets():
            self.preset_combo.addItem(f"★ {name}", ("custom", name))

    def _apply_preset(self, idx: int):
        data = self.preset_combo.itemData(idx)
        if not data:
            return
        kind, name = data
        p = PRESETS[name] if kind == "builtin" else ScreenParams.from_dict(self._custom_presets()[name])
        self.set_params(p)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "프리셋 저장", "프리셋 이름")
        if ok and name.strip():
            presets = self._custom_presets()
            presets[name.strip()] = self.params().to_dict()
            self.settings.setValue("presets", json.dumps(presets, ensure_ascii=False))
            self._reload_presets()

    # ---------------------------------------------------------------- 데이터
    def ensure_unlocked(self) -> bool:
        """세션 중 한 번 잠금 해제하면 계정 정보를 메모리에 둔다."""
        if self.creds is not None:
            return True
        res = account.unlock(self.vault, self)
        if res is None:
            return False
        self.session_pw, self.creds = res
        self._dart_client = None
        return True

    def edit_account(self, already_unlocked: bool = False):
        """계정 화면 진입: 매번 계정 비밀번호를 확인한다."""
        if not already_unlocked:
            res = account.unlock(self.vault, self)
            if res is None:
                return
            self.session_pw, self.creds = res
        dlg = account.AccountDialog(self.vault, self.session_pw, self.creds, self)
        accepted = dlg.exec() == QDialog.Accepted
        if accepted or dlg.password != self.session_pw:
            self.creds = dlg.creds()  # 비밀번호 변경 시에도 화면 내용이 함께 저장됨
        self.session_pw = dlg.password
        self._dart_client = None
        if accepted:
            self.statusBar().showMessage("계정 정보를 암호화하여 저장했습니다.", 5000)
            if self._selected:
                self._load_disclosures(self._selected)

    def load_data(self):
        source = self.source.currentData()
        if source == "krx":
            if not self.ensure_unlocked():
                return
            if not (self.creds.krx_id and self.creds.krx_pw):
                QMessageBox.information(self, "계정", "KRX ID/비밀번호를 먼저 입력하세요.")
                self.edit_account(already_unlocked=True)
                if not (self.creds.krx_id and self.creds.krx_pw):
                    return
        p = self.params()
        # 슬라이더를 늘려도 다시 받지 않도록 여유 있게 가져온다
        days = max(60, p.required_days + 5)
        end = self.date_edit.date().toPython()
        creds = self.creds or Credentials()
        self._loader = LoadWorker(source, end, days, creds.krx_id, creds.krx_pw, db_path=self.db_path)
        self._loading_source = source
        self._loader.progress.connect(self._on_progress)
        self._loader.done.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self.load_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._loader.start()

    def _on_progress(self, i: int, n: int, msg: str):
        self.progress.setMaximum(max(n, 1))
        self.progress.setValue(i)
        self.statusBar().showMessage(msg)

    def _on_loaded(self, panel: Panel):
        self.panel = panel
        self._panel_source = self._loading_source
        self.load_btn.setEnabled(True)
        self.progress.setVisible(False)
        d = panel.dates
        self.chip_total.set(f"{panel.close.shape[1]:,}")
        self.chip_asof.set(f"{d[-1]:%m/%d} · {len(d)}일")
        self.statusBar().showMessage(
            f"{d[0]:%Y-%m-%d} ~ {d[-1]:%Y-%m-%d}, {panel.close.shape[1]:,}종목 로드 완료. "
            "왼쪽 조건을 조정하면 즉시 다시 스크리닝됩니다.")
        self.rescreen()

    def _on_failed(self, msg: str):
        self.load_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.statusBar().showMessage("데이터 로딩 실패")
        QMessageBox.critical(self, "데이터 로딩 실패", msg)

    # ---------------------------------------------------------------- 스크리닝
    def _schedule(self, *_):
        self._debounce.start()

    def rescreen(self):
        p = self.params()
        self.settings.setValue("params", json.dumps(p.to_dict(), ensure_ascii=False))
        if self.panel is None:
            return
        if len(self.panel.dates) < p.required_days:
            self.statusBar().showMessage(
                f"⚠ 데이터 기간({len(self.panel.dates)}일)이 조건에 필요한 {p.required_days}일보다 짧습니다. "
                "‘데이터 불러오기’를 다시 눌러주세요.")
        no_flows = not self.panel.has_flows
        self.flow_warn.setText("⚠ 투자자별 순매수 데이터가 없어 수급 조건을 만족하는 종목이 없습니다. "
                               "데이터를 다시 불러오세요." if no_flows else "")
        self.flow_warn.setVisible(no_flows and p.flow_enabled)
        self.result = run_screen(self.panel, p)
        self.model.set_frame(self.result)
        self.table.sortByColumn(len(COLUMNS) - 1, Qt.DescendingOrder)  # 점수 내림차순
        mk = self.result["market"].value_counts()
        self.chip_hits.set(f"{len(self.result):,}")
        self.chip_kospi.set(f"{mk.get('KOSPI', 0):,}")
        self.chip_kosdaq.set(f"{mk.get('KOSDAQ', 0):,}")
        if len(self.result):
            row = self._row_of(self._selected) if self._selected else 0
            self.table.selectRow(row if row is not None else 0)
        else:
            self.chart.show_message("조건을 만족하는 종목이 없습니다 — 조건을 완화해 보세요")
            self.disc.clear()

    def _row_of(self, ticker: str) -> int | None:
        for r in range(self.proxy.rowCount()):
            if self.proxy.index(r, 0).data() == ticker:
                return r
        return None

    def _on_select(self, cur, _prev):
        if not cur.isValid():
            return
        src = self.proxy.mapToSource(cur)
        row = self.model.row(src.row())
        self._selected = row["ticker"]
        p = self.params()
        hist = self.panel.history(row["ticker"]).iloc[-(p.required_days + 10):]
        self.chart.plot(hist, f"{row['name']} ({row['ticker']}) · {row['market']}  "
                              f"거래량 {row['vol_ratio']:.1f}배 · {row['chg_pct']:+.2f}%"
                              + (f" · 외국인 {row['frg_eok']:+,.1f}억 · 기관 {row['inst_eok']:+,.1f}억"
                                 if pd.notna(row["frg_eok"]) else ""),
                        p.recent_days, p.base_days, row["base_vol"])
        self._load_disclosures(row["ticker"])

    def _load_disclosures(self, ticker: str):
        self.disc.clear()
        if self.creds is None:
            QTreeWidgetItem(self.disc, ["", "‘계정’에서 잠금 해제하면 DART 공시 표시"])
            return
        if not self.creds.dart_key:
            QTreeWidgetItem(self.disc, ["", "‘계정’에 DART API 인증키 입력 시 공시 표시"])
            return
        if self._dart_client is None:
            from ..data.dart import DartClient
            self._dart_client = DartClient(self.creds.dart_key)
        QTreeWidgetItem(self.disc, ["", "공시 불러오는 중…"])
        w = DartWorker(self._dart_client, ticker)
        w.done.connect(self._on_disc)
        w.failed.connect(lambda t, m: t == self._selected and self._disc_message(f"DART 오류: {m}"))
        w.finished.connect(lambda: self._dart_workers.remove(w))
        self._dart_workers.append(w)
        w.start()

    def _disc_message(self, msg: str):
        self.disc.clear()
        QTreeWidgetItem(self.disc, ["", msg])

    def _on_disc(self, ticker: str, df: pd.DataFrame):
        if ticker != self._selected:
            return  # 그 사이 다른 종목을 선택함
        self.disc.clear()
        if df.empty:
            QTreeWidgetItem(self.disc, ["", "최근 90일 공시 없음"])
            return
        colors = {"risk": QColor(T.UP), "positive": QColor(T.GOOD)}
        for r in df.itertuples():
            it = QTreeWidgetItem(self.disc, [f"{r.date[2:4]}.{r.date[4:6]}.{r.date[6:]}", r.title])
            it.setData(0, Qt.UserRole, r.url)
            it.setToolTip(1, f"{r.title}\n제출인: {r.submitter}")
            if r.flag in colors:
                it.setForeground(1, colors[r.flag])

    def _open_naver(self, idx):
        ticker = self.proxy.index(idx.row(), 0).data()
        QDesktopServices.openUrl(QUrl(f"https://finance.naver.com/item/main.naver?code={ticker}"))

    def open_db_dialog(self):
        dlg = DbDialog(self.db_path, self)
        dlg.exec()
        if dlg.path != self.db_path:
            self.db_path = dlg.path
            self.settings.setValue("db_path", self.db_path)
            self.statusBar().showMessage(f"DB 위치 변경: {self.db_path} — 다음 '데이터 불러오기'부터 사용", 8000)

    def save_results(self):
        if self.panel is None or self.result.empty:
            QMessageBox.information(self, "결과 저장", "저장할 스크리닝 결과가 없습니다.")
            return
        if self._panel_source == "demo":
            QMessageBox.information(self, "결과 저장", "데모 데이터 결과는 DB 에 저장하지 않습니다.")
            return
        asof = self.panel.dates[-1].strftime("%Y%m%d")
        try:
            StockDB(self.db_path).save_results(asof, self.result, self.params().to_dict(),
                                               source=self._panel_source)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "결과 저장", f"저장 실패: {e}")
            return
        self.statusBar().showMessage(
            f"{asof} 기준 {len(self.result)}종목을 DB 에 저장했습니다 ({self.db_path})", 8000)

    def export_csv(self):
        if self.result.empty:
            QMessageBox.information(self, "내보내기", "내보낼 결과가 없습니다.")
            return
        default = f"screen_{dt.date.today():%Y%m%d}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "CSV 저장", default, "CSV (*.csv)")
        if path:
            out = self.result.rename(columns={c: h for c, h, *_ in COLUMNS})
            out.to_csv(path, index=False, encoding="utf-8-sig")  # 엑셀 한글 호환
            self.statusBar().showMessage(f"저장됨: {path}", 5000)
