"""KRX 수집 경로를 pykrx 모킹으로 검증 (네트워크 없이)."""
import datetime as dt

import pandas as pd
import pytest

pykrx_stock = pytest.importorskip("pykrx.stock")

from screener.core import ScreenParams, run_screen  # noqa: E402
from screener.data import krx  # noqa: E402

DAYS = pd.bdate_range(end="2026-10-09", periods=30)


def fake_ohlcv(date, market="KOSPI"):
    tick = "005930" if market == "KOSPI" else "035720"
    last = date == DAYS[-1].strftime("%Y%m%d")
    vol = 500_000 if last else 100_000
    df = pd.DataFrame({"시가": [10000], "고가": [10050], "저가": [9950], "종가": [10000],
                       "거래량": [vol], "거래대금": [vol * 10000], "등락률": [0.0]},
                      index=pd.Index([tick], name="티커"))
    return df


def fake_cap(date, market="KOSPI"):
    tick = "005930" if market == "KOSPI" else "035720"
    return pd.DataFrame({"종가": [10000], "시가총액": [1e12], "거래량": [0], "거래대금": [0],
                         "상장주식수": [1e8]}, index=pd.Index([tick], name="티커"))


def test_load_panel(monkeypatch, tmp_path):
    monkeypatch.setattr(krx, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(pykrx_stock, "get_previous_business_days", lambda **kw: list(DAYS))
    monkeypatch.setattr(pykrx_stock, "get_market_ohlcv_by_ticker", fake_ohlcv)
    monkeypatch.setattr(pykrx_stock, "get_market_cap_by_ticker", fake_cap)
    monkeypatch.setattr(pykrx_stock, "get_market_ticker_name",
                        lambda t: {"005930": "삼성전자", "035720": "카카오"}[t])

    panel = krx.load_panel(DAYS[-1].date(), 25, sleep=0)
    assert len(panel.dates) == 25
    assert panel.meta.loc["035720", "market"] == "KOSDAQ"
    out = run_screen(panel, ScreenParams())
    assert set(out["name"]) == {"삼성전자", "카카오"}
    assert len(list(tmp_path.glob("*.pkl"))) == 50  # 영업일 x 시장 캐시


def test_login_requires_credentials():
    with pytest.raises(krx.KrxError):
        krx.login("", "")
