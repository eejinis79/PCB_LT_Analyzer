"""KRX(data.krx.co.kr) 시세 수집 — pykrx 사용.

2025년 이후 KRX 정보데이터시스템은 로그인 세션이 필요하므로 KRX 계정으로 로그인한 뒤
날짜별 '전종목 시세'를 받아 Panel 로 합친다. (종목별 조회보다 호출 수가 훨씬 적다)
받은 데이터는 SQLite DB(screener/db.py, 기본: 프로그램 폴더의 stock_data.db)에 쌓아
재실행 시 새 날짜만 받는다.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from typing import Callable

import pandas as pd

from ..core import FLOW_DAYS, Panel
from ..db import StockDB
from ..paths import cache_dir

# 예전 버전의 .pkl 캐시 위치 (DB 로 이전할 때만 사용)
LEGACY_CACHE_DIR = cache_dir("krx")
FINAL_HOUR = 18  # 이 시각 이후의 당일 데이터는 확정으로 보고 DB 에 고정
# pykrx 투자자 구분 이름
INVESTOR_NAMES = {"foreign": "외국인", "inst": "기관합계"}
OHLCV_COLS = {"시가": "open", "고가": "high", "저가": "low", "종가": "close",
              "거래량": "volume", "거래대금": "value"}

Progress = Callable[[int, int, str], None]


class KrxError(RuntimeError):
    pass


def login(krx_id: str, krx_pw: str) -> None:
    """pykrx 전역 세션을 주어진 계정으로 교체한다. 실패 시 KrxError."""
    if not (krx_id and krx_pw):
        raise KrxError("KRX ID/PW 가 비어 있습니다.")
    # pykrx 는 import 시점에 환경변수로 로그인하므로 먼저 설정해 둔다.
    os.environ["KRX_ID"], os.environ["KRX_PW"] = krx_id, krx_pw
    from pykrx.website.comm.auth import build_krx_session, set_auth_session

    session = build_krx_session(krx_id, krx_pw)
    if session is None:
        raise KrxError("KRX 로그인 실패: ID/PW 를 확인하세요. (비밀번호 변경 요구 시 krx.co.kr 에서 변경)")
    set_auth_session(session)


def _business_days(end: dt.date, count: int) -> list[str]:
    """end 이전(포함) 영업일 count 개 (오름차순, YYYYMMDD)."""
    from pykrx import stock

    start = end - dt.timedelta(days=int(count * 1.6) + 15)
    try:
        days = stock.get_previous_business_days(
            fromdate=start.strftime("%Y%m%d"), todate=end.strftime("%Y%m%d"))
        days = [d.strftime("%Y%m%d") for d in days]
    except Exception:
        days = []
    if not days:  # 실패 시 평일 목록으로 대체 (휴장일은 수집 단계에서 걸러짐)
        days = [d.strftime("%Y%m%d") for d in pd.bdate_range(start, end)]
    return days[-count:]


def is_final(date: str, now: dt.datetime | None = None) -> bool:
    """DB 에 확정 저장해도 되는 날짜인지. 오늘 데이터는 장 마감 후(18시 이후)에만 확정으로 본다."""
    now = now or dt.datetime.now()
    today = now.strftime("%Y%m%d")
    return date < today or (date == today and now.hour >= FINAL_HOUR)


def _fetch_day(db: StockDB, date: str, market: str) -> bool:
    """DB 에 없으면 KRX 에서 받아 저장. 네트워크를 썼으면 True."""
    if db.fetched(date, market, "ohlcv"):  # 확정 데이터만 기록되므로 그대로 사용
        return False
    from pykrx import stock

    df = stock.get_market_ohlcv_by_ticker(date, market=market)
    if df is None or df.empty:
        raise KrxError(f"{date} {market} 시세가 비어 있습니다. (로그인 세션/휴장일 확인)")
    df = df.rename(columns=OHLCV_COLS)[list(OHLCV_COLS.values())]
    holiday = bool((df[["open", "high", "low", "close"]] == 0).all(axis=None))
    db.save_ohlcv(date, market, df, holiday=holiday, final=is_final(date))
    return True


def load_panel(end: dt.date, days: int, markets=("KOSPI", "KOSDAQ"),
               progress: Progress | None = None, sleep: float = 0.2,
               db: StockDB | None = None) -> Panel:
    """기준일 end 까지 days 영업일의 KOSPI/KOSDAQ 전종목 패널을 만든다.

    이미 DB 에 있는 날짜는 KRX 에 다시 묻지 않는다. (처음 한 번만 오래 걸림)
    """
    from pykrx import stock

    db = db or StockDB()
    progress = progress or (lambda *_: None)
    progress(0, 1, f"DB 확인 중… ({db.path})")
    dates = _business_days(end, days)
    flow_dates = dates[-FLOW_DAYS:]
    total = len(dates) * len(markets) + len(markets) + len(flow_dates) * len(markets) * len(INVESTOR_NAMES)
    step, fetched = 0, 0
    for date in dates:
        for market in markets:
            step += 1
            progress(step, total, f"{date} {market} 시세")
            if _fetch_day(db, date, market):
                fetched += 1
                time.sleep(sleep)

    long = db.load_ohlcv(dates, markets)
    if long.empty:
        raise KrxError("수집된 시세가 없습니다.")
    long["date"] = pd.to_datetime(long["date"])
    wide = {f: long.pivot(index="date", columns="ticker", values=f).sort_index()
            for f in OHLCV_COLS.values()}
    market_of = long.drop_duplicates("ticker", keep="last").set_index("ticker")["market"]
    trade_dates = [d.strftime("%Y%m%d") for d in wide["close"].index]

    last = trade_dates[-1]
    cap = db.load_caps(last)
    if cap.empty or not is_final(last):
        parts = []
        for market in markets:
            step += 1
            progress(step, total, f"{market} 시가총액")
            parts.append(stock.get_market_cap_by_ticker(last, market=market))
        raw = pd.concat(parts)
        cap = pd.DataFrame({"market_cap": raw["시가총액"], "shares": raw["상장주식수"]})
        db.save_caps(last, cap)
    meta = pd.DataFrame(index=wide["close"].columns)
    meta["market"] = market_of
    meta["market_cap"] = cap["market_cap"].reindex(meta.index)
    meta["shares"] = cap["shares"].reindex(meta.index)

    flows, step = _load_flows(db, [d for d in flow_dates if d in trade_dates], markets,
                              progress, step, total, sleep)

    names = db.load_names()
    missing = [t for t in meta.index if t not in names]
    if missing:
        progress(total, total, f"종목명 {len(missing)}개 조회")
        new = {t: _safe_name(stock, t) for t in missing}
        db.save_names(new, market_of.to_dict())
        names.update(new)
    meta["name"] = [names.get(t, "") for t in meta.index]
    progress(total, total, f"완료 — KRX 신규 조회 {fetched}일분, 나머지는 DB 사용")
    # 상장 전/거래정지 등 결측은 NaN 으로 둔다 (0 으로 채우면 평소 거래량이 왜곡됨)
    return Panel(**wide, meta=meta, flows=flows)


def _load_flows(db: StockDB, dates, markets, progress, step, total, sleep):
    """최근 영업일별 외국인/기관 순매수거래대금. 실패해도 시세 로딩은 계속한다(빈 dict)."""
    from pykrx import stock

    try:
        for inv, inv_name in INVESTOR_NAMES.items():
            for date in dates:
                for market in markets:
                    step += 1
                    progress(step, total, f"{date} {market} {inv_name} 순매수")
                    if db.fetched(date, market, f"flow_{inv}"):
                        continue
                    df = stock.get_market_net_purchases_of_equities_by_ticker(
                        date, date, market=market, investor=inv_name)
                    s = (df["순매수거래대금"].astype(float) if df is not None and not df.empty
                         else pd.Series(dtype=float))
                    db.save_flow(date, market, inv, s, final=is_final(date))
                    time.sleep(sleep)
    except Exception as e:  # noqa: BLE001
        print(f"투자자별 순매수 수집 실패 (수급 조건 사용 불가): {e}")
        return {}, step
    out = {inv: db.load_flows(dates, inv).fillna(0) for inv in INVESTOR_NAMES}
    if any(df.empty for df in out.values()):
        return {}, step
    return out, step


def _safe_name(stock, ticker: str) -> str:
    try:
        return stock.get_market_ticker_name(ticker)
    except Exception:
        return ""
