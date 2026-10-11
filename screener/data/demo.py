"""오프라인 데모/테스트용 합성 시세. 일부 종목에 '거래량 급증 + 가격 횡보' 패턴을 심어 둔다."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..core import Panel

_NAMES = ["가온", "누리", "다솜", "라온", "마루", "바다", "새봄", "아라", "자람", "하늘",
          "한빛", "윤슬", "온새", "미르", "도담", "별하", "솔찬", "여울", "은결", "푸름"]
_SUFFIX = ["전자", "바이오", "화학", "소재", "테크", "제약", "에너지", "로보틱스", "반도체", "중공업"]


def load_panel(end: dt.date | None = None, days: int = 60, n_tickers: int = 400,
               seed: int = 7, progress=None) -> Panel:
    rng = np.random.default_rng(seed)
    end = end or dt.date.today()
    dates = pd.bdate_range(end=end, periods=days)
    tickers = [f"{100000 + i * 10:06d}" for i in range(n_tickers)]

    rets = rng.normal(0, 0.018, (days, n_tickers))
    base_px = rng.choice([1500, 4000, 9000, 25000, 70000], n_tickers) * rng.uniform(0.7, 1.4, n_tickers)
    close = base_px * np.exp(np.cumsum(rets, axis=0))
    base_vol = rng.lognormal(11, 1.2, n_tickers)
    volume = base_vol * rng.lognormal(0, 0.35, (days, n_tickers))

    # 패턴 심기: 마지막 1~3일 거래량 3~10배, 가격은 거의 그대로
    planted = rng.choice(n_tickers, 25, replace=False)
    for j in planted:
        k = rng.integers(1, 4)
        volume[-k:, j] *= rng.uniform(3, 10)
        close[-k:, j] = close[-k - 1, j] * (1 + rng.normal(0, 0.004, k))
    # 대조군: 거래량 급증 + 급등
    for j in rng.choice(np.setdiff1d(np.arange(n_tickers), planted), 15, replace=False):
        volume[-1, j] *= rng.uniform(4, 12)
        close[-1, j] = close[-2, j] * rng.uniform(1.08, 1.29)

    close = np.round(close)
    prev = np.vstack([close[:1], close[:-1]])
    spread = np.abs(rng.normal(0, 0.012, close.shape)) + np.abs(close / prev - 1) / 2
    high = np.round(np.maximum(close, prev) * (1 + spread / 2))
    low = np.round(np.minimum(close, prev) * (1 - spread / 2))
    open_ = np.round((prev + close) / 2)
    volume = np.round(volume)

    df = lambda a: pd.DataFrame(a, index=dates, columns=tickers)  # noqa: E731
    shares = rng.integers(5_000_000, 200_000_000, n_tickers)
    meta = pd.DataFrame({
        "name": [f"{_NAMES[i % 20]}{_SUFFIX[(i // 20) % 10]}" for i in range(n_tickers)],
        "market": np.where(np.arange(n_tickers) % 3 == 0, "KOSPI", "KOSDAQ"),
        "market_cap": close[-1] * shares,
        "shares": shares,
    }, index=tickers)
    if progress:
        progress(1, 1, "데모 데이터 생성 완료")
    return Panel(open=df(open_), high=df(high), low=df(low), close=df(close),
                 volume=df(volume), value=df(volume * close), meta=meta)
