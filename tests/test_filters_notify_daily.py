import importlib.util
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from screener.core import ScreenParams, run_screen
from screener.notify import format_results

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_core import make_panel  # noqa: E402


def test_position_filter():
    pnl = make_panel(70)
    pnl.close.iloc[:10, 0] = 5000                  # 60일 전 저점 5000 → 현재 10050 은 +101%
    p = ScreenParams(pos_enabled=True, pos_days=65, pos_max_pct=30)
    assert "000010" not in set(run_screen(pnl, p)["ticker"])
    assert "000010" in set(run_screen(pnl, replace(p, pos_max_pct=150))["ticker"])
    assert run_screen(pnl, ScreenParams()).iloc[0]["pos_pct"] == pytest.approx(0.5)


def test_obv_filter():
    pnl = make_panel(30)
    # 기준 구간: 하락일에 거래량 집중 → OBV 음수 (분배)
    for i in range(5, 28, 2):
        pnl.close.iloc[i, 0] = 9900
        pnl.volume.iloc[i, 0] = 300_000
    out = run_screen(pnl, ScreenParams(price_chg_max=5, daily_chg_max=5, vol_ratio_min=1.5))
    row = out.set_index("ticker").loc["000010"]
    assert row["obv_trend"] < 0
    assert "000010" not in set(run_screen(pnl, ScreenParams(price_chg_max=5, daily_chg_max=5, vol_ratio_min=1.5,
                                                            obv_enabled=True, obv_min=0))["ticker"])


def test_expiry_filter():
    pnl = make_panel(30)                            # 마지막 날 2026-10-09 (금) — 만기 아님
    assert len(run_screen(pnl, ScreenParams(exclude_expiry=True))) == 1
    idx = pd.bdate_range(end="2026-10-08", periods=30)   # 마지막 날 = 10월 둘째 목요일
    for f in ("open", "high", "low", "close", "volume", "value"):
        getattr(pnl, f).index = idx
    assert run_screen(pnl, ScreenParams(exclude_expiry=True)).empty
    assert len(run_screen(pnl, ScreenParams())) == 1


def test_learned_score_weights_rank():
    pnl = make_panel()
    pnl.volume.iloc[-1, 3] = 900_000               # 000015(우선주) — 제외 해제 후 비교
    p = ScreenParams(exclude_preferred=False)
    base = run_screen(pnl, p)
    w = {"log_vol_ratio": [-1.0, 0.0, 1.0]}        # 거래량 배수 클수록 '감점'
    learned = run_screen(pnl, replace(p, score_weights=w))
    assert base.iloc[0]["ticker"] == "000015" and learned.iloc[0]["ticker"] == "000010"


def test_format_results():
    res = run_screen(make_panel(), ScreenParams())
    txt = format_results("20261009", res, {"KOSPI": True, "KOSDAQ": False})
    assert "2026-10-09" in txt and "가(000010)" in txt and "KOSDAQ" in txt


def test_daily_run_end_to_end(tmp_path, monkeypatch):
    """vault 잠금 해제 → (모킹) KRX → 스크리닝 → DB 저장 → 텔레그램 (모킹)."""
    pykrx_stock = pytest.importorskip("pykrx.stock")
    pytest.importorskip("PySide6.QtCore")
    import test_krx_mock as m
    from screener.data import krx
    from screener.db import StockDB
    from screener.vault import Credentials, Vault

    monkeypatch.setenv("VSS_HOME", str(tmp_path))
    monkeypatch.setenv("VSS_TEST_PW", "abcd1234")
    v = Vault()
    v.change_password("0000", "abcd1234")
    v.save("abcd1234", Credentials(krx_id="id", krx_pw="pw", telegram_token="T", telegram_chat="C"))
    for name, fn in (("get_previous_business_days", lambda **kw: list(m.DAYS)),
                     ("get_market_ohlcv_by_ticker", m.fake_ohlcv), ("get_market_cap_by_ticker", m.fake_cap),
                     ("get_market_net_purchases_of_equities_by_ticker", m.fake_flow),
                     ("get_market_ticker_name", m.fake_name)):
        monkeypatch.setattr(pykrx_stock, name, fn)
    monkeypatch.setattr(krx, "login", lambda i, p: None)
    monkeypatch.setattr(krx, "fetch_index", lambda *a, **k: 0)
    sent = []
    import screener.notify as notify
    monkeypatch.setattr(notify, "send_telegram", lambda t, c, text: sent.append((t, c, text)))

    spec = importlib.util.spec_from_file_location("daily_run", Path(__file__).resolve().parents[1] / "daily_run.py")
    dr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dr)
    from screener.core import ScreenParams as SP
    monkeypatch.setattr(dr, "load_settings", lambda: (SP(), str(tmp_path / "stock_data.db")))
    monkeypatch.setattr(sys, "argv", ["daily_run.py", "--password-env", "VSS_TEST_PW", "--force"])
    assert dr.main() == 0
    runs = StockDB(tmp_path / "stock_data.db").list_runs()
    assert len(runs) == 1 and runs.iloc[0]["source"] == "auto" and runs.iloc[0]["n_hits"] == 2
    assert sent and sent[0][:2] == ("T", "C") and "삼성전자" in sent[0][2]

    monkeypatch.setenv("VSS_TEST_PW", "wrong")
    assert dr.main() == 2
