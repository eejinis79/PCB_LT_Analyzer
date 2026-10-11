import pandas as pd

from screener.core import ScreenParams, run_screen
from screener.data import demo
from screener.db import StockDB


def test_results_roundtrip_and_overwrite(tmp_path):
    db = StockDB(tmp_path / "s.db")
    res = run_screen(demo.load_panel(), ScreenParams())
    p = ScreenParams().to_dict()
    db.save_results("20261009", res, p)
    db.save_results("20261009", res, p)  # 같은 기준일·조건 → 덮어씀
    db.save_results("20261009", res.head(3), ScreenParams(vol_ratio_min=5).to_dict())
    out = db.load_results("20261009")
    assert db.stats()["runs"] == 2
    assert len(out) == len(res) + 3
    first = out[out["run_id"] == out["run_id"].min()].sort_values("rank")
    assert first["ticker"].tolist() == res["ticker"].tolist()


def test_import_pickle_cache(tmp_path):
    cache = tmp_path / "krx"
    cache.mkdir()
    df = pd.DataFrame({"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5],
                       "volume": [100.0], "value": [150.0]}, index=["005930"])
    df.to_pickle(cache / "20261008_KOSPI.pkl")
    pd.Series({"005930": 3e8}).to_pickle(cache / "flow_20261008_KOSPI_foreign.pkl")
    db = StockDB(tmp_path / "s.db")
    assert db.import_pickle_cache(cache) == 2
    assert db.fetched("20261008", "KOSPI", "ohlcv") and db.fetched("20261008", "KOSPI", "flow_foreign")
    assert db.load_ohlcv(["20261008"], ["KOSPI"])["close"].tolist() == [1.5]
    assert db.import_pickle_cache(cache) == 0  # 이미 옮김
