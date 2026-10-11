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
        return self.base_days + self.recent_days + 1


RESULT_COLUMNS = [
    "ticker", "name", "market", "close", "chg_pct", "max_daily_chg", "max_range",
    "vol_ratio", "recent_vol", "base_vol", "value_eok", "mcap_eok", "box_pct", "score",
]


def is_preferred(ticker: str) -> bool:
    # 보통주 코드는 끝자리 0, 우선주는 5/7/9/K 등
    return not ticker.endswith("0")


def run_screen(panel: Panel, p: ScreenParams) -> pd.DataFrame:
    """조건에 맞는 종목을 score 내림차순으로 반환."""
    n = len(panel.dates)
    if n < p.recent_days + 2:
        return pd.DataFrame(columns=RESULT_COLUMNS)

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
    max_daily_chg = daily_chg.abs().max()
    max_range = day_range.max()

    base_close = close.iloc[base].replace(0, np.nan)
    box_pct = (base_close.max() - base_close.min()) / base_close.mean() * 100

    value_eok = panel.value.iloc[recent].mean() / 1e8
    # 최근 구간에 거래정지(거래량 0)일이 있으면 제외
    halted = (vol.iloc[recent] == 0).any()

    df = pd.DataFrame({
        "close": last_close,
        "chg_pct": chg_pct,
        "max_daily_chg": max_daily_chg,
        "max_range": max_range,
        "vol_ratio": vol_ratio,
        "recent_vol": recent_vol,
        "base_vol": base_vol,
        "value_eok": value_eok,
        "box_pct": box_pct,
        "halted": halted,
    })
    df.index.name = "ticker"

    meta = panel.meta.reindex(df.index)
    df["name"] = meta.get("name", pd.Series(index=df.index, dtype=object)).fillna("")
    df["market"] = meta.get("market", pd.Series(index=df.index, dtype=object)).fillna("")
    mcap = meta.get("market_cap", pd.Series(index=df.index, dtype=float))
    df["mcap_eok"] = mcap / 1e8

    m = (
        ~df["halted"]
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

    out = df[m.fillna(False)].copy()
    # 거래량 배수가 클수록, 가격 움직임이 작을수록 높은 점수
    out["score"] = np.log2(out["vol_ratio"]) * 10 / (1 + out["chg_pct"].abs() + 0.5 * out["max_range"])
    out = out.reset_index().sort_values("score", ascending=False)
    return out[RESULT_COLUMNS].reset_index(drop=True)
