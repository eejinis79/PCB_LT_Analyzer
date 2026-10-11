"""KRX 수집 경로를 pykrx 모킹으로 검증 (네트워크 없이). DB 저장·재사용 포함."""
import datetime as dt

import pandas as pd
import pytest

pykrx_stock = pytest.importorskip("pykrx.stock")

from screener.core import ScreenParams, run_screen  # noqa: E402
from screener.data import krx  # noqa: E402
from screener.db import StockDB  # noqa: E402

DAYS = pd.bdate_range(end="2026-10-09", periods=30)
CALLS = {"ohlcv": 0, "flow": 0, "cap": 0, "name": 0}


def fake_ohlcv(date, market="KOSPI"):
    CALLS["ohlcv"] += 1
    tick = "005930" if market == "KOSPI" else "035720"
    last = date == DAYS[-1].strftime("%Y%m%d")
    vol = 500_000 if last else 100_000
    return pd.DataFrame({"시가": [10000], "고가": [10050], "저가": [9950], "종가": [10000],
                         "거래량": [vol], "거래대금": [vol * 10000], "등락률": [0.0]},
                        index=pd.Index([tick], name="티커"))


def fake_cap(date, market="KOSPI"):
    CALLS["cap"] += 1
    tick = "005930" if market == "KOSPI" else "035720"
    return pd.DataFrame({"종가": [10000], "시가총액": [1e12], "거래량": [0], "거래대금": [0],
                         "상장주식수": [1e8]}, index=pd.Index([tick], name="티커"))


def fake_flow(fromdate, todate, market="KOSPI", investor="개인"):
    CALLS["flow"] += 1
    tick = "005930" if market == "KOSPI" else "035720"
    amt = 3e8 if investor == "외국인" else -1e8
    return pd.DataFrame({"종목명": ["x"], "순매수거래대금": [amt]}, index=pd.Index([tick], name="티커"))


def fake_name(t):
    CALLS["name"] += 1
    return {"005930": "삼성전자", "035720": "카카오"}[t]


@pytest.fixture
def mocked(monkeypatch):
    for k in CALLS:
        CALLS[k] = 0
    monkeypatch.setattr(pykrx_stock, "get_previous_business_days", lambda **kw: list(DAYS))
    monkeypatch.setattr(pykrx_stock, "get_market_ohlcv_by_ticker", fake_ohlcv)
    monkeypatch.setattr(pykrx_stock, "get_market_cap_by_ticker", fake_cap)
    monkeypatch.setattr(pykrx_stock, "get_market_net_purchases_of_equities_by_ticker", fake_flow)
    monkeypatch.setattr(pykrx_stock, "get_market_ticker_name", fake_name)


def test_load_panel_and_db_reuse(mocked, tmp_path):
    db = StockDB(tmp_path / "stock.db")
    panel = krx.load_panel(DAYS[-1].date(), 25, sleep=0, db=db)
    assert len(panel.dates) == 25
    assert panel.meta.loc["035720", "market"] == "KOSDAQ"
    out = run_screen(panel, ScreenParams())
    assert set(out["name"]) == {"삼성전자", "카카오"}
    assert CALLS == {"ohlcv": 50, "flow": 40, "cap": 2, "name": 2}
    assert panel.has_flows and panel.flows["foreign"].shape == (10, 2)
    hit = run_screen(panel, ScreenParams(flow_enabled=True, flow_mode="foreign", flow_days=3))
    assert hit["frg_eok"].tolist() == [pytest.approx(9.0)] * 2
    assert run_screen(panel, ScreenParams(flow_enabled=True, flow_mode="inst")).empty

    # 두 번째 실행: 모두 DB 에서 — KRX 호출 없음, 같은 결과
    for k in CALLS:
        CALLS[k] = 0
    panel2 = krx.load_panel(DAYS[-1].date(), 25, sleep=0, db=StockDB(tmp_path / "stock.db"))
    assert CALLS == {"ohlcv": 0, "flow": 0, "cap": 0, "name": 0}
    pd.testing.assert_frame_equal(panel.close, panel2.close)
    pd.testing.assert_frame_equal(run_screen(panel, ScreenParams()), run_screen(panel2, ScreenParams()))
    st = db.stats()
    assert st["days"] == 25 + 0 and st["first"] == DAYS[5].strftime("%Y%m%d")

    # 다음 날: 새 날짜 1일 x 2시장만 받는다
    for k in CALLS:
        CALLS[k] = 0
    krx.load_panel(DAYS[-1].date(), 26, sleep=0, db=db)
    assert CALLS["ohlcv"] == 2


def test_intraday_today_is_refetched(mocked, tmp_path, monkeypatch):
    db = StockDB(tmp_path / "stock.db")
    last = DAYS[-1].strftime("%Y%m%d")
    monkeypatch.setattr(krx, "is_final", lambda d, now=None: d < last)  # 마지막 날 = 장중
    krx.load_panel(DAYS[-1].date(), 25, sleep=0, db=db)
    assert not db.fetched(last, "KOSPI", "ohlcv")
    CALLS["ohlcv"] = 0
    krx.load_panel(DAYS[-1].date(), 25, sleep=0, db=db)
    assert CALLS["ohlcv"] == 2  # 장중 데이터는 매번 다시 받음
    monkeypatch.setattr(krx, "is_final", lambda d, now=None: True)  # 장 마감 후
    krx.load_panel(DAYS[-1].date(), 25, sleep=0, db=db)
    assert db.fetched(last, "KOSPI", "ohlcv")


def test_is_final():
    now = dt.datetime(2026, 10, 9, 15, 0)
    assert krx.is_final("20261008", now) and not krx.is_final("20261009", now)
    assert krx.is_final("20261009", now.replace(hour=18))


def test_flow_failure_keeps_prices(mocked, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network")
    monkeypatch.setattr(pykrx_stock, "get_market_net_purchases_of_equities_by_ticker", boom)
    panel = krx.load_panel(DAYS[-1].date(), 25, sleep=0, db=StockDB(tmp_path / "s.db"))
    assert not panel.has_flows and len(run_screen(panel, ScreenParams())) == 2


def test_login_requires_credentials():
    with pytest.raises(krx.KrxError):
        krx.login("", "")
