"""KRX(data.krx.co.kr) 시세 수집 — pykrx 사용.

2025년 이후 KRX 정보데이터시스템은 로그인 세션이 필요하므로 KRX 계정으로 로그인한 뒤
날짜별 '전종목 시세'를 받아 Panel 로 합친다. (종목별 조회보다 호출 수가 훨씬 적다)
지난 영업일 데이터는 .cache/ 에 저장해 재실행 시 다시 받지 않는다.
"""
from __future__ import annotations

import datetime as dt
import os
import time
from pathlib import Path
from typing import Callable

import pandas as pd

from ..core import Panel

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "krx"
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


def _fetch_day(date: str, market: str, use_cache: bool) -> tuple[pd.DataFrame, bool]:
    """(시세, 휴장일 여부)"""
    from pykrx import stock

    path = CACHE_DIR / f"{date}_{market}.pkl"
    if use_cache and path.exists():
        return pd.read_pickle(path), False
    df = stock.get_market_ohlcv_by_ticker(date, market=market)
    if df is None or df.empty:
        raise KrxError(f"{date} {market} 시세가 비어 있습니다. (로그인 세션/휴장일 확인)")
    df = df.rename(columns=OHLCV_COLS)[list(OHLCV_COLS.values())]
    is_holiday = (df[["open", "high", "low", "close"]] == 0).all(axis=None)
    if use_cache and not is_holiday:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_pickle(path)
    return df, is_holiday


def load_panel(end: dt.date, days: int, markets=("KOSPI", "KOSDAQ"),
               progress: Progress | None = None, sleep: float = 0.2) -> Panel:
    """기준일 end 까지 days 영업일의 KOSPI/KOSDAQ 전종목 패널을 만든다."""
    from pykrx import stock

    progress = progress or (lambda *_: None)
    today = dt.date.today()
    dates = _business_days(end, days)
    total = len(dates) * len(markets) + len(markets)
    frames: dict[str, list[pd.DataFrame]] = {}
    market_of: dict[str, str] = {}
    step = 0
    for date in dates:
        parts = []
        for market in markets:
            step += 1
            progress(step, total, f"{date} {market} 시세 수집")
            # 당일 데이터는 장중에 바뀌므로 캐시하지 않는다
            df, holiday = _fetch_day(date, market, use_cache=date != today.strftime("%Y%m%d"))
            if holiday:
                continue
            for t in df.index:
                market_of[t] = market
            parts.append(df)
            time.sleep(sleep)
        if parts:
            frames[date] = parts

    if not frames:
        raise KrxError("수집된 시세가 없습니다.")
    long = pd.concat(
        {pd.Timestamp(d): pd.concat(p) for d, p in frames.items()}, names=["date", "ticker"])
    wide = {f: long[f].unstack("ticker").sort_index() for f in OHLCV_COLS.values()}

    last = max(frames)
    caps = []
    for market in markets:
        step += 1
        progress(step, total, f"{market} 시가총액 수집")
        caps.append(stock.get_market_cap_by_ticker(last, market=market))
    cap = pd.concat(caps)
    meta = pd.DataFrame(index=wide["close"].columns)
    meta["market"] = pd.Series(market_of)
    meta["market_cap"] = cap["시가총액"].reindex(meta.index)
    meta["shares"] = cap["상장주식수"].reindex(meta.index)
    progress(total, total, "종목명 매핑")
    meta["name"] = [_safe_name(stock, t) for t in meta.index]
    # 상장 전/거래정지 등 결측은 NaN 으로 둔다 (0 으로 채우면 평소 거래량이 왜곡됨)
    return Panel(**wide, meta=meta)


def _safe_name(stock, ticker: str) -> str:
    try:
        return stock.get_market_ticker_name(ticker)
    except Exception:
        return ""
