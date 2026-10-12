"""거래량 급증 + 가격 횡보 스크리닝 로직 (GUI/데이터 소스와 독립된 순수 pandas 코드).

용어
  - 기준 구간(base window): '평소' 거래량을 계산하는 과거 구간
  - 최근 구간(recent window): 거래량이 '터졌는지' 확인하는 최근 N 영업일
  - 둘은 겹치지 않는다: [ ... base ... ][ recent ]  (마지막 날 = 기준일)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields

import numpy as np
import pandas as pd

FIELDS = ("open", "high", "low", "close", "volume", "value")
FLOW_DAYS = 10  # 투자자별 순매수를 받아두는 최근 영업일 수 (수급 기간 슬라이더 최대값)
INVESTORS = ("foreign", "inst")  # 외국인, 기관합계
FLOW_MODES = {
    "both": "외국인 + 기관 모두 순매수",
    "either": "외국인 또는 기관 순매수",
    "foreign": "외국인 순매수",
    "inst": "기관 순매수",
    "sum": "외국인+기관 합산 순매수",
}


@dataclass
class Panel:
    """날짜(index) x 종목코드(columns) 형태의 시세 패널."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    value: pd.DataFrame  # 거래대금(원)
    # index=종목코드, columns=[name, market, market_cap, shares]
    meta: pd.DataFrame = field(default_factory=pd.DataFrame)
    # 투자자별 일별 순매수거래대금(원): {"foreign": 날짜 x 종목, "inst": 날짜 x 종목}
    # 최근 FLOW_DAYS 영업일만 있다. 수집 실패 시 비어 있음.
    flows: dict = field(default_factory=dict)

    @property
    def has_flows(self) -> bool:
        return all(k in self.flows for k in INVESTORS)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index

    def history(self, ticker: str) -> pd.DataFrame:
        return pd.DataFrame({f: getattr(self, f)[ticker] for f in FIELDS})


@dataclass
class ScreenParams:
    # 거래량 조건
    base_days: int = 20            # 평소 거래량 계산 구간 (영업일)
    recent_days: int = 1           # 거래량 급증 확인 구간 (영업일)
    base_stat: str = "median"      # "median" | "mean"
    vol_ratio_min: float = 3.0     # 최근 거래량 / 평소 거래량 최소 배수
    vol_ratio_max: float = 100.0   # 상한 (이상치 제거용)

    # 가격 조건
    price_chg_max: float = 2.0     # 최근 구간 누적 등락률 절대값 상한 (%)
    daily_chg_max: float = 3.0     # 최근 구간 일별 등락률 절대값 상한 (%)
    range_max: float = 6.0         # 최근 구간 일중 변동폭 (고가-저가)/전일종가 상한 (%)
    use_box: bool = False          # 기준 구간 박스권(횡보) 조건 사용 여부
    box_range_max: float = 15.0    # 기준 구간 (최고종가-최저종가)/평균종가 상한 (%)

    # 유동성/규모 필터
    min_value_eok: float = 5.0     # 최근 구간 평균 거래대금 하한 (억원)
    min_price: int = 1000          # 종가 하한 (원)
    mcap_min_eok: float = 0.0      # 시가총액 하한 (억원, 0=제한 없음)
    mcap_max_eok: float = 0.0      # 시가총액 상한 (억원, 0=제한 없음)

    # 시장/제외
    markets: tuple = ("KOSPI", "KOSDAQ")
    exclude_preferred: bool = True
    exclude_spac: bool = True

    # 수급 (외국인·기관 순매수)
    flow_enabled: bool = False
    flow_mode: str = "either"      # FLOW_MODES 의 키
    flow_days: int = 3             # 순매수 집계 기간 (기준일 포함 최근 N 영업일)
    flow_min_eok: float = 0.0      # 기간 순매수금액 하한 (억원). 0 이면 '순매수(>0)'만 요구
    flow_min_buy_days: int = 1     # 기간 중 순매수한 날 수 하한

    # 추가 필터
    pos_enabled: bool = False      # 위치 필터: 최근 pos_days 저점 대비 상승률 상한 (바닥권만)
    pos_days: int = 60
    pos_max_pct: float = 30.0
    obv_enabled: bool = False      # 매집(OBV) 필터: 기준+최근 구간 OBV 추세 하한
    obv_min: float = 0.05          # -1(분배) ~ +1(매집)
    exclude_expiry: bool = False   # 옵션 만기일(매월 둘째 목요일) 신호 제외

    # 점수: None 이면 기본 공식, 백테스트 '요인 분석'에서 학습한 가중치를 넣으면 그것을 사용
    # {특징이름: [가중치, 평균, 표준편차]}
    score_weights: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["markets"] = list(self.markets)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ScreenParams":
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        if "markets" in kw:
            kw["markets"] = tuple(kw["markets"])
        return cls(**kw)

    @property
    def required_days(self) -> int:
        # +1: 최근 구간 직전 종가(누적 등락률 기준점)
        need = self.base_days + self.recent_days + 1
        if self.pos_enabled:
            need = max(need, self.pos_days)
        return need


RESULT_COLUMNS = [
    "ticker", "name", "market", "close", "chg_pct", "max_daily_chg", "max_range",
    "vol_ratio", "recent_vol", "base_vol", "value_eok", "frg_eok", "inst_eok",
    "frg_days", "inst_days", "mcap_eok", "box_pct", "pos_pct", "obv_trend", "score",
]

# 학습 점수에 쓰는 특징 (features() 결과 컬럼으로 계산)
LEARN_FEATURES = {
    "log_vol_ratio": "거래량 배수(log)",
    "abs_chg": "기간 등락률 절대값",
    "max_range": "일중 변동폭",
    "log_value": "거래대금(log)",
    "log_mcap": "시가총액(log)",
    "flow_ratio": "외국인+기관 순매수 / 거래대금",
    "pos_pct": "저점 대비 위치",
    "obv_trend": "OBV 매집 추세",
    "box_pct": "기준구간 박스폭",
}


def is_preferred(ticker: str) -> bool:
    # 보통주 코드는 끝자리 0, 우선주는 5/7/9/K 등
    return not ticker.endswith("0")


def flow_stats(panel: Panel, days: int) -> pd.DataFrame:
    """최근 days 영업일 투자자별 순매수 합계(억)와 순매수일 수. 수급 데이터가 없으면 빈 프레임."""
    if not panel.has_flows:
        return pd.DataFrame(columns=["frg_eok", "inst_eok", "frg_days", "inst_days",
                                     "sum_eok", "sum_days"])
    # 기준일(패널 마지막 날) 이전 데이터만, 최근 days 일
    last = panel.dates[-1]
    f = panel.flows["foreign"].loc[:last].iloc[-days:]
    i = panel.flows["inst"].loc[:last].iloc[-days:]
    f, i = f.align(i, join="outer", fill_value=0)
    both = f + i
    return pd.DataFrame({
        "frg_eok": f.sum() / 1e8, "inst_eok": i.sum() / 1e8,
        "frg_days": (f > 0).sum(), "inst_days": (i > 0).sum(),
        "sum_eok": both.sum() / 1e8, "sum_days": (both > 0).sum(),
    })


def flow_mask(fs: pd.DataFrame, p: ScreenParams) -> pd.Series:
    def ok(prefix: str) -> pd.Series:
        amt, days = fs[f"{prefix}_eok"], fs[f"{prefix}_days"]
        return (amt > 0) & (amt >= p.flow_min_eok) & (days >= p.flow_min_buy_days)

    frg, inst = ok("frg"), ok("inst")
    return {
        "both": frg & inst, "either": frg | inst, "foreign": frg, "inst": inst, "sum": ok("sum"),
    }[p.flow_mode]


def second_thursday(year: int, month: int) -> pd.Timestamp:
    offset = (3 - pd.Timestamp(year=year, month=month, day=1).weekday()) % 7  # 목요일 = 3
    return pd.Timestamp(year=year, month=month, day=1 + offset + 7)


def is_option_expiry(day: pd.Timestamp, trading_dates) -> bool:
    """매월 둘째 목요일(휴장이면 직전 거래일) 여부."""
    day = pd.Timestamp(day).normalize()
    nominal = second_thursday(day.year, day.month)
    dates = pd.DatetimeIndex(trading_dates).normalize()
    if nominal in dates or nominal > dates[-1]:
        return day == nominal
    prior = dates[(dates < nominal) & (dates.month == nominal.month)]
    return len(prior) > 0 and day == prior[-1]


def features(panel: Panel, p: ScreenParams) -> pd.DataFrame:
    """기준일(패널 마지막 날) 기준 전 종목 지표. 필터는 적용하지 않는다."""
    n = len(panel.dates)
    recent = slice(n - p.recent_days, n)
    base_start = max(0, n - p.recent_days - p.base_days)
    base = slice(base_start, n - p.recent_days)
    anchor = n - p.recent_days - 1  # 최근 구간 직전일

    vol = panel.volume
    close = panel.close

    base_vol_df = vol.iloc[base]
    base_vol = base_vol_df.median() if p.base_stat == "median" else base_vol_df.mean()
    recent_vol = vol.iloc[recent].mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = recent_vol / base_vol.replace(0, np.nan)

    last_close = close.iloc[-1]
    anchor_close = close.iloc[anchor].replace(0, np.nan)
    chg_pct = (last_close / anchor_close - 1) * 100

    prev_close = close.shift(1).replace(0, np.nan)
    daily_chg = ((close / prev_close - 1) * 100).iloc[recent]
    day_range = ((panel.high - panel.low) / prev_close * 100).iloc[recent]

    base_close = close.iloc[base].replace(0, np.nan)
    box_pct = (base_close.max() - base_close.min()) / base_close.mean() * 100

    # 위치: 최근 pos_days 종가 최저 대비 현재 상승률(%)
    low_close = close.iloc[-p.pos_days:].replace(0, np.nan).min()
    pos_pct = (last_close / low_close - 1) * 100

    # OBV 추세: (기준+최근 구간 OBV 변화) / 같은 구간 거래량 합 → -1 ~ +1
    win = slice(base_start, n)
    direction = np.sign(close.diff()).iloc[win].fillna(0)
    v = vol.iloc[win]
    with np.errstate(divide="ignore", invalid="ignore"):
        obv_trend = (direction * v).sum() / v.sum().replace(0, np.nan)

    df = pd.DataFrame({
        "close": last_close,
        "chg_pct": chg_pct,
        "max_daily_chg": daily_chg.abs().max(),
        "max_range": day_range.max(),
        "vol_ratio": vol_ratio,
        "recent_vol": recent_vol,
        "base_vol": base_vol,
        "value_eok": panel.value.iloc[recent].mean() / 1e8,
        "box_pct": box_pct,
        "pos_pct": pos_pct,
        "obv_trend": obv_trend,
        # 최근 구간에 거래정지(거래량 0)일이 있으면 제외
        "halted": (vol.iloc[recent] == 0).any(),
    })
    df.index.name = "ticker"

    meta = panel.meta.reindex(df.index)
    df["name"] = meta.get("name", pd.Series(index=df.index, dtype=object)).fillna("")
    df["market"] = meta.get("market", pd.Series(index=df.index, dtype=object)).fillna("")
    mcap = meta.get("market_cap", pd.Series(index=df.index, dtype=float))
    df["mcap_eok"] = mcap / 1e8
    fs = flow_stats(panel, p.flow_days).reindex(df.index)
    for c in fs.columns:
        df[c] = fs[c]
    return df


def filter_mask(df: pd.DataFrame, p: ScreenParams, expiry=False) -> pd.Series:
    """features() 결과에 조건을 적용한 bool 마스크. expiry 는 bool 또는 행별 bool 배열."""
    m = (
        ~df["halted"].astype(bool)
        & df["market"].isin(p.markets)
        & df["vol_ratio"].between(p.vol_ratio_min, p.vol_ratio_max)
        & (df["chg_pct"].abs() <= p.price_chg_max)
        & (df["max_daily_chg"] <= p.daily_chg_max)
        & (df["max_range"] <= p.range_max)
        & (df["value_eok"] >= p.min_value_eok)
        & (df["close"] >= p.min_price)
    )
    if p.use_box:
        m &= df["box_pct"] <= p.box_range_max
    if p.mcap_min_eok > 0:
        m &= df["mcap_eok"] >= p.mcap_min_eok
    if p.mcap_max_eok > 0:
        m &= df["mcap_eok"] <= p.mcap_max_eok
    if p.exclude_preferred:
        m &= ~df.index.to_series().map(is_preferred)
    if p.exclude_spac:
        m &= ~df["name"].str.contains("스팩", na=False)
    if p.flow_enabled:
        # 수급 데이터가 없는 종목/패널은 조건을 만족하지 못한 것으로 본다
        m &= flow_mask(df, p).fillna(False).astype(bool)
    if p.pos_enabled:
        m &= df["pos_pct"] <= p.pos_max_pct
    if p.obv_enabled:
        m &= df["obv_trend"] >= p.obv_min
    if p.exclude_expiry:
        ex = np.asarray(expiry, dtype=bool)
        m &= ~(np.full(len(df), bool(ex)) if ex.ndim == 0 else ex)
    return m.fillna(False).astype(bool)


def learn_matrix(df: pd.DataFrame, p: ScreenParams) -> pd.DataFrame:
    """LEARN_FEATURES 값 (결측은 0 또는 중립값)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        net = sum(pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df else 0.0
                  for c in ("frg_eok", "inst_eok"))
        flow = net / (df["value_eok"] * max(1, p.flow_days)).replace(0, np.nan)
        return pd.DataFrame({
            "log_vol_ratio": np.log(df["vol_ratio"].clip(lower=1e-6)),
            "abs_chg": df["chg_pct"].abs(),
            "max_range": df["max_range"],
            "log_value": np.log(df["value_eok"].clip(lower=1e-6)),
            "log_mcap": np.log(df["mcap_eok"].clip(lower=1e-6)),
            "flow_ratio": flow.fillna(0).clip(-1, 1),
            "pos_pct": df["pos_pct"],
            "obv_trend": df["obv_trend"],
            "box_pct": df["box_pct"],
        }, index=df.index)


def score(df: pd.DataFrame, p: ScreenParams) -> pd.Series:
    if p.score_weights:
        X = learn_matrix(df, p)
        s = pd.Series(0.0, index=df.index)
        for f, (w, mu, sd) in p.score_weights.items():
            if f in X:
                s += w * ((X[f] - mu) / (sd or 1)).fillna(0)
        return s * 100
    # 기본: 거래량 배수가 클수록, 가격 움직임이 작을수록 높은 점수
    return np.log2(df["vol_ratio"]) * 10 / (1 + df["chg_pct"].abs() + 0.5 * df["max_range"])


def run_screen(panel: Panel, p: ScreenParams) -> pd.DataFrame:
    """조건에 맞는 종목을 score 내림차순으로 반환."""
    n = len(panel.dates)
    if n < p.recent_days + 2:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    df = features(panel, p)
    expiry = p.exclude_expiry and is_option_expiry(panel.dates[-1], panel.dates)
    out = df[filter_mask(df, p, expiry)].copy()
    out["score"] = score(out, p)
    out = out.reset_index().sort_values("score", ascending=False)
    return out[RESULT_COLUMNS].reset_index(drop=True)
