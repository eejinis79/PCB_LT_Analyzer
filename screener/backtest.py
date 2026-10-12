"""과거 데이터 백테스트 · 조건 최적화(워크포워드) · 요인 분석 · 모의투자.

흐름
  1. load_history(db)               DB 에 쌓인 과거 시세로 전체 기간 Panel 구성
  2. build_candidates(panel, p)     날짜마다 '느슨한' 조건으로 후보를 뽑고 지표 + 이후 수익률 기록
                                    (실제 조건은 select() 로 다시 거른다 → 최적화가 빠름)
  3. select(cands, p)               현재 조건을 적용한 신호
  4. simulate(signals, panel, rules) 포트폴리오 매매 시뮬레이션 (손절·익절·보유기간·비용·동시보유)
  5. grid_search / factor_analysis  조건 조합 비교(학습/검증 기간 분리), 지표별 수익 관련성 · 학습 점수

미래 데이터 사용 금지: 신호는 t 일 종가까지의 정보로 만들고, 진입은 t+1 일 시가.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd

from .data.krx import INDEX_CODES
from .core import (
    FIELDS, FLOW_DAYS, INVESTORS, LEARN_FEATURES, Panel, ScreenParams, features, filter_mask,
    is_option_expiry, learn_matrix, score,
)

FWD_HORIZONS = (5, 10, 20)  # 후보별 이후 수익률을 기록하는 보유 기간
GRID_VOL = (2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0)
GRID_CHG = (1.0, 2.0, 3.0, 4.0, 5.0)


@dataclass
class TradeRules:
    stop_pct: float = 5.0        # 손절 (진입가 대비 %, 0 = 사용 안 함)
    target_pct: float = 15.0     # 익절 (%, 0 = 사용 안 함)
    max_hold: int = 20           # 최대 보유 영업일
    cost_pct: float = 0.25       # 왕복 비용 % (수수료 + 매도세 + 슬리피지)
    max_positions: int = 5       # 동시 보유 종목 수 (자금을 균등 배분)
    per_day: int = 2             # 하루 신규 진입 최대 수 (점수 상위부터)
    regime_filter: bool = True   # 해당 시장 지수가 이동평균 아래면 신규 진입 안 함
    regime_ma: int = 60

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------- 데이터
def load_history(db, start: str | None = None, end: str | None = None,
                 markets=("KOSPI", "KOSDAQ")) -> Panel:
    """DB 의 기간 시세로 Panel 구성. 시가총액은 최신 상장주식수 × 당일 종가로 근사한다."""
    dates = db.trade_dates(start, end)
    if not dates:
        raise ValueError("DB 에 시세가 없습니다. 먼저 '과거 데이터 수집'을 실행하세요.")
    long = db.load_ohlcv_range(dates[0], dates[-1], markets)
    long["date"] = pd.to_datetime(long["date"])
    wide = {f: long.pivot(index="date", columns="ticker", values=f).sort_index() for f in FIELDS}
    tickers = wide["close"].columns
    meta = pd.DataFrame(index=tickers)
    meta["market"] = long.drop_duplicates("ticker", keep="last").set_index("ticker")["market"]
    names = db.load_names()
    meta["name"] = [names.get(t, "") for t in tickers]
    meta["shares"] = db.latest_shares().reindex(tickers)
    meta["market_cap"] = meta["shares"] * wide["close"].ffill().iloc[-1]
    flows = {}
    for inv in INVESTORS:
        f = db.load_flows_range(dates[0], dates[-1], inv)
        if not f.empty:
            flows[inv] = f.reindex(columns=tickers).fillna(0)
    panel = Panel(**wide, meta=meta, flows=flows if len(flows) == len(INVESTORS) else {})
    panel.index_close = {m: db.load_index(c) for m, c in INDEX_CODES.items()}
    return panel


def regime_table(panel: Panel, ma: int = 60) -> pd.DataFrame:
    """날짜 x 시장 → 지수가 ma 일 이동평균 위인지. 지수가 없으면 전종목 동일가중 지수로 대체."""
    out = {}
    idx = getattr(panel, "index_close", {}) or {}
    rets = panel.close.pct_change(fill_method=None).clip(-0.3, 0.3)
    for market in ("KOSPI", "KOSDAQ"):
        s = idx.get(market)
        if s is None or s.empty:
            cols = panel.meta.index[panel.meta["market"] == market]
            s = (1 + rets[cols].mean(axis=1).fillna(0)).cumprod()
        s = s.reindex(panel.dates).ffill()
        out[market] = (s >= s.rolling(ma, min_periods=max(5, ma // 2)).mean()).fillna(True)
    return pd.DataFrame(out)


def current_regime(panel: Panel, ma: int = 60) -> dict:
    """기준일(패널 마지막 날) 시장별 추세 양호 여부. {'KOSPI': True, 'KOSDAQ': False}"""
    return {m: bool(v) for m, v in regime_table(panel, ma).iloc[-1].items()}


def loose_params(p: ScreenParams) -> ScreenParams:
    """후보 풀을 만들 때 쓰는 느슨한 조건 (최적화 격자 범위 + 추가 필터 해제)."""
    return replace(
        p,
        vol_ratio_min=min(p.vol_ratio_min, GRID_VOL[0]),
        price_chg_max=max(p.price_chg_max, GRID_CHG[-1]),
        daily_chg_max=max(p.daily_chg_max, GRID_CHG[-1]),
        range_max=max(p.range_max, 15.0),
        flow_enabled=False, pos_enabled=False, obv_enabled=False, exclude_expiry=False,
    )


def build_candidates(panel: Panel, p: ScreenParams, start: str | None = None,
                     progress=None, cancelled=lambda: False, cost_pct: float = 0.25) -> pd.DataFrame:
    """날짜마다 느슨한 조건의 후보 + 지표 + 다음날 시가 진입 기준 이후 수익률(비용 차감)."""
    progress = progress or (lambda *_: None)
    loose = loose_params(p)
    window = max(loose.required_days, p.required_days, p.pos_days if p.pos_enabled else 0, loose.pos_days) + 1
    dates = panel.dates
    first = max(window, int(np.searchsorted(dates, pd.Timestamp(start))) if start else 0)
    shares = panel.meta.get("shares")
    close_ff = panel.close.ffill()
    parts = []
    total = len(dates) - first
    for k, i in enumerate(range(first, len(dates))):
        if cancelled():
            break
        if k % 10 == 0:
            progress(k, total, f"후보 계산 {dates[i]:%Y-%m-%d} ({k}/{total})")
        sl = slice(i - window + 1, i + 1)
        meta = panel.meta.copy()
        if shares is not None:
            meta["market_cap"] = shares * close_ff.iloc[i]
        flows = {}
        if panel.has_flows:
            flows = {kk: v.loc[:dates[i]].iloc[-FLOW_DAYS:] for kk, v in panel.flows.items()}
        sub = Panel(**{f: getattr(panel, f).iloc[sl] for f in FIELDS}, meta=meta, flows=flows)
        df = features(sub, loose)
        df = df[filter_mask(df, loose)]
        if df.empty:
            continue
        df = df.assign(di=i, date=dates[i], expiry=is_option_expiry(dates[i], dates[:i + 1]))
        parts.append(df.reset_index())
    progress(total, total, "이후 수익률 계산")
    if not parts:
        return pd.DataFrame()
    cands = pd.concat(parts, ignore_index=True)
    return add_forward_returns(cands, panel, cost_pct)


def add_forward_returns(cands: pd.DataFrame, panel: Panel, cost_pct: float) -> pd.DataFrame:
    col = {t: j for j, t in enumerate(panel.close.columns)}
    O, C = panel.open.to_numpy(float), panel.close.to_numpy(float)
    O[O == 0] = np.nan
    C[C == 0] = np.nan
    n = len(panel.dates)
    ci = cands["ticker"].map(col).to_numpy()
    di = cands["di"].to_numpy()
    entry = np.where(di + 1 < n, O[np.minimum(di + 1, n - 1), ci], np.nan)
    cands["entry"] = entry
    for h in FWD_HORIZONS:
        j = di + h
        px = np.where(j < n, C[np.minimum(j, n - 1), ci], np.nan)
        cands[f"fwd{h}"] = px / entry - 1 - cost_pct / 100
    return cands


def select(cands: pd.DataFrame, p: ScreenParams) -> pd.DataFrame:
    """후보 풀에 실제 조건을 적용하고 날짜별 점수 순으로 정렬."""
    if cands.empty:
        return cands
    c = cands.set_index("ticker")
    out = c[filter_mask(c, p, c["expiry"].to_numpy())].copy()
    out["score"] = score(out, p)
    return out.reset_index().sort_values(["di", "score"], ascending=[True, False])


# ---------------------------------------------------------------------------- 매매 시뮬레이션
def simulate(signals: pd.DataFrame, panel: Panel, rules: TradeRules,
             start_di: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """signals: di(신호일 위치), ticker, score[, name]. 진입은 신호 다음날 시가.

    반환 (trades, equity, metrics). equity: date, equity, bench(전종목 평균), exposure
    """
    dates = panel.dates
    n = len(dates)
    col = {t: j for j, t in enumerate(panel.close.columns)}
    O, H, L, C = (getattr(panel, f).to_numpy(float) for f in ("open", "high", "low", "close"))
    for a in (O, H, L, C):
        a[a == 0] = np.nan
    names = panel.meta.get("name", pd.Series(dtype=object))
    markets = panel.meta.get("market", pd.Series(dtype=object))
    regime = regime_table(panel, rules.regime_ma) if rules.regime_filter else None

    by_day = {d: g for d, g in signals.groupby("di")} if not signals.empty else {}
    i0 = start_di if start_di is not None else (int(signals["di"].min()) + 1 if by_day else n)
    cash, positions, trades, curve = 1.0, {}, [], []
    last_px = {}

    def close_pos(t, i, px, reason):
        nonlocal cash
        pos = positions.pop(t)
        proceeds = pos["shares"] * px * (1 - rules.cost_pct / 100)
        cash += proceeds
        trades.append({
            "ticker": t, "name": names.get(t, ""), "market": markets.get(t, ""),
            "signal_date": dates[pos["e"] - 1], "entry_date": dates[pos["e"]], "entry": pos["px"],
            "exit_date": dates[i], "exit": px, "reason": reason,
            "ret": px / pos["px"] * (1 - rules.cost_pct / 100) - 1, "days": i - pos["e"] + 1,
            "score": pos["score"],
        })

    equity_prev = 1.0
    for i in range(max(i0, 1), n):
        # 1) 보유 종목 청산 점검 (진입 당일 포함)
        for t in list(positions):
            pos, c = positions[t], col[t]
            o, h, lo, cl = O[i, c], H[i, c], L[i, c], C[i, c]
            if np.isnan(cl):  # 거래정지/상장폐지 — 마지막 가격으로 평가만
                if i - pos["e"] + 1 >= rules.max_hold * 3:  # 장기 정지면 마지막 가격으로 정리
                    close_pos(t, i, last_px.get(t, pos["px"]), "거래정지")
                continue
            last_px[t] = cl
            stop = pos["px"] * (1 - rules.stop_pct / 100) if rules.stop_pct > 0 else None
            tgt = pos["px"] * (1 + rules.target_pct / 100) if rules.target_pct > 0 else None
            if stop is not None and lo <= stop:
                close_pos(t, i, o if o < stop else stop, "손절")
            elif tgt is not None and h >= tgt:
                close_pos(t, i, o if o > tgt else tgt, "익절")
            elif i - pos["e"] + 1 >= rules.max_hold:
                close_pos(t, i, cl, "기간만료")

        # 2) 전날 신호로 오늘 시가 진입
        sig = by_day.get(i - 1)
        if sig is not None and len(positions) < rules.max_positions:
            entered = 0
            for r in sig.itertuples():
                if len(positions) >= rules.max_positions or entered >= rules.per_day:
                    break
                t = r.ticker
                if t in positions or t not in col:
                    continue
                if regime is not None and not regime.iloc[i - 1].get(markets.get(t, "KOSPI"), True):
                    continue
                px = O[i, col[t]]
                if np.isnan(px):
                    continue
                alloc = min(cash, equity_prev / rules.max_positions)
                if alloc <= 1e-9:
                    break
                cash -= alloc
                positions[t] = {"shares": alloc / px, "px": px, "e": i, "score": r.score}
                entered += 1
                # 진입 당일 손절/익절 점검
                c = col[t]
                stop = px * (1 - rules.stop_pct / 100) if rules.stop_pct > 0 else None
                if stop is not None and L[i, c] <= stop:
                    close_pos(t, i, stop, "손절")

        value = sum(p["shares"] * (C[i, col[t]] if not np.isnan(C[i, col[t]]) else last_px.get(t, p["px"]))
                    for t, p in positions.items())
        equity_prev = cash + value
        curve.append({"date": dates[i], "equity": equity_prev, "exposure": value / equity_prev if equity_prev else 0,
                      "positions": len(positions)})

    for t in list(positions):  # 기간 끝: 마지막 가격으로 평가 청산
        close_pos(t, n - 1, last_px.get(t, positions[t]["px"]), "보유중")

    equity = pd.DataFrame(curve)
    if not equity.empty:
        rets = panel.close.pct_change(fill_method=None).clip(-0.3, 0.3).mean(axis=1).fillna(0)
        # 시장 = 전종목 동일가중 일간 수익률 누적 (equity 와 같은 날부터)
        equity["bench"] = (1 + rets.loc[equity["date"]]).cumprod().to_numpy()
    trades = pd.DataFrame(trades)
    return trades, equity, metrics(trades, equity)


def metrics(trades: pd.DataFrame, equity: pd.DataFrame) -> dict:
    if equity.empty:
        return {"trades": 0}
    eq = equity["equity"]
    years = max((equity["date"].iloc[-1] - equity["date"].iloc[0]).days / 365.25, 1e-9)
    mdd = (eq / eq.cummax() - 1).min()
    bench = equity["bench"]
    out = {
        "trades": len(trades),
        "total": eq.iloc[-1] - 1,
        "cagr": eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1,
        "mdd": mdd,
        "bench_total": bench.iloc[-1] - 1,
        "exposure": equity["exposure"].mean(),
        "start": equity["date"].iloc[0], "end": equity["date"].iloc[-1],
    }
    if len(trades):
        r = trades["ret"]
        out.update({
            "win_rate": (r > 0).mean(), "avg_ret": r.mean(),
            "avg_win": r[r > 0].mean() if (r > 0).any() else 0.0,
            "avg_loss": r[r <= 0].mean() if (r <= 0).any() else 0.0,
            "avg_days": trades["days"].mean(),
        })
    return out


# ---------------------------------------------------------------------------- 최적화 · 요인 분석
def split_date(cands: pd.DataFrame, train_frac: float = 0.67) -> pd.Timestamp:
    d = np.sort(cands["date"].unique())
    return pd.Timestamp(d[min(len(d) - 1, int(len(d) * train_frac))])


def _stats(r: pd.Series) -> dict:
    r = r.dropna()
    return {"n": len(r), "mean": r.mean() if len(r) else np.nan,
            "win": (r > 0).mean() if len(r) else np.nan}


def grid_search(cands: pd.DataFrame, p: ScreenParams, horizon: int = 10, train_frac: float = 0.67,
                vol_grid=GRID_VOL, chg_grid=GRID_CHG, top_per_day: int = 0) -> pd.DataFrame:
    """거래량 배수 x 허용 등락률 조합별 학습/검증 기간 성과 (신호 단위 평균 수익, 비용 차감).

    top_per_day > 0 이면 날짜별 점수 상위 N 개만 (실제 매매처럼).
    """
    cut = split_date(cands, train_frac)
    rows = []
    for v in vol_grid:
        for c in chg_grid:
            q = replace(p, vol_ratio_min=v, price_chg_max=c, daily_chg_max=max(p.daily_chg_max, c))
            sel = select(cands, q)
            if top_per_day:
                sel = sel.groupby("di", group_keys=False).head(top_per_day)
            r = sel[f"fwd{horizon}"]
            tr, te = _stats(r[sel["date"] < cut]), _stats(r[sel["date"] >= cut])
            rows.append({"vol_ratio_min": v, "price_chg_max": c,
                         **{f"train_{k}": x for k, x in tr.items()}, **{f"test_{k}": x for k, x in te.items()}})
    out = pd.DataFrame(rows)
    out.attrs["split"] = cut
    return out


def best_cell(grid: pd.DataFrame, min_n: int = 30) -> pd.Series | None:
    ok = grid[grid["train_n"] >= min_n]
    if ok.empty:
        return None
    return ok.sort_values("train_mean", ascending=False).iloc[0]


def factor_analysis(cands: pd.DataFrame, p: ScreenParams, horizon: int = 10, train_frac: float = 0.67,
                    ridge: float = 10.0, top_k: int = 3) -> dict:
    """지표별 이후 수익률과의 관련성(날짜별 순위상관 평균, IC)과 학습 점수 가중치.

    학습 기간으로 가중치를 정하고, 검증 기간에서 '학습 점수 상위 top_k' vs '기본 점수 상위 top_k' 를 비교.
    """
    y_col = f"fwd{horizon}"
    data = cands.dropna(subset=[y_col]).copy()
    if len(data) < 50:
        return {"error": "표본이 부족합니다 (최소 50개 신호 필요). 과거 데이터를 더 수집하세요."}
    data = data.set_index("ticker", drop=False)
    X = learn_matrix(data, p).reset_index(drop=True)
    y = data[y_col].clip(*data[y_col].quantile([0.01, 0.99])).reset_index(drop=True)
    dates = data["date"].reset_index(drop=True)
    cut = split_date(data.reset_index(drop=True), train_frac)
    train = dates < cut

    ic = {}
    for f in X.columns:
        per_day = []
        for _, g in pd.DataFrame({"x": X[f], "y": y, "d": dates})[train].groupby("d"):
            if len(g) >= 5 and g["x"].nunique() > 1:
                per_day.append(g["x"].rank().corr(g["y"].rank()))
        ic[f] = (np.nanmean(per_day) if per_day else np.nan, len(per_day))

    Xt = X[train].fillna(0)
    mu, sd = Xt.mean(), Xt.std().replace(0, 1)
    Z = ((Xt - mu) / sd).to_numpy()
    yt = y[train].to_numpy()
    w = np.linalg.solve(Z.T @ Z + ridge * np.eye(Z.shape[1]), Z.T @ (yt - yt.mean()))
    weights = {f: [float(w[k]), float(mu[f]), float(sd[f])] for k, f in enumerate(X.columns)}

    test = data.reset_index(drop=True)[~train].copy()
    if test.empty:
        comp = {}
    else:
        qp = replace(p, score_weights=weights)
        test["learned"] = score(test.set_index("ticker"), qp).to_numpy()
        test["base"] = score(test.set_index("ticker"), replace(p, score_weights=None)).to_numpy()
        top = lambda col: test.sort_values(col, ascending=False).groupby("di").head(top_k)[y_col]  # noqa: E731
        comp = {"all": _stats(test[y_col]), "base_top": _stats(top("base")), "learned_top": _stats(top("learned"))}

    table = pd.DataFrame([
        {"feature": f, "label": LEARN_FEATURES[f], "ic": ic[f][0], "days": ic[f][1], "weight": weights[f][0]}
        for f in X.columns
    ]).sort_values("ic", key=lambda s: s.abs(), ascending=False)
    return {"table": table, "weights": weights, "test": comp, "split": cut, "horizon": horizon, "n": len(data)}


# ---------------------------------------------------------------------------- 모의투자 (저장된 실제 신호)
def saved_signals(db, panel: Panel, source_filter=("krx", "auto")) -> pd.DataFrame:
    """DB 에 저장된 스크리닝 결과를 시뮬레이션 신호 형식으로 (같은 날 같은 종목은 1번)."""
    res = db.load_results()
    if res.empty:
        return pd.DataFrame(columns=["di", "ticker", "score"])
    runs = db.list_runs().set_index("run_id")
    res = res[res["run_id"].map(runs["source"]).isin(source_filter)]
    pos = {d.strftime("%Y%m%d"): i for i, d in enumerate(panel.dates)}
    res = res.assign(di=res["asof"].map(pos)).dropna(subset=["di"])
    res["di"] = res["di"].astype(int)
    res = res.sort_values(["di", "run_id", "rank"]).drop_duplicates(["di", "ticker"])
    return res[["di", "ticker", "score", "name"]].sort_values(["di", "score"], ascending=[True, False])
