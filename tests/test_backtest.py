from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from screener import backtest as bt
from screener.core import FIELDS, Panel, ScreenParams, is_option_expiry, run_screen, second_thursday
from screener.data import demo


def flat_panel(n=40, tickers=("A", "B")):
    dates = pd.bdate_range("2026-01-05", periods=n)
    base = pd.DataFrame(100.0, index=dates, columns=list(tickers))
    p = Panel(open=base.copy(), high=base * 1.01, low=base * 0.99, close=base.copy(),
              volume=base * 1000, value=base * 100000,
              meta=pd.DataFrame({"name": list(tickers), "market": "KOSPI"}, index=list(tickers)))
    return p


def set_bar(p, i, t, o, h, l, c):
    for f, v in zip(("open", "high", "low", "close"), (o, h, l, c)):
        getattr(p, f).iloc[i, p.close.columns.get_loc(t)] = v


def rules(**kw):
    return bt.TradeRules(**{"cost_pct": 0.0, "regime_filter": False, **kw})


def test_take_profit_exit():
    p = flat_panel()
    set_bar(p, 11, "A", 100, 101, 99, 100)      # 진입일 (신호 10일 다음날 시가 100)
    set_bar(p, 13, "A", 110, 120, 109, 118)     # 익절 15% → 115 에 청산
    sig = pd.DataFrame({"di": [10], "ticker": ["A"], "score": [1.0]})
    trades, eq, m = bt.simulate(sig, p, rules(stop_pct=5, target_pct=15, max_positions=1))
    t = trades.iloc[0]
    assert t["reason"] == "익절" and t["exit"] == pytest.approx(115) and t["ret"] == pytest.approx(0.15)
    assert t["entry_date"] == p.dates[11] and t["exit_date"] == p.dates[13]
    assert eq["equity"].iloc[-1] == pytest.approx(1.15)


def test_stop_with_gap_down_exits_at_open():
    p = flat_panel()
    set_bar(p, 12, "A", 90, 92, 88, 91)         # 갭하락: 손절가 95 보다 낮은 시가 90 에 청산
    sig = pd.DataFrame({"di": [10], "ticker": ["A"], "score": [1.0]})
    trades, _, _ = bt.simulate(sig, p, rules(stop_pct=5, target_pct=0, max_positions=1))
    assert trades.iloc[0]["reason"] == "손절" and trades.iloc[0]["exit"] == pytest.approx(90)


def test_max_hold_and_costs_and_slots():
    p = flat_panel()
    p.close.iloc[15:, 0] = 104                  # A: 보유기간 만료 시점 종가 104
    sig = pd.DataFrame({"di": [10, 10], "ticker": ["A", "B"], "score": [2.0, 1.0]})
    trades, eq, m = bt.simulate(sig, p, rules(stop_pct=0, target_pct=0, max_hold=5, cost_pct=1.0,
                                              max_positions=1, per_day=2))
    assert len(trades) == 1 and trades.iloc[0]["ticker"] == "A"      # 자리 1개 → 점수 높은 A만
    t = trades.iloc[0]
    assert t["reason"] == "기간만료" and t["days"] == 5
    assert t["ret"] == pytest.approx(104 / 100 * 0.99 - 1)


def test_regime_filter_blocks_entries():
    p = flat_panel(n=80)
    p.close.iloc[60:] = 80                       # 급락 → 이동평균 아래
    sig = pd.DataFrame({"di": [70], "ticker": ["A"], "score": [1.0]})
    trades, _, _ = bt.simulate(sig, p, bt.TradeRules(regime_filter=True, regime_ma=20, cost_pct=0))
    assert trades.empty
    trades, _, _ = bt.simulate(sig, p, bt.TradeRules(regime_filter=False, cost_pct=0))
    assert len(trades) == 1


def test_option_expiry():
    assert second_thursday(2026, 10) == pd.Timestamp("2026-10-08")
    dates = pd.bdate_range("2026-10-01", "2026-10-20")
    assert is_option_expiry(pd.Timestamp("2026-10-08"), dates)
    assert not is_option_expiry(pd.Timestamp("2026-10-07"), dates)
    holiday = dates.drop(pd.Timestamp("2026-10-08"))          # 만기일 휴장 → 전 거래일
    assert is_option_expiry(pd.Timestamp("2026-10-07"), holiday)


@pytest.fixture(scope="module")
def demo_hist():
    return demo.load_panel(days=260, n_tickers=150, seed=3)


def test_candidates_match_live_screen_no_lookahead(demo_hist):
    p = ScreenParams(min_value_eok=0)
    cands = bt.build_candidates(demo_hist, p, cost_pct=0)
    for i in (100, 180, 259):
        sub = Panel(**{f: getattr(demo_hist, f).iloc[:i + 1] for f in FIELDS}, meta=demo_hist.meta)
        live = run_screen(sub, replace(p, flow_enabled=False))
        sel = bt.select(cands[cands["di"] == i], replace(p, flow_enabled=False))
        assert sorted(sel["ticker"]) == sorted(live["ticker"])   # t 일까지 데이터만으로 같은 결과
    row = cands[cands["di"] == 100].iloc[0]
    c = demo_hist.close.columns.get_loc(row["ticker"])
    assert row["entry"] == demo_hist.open.iloc[101, c]
    assert row["fwd5"] == pytest.approx(demo_hist.close.iloc[105, c] / row["entry"] - 1)
    assert np.isnan(cands[cands["di"] == 259]["fwd5"]).all()     # 미래 없음


def test_grid_and_factor_analysis_run(demo_hist):
    p = ScreenParams(min_value_eok=0)
    cands = bt.build_candidates(demo_hist, p)
    grid = bt.grid_search(cands, p, horizon=5)
    assert len(grid) == len(bt.GRID_VOL) * len(bt.GRID_CHG)
    fa = bt.factor_analysis(cands, p, horizon=5)
    assert set(fa["weights"]) == set(fa["table"]["feature"])
    sig = bt.select(cands, p)
    trades, eq, m = bt.simulate(sig, demo_hist, bt.TradeRules())
    assert m["trades"] == len(trades) and len(eq) > 100
    assert -1 < m["mdd"] <= 0
