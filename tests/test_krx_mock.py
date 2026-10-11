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


def fake_flow(fromdate, todate, market="KOSPI", investor="개인"):
    tick = "005930" if market == "KOSPI" else "035720"
    amt = 3e8 if investor == "외국인" else -1e8
    return pd.DataFrame({"종목명": ["x"], "순매수거래대금": [amt]}, index=pd.Index([tick], name="티커"))


def test_load_panel(monkeypatch, tmp_path):
    monkeypatch.setattr(krx, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(pykrx_stock, "get_previous_business_days", lambda **kw: list(DAYS))
    monkeypatch.setattr(pykrx_stock, "get_market_ohlcv_by_ticker", fake_ohlcv)
    monkeypatch.setattr(pykrx_stock, "get_market_cap_by_ticker", fake_cap)
    monkeypatch.setattr(pykrx_stock, "get_market_net_purchases_of_equities_by_ticker", fake_flow)
    monkeypatch.setattr(pykrx_stock, "get_market_ticker_name",
                        lambda t: {"005930": "삼성전자", "035720": "카카오"}[t])

    panel = krx.load_panel(DAYS[-1].date(), 25, sleep=0)
    assert len(panel.dates) == 25
    assert panel.meta.loc["035720", "market"] == "KOSDAQ"
    out = run_screen(panel, ScreenParams())
    assert set(out["name"]) == {"삼성전자", "카카오"}
    assert len(list(tmp_path.glob("2*.pkl"))) == 50  # 영업일 x 시장 캐시
    assert len(list(tmp_path.glob("flow_*.pkl"))) == 10 * 2 * 2  # 10일 x 시장 x 투자자
    assert panel.has_flows and panel.flows["foreign"].shape == (10, 2)
    hit = run_screen(panel, ScreenParams(flow_enabled=True, flow_mode="foreign", flow_days=3))
    assert hit["frg_eok"].tolist() == [pytest.approx(9.0)] * 2
    assert run_screen(panel, ScreenParams(flow_enabled=True, flow_mode="inst")).empty


def test_flow_failure_keeps_prices(monkeypatch, tmp_path):
    monkeypatch.setattr(krx, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(pykrx_stock, "get_previous_business_days", lambda **kw: list(DAYS))
    monkeypatch.setattr(pykrx_stock, "get_market_ohlcv_by_ticker", fake_ohlcv)
    monkeypatch.setattr(pykrx_stock, "get_market_cap_by_ticker", fake_cap)
    monkeypatch.setattr(pykrx_stock, "get_market_ticker_name", lambda t: "x")

    def boom(*a, **k):
        raise RuntimeError("network")
    monkeypatch.setattr(pykrx_stock, "get_market_net_purchases_of_equities_by_ticker", boom)
    panel = krx.load_panel(DAYS[-1].date(), 25, sleep=0)
    assert not panel.has_flows and len(run_screen(panel, ScreenParams())) == 2


def test_login_requires_credentials():
    with pytest.raises(krx.KrxError):
        krx.login("", "")
