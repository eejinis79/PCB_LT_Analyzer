"""저장한 스크리닝 신호의 이후 수익률 계산.

신호일(기준일) 이후 DB 에 쌓인 시세로 N 영업일 뒤 수익률, 기간 중 최고/최저,
같은 기간 시장(전종목 동일가중 평균) 수익률 대비 초과수익을 구한다.
데이터는 매일 '데이터 불러오기'를 할 때 DB 에 쌓이므로, 신호 이후 날짜가 쌓일수록 칸이 채워진다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

HORIZONS = (1, 5, 10, 20)
PATH_DAYS = 20          # 평균 누적수익률 곡선 / 최고·최저 계산 구간
ENTRY_MODES = {"close": "신호일 종가", "next_open": "다음날 시가"}


def _wide(prices: pd.DataFrame, field: str) -> pd.DataFrame:
    w = prices.pivot(index="date", columns="ticker", values=field).sort_index()
    return w.replace(0, np.nan)  # 거래정지(0) 는 결측으로


def compute(signals: pd.DataFrame, prices: pd.DataFrame, entry: str = "close",
            horizons=HORIZONS) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    signals: asof, ticker (+ name, market, vol_ratio, chg_pct 등) — db.load_results() 형태
    prices : date, ticker, open, high, low, close — db.load_prices_since() 형태

    반환 (detail, summary, path)
      detail : 신호별 진입가 · r{N} · bench{N} · max_gain · max_loss · last_ret · held
      summary: 기간별 표본수 · 평균 · 중앙값 · 승률 · 시장평균 · 초과수익
      path   : day(1..20) 별 신호 평균 누적수익률과 시장 평균
    """
    cols = [f"r{h}" for h in horizons]
    if signals.empty or prices.empty:
        empty = signals.assign(**{c: np.nan for c in cols})
        return empty, pd.DataFrame(), pd.DataFrame()

    close, open_ = _wide(prices, "close"), _wide(prices, "open")
    high, low = _wide(prices, "high"), _wide(prices, "low")
    dates = list(close.index)
    pos = {d: i for i, d in enumerate(dates)}
    n = len(dates)

    # 시장 평균: 날짜 i 에서 N 일 뒤 전종목 동일가중 평균 수익률 (진입 기준: 종가)
    max_h = max(max(horizons), PATH_DAYS)
    bench = {k: (close.shift(-k) / close - 1).mean(axis=1) for k in range(1, max_h + 1)}

    rows = []
    for sig in signals.itertuples(index=False):
        d, t = str(sig.asof), sig.ticker
        rec = {"asof": d, "ticker": t}
        i = pos.get(d)
        if i is None or t not in close.columns:
            rows.append(rec)
            continue
        if entry == "next_open":
            e_idx = i + 1
            entry_px = open_[t].iloc[e_idx] if e_idx < n else np.nan
        else:
            e_idx = i
            entry_px = close[t].iloc[i]
        rec["entry"] = entry_px
        if pd.isna(entry_px):
            rows.append(rec)
            continue
        for h in horizons:
            j = i + h
            if j < n and j >= e_idx:
                rec[f"r{h}"] = close[t].iloc[j] / entry_px - 1
                rec[f"bench{h}"] = bench[h].iloc[i]
        window = slice(e_idx + (0 if entry == "next_open" else 1), min(i + PATH_DAYS, n - 1) + 1)
        hi, lo = high[t].iloc[window], low[t].iloc[window]
        if len(hi.dropna()):
            rec["max_gain"] = hi.max() / entry_px - 1
            rec["max_loss"] = lo.min() / entry_px - 1
        rec["last_ret"] = close[t].iloc[-1] / entry_px - 1
        rec["held"] = n - 1 - i
        for k in range(1, PATH_DAYS + 1):
            if i + k < n:
                rec[f"p{k}"] = close[t].iloc[i + k] / entry_px - 1
                rec[f"pb{k}"] = bench[k].iloc[i]
        rows.append(rec)

    # 신호 행과 1:1 (같은 날 같은 종목이 여러 번 있어도 행이 불어나지 않게 위치로 붙임)
    perf = pd.DataFrame(rows, index=range(len(rows))).drop(columns=["asof", "ticker"])
    base = signals.reset_index(drop=True).assign(asof=signals["asof"].astype(str).values)
    detail = pd.concat([base.drop(columns=[c for c in perf.columns if c in base.columns]), perf], axis=1)
    for c in cols + [f"bench{h}" for h in horizons] + ["entry", "max_gain", "max_loss", "last_ret", "held"]:
        if c not in detail:
            detail[c] = np.nan

    summary = []
    for h in horizons:
        r = detail[f"r{h}"].dropna()
        b = detail.loc[r.index, f"bench{h}"]
        summary.append({
            "horizon": h, "n": len(r),
            "mean": r.mean() if len(r) else np.nan,
            "median": r.median() if len(r) else np.nan,
            "win_rate": (r > 0).mean() if len(r) else np.nan,
            "bench": b.mean() if len(r) else np.nan,
            "excess": (r - b).mean() if len(r) else np.nan,
        })
    summary = pd.DataFrame(summary)

    path = pd.DataFrame({
        "day": range(1, PATH_DAYS + 1),
        "signal": [detail.get(f"p{k}", pd.Series(dtype=float)).mean() for k in range(1, PATH_DAYS + 1)],
        "bench": [detail.get(f"pb{k}", pd.Series(dtype=float)).mean() for k in range(1, PATH_DAYS + 1)],
        "n": [detail.get(f"p{k}", pd.Series(dtype=float)).notna().sum() for k in range(1, PATH_DAYS + 1)],
    })
    detail = detail.drop(columns=[c for c in detail.columns if c[:1] == "p" and c[1:].isdigit()
                                  or c.startswith("pb") and c[2:].isdigit()])
    return detail, summary, path


def from_db(db, run_ids=None, entry: str = "close", dedupe: bool = True):
    """DB 에서 신호와 시세를 읽어 compute(). run_ids=None 이면 전체."""
    sig = db.load_results()
    if run_ids is not None:
        sig = sig[sig["run_id"].isin(run_ids)]
    if dedupe and not sig.empty:  # 같은 날 같은 종목이 여러 조건에서 잡혀도 한 번만
        sig = sig.sort_values(["asof", "run_id", "rank"]).drop_duplicates(["asof", "ticker"])
    if sig.empty:
        return compute(sig, pd.DataFrame(), entry)
    prices = db.load_prices_since(str(sig["asof"].min()))
    return compute(sig, prices, entry)
