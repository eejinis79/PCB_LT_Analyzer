"""시세·수급·스크리닝 결과를 저장하는 SQLite DB.

파일 하나(기본: 프로그램 폴더의 stock_data.db)에 모두 쌓는다. 서버 설치가 필요 없고,
DB Browser for SQLite 같은 도구나 pandas 로 직접 열어볼 수 있다.

테이블
  fetch_log    (date, market, kind)        — 받아온 날짜 기록 (휴장일 포함, 재조회 방지)
  ohlcv        (date, ticker)              — 일별 전종목 시세
  flows        (date, ticker, investor)    — 일별 투자자별 순매수거래대금(원)
  market_cap   (date, ticker)              — 시가총액 · 상장주식수
  tickers      (ticker)                    — 종목명 · 시장
  screen_runs / screen_results             — 저장한 스크리닝 결과 (조건 + 종목별 지표)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys
from contextlib import closing, contextmanager
from pathlib import Path

import pandas as pd

DB_FILENAME = "stock_data.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS fetch_log (
    date TEXT NOT NULL, market TEXT NOT NULL, kind TEXT NOT NULL,
    holiday INTEGER NOT NULL DEFAULT 0, fetched_at TEXT NOT NULL,
    PRIMARY KEY (date, market, kind)
);
CREATE TABLE IF NOT EXISTS ohlcv (
    date TEXT NOT NULL, ticker TEXT NOT NULL, market TEXT NOT NULL,
    open REAL, high REAL, low REAL, close REAL, volume REAL, value REAL,
    PRIMARY KEY (date, ticker)
);
CREATE TABLE IF NOT EXISTS flows (
    date TEXT NOT NULL, ticker TEXT NOT NULL, investor TEXT NOT NULL, net_value REAL,
    PRIMARY KEY (date, ticker, investor)
);
CREATE TABLE IF NOT EXISTS market_cap (
    date TEXT NOT NULL, ticker TEXT NOT NULL, market_cap REAL, shares REAL,
    PRIMARY KEY (date, ticker)
);
CREATE TABLE IF NOT EXISTS tickers (
    ticker TEXT PRIMARY KEY, name TEXT, market TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS screen_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asof TEXT NOT NULL, saved_at TEXT NOT NULL, source TEXT, params TEXT, n_hits INTEGER
);
CREATE TABLE IF NOT EXISTS screen_results (
    run_id INTEGER NOT NULL REFERENCES screen_runs(run_id) ON DELETE CASCADE,
    rank INTEGER, ticker TEXT NOT NULL, name TEXT, market TEXT, close REAL,
    chg_pct REAL, vol_ratio REAL, value_eok REAL, frg_eok REAL, inst_eok REAL, score REAL,
    metrics TEXT,
    PRIMARY KEY (run_id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_screen_results_ticker ON screen_results(ticker);
"""

OHLCV_FIELDS = ["open", "high", "low", "close", "volume", "value"]


def program_dir() -> Path:
    """프로그램 폴더: exe 로 실행하면 exe 가 있는 폴더, 소스로 실행하면 run_screener.py 가 있는 폴더."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    here = Path(__file__).resolve().parent
    for d in (here.parent, here):  # 패키지(screener/db.py) 또는 단일 파일 배포본
        if (d / "run_screener.py").exists():
            return d
    return here


def default_db_path() -> Path:
    if os.environ.get("VSS_HOME"):  # 테스트용
        return Path(os.environ["VSS_HOME"]) / DB_FILENAME
    return program_dir() / DB_FILENAME


class StockDB:
    """스레드마다 연결을 새로 여는 얇은 래퍼 (sqlite3 연결은 스레드 간 공유 불가)."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def _conn(self):
        with closing(sqlite3.connect(self.path, timeout=30)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            with conn:  # 커밋/롤백
                yield conn

    # ------------------------------------------------------------ 수집 기록
    def fetched(self, date: str, market: str, kind: str) -> bool:
        """이미 받아 둔 날짜인지 (휴장일로 기록된 날 포함)."""
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM fetch_log WHERE date=? AND market=? AND kind=?",
                            (date, market, kind)).fetchone()
        return row is not None

    def is_holiday(self, date: str, market: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT holiday FROM fetch_log WHERE date=? AND market=? AND kind='ohlcv'",
                            (date, market)).fetchone()
        return bool(row and row[0])

    def _log(self, c, date, market, kind, holiday=False):
        c.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?,?)",
                  (date, market, kind, int(holiday), dt.datetime.now().isoformat(timespec="seconds")))

    # ------------------------------------------------------------ 시세
    def save_ohlcv(self, date: str, market: str, df: pd.DataFrame, holiday: bool = False,
                   final: bool = True):
        """df: index=종목코드, columns=OHLCV_FIELDS.
        final=False(장중 데이터)면 값은 저장하되 '받음' 기록은 남기지 않아 다음에 다시 받는다."""
        rows = [] if holiday else [
            (date, t, market, *[None if pd.isna(v) else float(v) for v in r])
            for t, r in zip(df.index, df[OHLCV_FIELDS].itertuples(index=False))]
        with self._conn() as c:
            c.execute("DELETE FROM ohlcv WHERE date=? AND market=?", (date, market))
            c.executemany("INSERT OR REPLACE INTO ohlcv VALUES (?,?,?,?,?,?,?,?,?)", rows)
            if final:
                self._log(c, date, market, "ohlcv", holiday)

    def load_ohlcv(self, dates: list[str], markets) -> pd.DataFrame:
        """long 형태 (date, ticker, market, open..value)."""
        if not dates:
            return pd.DataFrame(columns=["date", "ticker", "market", *OHLCV_FIELDS])
        q = (f"SELECT * FROM ohlcv WHERE date IN ({','.join('?' * len(dates))}) "
             f"AND market IN ({','.join('?' * len(markets))})")
        with self._conn() as c:
            return pd.read_sql_query(q, c, params=[*dates, *markets])

    # ------------------------------------------------------------ 수급
    def save_flow(self, date: str, market: str, investor: str, s: pd.Series, final: bool = True):
        rows = [(date, t, investor, float(v)) for t, v in s.items() if pd.notna(v)]
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO flows VALUES (?,?,?,?)", rows)
            if final and rows:  # 빈 응답은 기록하지 않아 다음에 다시 시도
                self._log(c, date, market, f"flow_{investor}")

    def load_flows(self, dates: list[str], investor: str) -> pd.DataFrame:
        """날짜 x 종목 순매수거래대금."""
        if not dates:
            return pd.DataFrame()
        q = (f"SELECT date, ticker, net_value FROM flows WHERE investor=? "
             f"AND date IN ({','.join('?' * len(dates))})")
        with self._conn() as c:
            df = pd.read_sql_query(q, c, params=[investor, *dates])
        if df.empty:
            return pd.DataFrame()
        wide = df.pivot(index="date", columns="ticker", values="net_value")
        wide.index = pd.to_datetime(wide.index)
        return wide.sort_index()

    # ------------------------------------------------------------ 시가총액 · 종목명
    def save_caps(self, date: str, df: pd.DataFrame):
        rows = [(date, t, float(r.market_cap), float(r.shares)) for t, r in df.iterrows()]
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO market_cap VALUES (?,?,?,?)", rows)

    def load_caps(self, date: str) -> pd.DataFrame:
        with self._conn() as c:
            df = pd.read_sql_query("SELECT ticker, market_cap, shares FROM market_cap WHERE date=?",
                                   c, params=[date])
        return df.set_index("ticker")

    def save_names(self, names: dict[str, str], markets: dict[str, str]):
        now = dt.date.today().isoformat()
        rows = [(t, n, markets.get(t), now) for t, n in names.items() if n]
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO tickers VALUES (?,?,?,?)", rows)

    def load_names(self) -> dict[str, str]:
        with self._conn() as c:
            return dict(c.execute("SELECT ticker, name FROM tickers").fetchall())

    # ------------------------------------------------------------ 스크리닝 결과
    def save_results(self, asof: str, result: pd.DataFrame, params: dict, source: str = "krx") -> int:
        """스크리닝 결과 저장. 같은 기준일·같은 조건으로 다시 저장하면 덮어쓴다. run_id 반환."""
        pj = json.dumps(params, ensure_ascii=False, sort_keys=True)
        main_cols = ["close", "chg_pct", "vol_ratio", "value_eok", "frg_eok", "inst_eok", "score"]

        def num(v):
            return None if pd.isna(v) else float(v)

        with self._conn() as c:
            c.execute("DELETE FROM screen_runs WHERE asof=? AND params=? AND source=?", (asof, pj, source))
            cur = c.execute(
                "INSERT INTO screen_runs (asof, saved_at, source, params, n_hits) VALUES (?,?,?,?,?)",
                (asof, dt.datetime.now().isoformat(timespec="seconds"), source, pj, len(result)))
            run_id = cur.lastrowid
            rows = []
            for rank, r in enumerate(result.to_dict("records"), 1):
                extra = {k: num(v) for k, v in r.items()
                         if k not in main_cols and k not in ("ticker", "name", "market")}
                rows.append((run_id, rank, r["ticker"], r.get("name"), r.get("market"),
                             *[num(r.get(k)) for k in main_cols], json.dumps(extra)))
            c.executemany("INSERT INTO screen_results VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        return run_id

    def load_results(self, asof: str | None = None) -> pd.DataFrame:
        q = ("SELECT r.asof, r.saved_at, s.* FROM screen_results s JOIN screen_runs r USING(run_id)"
             + (" WHERE r.asof=?" if asof else "") + " ORDER BY r.asof DESC, s.run_id DESC, s.rank")
        with self._conn() as c:
            return pd.read_sql_query(q, c, params=[asof] if asof else [])

    # ------------------------------------------------------------ 상태
    def stats(self) -> dict:
        with self._conn() as c:
            d0, d1, nd = c.execute(
                "SELECT MIN(date), MAX(date), COUNT(DISTINCT date) FROM ohlcv").fetchone()
            n_ohlcv = c.execute("SELECT COUNT(*) FROM ohlcv").fetchone()[0]
            n_flow_days = c.execute("SELECT COUNT(DISTINCT date) FROM flows").fetchone()[0]
            n_runs = c.execute("SELECT COUNT(*) FROM screen_runs").fetchone()[0]
        size = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.exists())
        return {"path": str(self.path), "size_mb": size / 1e6, "first": d0, "last": d1,
                "days": nd or 0, "ohlcv_rows": n_ohlcv, "flow_days": n_flow_days, "runs": n_runs}

    # ------------------------------------------------------------ 이전 캐시(pkl) 이전
    def import_pickle_cache(self, cache_dir: Path) -> int:
        """예전 버전이 저장한 .pkl 캐시를 DB 로 옮긴다. 옮긴 파일 수 반환 (원본은 그대로 둠)."""
        if not cache_dir.exists():
            return 0
        n = 0
        for p in sorted(cache_dir.glob("*.pkl")):
            parts = p.stem.split("_")
            try:
                if parts[0] == "flow" and len(parts) == 4:
                    _, date, market, inv = parts
                    if not self.fetched(date, market, f"flow_{inv}"):
                        self.save_flow(date, market, inv, pd.read_pickle(p))
                        n += 1
                elif len(parts) == 2 and parts[0].isdigit():
                    date, market = parts
                    if not self.fetched(date, market, "ohlcv"):
                        self.save_ohlcv(date, market, pd.read_pickle(p))
                        n += 1
            except Exception as e:  # noqa: BLE001 — 깨진 캐시 파일은 건너뜀
                print(f"캐시 이전 실패 {p.name}: {e}")
        return n
