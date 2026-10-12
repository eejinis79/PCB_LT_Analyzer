import numpy as np
import pandas as pd
import pytest

from screener import performance
from screener.core import ScreenParams
from screener.db import StockDB


def make_prices(n=30):
    """A: 매일 +1%, B: 매일 -1%, C: 보합 (시장 평균 계산용)."""
    dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2026-09-01", periods=n)]
    rows = []
    for i, d in enumerate(dates):
        for t, g in (("A", 1.01), ("B", 0.99), ("C", 1.0)):
            c = 100 * g ** i
            rows.append({"date": d, "ticker": t, "open": c * 0.995, "high": c * 1.02,
                         "low": c * 0.98, "close": c})
    return dates, pd.DataFrame(rows)


def test_returns_and_benchmark():
    dates, prices = make_prices()
    sig = pd.DataFrame({"asof": [dates[0], dates[0], dates[25]], "ticker": ["A", "B", "A"],
                        "name": ["에이", "비", "에이"]})
    detail, summary, path = performance.compute(sig, prices)
    a0 = detail.iloc[0]
    assert a0["r1"] == pytest.approx(0.01)
    assert a0["r5"] == pytest.approx(1.01 ** 5 - 1)
    assert a0["r20"] == pytest.approx(1.01 ** 20 - 1)
    bench5 = ((1.01 ** 5 - 1) + (0.99 ** 5 - 1) + 0) / 3
    assert a0["bench5"] == pytest.approx(bench5)
    assert a0["max_gain"] == pytest.approx(1.01 ** 20 * 1.02 - 1)
    assert detail.iloc[1]["r5"] == pytest.approx(0.99 ** 5 - 1)
    # 마지막 신호: 이후 4일뿐 → r5 이상은 아직 없음
    late = detail.iloc[2]
    assert late["r1"] == pytest.approx(0.01) and np.isnan(late["r5"]) and late["held"] == 4
    s5 = summary.set_index("horizon").loc[5]
    assert s5["n"] == 2 and s5["win_rate"] == pytest.approx(0.5)
    assert s5["excess"] == pytest.approx(((1.01 ** 5 - 1) + (0.99 ** 5 - 1)) / 2 - bench5)
    assert path.loc[0, "n"] == 3 and path.loc[19, "n"] == 2


def test_next_open_entry():
    dates, prices = make_prices()
    sig = pd.DataFrame({"asof": [dates[0]], "ticker": ["A"]})
    detail, _, _ = performance.compute(sig, prices, entry="next_open")
    entry = 100 * 1.01 * 0.995
    assert detail.iloc[0]["entry"] == pytest.approx(entry)
    assert detail.iloc[0]["r5"] == pytest.approx(100 * 1.01 ** 5 / entry - 1)


def test_from_db_dedupes(tmp_path):
    dates, prices = make_prices()
    db = StockDB(tmp_path / "s.db")
    for d, g in prices.groupby("date"):
        db.save_ohlcv(d, "KOSPI", g.set_index("ticker").assign(volume=1.0, value=1.0))
    res = pd.DataFrame({"ticker": ["A", "B"], "name": ["에이", "비"], "market": ["KOSPI"] * 2,
                        "close": [100.0, 100.0], "vol_ratio": [3.0, 4.0], "score": [1.0, 2.0]})
    db.save_results(dates[0], res, ScreenParams().to_dict())
    db.save_results(dates[0], res.head(1), ScreenParams(vol_ratio_min=5).to_dict())
    detail, summary, _ = performance.from_db(db)
    assert sorted(detail["ticker"]) == ["A", "B"]          # 중복 제거
    detail_all, _, _ = performance.from_db(db, dedupe=False)
    assert len(detail_all) == 3
    one_run = db.list_runs()["run_id"].min()
    d1, _, _ = performance.from_db(db, run_ids=[one_run])
    assert len(d1) == 2
    assert summary.set_index("horizon").loc[20, "n"] == 2


def test_empty_db(tmp_path):
    detail, summary, path = performance.from_db(StockDB(tmp_path / "s.db"))
    assert detail.empty and summary.empty
