import datetime as dt

import numpy as np
import pandas as pd
import pytest

from screener.core import Panel, ScreenParams, run_screen
from screener.data import demo


def make_panel(n_days=30):
    """A: 거래량 5배 + 보합 / B: 거래량 5배 + 10% 급등 / C: 평범 / D: 우선주 패턴 / E: 거래정지."""
    dates = pd.bdate_range(end="2026-10-09", periods=n_days)
    tickers = ["000010", "000020", "000030", "000015", "000040"]
    close = pd.DataFrame(10000.0, index=dates, columns=tickers)
    vol = pd.DataFrame(100_000.0, index=dates, columns=tickers)
    vol.iloc[-1, [0, 1, 3, 4]] = 500_000
    close.iloc[-1, 0] = 10050      # +0.5%
    close.iloc[-1, 1] = 11000      # +10%
    close.iloc[-1, 3] = 10050
    vol.iloc[-1, 4] = 0            # 거래정지
    high, low = close * 1.005, close * 0.995
    meta = pd.DataFrame({"name": ["가", "나", "다", "가우", "마"], "market": "KOSPI",
                         "market_cap": 1e11, "shares": 1e7}, index=tickers)
    return Panel(open=close, high=high, low=low, close=close, volume=vol, value=vol * close, meta=meta)


def test_finds_volume_spike_without_price_move():
    out = run_screen(make_panel(), ScreenParams())
    assert out["ticker"].tolist() == ["000010"]
    row = out.iloc[0]
    assert row["vol_ratio"] == pytest.approx(5.0)
    assert row["chg_pct"] == pytest.approx(0.5)


def test_relaxing_price_condition_includes_jump():
    p = ScreenParams(price_chg_max=15, daily_chg_max=15, range_max=30)
    assert set(run_screen(make_panel(), p)["ticker"]) == {"000010", "000020"}


def test_preferred_toggle():
    p = ScreenParams(exclude_preferred=False)
    assert set(run_screen(make_panel(), p)["ticker"]) == {"000010", "000015"}


def test_volume_threshold():
    assert run_screen(make_panel(), ScreenParams(vol_ratio_min=6)).empty


def test_market_filter():
    assert run_screen(make_panel(), ScreenParams(markets=("KOSDAQ",))).empty


def test_multi_day_recent_window_uses_anchor_before_window():
    pnl = make_panel()
    pnl.volume.iloc[-3:, 2] = 400_000
    pnl.close.iloc[-3:, 2] = [10100, 9950, 10080]
    out = run_screen(pnl, ScreenParams(recent_days=3))
    row = out.set_index("ticker").loc["000030"]
    assert row["vol_ratio"] == pytest.approx(4.0)
    assert row["chg_pct"] == pytest.approx(0.8)


def test_params_roundtrip():
    p = ScreenParams(recent_days=3, markets=("KOSDAQ",))
    assert ScreenParams.from_dict(p.to_dict()) == p


def test_demo_panel_has_hits():
    out = run_screen(demo.load_panel(dt.date(2026, 10, 9)), ScreenParams(min_value_eok=0))
    assert len(out) > 5
    assert (out["chg_pct"].abs() <= 2).all() and (out["vol_ratio"] >= 3).all()
    assert out["score"].is_monotonic_decreasing


def test_short_panel_returns_empty():
    assert run_screen(make_panel(2), ScreenParams()).empty


def add_flows(pnl, frg_last, inst_last):
    """000010 의 최근 3일 외국인/기관 순매수(원) 설정."""
    idx = pnl.dates[-10:]
    f = pd.DataFrame(0.0, index=idx, columns=pnl.close.columns)
    i = f.copy()
    f.iloc[-3:, 0] = frg_last
    i.iloc[-3:, 0] = inst_last
    pnl.flows = {"foreign": f, "inst": i}
    return pnl


@pytest.mark.parametrize("mode,frg,inst,hit", [
    ("both", 1e8, 1e8, True), ("both", 1e8, -1e8, False),
    ("either", 1e8, -1e8, True), ("either", -1e8, -1e8, False),
    ("foreign", 1e8, -5e8, True), ("inst", 1e8, -5e8, False),
    ("sum", 2e8, -1e8, True), ("sum", 1e8, -2e8, False),
])
def test_flow_modes(mode, frg, inst, hit):
    pnl = add_flows(make_panel(), frg, inst)
    out = run_screen(pnl, ScreenParams(flow_enabled=True, flow_mode=mode))
    assert ("000010" in set(out["ticker"])) is hit


def test_flow_amount_and_days():
    pnl = add_flows(make_panel(), 1e8, 0)   # 3일 x 1억 = 3억, 순매수 3일
    base = dict(flow_enabled=True, flow_mode="foreign")
    row = run_screen(pnl, ScreenParams(**base)).iloc[0]
    assert row["frg_eok"] == pytest.approx(3.0) and row["frg_days"] == 3
    assert len(run_screen(pnl, ScreenParams(**base, flow_min_eok=3.0, flow_min_buy_days=3))) == 1
    assert run_screen(pnl, ScreenParams(**base, flow_min_eok=3.5)).empty
    assert run_screen(pnl, ScreenParams(**base, flow_min_buy_days=4)).empty
    # 집계 기간 1일이면 1억
    assert run_screen(pnl, ScreenParams(**base, flow_days=1)).iloc[0]["frg_eok"] == pytest.approx(1.0)


def test_flow_enabled_without_data_returns_empty():
    out = run_screen(make_panel(), ScreenParams(flow_enabled=True))
    assert out.empty
    assert pd.isna(run_screen(make_panel(), ScreenParams()).iloc[0]["frg_eok"])
