#!/usr/bin/env python3
"""Volume Spike Screener — 거래량 급증 · 가격 횡보 종목 스크리너 (단일 파일 버전)

평소보다 거래량이 터졌는데 가격은 거의 움직이지 않은 KOSPI/KOSDAQ 종목을 찾는다.
외국인·기관 순매수 조건, DART 공시, 계정 정보 암호화(초기 비밀번호 0000) 포함.

설치 (Python 3.10 이상):
    pip install pandas numpy requests pykrx PySide6 matplotlib cryptography

실행:
    python volume_spike_screener.py           # KRX 실데이터 (상단 '계정'에서 KRX/DART 입력)
    python volume_spike_screener.py --demo    # 계정/네트워크 없이 데모 데이터로 화면 확인
    python volume_spike_screener.py --selftest  # 데모로 실행 → 결과 확인 후 자동 종료

이 파일은 저장소의 screener/ 패키지를 tools/build_single_py.py 로 합친 것이다.
섹션 구분선(# ====) 아래 원래 파일 경로가 적혀 있다. 자주 고칠 곳:
  - 조건 기본값 / 스크리닝 공식 : 'screener/core.py' 섹션의 ScreenParams, run_screen
  - 조건 세트(프리셋)          : 'screener/gui/main_window.py' 섹션의 PRESETS
  - 화면 색상 / 글꼴            : 'screener/gui/theme.py' 섹션
  - KRX 수집 방식              : 'screener/data/krx.py' 섹션
"""
from __future__ import annotations



# ============================================================================
# screener/paths.py
# ============================================================================
"""사용자 데이터(계정 금고, 시세 캐시, 로그) 저장 위치.

실행파일(PyInstaller)로 배포해도 쓰기 가능한 위치여야 하므로 프로젝트 폴더가 아닌
OS 별 사용자 폴더를 쓴다. 환경변수 VSS_HOME 으로 바꿀 수 있다 (테스트용).
"""

import os
import sys
from pathlib import Path

APP_NAME = "VolumeSpikeScreener"


def app_dir() -> Path:
    if os.environ.get("VSS_HOME"):
        base = Path(os.environ["VSS_HOME"])
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def cache_dir(*parts: str) -> Path:
    return app_dir().joinpath("cache", *parts)


# ============================================================================
# screener/core.py
# ============================================================================
"""거래량 급증 + 가격 횡보 스크리닝 로직 (GUI/데이터 소스와 독립된 순수 pandas 코드).

용어
  - 기준 구간(base window): '평소' 거래량을 계산하는 과거 구간
  - 최근 구간(recent window): 거래량이 '터졌는지' 확인하는 최근 N 영업일
  - 둘은 겹치지 않는다: [ ... base ... ][ recent ]  (마지막 날 = 기준일)
"""

from dataclasses import asdict, dataclass, field, fields

import numpy as np
import pandas as pd

FIELDS = ("open", "high", "low", "close", "volume", "value")
FLOW_DAYS = 10  # 투자자별 순매수를 받아두는 최근 영업일 수 (수급 기간 슬라이더 최대값)
INVESTORS = ("foreign", "inst")  # 외국인, 기관합계
FLOW_MODES = {
    "both": "외국인 + 기관 모두 순매수",
    "either": "외국인 또는 기관 순매수",
    "foreign": "외국인 순매수",
    "inst": "기관 순매수",
    "sum": "외국인+기관 합산 순매수",
}


@dataclass
class Panel:
    """날짜(index) x 종목코드(columns) 형태의 시세 패널."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    value: pd.DataFrame  # 거래대금(원)
    # index=종목코드, columns=[name, market, market_cap, shares]
    meta: pd.DataFrame = field(default_factory=pd.DataFrame)
    # 투자자별 일별 순매수거래대금(원): {"foreign": 날짜 x 종목, "inst": 날짜 x 종목}
    # 최근 FLOW_DAYS 영업일만 있다. 수집 실패 시 비어 있음.
    flows: dict = field(default_factory=dict)

    @property
    def has_flows(self) -> bool:
        return all(k in self.flows for k in INVESTORS)

    @property
    def dates(self) -> pd.DatetimeIndex:
        return self.close.index

    def history(self, ticker: str) -> pd.DataFrame:
        return pd.DataFrame({f: getattr(self, f)[ticker] for f in FIELDS})


@dataclass
class ScreenParams:
    # 거래량 조건
    base_days: int = 20            # 평소 거래량 계산 구간 (영업일)
    recent_days: int = 1           # 거래량 급증 확인 구간 (영업일)
    base_stat: str = "median"      # "median" | "mean"
    vol_ratio_min: float = 3.0     # 최근 거래량 / 평소 거래량 최소 배수
    vol_ratio_max: float = 100.0   # 상한 (이상치 제거용)

    # 가격 조건
    price_chg_max: float = 2.0     # 최근 구간 누적 등락률 절대값 상한 (%)
    daily_chg_max: float = 3.0     # 최근 구간 일별 등락률 절대값 상한 (%)
    range_max: float = 6.0         # 최근 구간 일중 변동폭 (고가-저가)/전일종가 상한 (%)
    use_box: bool = False          # 기준 구간 박스권(횡보) 조건 사용 여부
    box_range_max: float = 15.0    # 기준 구간 (최고종가-최저종가)/평균종가 상한 (%)

    # 유동성/규모 필터
    min_value_eok: float = 5.0     # 최근 구간 평균 거래대금 하한 (억원)
    min_price: int = 1000          # 종가 하한 (원)
    mcap_min_eok: float = 0.0      # 시가총액 하한 (억원, 0=제한 없음)
    mcap_max_eok: float = 0.0      # 시가총액 상한 (억원, 0=제한 없음)

    # 시장/제외
    markets: tuple = ("KOSPI", "KOSDAQ")
    exclude_preferred: bool = True
    exclude_spac: bool = True

    # 수급 (외국인·기관 순매수)
    flow_enabled: bool = False
    flow_mode: str = "either"      # FLOW_MODES 의 키
    flow_days: int = 3             # 순매수 집계 기간 (기준일 포함 최근 N 영업일)
    flow_min_eok: float = 0.0      # 기간 순매수금액 하한 (억원). 0 이면 '순매수(>0)'만 요구
    flow_min_buy_days: int = 1     # 기간 중 순매수한 날 수 하한

    # 추가 필터
    pos_enabled: bool = False      # 위치 필터: 최근 pos_days 저점 대비 상승률 상한 (바닥권만)
    pos_days: int = 60
    pos_max_pct: float = 30.0
    obv_enabled: bool = False      # 매집(OBV) 필터: 기준+최근 구간 OBV 추세 하한
    obv_min: float = 0.05          # -1(분배) ~ +1(매집)
    exclude_expiry: bool = False   # 옵션 만기일(매월 둘째 목요일) 신호 제외

    # 점수: None 이면 기본 공식, 백테스트 '요인 분석'에서 학습한 가중치를 넣으면 그것을 사용
    # {특징이름: [가중치, 평균, 표준편차]}
    score_weights: dict | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["markets"] = list(self.markets)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ScreenParams":
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known}
        if "markets" in kw:
            kw["markets"] = tuple(kw["markets"])
        return cls(**kw)

    @property
    def required_days(self) -> int:
        # +1: 최근 구간 직전 종가(누적 등락률 기준점)
        need = self.base_days + self.recent_days + 1
        if self.pos_enabled:
            need = max(need, self.pos_days)
        return need


RESULT_COLUMNS = [
    "ticker", "name", "market", "close", "chg_pct", "max_daily_chg", "max_range",
    "vol_ratio", "recent_vol", "base_vol", "value_eok", "frg_eok", "inst_eok",
    "frg_days", "inst_days", "mcap_eok", "box_pct", "pos_pct", "obv_trend", "score",
]

# 학습 점수에 쓰는 특징 (features() 결과 컬럼으로 계산)
LEARN_FEATURES = {
    "log_vol_ratio": "거래량 배수(log)",
    "abs_chg": "기간 등락률 절대값",
    "max_range": "일중 변동폭",
    "log_value": "거래대금(log)",
    "log_mcap": "시가총액(log)",
    "flow_ratio": "외국인+기관 순매수 / 거래대금",
    "pos_pct": "저점 대비 위치",
    "obv_trend": "OBV 매집 추세",
    "box_pct": "기준구간 박스폭",
}


def is_preferred(ticker: str) -> bool:
    # 보통주 코드는 끝자리 0, 우선주는 5/7/9/K 등
    return not ticker.endswith("0")


def flow_stats(panel: Panel, days: int) -> pd.DataFrame:
    """최근 days 영업일 투자자별 순매수 합계(억)와 순매수일 수. 수급 데이터가 없으면 빈 프레임."""
    if not panel.has_flows:
        return pd.DataFrame(columns=["frg_eok", "inst_eok", "frg_days", "inst_days",
                                     "sum_eok", "sum_days"])
    # 기준일(패널 마지막 날) 이전 데이터만, 최근 days 일
    last = panel.dates[-1]
    f = panel.flows["foreign"].loc[:last].iloc[-days:]
    i = panel.flows["inst"].loc[:last].iloc[-days:]
    f, i = f.align(i, join="outer", fill_value=0)
    both = f + i
    return pd.DataFrame({
        "frg_eok": f.sum() / 1e8, "inst_eok": i.sum() / 1e8,
        "frg_days": (f > 0).sum(), "inst_days": (i > 0).sum(),
        "sum_eok": both.sum() / 1e8, "sum_days": (both > 0).sum(),
    })


def flow_mask(fs: pd.DataFrame, p: ScreenParams) -> pd.Series:
    def ok(prefix: str) -> pd.Series:
        amt, days = fs[f"{prefix}_eok"], fs[f"{prefix}_days"]
        return (amt > 0) & (amt >= p.flow_min_eok) & (days >= p.flow_min_buy_days)

    frg, inst = ok("frg"), ok("inst")
    return {
        "both": frg & inst, "either": frg | inst, "foreign": frg, "inst": inst, "sum": ok("sum"),
    }[p.flow_mode]


def second_thursday(year: int, month: int) -> pd.Timestamp:
    offset = (3 - pd.Timestamp(year=year, month=month, day=1).weekday()) % 7  # 목요일 = 3
    return pd.Timestamp(year=year, month=month, day=1 + offset + 7)


def is_option_expiry(day: pd.Timestamp, trading_dates) -> bool:
    """매월 둘째 목요일(휴장이면 직전 거래일) 여부."""
    day = pd.Timestamp(day).normalize()
    nominal = second_thursday(day.year, day.month)
    dates = pd.DatetimeIndex(trading_dates).normalize()
    if nominal in dates or nominal > dates[-1]:
        return day == nominal
    prior = dates[(dates < nominal) & (dates.month == nominal.month)]
    return len(prior) > 0 and day == prior[-1]


def features(panel: Panel, p: ScreenParams) -> pd.DataFrame:
    """기준일(패널 마지막 날) 기준 전 종목 지표. 필터는 적용하지 않는다."""
    n = len(panel.dates)
    recent = slice(n - p.recent_days, n)
    base_start = max(0, n - p.recent_days - p.base_days)
    base = slice(base_start, n - p.recent_days)
    anchor = n - p.recent_days - 1  # 최근 구간 직전일

    vol = panel.volume
    close = panel.close

    base_vol_df = vol.iloc[base]
    base_vol = base_vol_df.median() if p.base_stat == "median" else base_vol_df.mean()
    recent_vol = vol.iloc[recent].mean()
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = recent_vol / base_vol.replace(0, np.nan)

    last_close = close.iloc[-1]
    anchor_close = close.iloc[anchor].replace(0, np.nan)
    chg_pct = (last_close / anchor_close - 1) * 100

    prev_close = close.shift(1).replace(0, np.nan)
    daily_chg = ((close / prev_close - 1) * 100).iloc[recent]
    day_range = ((panel.high - panel.low) / prev_close * 100).iloc[recent]

    base_close = close.iloc[base].replace(0, np.nan)
    box_pct = (base_close.max() - base_close.min()) / base_close.mean() * 100

    # 위치: 최근 pos_days 종가 최저 대비 현재 상승률(%)
    low_close = close.iloc[-p.pos_days:].replace(0, np.nan).min()
    pos_pct = (last_close / low_close - 1) * 100

    # OBV 추세: (기준+최근 구간 OBV 변화) / 같은 구간 거래량 합 → -1 ~ +1
    win = slice(base_start, n)
    direction = np.sign(close.diff()).iloc[win].fillna(0)
    v = vol.iloc[win]
    with np.errstate(divide="ignore", invalid="ignore"):
        obv_trend = (direction * v).sum() / v.sum().replace(0, np.nan)

    df = pd.DataFrame({
        "close": last_close,
        "chg_pct": chg_pct,
        "max_daily_chg": daily_chg.abs().max(),
        "max_range": day_range.max(),
        "vol_ratio": vol_ratio,
        "recent_vol": recent_vol,
        "base_vol": base_vol,
        "value_eok": panel.value.iloc[recent].mean() / 1e8,
        "box_pct": box_pct,
        "pos_pct": pos_pct,
        "obv_trend": obv_trend,
        # 최근 구간에 거래정지(거래량 0)일이 있으면 제외
        "halted": (vol.iloc[recent] == 0).any(),
    })
    df.index.name = "ticker"

    meta = panel.meta.reindex(df.index)
    df["name"] = meta.get("name", pd.Series(index=df.index, dtype=object)).fillna("")
    df["market"] = meta.get("market", pd.Series(index=df.index, dtype=object)).fillna("")
    mcap = meta.get("market_cap", pd.Series(index=df.index, dtype=float))
    df["mcap_eok"] = mcap / 1e8
    fs = flow_stats(panel, p.flow_days).reindex(df.index)
    for c in fs.columns:
        df[c] = fs[c]
    return df


def filter_mask(df: pd.DataFrame, p: ScreenParams, expiry=False) -> pd.Series:
    """features() 결과에 조건을 적용한 bool 마스크. expiry 는 bool 또는 행별 bool 배열."""
    m = (
        ~df["halted"].astype(bool)
        & df["market"].isin(p.markets)
        & df["vol_ratio"].between(p.vol_ratio_min, p.vol_ratio_max)
        & (df["chg_pct"].abs() <= p.price_chg_max)
        & (df["max_daily_chg"] <= p.daily_chg_max)
        & (df["max_range"] <= p.range_max)
        & (df["value_eok"] >= p.min_value_eok)
        & (df["close"] >= p.min_price)
    )
    if p.use_box:
        m &= df["box_pct"] <= p.box_range_max
    if p.mcap_min_eok > 0:
        m &= df["mcap_eok"] >= p.mcap_min_eok
    if p.mcap_max_eok > 0:
        m &= df["mcap_eok"] <= p.mcap_max_eok
    if p.exclude_preferred:
        m &= ~df.index.to_series().map(is_preferred)
    if p.exclude_spac:
        m &= ~df["name"].str.contains("스팩", na=False)
    if p.flow_enabled:
        # 수급 데이터가 없는 종목/패널은 조건을 만족하지 못한 것으로 본다
        m &= flow_mask(df, p).fillna(False).astype(bool)
    if p.pos_enabled:
        m &= df["pos_pct"] <= p.pos_max_pct
    if p.obv_enabled:
        m &= df["obv_trend"] >= p.obv_min
    if p.exclude_expiry:
        ex = np.asarray(expiry, dtype=bool)
        m &= ~(np.full(len(df), bool(ex)) if ex.ndim == 0 else ex)
    return m.fillna(False).astype(bool)


def learn_matrix(df: pd.DataFrame, p: ScreenParams) -> pd.DataFrame:
    """LEARN_FEATURES 값 (결측은 0 또는 중립값)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        net = sum(pd.to_numeric(df[c], errors="coerce").fillna(0.0) if c in df else 0.0
                  for c in ("frg_eok", "inst_eok"))
        flow = net / (df["value_eok"] * max(1, p.flow_days)).replace(0, np.nan)
        return pd.DataFrame({
            "log_vol_ratio": np.log(df["vol_ratio"].clip(lower=1e-6)),
            "abs_chg": df["chg_pct"].abs(),
            "max_range": df["max_range"],
            "log_value": np.log(df["value_eok"].clip(lower=1e-6)),
            "log_mcap": np.log(df["mcap_eok"].clip(lower=1e-6)),
            "flow_ratio": flow.fillna(0).clip(-1, 1),
            "pos_pct": df["pos_pct"],
            "obv_trend": df["obv_trend"],
            "box_pct": df["box_pct"],
        }, index=df.index)


def score(df: pd.DataFrame, p: ScreenParams) -> pd.Series:
    if p.score_weights:
        X = learn_matrix(df, p)
        s = pd.Series(0.0, index=df.index)
        for f, (w, mu, sd) in p.score_weights.items():
            if f in X:
                s += w * ((X[f] - mu) / (sd or 1)).fillna(0)
        return s * 100
    # 기본: 거래량 배수가 클수록, 가격 움직임이 작을수록 높은 점수
    return np.log2(df["vol_ratio"]) * 10 / (1 + df["chg_pct"].abs() + 0.5 * df["max_range"])


def run_screen(panel: Panel, p: ScreenParams) -> pd.DataFrame:
    """조건에 맞는 종목을 score 내림차순으로 반환."""
    n = len(panel.dates)
    if n < p.recent_days + 2:
        return pd.DataFrame(columns=RESULT_COLUMNS)
    df = features(panel, p)
    expiry = p.exclude_expiry and is_option_expiry(panel.dates[-1], panel.dates)
    out = df[filter_mask(df, p, expiry)].copy()
    out["score"] = score(out, p)
    out = out.reset_index().sort_values("score", ascending=False)
    return out[RESULT_COLUMNS].reset_index(drop=True)


# ============================================================================
# screener/vault.py
# ============================================================================
"""계정 정보 금고: KRX/DART 계정·비밀번호·API 키를 '계정 비밀번호'로 암호화해 저장한다.

- 금고 파일이 없으면 초기 비밀번호는 0000 이며, 첫 접속 시 비밀번호 변경이 강제된다.
- 키 유도: PBKDF2-HMAC-SHA256(비밀번호, 랜덤 salt, 600,000회) → Fernet(AES-128-CBC + HMAC).
  비밀번호가 틀리면 복호화(무결성 검증)가 실패하므로 별도 해시를 저장하지 않는다.
- 비밀번호를 잊으면 복구할 수 없다. reset() 으로 금고를 지우고 0000 으로 되돌린다.
"""

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


DEFAULT_PASSWORD = "0000"
MIN_PASSWORD_LEN = 4
ITERATIONS = 600_000


@dataclass
class Credentials:
    krx_id: str = ""
    krx_pw: str = ""
    dart_id: str = ""
    dart_pw: str = ""
    dart_key: str = ""  # OpenDART API 인증키 (API 호출에는 이것만 사용)
    telegram_token: str = ""  # 텔레그램 봇 토큰 (@BotFather 에서 발급)
    telegram_chat: str = ""   # 받을 채팅 ID

    @classmethod
    def from_dict(cls, d: dict) -> "Credentials":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})


class VaultError(Exception):
    pass


class WrongPassword(VaultError):
    pass


def _fernet(password: str, salt: bytes) -> Fernet:
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS, dklen=32)
    return Fernet(base64.urlsafe_b64encode(key))


def validate_new_password(new: str, confirm: str) -> str | None:
    """문제가 있으면 오류 메시지, 없으면 None."""
    if new != confirm:
        return "새 비밀번호가 서로 일치하지 않습니다."
    if len(new) < MIN_PASSWORD_LEN:
        return f"비밀번호는 {MIN_PASSWORD_LEN}자 이상이어야 합니다."
    if new == DEFAULT_PASSWORD:
        return "초기 비밀번호(0000)는 사용할 수 없습니다."
    return None


class Vault:
    def __init__(self, path: Path | None = None):
        self.path = path or app_dir() / "vault.json"

    def exists(self) -> bool:
        return self.path.exists()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise VaultError(f"계정 파일을 읽을 수 없습니다: {e}") from e

    @property
    def must_change_password(self) -> bool:
        return not self.exists() or self._read().get("must_change", True)

    def unlock(self, password: str) -> Credentials:
        """비밀번호가 맞으면 저장된 계정 정보를 반환, 틀리면 WrongPassword."""
        if not self.exists():
            if password != DEFAULT_PASSWORD:
                raise WrongPassword("비밀번호가 올바르지 않습니다. (초기 비밀번호: 0000)")
            return Credentials()
        data = self._read()
        try:
            raw = _fernet(password, base64.b64decode(data["salt"])).decrypt(data["token"].encode())
        except InvalidToken:
            raise WrongPassword("비밀번호가 올바르지 않습니다.") from None
        return Credentials.from_dict(json.loads(raw))

    def save(self, password: str, creds: Credentials, must_change: bool = False) -> None:
        salt = os.urandom(16)
        token = _fernet(password, salt).encrypt(json.dumps(asdict(creds)).encode())
        payload = {"version": 1, "kdf": "pbkdf2-sha256", "iterations": ITERATIONS,
                   "salt": base64.b64encode(salt).decode(), "token": token.decode(),
                   "must_change": must_change}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(self.path)

    def change_password(self, old: str, new: str, confirm: str | None = None) -> Credentials:
        err = validate_new_password(new, new if confirm is None else confirm)
        if err:
            raise VaultError(err)
        if new == old:
            raise VaultError("새 비밀번호가 현재 비밀번호와 같습니다.")
        creds = self.unlock(old)
        self.save(new, creds)
        return creds

    def reset(self) -> None:
        """금고 삭제 → 초기 비밀번호 0000, 저장된 계정 정보는 모두 사라진다."""
        if self.exists():
            self.path.unlink()


# ============================================================================
# screener/db.py
# ============================================================================
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
CREATE TABLE IF NOT EXISTS index_ohlcv (
    date TEXT NOT NULL, code TEXT NOT NULL, close REAL,
    PRIMARY KEY (date, code)
);
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
            try:
                return None if pd.isna(v) else float(v)
            except (TypeError, ValueError):
                return None

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

    # ------------------------------------------------------------ 기간 조회 (백테스트용)
    def trade_dates(self, start: str | None = None, end: str | None = None) -> list[str]:
        with self._conn() as c:
            rows = c.execute("SELECT DISTINCT date FROM ohlcv WHERE date >= ? AND date <= ? ORDER BY date",
                             (start or "00000000", end or "99999999")).fetchall()
        return [r[0] for r in rows]

    def load_ohlcv_range(self, start: str, end: str, markets) -> pd.DataFrame:
        q = (f"SELECT * FROM ohlcv WHERE date >= ? AND date <= ? "
             f"AND market IN ({','.join('?' * len(markets))})")
        with self._conn() as c:
            return pd.read_sql_query(q, c, params=[start, end, *markets])

    def load_flows_range(self, start: str, end: str, investor: str) -> pd.DataFrame:
        with self._conn() as c:
            df = pd.read_sql_query(
                "SELECT date, ticker, net_value FROM flows WHERE investor=? AND date >= ? AND date <= ?",
                c, params=[investor, start, end])
        if df.empty:
            return pd.DataFrame()
        wide = df.pivot(index="date", columns="ticker", values="net_value")
        wide.index = pd.to_datetime(wide.index)
        return wide.sort_index()

    def latest_shares(self) -> pd.Series:
        """종목별 가장 최근 상장주식수 (과거 시가총액 근사용)."""
        with self._conn() as c:
            df = pd.read_sql_query(
                "SELECT m.ticker, m.shares FROM market_cap m JOIN "
                "(SELECT ticker, MAX(date) d FROM market_cap GROUP BY ticker) x "
                "ON m.ticker = x.ticker AND m.date = x.d", c)
        return df.set_index("ticker")["shares"]

    def ticker_markets(self) -> pd.Series:
        with self._conn() as c:
            df = pd.read_sql_query(
                "SELECT ticker, market FROM ohlcv WHERE date = (SELECT MAX(date) FROM ohlcv) "
                "UNION SELECT ticker, market FROM tickers WHERE market IS NOT NULL", c)
        return df.drop_duplicates("ticker").set_index("ticker")["market"]

    def save_index(self, code: str, s: pd.Series):
        """s: index=YYYYMMDD, 값=종가."""
        rows = [(d, code, float(v)) for d, v in s.items() if pd.notna(v)]
        with self._conn() as c:
            c.executemany("INSERT OR REPLACE INTO index_ohlcv VALUES (?,?,?)", rows)

    def load_index(self, code: str) -> pd.Series:
        with self._conn() as c:
            df = pd.read_sql_query("SELECT date, close FROM index_ohlcv WHERE code=? ORDER BY date",
                                   c, params=[code])
        s = df.set_index("date")["close"]
        s.index = pd.to_datetime(s.index)
        return s

    def list_runs(self) -> pd.DataFrame:
        with self._conn() as c:
            return pd.read_sql_query(
                "SELECT run_id, asof, saved_at, source, params, n_hits FROM screen_runs "
                "ORDER BY asof DESC, run_id DESC", c)

    def load_prices_since(self, start: str) -> pd.DataFrame:
        """start(YYYYMMDD) 이후 전종목 시세 (long). 성과 계산용."""
        with self._conn() as c:
            return pd.read_sql_query(
                "SELECT date, ticker, open, high, low, close FROM ohlcv WHERE date >= ?", c, params=[start])

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


# ============================================================================
# screener/performance.py
# ============================================================================
"""저장한 스크리닝 신호의 이후 수익률 계산.

신호일(기준일) 이후 DB 에 쌓인 시세로 N 영업일 뒤 수익률, 기간 중 최고/최저,
같은 기간 시장(전종목 동일가중 평균) 수익률 대비 초과수익을 구한다.
데이터는 매일 '데이터 불러오기'를 할 때 DB 에 쌓이므로, 신호 이후 날짜가 쌓일수록 칸이 채워진다.
"""

import numpy as np
import pandas as pd

HORIZONS = (1, 5, 10, 20)
PATH_DAYS = 20          # 평균 누적수익률 곡선 / 최고·최저 계산 구간
ENTRY_MODES = {"close": "신호일 종가", "next_open": "다음날 시가"}


def _wide(prices: pd.DataFrame, field: str) -> pd.DataFrame:
    w = prices.pivot(index="date", columns="ticker", values=field).sort_index()
    return w.replace(0, np.nan)  # 거래정지(0) 는 결측으로


def compute(signals: pd.DataFrame, prices: pd.DataFrame, entry: str = "close",
            horizons=HORIZONS) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    signals: asof, ticker (+ name, market, vol_ratio, chg_pct 등) — db.load_results() 형태
    prices : date, ticker, open, high, low, close — db.load_prices_since() 형태

    반환 (detail, summary, path)
      detail : 신호별 진입가 · r{N} · bench{N} · max_gain · max_loss · last_ret · held
      summary: 기간별 표본수 · 평균 · 중앙값 · 승률 · 시장평균 · 초과수익
      path   : day(1..20) 별 신호 평균 누적수익률과 시장 평균
    """
    cols = [f"r{h}" for h in horizons]
    if signals.empty or prices.empty:
        empty = signals.assign(**{c: np.nan for c in cols})
        return empty, pd.DataFrame(), pd.DataFrame()

    close, open_ = _wide(prices, "close"), _wide(prices, "open")
    high, low = _wide(prices, "high"), _wide(prices, "low")
    dates = list(close.index)
    pos = {d: i for i, d in enumerate(dates)}
    n = len(dates)

    # 시장 평균: 날짜 i 에서 N 일 뒤 전종목 동일가중 평균 수익률 (진입 기준: 종가)
    max_h = max(max(horizons), PATH_DAYS)
    bench = {k: (close.shift(-k) / close - 1).mean(axis=1) for k in range(1, max_h + 1)}

    rows = []
    for sig in signals.itertuples(index=False):
        d, t = str(sig.asof), sig.ticker
        rec = {"asof": d, "ticker": t}
        i = pos.get(d)
        if i is None or t not in close.columns:
            rows.append(rec)
            continue
        if entry == "next_open":
            e_idx = i + 1
            entry_px = open_[t].iloc[e_idx] if e_idx < n else np.nan
        else:
            e_idx = i
            entry_px = close[t].iloc[i]
        rec["entry"] = entry_px
        if pd.isna(entry_px):
            rows.append(rec)
            continue
        for h in horizons:
            j = i + h
            if j < n and j >= e_idx:
                rec[f"r{h}"] = close[t].iloc[j] / entry_px - 1
                rec[f"bench{h}"] = bench[h].iloc[i]
        window = slice(e_idx + (0 if entry == "next_open" else 1), min(i + PATH_DAYS, n - 1) + 1)
        hi, lo = high[t].iloc[window], low[t].iloc[window]
        if len(hi.dropna()):
            rec["max_gain"] = hi.max() / entry_px - 1
            rec["max_loss"] = lo.min() / entry_px - 1
        rec["last_ret"] = close[t].iloc[-1] / entry_px - 1
        rec["held"] = n - 1 - i
        for k in range(1, PATH_DAYS + 1):
            if i + k < n:
                rec[f"p{k}"] = close[t].iloc[i + k] / entry_px - 1
                rec[f"pb{k}"] = bench[k].iloc[i]
        rows.append(rec)

    # 신호 행과 1:1 (같은 날 같은 종목이 여러 번 있어도 행이 불어나지 않게 위치로 붙임)
    perf = pd.DataFrame(rows, index=range(len(rows))).drop(columns=["asof", "ticker"])
    base = signals.reset_index(drop=True).assign(asof=signals["asof"].astype(str).values)
    detail = pd.concat([base.drop(columns=[c for c in perf.columns if c in base.columns]), perf], axis=1)
    for c in cols + [f"bench{h}" for h in horizons] + ["entry", "max_gain", "max_loss", "last_ret", "held"]:
        if c not in detail:
            detail[c] = np.nan

    summary = []
    for h in horizons:
        r = detail[f"r{h}"].dropna()
        b = detail.loc[r.index, f"bench{h}"]
        summary.append({
            "horizon": h, "n": len(r),
            "mean": r.mean() if len(r) else np.nan,
            "median": r.median() if len(r) else np.nan,
            "win_rate": (r > 0).mean() if len(r) else np.nan,
            "bench": b.mean() if len(r) else np.nan,
            "excess": (r - b).mean() if len(r) else np.nan,
        })
    summary = pd.DataFrame(summary)

    path = pd.DataFrame({
        "day": range(1, PATH_DAYS + 1),
        "signal": [detail.get(f"p{k}", pd.Series(dtype=float)).mean() for k in range(1, PATH_DAYS + 1)],
        "bench": [detail.get(f"pb{k}", pd.Series(dtype=float)).mean() for k in range(1, PATH_DAYS + 1)],
        "n": [detail.get(f"p{k}", pd.Series(dtype=float)).notna().sum() for k in range(1, PATH_DAYS + 1)],
    })
    detail = detail.drop(columns=[c for c in detail.columns if c[:1] == "p" and c[1:].isdigit()
                                  or c.startswith("pb") and c[2:].isdigit()])
    return detail, summary, path


def from_db(db, run_ids=None, entry: str = "close", dedupe: bool = True):
    """DB 에서 신호와 시세를 읽어 compute(). run_ids=None 이면 전체."""
    sig = db.load_results()
    if run_ids is not None:
        sig = sig[sig["run_id"].isin(run_ids)]
    if dedupe and not sig.empty:  # 같은 날 같은 종목이 여러 조건에서 잡혀도 한 번만
        sig = sig.sort_values(["asof", "run_id", "rank"]).drop_duplicates(["asof", "ticker"])
    if sig.empty:
        return compute(sig, pd.DataFrame(), entry)
    prices = db.load_prices_since(str(sig["asof"].min()))
    return compute(sig, prices, entry)


# ============================================================================
# screener/data/demo.py
# ============================================================================
"""오프라인 데모/테스트용 합성 시세. 일부 종목에 '거래량 급증 + 가격 횡보' 패턴을 심어 둔다."""

import datetime as dt

import numpy as np
import pandas as pd


_NAMES = ["가온", "누리", "다솜", "라온", "마루", "바다", "새봄", "아라", "자람", "하늘",
          "한빛", "윤슬", "온새", "미르", "도담", "별하", "솔찬", "여울", "은결", "푸름"]
_SUFFIX = ["전자", "바이오", "화학", "소재", "테크", "제약", "에너지", "로보틱스", "반도체", "중공업"]


def demo_load_panel(end: dt.date | None = None, days: int = 60, n_tickers: int = 400,
               seed: int = 7, progress=None) -> Panel:
    rng = np.random.default_rng(seed)
    end = end or dt.date.today()
    dates = pd.bdate_range(end=end, periods=days)
    tickers = [f"{100000 + i * 10:06d}" for i in range(n_tickers)]

    rets = rng.normal(0, 0.018, (days, n_tickers))
    base_px = rng.choice([1500, 4000, 9000, 25000, 70000], n_tickers) * rng.uniform(0.7, 1.4, n_tickers)
    close = base_px * np.exp(np.cumsum(rets, axis=0))
    base_vol = rng.lognormal(11, 1.2, n_tickers)
    volume = base_vol * rng.lognormal(0, 0.35, (days, n_tickers))

    # 패턴 심기: 마지막 1~3일 거래량 3~10배, 가격은 거의 그대로
    planted = rng.choice(n_tickers, 25, replace=False)
    for j in planted:
        k = rng.integers(1, 4)
        volume[-k:, j] *= rng.uniform(3, 10)
        close[-k:, j] = close[-k - 1, j] * (1 + rng.normal(0, 0.004, k))
    # 대조군: 거래량 급증 + 급등
    for j in rng.choice(np.setdiff1d(np.arange(n_tickers), planted), 15, replace=False):
        volume[-1, j] *= rng.uniform(4, 12)
        close[-1, j] = close[-2, j] * rng.uniform(1.08, 1.29)

    close = np.round(close)
    prev = np.vstack([close[:1], close[:-1]])
    spread = np.abs(rng.normal(0, 0.012, close.shape)) + np.abs(close / prev - 1) / 2
    high = np.round(np.maximum(close, prev) * (1 + spread / 2))
    low = np.round(np.minimum(close, prev) * (1 - spread / 2))
    open_ = np.round((prev + close) / 2)
    volume = np.round(volume)

    df = lambda a: pd.DataFrame(a, index=dates, columns=tickers)  # noqa: E731
    shares = rng.integers(5_000_000, 200_000_000, n_tickers)
    meta = pd.DataFrame({
        "name": [f"{_NAMES[i % 20]}{_SUFFIX[(i // 20) % 10]}" for i in range(n_tickers)],
        "market": np.where(np.arange(n_tickers) % 3 == 0, "KOSPI", "KOSDAQ"),
        "market_cap": close[-1] * shares,
        "shares": shares,
    }, index=tickers)
    # 투자자별 순매수(원): 평소엔 잡음, 패턴 종목 절반은 외국인/기관이 최근에 받아간 것으로
    value = volume * close
    flow_idx = dates[-FLOW_DAYS:]
    frg = rng.normal(0, 0.04, (FLOW_DAYS, n_tickers)) * value[-FLOW_DAYS:]
    inst = rng.normal(0, 0.03, (FLOW_DAYS, n_tickers)) * value[-FLOW_DAYS:]
    accum = planted[: len(planted) // 2]
    frg[-3:, accum] = np.abs(frg[-3:, accum]) + 0.08 * value[-3:, accum]
    inst[-3:, accum[::2]] = np.abs(inst[-3:, accum[::2]]) + 0.05 * value[-3:, accum[::2]]
    flows = {"foreign": pd.DataFrame(np.round(frg), index=flow_idx, columns=tickers),
             "inst": pd.DataFrame(np.round(inst), index=flow_idx, columns=tickers)}
    if progress:
        progress(1, 1, "데모 데이터 생성 완료")
    return Panel(open=df(open_), high=df(high), low=df(low), close=df(close),
                 volume=df(volume), value=df(value), meta=meta, flows=flows)


# ============================================================================
# screener/data/krx.py
# ============================================================================
"""KRX(data.krx.co.kr) 시세 수집 — pykrx 사용.

2025년 이후 KRX 정보데이터시스템은 로그인 세션이 필요하므로 KRX 계정으로 로그인한 뒤
날짜별 '전종목 시세'를 받아 Panel 로 합친다. (종목별 조회보다 호출 수가 훨씬 적다)
받은 데이터는 SQLite DB(screener/db.py, 기본: 프로그램 폴더의 stock_data.db)에 쌓아
재실행 시 새 날짜만 받는다.
"""

import datetime as dt
import os
import time
from typing import Callable

import pandas as pd


# 예전 버전의 .pkl 캐시 위치 (DB 로 이전할 때만 사용)
LEGACY_CACHE_DIR = cache_dir("krx")
FINAL_HOUR = 18  # 이 시각 이후의 당일 데이터는 확정으로 보고 DB 에 고정
INDEX_CODES = {"KOSPI": "1001", "KOSDAQ": "2001"}
# pykrx 투자자 구분 이름
INVESTOR_NAMES = {"foreign": "외국인", "inst": "기관합계"}
OHLCV_COLS = {"시가": "open", "고가": "high", "저가": "low", "종가": "close",
              "거래량": "volume", "거래대금": "value"}

Progress = Callable[[int, int, str], None]


class KrxError(RuntimeError):
    pass


def krx_login(krx_id: str, krx_pw: str) -> None:
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


def krx_load_panel(end: dt.date, days: int, markets=("KOSPI", "KOSDAQ"),
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
    try:  # 시장 국면(지수 vs 이동평균) 판단용 지수 — 2회 호출
        fetch_index(db, (end - dt.timedelta(days=150)).strftime("%Y%m%d"), trade_dates[-1])
    except Exception as e:  # noqa: BLE001
        print(f"지수 수집 실패 (전종목 평균으로 대체): {e}")
    progress(total, total, f"완료 — KRX 신규 조회 {fetched}일분, 나머지는 DB 사용")
    # 상장 전/거래정지 등 결측은 NaN 으로 둔다 (0 으로 채우면 평소 거래량이 왜곡됨)
    panel = Panel(**wide, meta=meta, flows=flows)
    panel.index_close = {m: db.load_index(c) for m, c in INDEX_CODES.items()}
    return panel


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


BACKFILL_DAYS_PER_YEAR = 248


def fetch_index(db: StockDB, start: str, end: str) -> int:
    """KOSPI/KOSDAQ 지수 종가를 DB 에 저장 (시장 국면 필터용). 저장한 일수 반환."""
    from pykrx import stock

    n = 0
    for code in INDEX_CODES.values():
        df = stock.get_index_ohlcv_by_date(start, end, code, name_display=False)
        if df is None or df.empty:
            continue
        s = df["종가"]
        s.index = [pd.Timestamp(d).strftime("%Y%m%d") for d in s.index]
        db.save_index(code, s)
        n = max(n, len(s))
    return n


def krx_backfill(db: StockDB, years: int = 3, end: dt.date | None = None, markets=("KOSPI", "KOSDAQ"),
             with_flows: bool = True, progress: Progress | None = None,
             cancelled=lambda: False, sleep: float = 0.3) -> dict:
    """과거 years 년치 전종목 시세(상장폐지 종목 포함)를 DB 에 채운다.

    이미 받은 날짜는 건너뛰므로 중간에 멈춰도 다시 실행하면 이어서 받는다.
    날짜 하나가 실패해도 전체를 멈추지 않고 건너뛴 뒤 개수를 보고한다.
    """
    from pykrx import stock

    progress = progress or (lambda *_: None)
    end = end or dt.date.today()
    progress(0, 1, "영업일 목록 조회…")
    dates = _business_days(end, years * BACKFILL_DAYS_PER_YEAR)
    kinds = ["ohlcv"] + ([f"flow_{k}" for k in INVESTOR_NAMES] if with_flows else [])
    todo = [(d, m, k) for d in dates for m in markets for k in kinds if not db.fetched(d, m, k)]
    total = len(todo) + 2
    stats = {"days": len(dates), "fetched": 0, "skipped": len(dates) * len(markets) * len(kinds) - len(todo),
             "errors": 0, "cancelled": False}
    for step, (date, market, kind) in enumerate(todo, 1):
        if cancelled():
            stats["cancelled"] = True
            break
        progress(step, total, f"{date} {market} {'시세' if kind == 'ohlcv' else INVESTOR_NAMES[kind[5:]] + ' 순매수'}"
                              f"  ({step}/{len(todo)}, 오류 {stats['errors']})")
        for attempt in (1, 2):
            try:
                if kind == "ohlcv":
                    _fetch_day(db, date, market)
                else:
                    inv = kind[5:]
                    df = stock.get_market_net_purchases_of_equities_by_ticker(
                        date, date, market=market, investor=INVESTOR_NAMES[inv])
                    s = (df["순매수거래대금"].astype(float) if df is not None and not df.empty
                         else pd.Series(dtype=float))
                    db.save_flow(date, market, inv, s, final=is_final(date))
                stats["fetched"] += 1
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 2:
                    stats["errors"] += 1
                    print(f"백필 실패 {date} {market} {kind}: {e}")
                else:
                    time.sleep(2)
        time.sleep(sleep)

    if not stats["cancelled"] and dates:
        progress(total - 1, total, "지수 · 상장주식수 · 종목명")
        try:
            fetch_index(db, dates[0], dates[-1])
        except Exception as e:  # noqa: BLE001
            print(f"지수 수집 실패 (시장 국면은 전종목 평균으로 대체): {e}")
        last = db.trade_dates()[-1:] or [dates[-1]]
        if db.load_caps(last[0]).empty:
            try:
                raw = pd.concat([stock.get_market_cap_by_ticker(last[0], market=m) for m in markets])
                db.save_caps(last[0], pd.DataFrame({"market_cap": raw["시가총액"], "shares": raw["상장주식수"]}))
            except Exception as e:  # noqa: BLE001
                print(f"시가총액 수집 실패: {e}")
        names = db.load_names()
        markets_of = db.ticker_markets()
        missing = [t for t in markets_of.index if t not in names]
        if missing:
            db.save_names({t: _safe_name(stock, t) for t in missing}, markets_of.to_dict())
    progress(total, total, "완료")
    return stats


# ============================================================================
# screener/data/dart.py
# ============================================================================
"""OpenDART 공시 조회.

OpenDART API 는 홈페이지 ID/PW 가 아니라 '인증키(crtfc_key)' 로 호출한다.
opendart.fss.or.kr 로그인 → 인증키 신청/관리 에서 발급받은 40자리 키를 사용.
"""

import datetime as dt
import io
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd
import requests


BASE = "https://opendart.fss.or.kr/api"
CACHE = cache_dir("dart_corp_codes.pkl")

# 스크리닝 후보에서 주의해서 봐야 할 공시 키워드 (희석·재무위험·거래제한)
RISK_KEYWORDS = (
    "유상증자", "전환사채", "신주인수권부사채", "교환사채", "감자", "관리종목", "불성실공시",
    "상장폐지", "거래정지", "횡령", "배임", "감사의견", "최대주주변경", "소송",
)
# 호재 성격이 있는 공시 키워드
POSITIVE_KEYWORDS = (
    "단일판매ㆍ공급계약", "공급계약", "자기주식취득", "무상증자", "특허", "현금ㆍ현물배당",
    "임상", "기술이전", "타법인주식및출자증권취득",
)


class DartError(RuntimeError):
    pass


class DartClient:
    def __init__(self, api_key: str, timeout: float = 15):
        if not api_key:
            raise DartError("DART API 키가 비어 있습니다.")
        self.key = api_key
        self.timeout = timeout
        self._corp: dict[str, str] | None = None  # stock_code -> corp_code

    def _corp_codes(self) -> dict[str, str]:
        if self._corp is not None:
            return self._corp
        if CACHE.exists() and dt.date.fromtimestamp(CACHE.stat().st_mtime) == dt.date.today():
            self._corp = pd.read_pickle(CACHE)
            return self._corp
        r = requests.get(f"{BASE}/corpCode.xml", params={"crtfc_key": self.key}, timeout=60)
        r.raise_for_status()
        try:
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                root = ET.fromstring(z.read(z.namelist()[0]))
        except zipfile.BadZipFile:
            raise DartError(f"corpCode 다운로드 실패: {r.text[:200]}") from None
        codes = {}
        for el in root.iter("list"):
            stock_code = (el.findtext("stock_code") or "").strip()
            if stock_code:
                codes[stock_code] = el.findtext("corp_code")
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle(codes, CACHE)
        self._corp = codes
        return codes

    def disclosures(self, ticker: str, days: int = 90) -> pd.DataFrame:
        """종목의 최근 공시 목록 (date, title, submitter, flag, url)."""
        corp_code = self._corp_codes().get(ticker)
        if not corp_code:
            return pd.DataFrame(columns=["date", "title", "submitter", "flag", "url"])
        end = dt.date.today()
        params = {
            "crtfc_key": self.key, "corp_code": corp_code,
            "bgn_de": (end - dt.timedelta(days=days)).strftime("%Y%m%d"),
            "end_de": end.strftime("%Y%m%d"), "page_count": 100,
        }
        r = requests.get(f"{BASE}/list.json", params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "013":  # 조회된 데이터 없음
            return pd.DataFrame(columns=["date", "title", "submitter", "flag", "url"])
        if data.get("status") != "000":
            raise DartError(f"DART 오류 {data.get('status')}: {data.get('message')}")
        rows = [{
            "date": it["rcept_dt"],
            "title": it["report_nm"].strip(),
            "submitter": it.get("flr_nm", ""),
            "flag": classify(it["report_nm"]),
            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it['rcept_no']}",
        } for it in data.get("list", [])]
        return pd.DataFrame(rows)


def classify(title: str) -> str:
    if any(k in title for k in RISK_KEYWORDS):
        return "risk"
    if any(k in title for k in POSITIVE_KEYWORDS):
        return "positive"
    return ""


def summarize_flag(df: pd.DataFrame) -> str:
    """공시 목록 → '위험: 유상증자결정' / '호재: 단일판매ㆍ공급계약체결' / ''."""
    if df is None or df.empty:
        return ""
    for flag, label in (("risk", "위험"), ("positive", "호재")):
        hit = df[df["flag"] == flag]
        if not hit.empty:
            return f"{label}: {hit.iloc[0]['title'][:20]}"
    return ""


# ============================================================================
# screener/backtest.py
# ============================================================================
"""과거 데이터 백테스트 · 조건 최적화(워크포워드) · 요인 분석 · 모의투자.

흐름
  1. load_history(db)               DB 에 쌓인 과거 시세로 전체 기간 Panel 구성
  2. build_candidates(panel, p)     날짜마다 '느슨한' 조건으로 후보를 뽑고 지표 + 이후 수익률 기록
                                    (실제 조건은 select() 로 다시 거른다 → 최적화가 빠름)
  3. select(cands, p)               현재 조건을 적용한 신호
  4. simulate(signals, panel, rules) 포트폴리오 매매 시뮬레이션 (손절·익절·보유기간·비용·동시보유)
  5. grid_search / factor_analysis  조건 조합 비교(학습/검증 기간 분리), 지표별 수익 관련성 · 학습 점수

미래 데이터 사용 금지: 신호는 t 일 종가까지의 정보로 만들고, 진입은 t+1 일 시가.
"""

from dataclasses import asdict, dataclass, replace

import numpy as np
import pandas as pd


FWD_HORIZONS = (5, 10, 20)  # 후보별 이후 수익률을 기록하는 보유 기간
GRID_VOL = (2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0)
GRID_CHG = (1.0, 2.0, 3.0, 4.0, 5.0)


@dataclass
class TradeRules:
    stop_pct: float = 5.0        # 손절 (진입가 대비 %, 0 = 사용 안 함)
    target_pct: float = 15.0     # 익절 (%, 0 = 사용 안 함)
    max_hold: int = 20           # 최대 보유 영업일
    cost_pct: float = 0.25       # 왕복 비용 % (수수료 + 매도세 + 슬리피지)
    max_positions: int = 5       # 동시 보유 종목 수 (자금을 균등 배분)
    per_day: int = 2             # 하루 신규 진입 최대 수 (점수 상위부터)
    regime_filter: bool = True   # 해당 시장 지수가 이동평균 아래면 신규 진입 안 함
    regime_ma: int = 60

    def to_dict(self):
        return asdict(self)


# ---------------------------------------------------------------------------- 데이터
def load_history(db, start: str | None = None, end: str | None = None,
                 markets=("KOSPI", "KOSDAQ")) -> Panel:
    """DB 의 기간 시세로 Panel 구성. 시가총액은 최신 상장주식수 × 당일 종가로 근사한다."""
    dates = db.trade_dates(start, end)
    if not dates:
        raise ValueError("DB 에 시세가 없습니다. 먼저 '과거 데이터 수집'을 실행하세요.")
    long = db.load_ohlcv_range(dates[0], dates[-1], markets)
    long["date"] = pd.to_datetime(long["date"])
    wide = {f: long.pivot(index="date", columns="ticker", values=f).sort_index() for f in FIELDS}
    tickers = wide["close"].columns
    meta = pd.DataFrame(index=tickers)
    meta["market"] = long.drop_duplicates("ticker", keep="last").set_index("ticker")["market"]
    names = db.load_names()
    meta["name"] = [names.get(t, "") for t in tickers]
    meta["shares"] = db.latest_shares().reindex(tickers)
    meta["market_cap"] = meta["shares"] * wide["close"].ffill().iloc[-1]
    flows = {}
    for inv in INVESTORS:
        f = db.load_flows_range(dates[0], dates[-1], inv)
        if not f.empty:
            flows[inv] = f.reindex(columns=tickers).fillna(0)
    panel = Panel(**wide, meta=meta, flows=flows if len(flows) == len(INVESTORS) else {})
    panel.index_close = {m: db.load_index(c) for m, c in INDEX_CODES.items()}
    return panel


def regime_table(panel: Panel, ma: int = 60) -> pd.DataFrame:
    """날짜 x 시장 → 지수가 ma 일 이동평균 위인지. 지수가 없으면 전종목 동일가중 지수로 대체."""
    out = {}
    idx = getattr(panel, "index_close", {}) or {}
    rets = panel.close.pct_change(fill_method=None).clip(-0.3, 0.3)
    for market in ("KOSPI", "KOSDAQ"):
        s = idx.get(market)
        if s is None or s.empty:
            cols = panel.meta.index[panel.meta["market"] == market]
            s = (1 + rets[cols].mean(axis=1).fillna(0)).cumprod()
        s = s.reindex(panel.dates).ffill()
        out[market] = (s >= s.rolling(ma, min_periods=max(5, ma // 2)).mean()).fillna(True)
    return pd.DataFrame(out)


def current_regime(panel: Panel, ma: int = 60) -> dict:
    """기준일(패널 마지막 날) 시장별 추세 양호 여부. {'KOSPI': True, 'KOSDAQ': False}"""
    return {m: bool(v) for m, v in regime_table(panel, ma).iloc[-1].items()}


def loose_params(p: ScreenParams) -> ScreenParams:
    """후보 풀을 만들 때 쓰는 느슨한 조건 (최적화 격자 범위 + 추가 필터 해제)."""
    return replace(
        p,
        vol_ratio_min=min(p.vol_ratio_min, GRID_VOL[0]),
        price_chg_max=max(p.price_chg_max, GRID_CHG[-1]),
        daily_chg_max=max(p.daily_chg_max, GRID_CHG[-1]),
        range_max=max(p.range_max, 15.0),
        flow_enabled=False, pos_enabled=False, obv_enabled=False, exclude_expiry=False,
    )


def build_candidates(panel: Panel, p: ScreenParams, start: str | None = None,
                     progress=None, cancelled=lambda: False, cost_pct: float = 0.25) -> pd.DataFrame:
    """날짜마다 느슨한 조건의 후보 + 지표 + 다음날 시가 진입 기준 이후 수익률(비용 차감)."""
    progress = progress or (lambda *_: None)
    loose = loose_params(p)
    window = max(loose.required_days, p.required_days, p.pos_days if p.pos_enabled else 0, loose.pos_days) + 1
    dates = panel.dates
    first = max(window, int(np.searchsorted(dates, pd.Timestamp(start))) if start else 0)
    shares = panel.meta.get("shares")
    close_ff = panel.close.ffill()
    parts = []
    total = len(dates) - first
    for k, i in enumerate(range(first, len(dates))):
        if cancelled():
            break
        if k % 10 == 0:
            progress(k, total, f"후보 계산 {dates[i]:%Y-%m-%d} ({k}/{total})")
        sl = slice(i - window + 1, i + 1)
        meta = panel.meta.copy()
        if shares is not None:
            meta["market_cap"] = shares * close_ff.iloc[i]
        flows = {}
        if panel.has_flows:
            flows = {kk: v.loc[:dates[i]].iloc[-FLOW_DAYS:] for kk, v in panel.flows.items()}
        sub = Panel(**{f: getattr(panel, f).iloc[sl] for f in FIELDS}, meta=meta, flows=flows)
        df = features(sub, loose)
        df = df[filter_mask(df, loose)]
        if df.empty:
            continue
        df = df.assign(di=i, date=dates[i], expiry=is_option_expiry(dates[i], dates[:i + 1]))
        parts.append(df.reset_index())
    progress(total, total, "이후 수익률 계산")
    if not parts:
        return pd.DataFrame()
    cands = pd.concat(parts, ignore_index=True)
    return add_forward_returns(cands, panel, cost_pct)


def add_forward_returns(cands: pd.DataFrame, panel: Panel, cost_pct: float) -> pd.DataFrame:
    col = {t: j for j, t in enumerate(panel.close.columns)}
    O, C = panel.open.to_numpy(float), panel.close.to_numpy(float)
    O[O == 0] = np.nan
    C[C == 0] = np.nan
    n = len(panel.dates)
    ci = cands["ticker"].map(col).to_numpy()
    di = cands["di"].to_numpy()
    entry = np.where(di + 1 < n, O[np.minimum(di + 1, n - 1), ci], np.nan)
    cands["entry"] = entry
    for h in FWD_HORIZONS:
        j = di + h
        px = np.where(j < n, C[np.minimum(j, n - 1), ci], np.nan)
        cands[f"fwd{h}"] = px / entry - 1 - cost_pct / 100
    return cands


def select(cands: pd.DataFrame, p: ScreenParams) -> pd.DataFrame:
    """후보 풀에 실제 조건을 적용하고 날짜별 점수 순으로 정렬."""
    if cands.empty:
        return cands
    c = cands.set_index("ticker")
    out = c[filter_mask(c, p, c["expiry"].to_numpy())].copy()
    out["score"] = score(out, p)
    return out.reset_index().sort_values(["di", "score"], ascending=[True, False])


# ---------------------------------------------------------------------------- 매매 시뮬레이션
def simulate(signals: pd.DataFrame, panel: Panel, rules: TradeRules,
             start_di: int | None = None) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """signals: di(신호일 위치), ticker, score[, name]. 진입은 신호 다음날 시가.

    반환 (trades, equity, metrics). equity: date, equity, bench(전종목 평균), exposure
    """
    dates = panel.dates
    n = len(dates)
    col = {t: j for j, t in enumerate(panel.close.columns)}
    O, H, L, C = (getattr(panel, f).to_numpy(float) for f in ("open", "high", "low", "close"))
    for a in (O, H, L, C):
        a[a == 0] = np.nan
    names = panel.meta.get("name", pd.Series(dtype=object))
    markets = panel.meta.get("market", pd.Series(dtype=object))
    regime = regime_table(panel, rules.regime_ma) if rules.regime_filter else None

    by_day = {d: g for d, g in signals.groupby("di")} if not signals.empty else {}
    i0 = start_di if start_di is not None else (int(signals["di"].min()) + 1 if by_day else n)
    cash, positions, trades, curve = 1.0, {}, [], []
    last_px = {}

    def close_pos(t, i, px, reason):
        nonlocal cash
        pos = positions.pop(t)
        proceeds = pos["shares"] * px * (1 - rules.cost_pct / 100)
        cash += proceeds
        trades.append({
            "ticker": t, "name": names.get(t, ""), "market": markets.get(t, ""),
            "signal_date": dates[pos["e"] - 1], "entry_date": dates[pos["e"]], "entry": pos["px"],
            "exit_date": dates[i], "exit": px, "reason": reason,
            "ret": px / pos["px"] * (1 - rules.cost_pct / 100) - 1, "days": i - pos["e"] + 1,
            "score": pos["score"],
        })

    equity_prev = 1.0
    for i in range(max(i0, 1), n):
        # 1) 보유 종목 청산 점검 (진입 당일 포함)
        for t in list(positions):
            pos, c = positions[t], col[t]
            o, h, lo, cl = O[i, c], H[i, c], L[i, c], C[i, c]
            if np.isnan(cl):  # 거래정지/상장폐지 — 마지막 가격으로 평가만
                if i - pos["e"] + 1 >= rules.max_hold * 3:  # 장기 정지면 마지막 가격으로 정리
                    close_pos(t, i, last_px.get(t, pos["px"]), "거래정지")
                continue
            last_px[t] = cl
            stop = pos["px"] * (1 - rules.stop_pct / 100) if rules.stop_pct > 0 else None
            tgt = pos["px"] * (1 + rules.target_pct / 100) if rules.target_pct > 0 else None
            if stop is not None and lo <= stop:
                close_pos(t, i, o if o < stop else stop, "손절")
            elif tgt is not None and h >= tgt:
                close_pos(t, i, o if o > tgt else tgt, "익절")
            elif i - pos["e"] + 1 >= rules.max_hold:
                close_pos(t, i, cl, "기간만료")

        # 2) 전날 신호로 오늘 시가 진입
        sig = by_day.get(i - 1)
        if sig is not None and len(positions) < rules.max_positions:
            entered = 0
            for r in sig.itertuples():
                if len(positions) >= rules.max_positions or entered >= rules.per_day:
                    break
                t = r.ticker
                if t in positions or t not in col:
                    continue
                if regime is not None and not regime.iloc[i - 1].get(markets.get(t, "KOSPI"), True):
                    continue
                px = O[i, col[t]]
                if np.isnan(px):
                    continue
                alloc = min(cash, equity_prev / rules.max_positions)
                if alloc <= 1e-9:
                    break
                cash -= alloc
                positions[t] = {"shares": alloc / px, "px": px, "e": i, "score": r.score}
                entered += 1
                # 진입 당일 손절/익절 점검
                c = col[t]
                stop = px * (1 - rules.stop_pct / 100) if rules.stop_pct > 0 else None
                if stop is not None and L[i, c] <= stop:
                    close_pos(t, i, stop, "손절")

        value = sum(p["shares"] * (C[i, col[t]] if not np.isnan(C[i, col[t]]) else last_px.get(t, p["px"]))
                    for t, p in positions.items())
        equity_prev = cash + value
        curve.append({"date": dates[i], "equity": equity_prev, "exposure": value / equity_prev if equity_prev else 0,
                      "positions": len(positions)})

    for t in list(positions):  # 기간 끝: 마지막 가격으로 평가 청산
        close_pos(t, n - 1, last_px.get(t, positions[t]["px"]), "보유중")

    equity = pd.DataFrame(curve)
    if not equity.empty:
        rets = panel.close.pct_change(fill_method=None).clip(-0.3, 0.3).mean(axis=1).fillna(0)
        # 시장 = 전종목 동일가중 일간 수익률 누적 (equity 와 같은 날부터)
        equity["bench"] = (1 + rets.loc[equity["date"]]).cumprod().to_numpy()
    trades = pd.DataFrame(trades)
    return trades, equity, metrics(trades, equity)


def metrics(trades: pd.DataFrame, equity: pd.DataFrame) -> dict:
    if equity.empty:
        return {"trades": 0}
    eq = equity["equity"]
    years = max((equity["date"].iloc[-1] - equity["date"].iloc[0]).days / 365.25, 1e-9)
    mdd = (eq / eq.cummax() - 1).min()
    bench = equity["bench"]
    out = {
        "trades": len(trades),
        "total": eq.iloc[-1] - 1,
        "cagr": eq.iloc[-1] ** (1 / years) - 1 if eq.iloc[-1] > 0 else -1,
        "mdd": mdd,
        "bench_total": bench.iloc[-1] - 1,
        "exposure": equity["exposure"].mean(),
        "start": equity["date"].iloc[0], "end": equity["date"].iloc[-1],
    }
    if len(trades):
        r = trades["ret"]
        out.update({
            "win_rate": (r > 0).mean(), "avg_ret": r.mean(),
            "avg_win": r[r > 0].mean() if (r > 0).any() else 0.0,
            "avg_loss": r[r <= 0].mean() if (r <= 0).any() else 0.0,
            "avg_days": trades["days"].mean(),
        })
    return out


# ---------------------------------------------------------------------------- 최적화 · 요인 분석
def split_date(cands: pd.DataFrame, train_frac: float = 0.67) -> pd.Timestamp:
    d = np.sort(cands["date"].unique())
    return pd.Timestamp(d[min(len(d) - 1, int(len(d) * train_frac))])


def _stats(r: pd.Series) -> dict:
    r = r.dropna()
    return {"n": len(r), "mean": r.mean() if len(r) else np.nan,
            "win": (r > 0).mean() if len(r) else np.nan}


def grid_search(cands: pd.DataFrame, p: ScreenParams, horizon: int = 10, train_frac: float = 0.67,
                vol_grid=GRID_VOL, chg_grid=GRID_CHG, top_per_day: int = 0) -> pd.DataFrame:
    """거래량 배수 x 허용 등락률 조합별 학습/검증 기간 성과 (신호 단위 평균 수익, 비용 차감).

    top_per_day > 0 이면 날짜별 점수 상위 N 개만 (실제 매매처럼).
    """
    cut = split_date(cands, train_frac)
    rows = []
    for v in vol_grid:
        for c in chg_grid:
            q = replace(p, vol_ratio_min=v, price_chg_max=c, daily_chg_max=max(p.daily_chg_max, c))
            sel = select(cands, q)
            if top_per_day:
                sel = sel.groupby("di", group_keys=False).head(top_per_day)
            r = sel[f"fwd{horizon}"]
            tr, te = _stats(r[sel["date"] < cut]), _stats(r[sel["date"] >= cut])
            rows.append({"vol_ratio_min": v, "price_chg_max": c,
                         **{f"train_{k}": x for k, x in tr.items()}, **{f"test_{k}": x for k, x in te.items()}})
    out = pd.DataFrame(rows)
    out.attrs["split"] = cut
    return out


def best_cell(grid: pd.DataFrame, min_n: int = 30) -> pd.Series | None:
    ok = grid[grid["train_n"] >= min_n]
    if ok.empty:
        return None
    return ok.sort_values("train_mean", ascending=False).iloc[0]


def factor_analysis(cands: pd.DataFrame, p: ScreenParams, horizon: int = 10, train_frac: float = 0.67,
                    ridge: float = 10.0, top_k: int = 3) -> dict:
    """지표별 이후 수익률과의 관련성(날짜별 순위상관 평균, IC)과 학습 점수 가중치.

    학습 기간으로 가중치를 정하고, 검증 기간에서 '학습 점수 상위 top_k' vs '기본 점수 상위 top_k' 를 비교.
    """
    y_col = f"fwd{horizon}"
    data = cands.dropna(subset=[y_col]).copy()
    if len(data) < 50:
        return {"error": "표본이 부족합니다 (최소 50개 신호 필요). 과거 데이터를 더 수집하세요."}
    data = data.set_index("ticker", drop=False)
    X = learn_matrix(data, p).reset_index(drop=True)
    y = data[y_col].clip(*data[y_col].quantile([0.01, 0.99])).reset_index(drop=True)
    dates = data["date"].reset_index(drop=True)
    cut = split_date(data.reset_index(drop=True), train_frac)
    train = dates < cut

    ic = {}
    for f in X.columns:
        per_day = []
        for _, g in pd.DataFrame({"x": X[f], "y": y, "d": dates})[train].groupby("d"):
            if len(g) >= 5 and g["x"].nunique() > 1:
                per_day.append(g["x"].rank().corr(g["y"].rank()))
        ic[f] = (np.nanmean(per_day) if per_day else np.nan, len(per_day))

    Xt = X[train].fillna(0)
    mu, sd = Xt.mean(), Xt.std().replace(0, 1)
    Z = ((Xt - mu) / sd).to_numpy()
    yt = y[train].to_numpy()
    w = np.linalg.solve(Z.T @ Z + ridge * np.eye(Z.shape[1]), Z.T @ (yt - yt.mean()))
    weights = {f: [float(w[k]), float(mu[f]), float(sd[f])] for k, f in enumerate(X.columns)}

    test = data.reset_index(drop=True)[~train].copy()
    if test.empty:
        comp = {}
    else:
        qp = replace(p, score_weights=weights)
        test["learned"] = score(test.set_index("ticker"), qp).to_numpy()
        test["base"] = score(test.set_index("ticker"), replace(p, score_weights=None)).to_numpy()
        top = lambda col: test.sort_values(col, ascending=False).groupby("di").head(top_k)[y_col]  # noqa: E731
        comp = {"all": _stats(test[y_col]), "base_top": _stats(top("base")), "learned_top": _stats(top("learned"))}

    table = pd.DataFrame([
        {"feature": f, "label": LEARN_FEATURES[f], "ic": ic[f][0], "days": ic[f][1], "weight": weights[f][0]}
        for f in X.columns
    ]).sort_values("ic", key=lambda s: s.abs(), ascending=False)
    return {"table": table, "weights": weights, "test": comp, "split": cut, "horizon": horizon, "n": len(data)}


# ---------------------------------------------------------------------------- 모의투자 (저장된 실제 신호)
def saved_signals(db, panel: Panel, source_filter=("krx", "auto")) -> pd.DataFrame:
    """DB 에 저장된 스크리닝 결과를 시뮬레이션 신호 형식으로 (같은 날 같은 종목은 1번)."""
    res = db.load_results()
    if res.empty:
        return pd.DataFrame(columns=["di", "ticker", "score"])
    runs = db.list_runs().set_index("run_id")
    res = res[res["run_id"].map(runs["source"]).isin(source_filter)]
    pos = {d.strftime("%Y%m%d"): i for i, d in enumerate(panel.dates)}
    res = res.assign(di=res["asof"].map(pos)).dropna(subset=["di"])
    res["di"] = res["di"].astype(int)
    res = res.sort_values(["di", "run_id", "rank"]).drop_duplicates(["di", "ticker"])
    return res[["di", "ticker", "score", "name"]].sort_values(["di", "score"], ascending=[True, False])


# ============================================================================
# screener/notify.py
# ============================================================================
"""알림: 텔레그램 봇으로 메시지 보내기.

설정: 텔레그램에서 @BotFather → /newbot → 봇 토큰 발급.
      만든 봇에게 아무 메시지나 보낸 뒤 '채팅 ID 찾기'(find_chat_id)로 채팅 ID 확인.
"""

import pandas as pd
import requests

API = "https://api.telegram.org/bot{token}/{method}"


class NotifyError(RuntimeError):
    pass


def send_telegram(token: str, chat_id: str, text: str, timeout: float = 15) -> None:
    if not (token and chat_id):
        raise NotifyError("텔레그램 봇 토큰과 채팅 ID 를 '계정'에 입력하세요.")
    for chunk in [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]:
        r = requests.post(API.format(token=token, method="sendMessage"),
                          data={"chat_id": chat_id, "text": chunk}, timeout=timeout)
        if r.status_code != 200 or not r.json().get("ok"):
            raise NotifyError(f"텔레그램 전송 실패: {r.text[:200]}")


def find_chat_id(token: str, timeout: float = 15) -> str | None:
    """봇에게 최근 메시지를 보낸 채팅의 ID."""
    r = requests.get(API.format(token=token, method="getUpdates"), timeout=timeout)
    data = r.json()
    if not data.get("ok"):
        raise NotifyError(f"봇 토큰을 확인하세요: {data.get('description')}")
    for upd in reversed(data.get("result", [])):
        msg = upd.get("message") or upd.get("channel_post") or {}
        if "chat" in msg:
            return str(msg["chat"]["id"])
    return None


def format_results(asof: str, result: pd.DataFrame, regime: dict | None = None, top: int = 15) -> str:
    d = f"{asof[:4]}-{asof[4:6]}-{asof[6:]}"
    lines = [f"📊 거래량 급증·횡보 스크리닝 {d}", f"조건 충족 {len(result)}종목"]
    if regime:
        weak = [m for m, ok in regime.items() if not ok]
        lines.append("⚠ 하락 추세 시장: " + ", ".join(weak) if weak else "시장 추세: 양호")
    lines.append("")
    for i, r in enumerate(result.head(top).itertuples(), 1):
        flow = ""
        if pd.notna(getattr(r, "frg_eok", float("nan"))):
            flow = f" 외{r.frg_eok:+.0f}억 기{r.inst_eok:+.0f}억"
        lines.append(f"{i}. {r.name}({r.ticker}) {r.close:,.0f}원 {r.chg_pct:+.1f}% 거래량{r.vol_ratio:.1f}배{flow}")
    if len(result) > top:
        lines.append(f"… 외 {len(result) - top}종목")
    return "\n".join(lines)


# ============================================================================
# screener/gui/theme.py
# ============================================================================
"""다크 테마 팔레트와 Qt 스타일시트."""

BG = "#0f1320"
SURFACE = "#171c2c"
SURFACE_2 = "#1e2438"
BORDER = "#2a3150"
TEXT = "#e6e9f2"
MUTED = "#8a93b2"
ACCENT = "#6c8cff"
ACCENT_2 = "#8f6cff"
UP = "#ff5c6c"      # 한국 관례: 상승 = 빨강
DOWN = "#4da3ff"    # 하락 = 파랑
WARN = "#ffb547"
GOOD = "#3ddc97"

QSS = f"""
* {{
    font-family: "Pretendard", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR",
                 "NanumBarunGothic", "Segoe UI", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QWidget#Root {{ background: {BG}; }}
QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; }}

QFrame#Header {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #1a2040, stop:1 #221a40);
    border-bottom: 1px solid {BORDER};
}}
QLabel#Title {{ font-size: 19px; font-weight: 700; }}
QLabel#Subtitle {{ color: {MUTED}; font-size: 12px; }}

QFrame#Card {{
    background: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QLabel#CardTitle {{ font-size: 13px; font-weight: 700; color: {TEXT}; }}
QLabel#CardHint {{ color: {MUTED}; font-size: 11px; }}
QLabel#ParamLabel {{ color: {MUTED}; }}

QFrame#Chip {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 10px;
}}
QLabel#ChipValue {{ font-size: 20px; font-weight: 700; }}
QLabel#ChipLabel {{ color: {MUTED}; font-size: 11px; }}

QPushButton {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 7px 14px;
}}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:pressed {{ background: {BORDER}; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPushButton#Primary {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border: none; font-weight: 700; color: white;
}}
QPushButton#Primary:hover {{ background: {ACCENT}; }}
QPushButton#Ghost {{ background: transparent; }}

QLineEdit, QSpinBox, QDoubleSpinBox, QDateEdit, QComboBox {{
    background: {SURFACE_2};
    border: 1px solid {BORDER};
    border-radius: 7px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QAbstractSpinBox::up-button, QAbstractSpinBox::down-button {{
    width: 0; border: none;  /* NoButtons 는 Qt 6 + 스타일시트에서 입력란이 사라지는 버그가 있어 QSS 로 숨김 */
}}
QAbstractSpinBox QLineEdit, QDateEdit QLineEdit {{
    padding: 0; border: none; background: transparent;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{ background: {SURFACE_2}; border: 1px solid {BORDER}; selection-background-color: {ACCENT}; }}

QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::sub-page:horizontal {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: white; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
    border: 2px solid {ACCENT};
}}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 4px; border: 1px solid {BORDER}; background: {SURFACE_2}; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QTableView, QTreeWidget {{
    background: {SURFACE};
    alternate-background-color: #1a2033;
    border: 1px solid {BORDER};
    border-radius: 10px;
    gridline-color: transparent;
    selection-background-color: #2b3566;
    selection-color: {TEXT};
}}
QHeaderView::section {{
    background: {SURFACE_2};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 7px 6px;
    font-weight: 600;
}}
QTableView::item {{ padding: 4px; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}

QSplitter::handle {{ background: {BG}; }}
QStatusBar {{ background: {SURFACE}; border-top: 1px solid {BORDER}; color: {MUTED}; }}
QProgressBar {{
    background: {SURFACE_2}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {ACCENT}, stop:1 {ACCENT_2});
    border-radius: 4px;
}}
QDialog {{ background: {BG}; }}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 10px; background: {BG}; top: -1px; }}
QTabBar::tab {{
    background: {SURFACE_2}; color: {MUTED}; padding: 8px 18px; margin-right: 4px;
    border: 1px solid {BORDER}; border-bottom: none; border-top-left-radius: 8px; border-top-right-radius: 8px;
}}
QTabBar::tab:selected {{ background: {SURFACE}; color: {TEXT}; font-weight: 700; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QMenu {{ background: {SURFACE_2}; border: 1px solid {BORDER}; }}
QMenu::item:selected {{ background: {ACCENT}; }}
"""


# ============================================================================
# screener/gui/widgets.py
# ============================================================================
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QSlider, QVBoxLayout, QWidget,
)


class Card(QFrame):
    """제목/설명이 있는 둥근 카드 컨테이너."""

    def __init__(self, title: str, hint: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(8)
        t = QLabel(title)
        t.setObjectName("CardTitle")
        lay.addWidget(t)
        if hint:
            h = QLabel(hint)
            h.setObjectName("CardHint")
            h.setWordWrap(True)
            lay.addWidget(h)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        lay.addLayout(self.body)


class ParamSlider(QWidget):
    """라벨 + 슬라이더 + 숫자 입력이 연동되는 파라미터 입력기."""

    valueChanged = Signal(float)

    def __init__(self, label: str, lo: float, hi: float, step: float, value: float,
                 suffix: str = "", decimals: int = 1, tooltip: str = "", parent=None):
        super().__init__(parent)
        self._step = step
        self._lo = lo
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        name = QLabel(label)
        name.setObjectName("ParamLabel")
        self.spin = QDoubleSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setSingleStep(step)
        self.spin.setDecimals(decimals)
        self.spin.setSuffix(f" {suffix}" if suffix else "")
        self.spin.setAlignment(Qt.AlignRight)
        self.spin.setFixedWidth(96)

        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, round((hi - lo) / step))

        grid.addWidget(name, 0, 0)
        grid.addWidget(self.spin, 0, 1, Qt.AlignRight)
        grid.addWidget(self.slider, 1, 0, 1, 2)
        if tooltip:
            for w in (name, self.spin, self.slider):
                w.setToolTip(tooltip)

        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)
        self.setValue(value)

    def _from_slider(self, pos: int):
        v = self._lo + pos * self._step
        if abs(self.spin.value() - v) > 1e-9:
            self.spin.setValue(v)

    def _from_spin(self, v: float):
        self.slider.blockSignals(True)
        self.slider.setValue(round((v - self._lo) / self._step))
        self.slider.blockSignals(False)
        self.valueChanged.emit(v)

    def value(self) -> float:
        return self.spin.value()

    def setValue(self, v: float):
        self.spin.setValue(v)
        self._from_spin(self.spin.value())


class StatChip(QFrame):
    def __init__(self, label: str, value: str = "–", color: str | None = None, parent=None):
        super().__init__(parent)
        self.setObjectName("Chip")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(0)
        self.value_lbl = QLabel(value)
        self.value_lbl.setObjectName("ChipValue")
        if color:
            self.value_lbl.setStyleSheet(f"color: {color};")
        lbl = QLabel(label)
        lbl.setObjectName("ChipLabel")
        lay.addWidget(self.value_lbl)
        lay.addWidget(lbl)

    def set(self, value: str):
        self.value_lbl.setText(value)


def hbox(*widgets, spacing=8, stretch_last=False) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        else:
            lay.addWidget(w)
    if stretch_last:
        lay.addStretch(1)
    return lay


# ============================================================================
# screener/gui/chart.py
# ============================================================================
"""선택 종목 캔들 + 거래량 차트 (matplotlib)."""

import importlib.util
from pathlib import Path

import matplotlib

try:
    matplotlib.use("QtAgg")
except ImportError:  # pyplot 이 다른 백엔드로 먼저 로드된 경우 — Qt 캔버스를 직접 쓰므로 무관
    pass
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib import font_manager as fm  # noqa: E402
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402



def _setup_korean_font():
    # pykrx 에 포함된 나눔바른고딕을 우선 등록 (OS 에 한글 폰트가 없어도 동작)
    spec = importlib.util.find_spec("pykrx")
    if spec and spec.origin:
        ttf = Path(spec.origin).parent / "NanumBarunGothic.ttf"
        if ttf.exists():
            fm.fontManager.addfont(str(ttf))
    available = {f.name for f in fm.fontManager.ttflist}
    prefs = ["Malgun Gothic", "AppleGothic", "NanumBarunGothic", "NanumGothic",
             "Noto Sans CJK KR", "DejaVu Sans"]
    matplotlib.rcParams["font.family"] = [f for f in prefs if f in available] or ["sans-serif"]
    matplotlib.rcParams["axes.unicode_minus"] = False


_setup_korean_font()


class ChartWidget(FigureCanvasQTAgg):
    def __init__(self, parent=None):
        self.fig = Figure(figsize=(6, 4), facecolor=SURFACE)
        super().__init__(self.fig)
        self.setParent(parent)
        self.setStyleSheet(f"background: {SURFACE}; border-radius: 10px;")
        self.show_message("종목을 선택하면 차트가 표시됩니다")

    def _style(self, ax):
        ax.set_facecolor(SURFACE)
        for s in ax.spines.values():
            s.set_color(BORDER)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.grid(True, color=BORDER, alpha=0.5, linewidth=0.6)

    def show_message(self, text: str):
        self.fig.clear()
        self.fig.text(0.5, 0.5, text, ha="center", va="center", color=MUTED, fontsize=11)
        self.draw_idle()

    def plot(self, hist: pd.DataFrame, title: str, recent_days: int, base_days: int,
             base_vol: float | None = None):
        hist = hist.dropna(subset=["close"])
        if hist.empty:
            self.show_message("데이터 없음")
            return
        self.fig.clear()
        gs = self.fig.add_gridspec(2, 1, height_ratios=[2.2, 1], hspace=0.05,
                                   left=0.08, right=0.98, top=0.9, bottom=0.1)
        ax = self.fig.add_subplot(gs[0])
        axv = self.fig.add_subplot(gs[1], sharex=ax)
        for a in (ax, axv):
            self._style(a)

        x = np.arange(len(hist))
        o, h, l, c = (hist[k].to_numpy() for k in ("open", "high", "low", "close"))
        up = c >= o
        colors = np.where(up, UP, DOWN)
        ax.vlines(x, l, h, color=colors, linewidth=1)
        ax.bar(x, np.maximum(np.abs(c - o), c * 0.0008), bottom=np.minimum(o, c),
               color=colors, width=0.65)

        n = len(hist)
        r0 = n - recent_days - 0.5
        b0 = max(-0.5, r0 - base_days)
        for a in (ax, axv):
            a.axvspan(b0, r0, color=ACCENT, alpha=0.06)
            a.axvspan(r0, n - 0.5, color=WARN, alpha=0.12)

        vol = hist["volume"].to_numpy()
        vcolors = [WARN if i >= n - recent_days else MUTED for i in range(n)]
        axv.bar(x, vol, color=vcolors, width=0.65, alpha=0.9)
        if base_vol:
            axv.axhline(base_vol, color=ACCENT, linewidth=1, linestyle="--")
            axv.text(0, base_vol, " 평소 거래량", color=ACCENT, fontsize=8, va="bottom")
        axv.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(_human))

        ticks = np.linspace(0, n - 1, min(6, n)).astype(int)
        axv.set_xticks(ticks)
        axv.set_xticklabels([hist.index[i].strftime("%m/%d") for i in ticks])
        ax.tick_params(labelbottom=False)
        ax.set_xlim(-0.8, n - 0.2)
        self.fig.suptitle(title, color=TEXT, fontsize=12, x=0.08, ha="left", fontweight="bold")
        self.draw_idle()


def _human(v, _pos=None):
    for unit, div in (("억", 1e8), ("만", 1e4)):
        if abs(v) >= div:
            return f"{v / div:.0f}{unit}"
    return f"{v:.0f}"


# ============================================================================
# screener/gui/models.py
# ============================================================================
import math

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt
from PySide6.QtGui import QColor, QFont


# (컬럼, 헤더, 포맷, 툴팁)
COLUMNS = [
    ("ticker", "코드", "{}", ""),
    ("name", "종목명", "{}", ""),
    ("market", "시장", "{}", ""),
    ("close", "종가", "{:,.0f}", "기준일 종가"),
    ("chg_pct", "기간등락%", "{:+.2f}", "최근 구간 누적 등락률 (구간 직전 종가 대비)"),
    ("max_daily_chg", "최대일등락%", "{:.2f}", "최근 구간 일별 등락률 절대값의 최대"),
    ("max_range", "일중변동%", "{:.2f}", "최근 구간 (고가-저가)/전일종가 최대"),
    ("vol_ratio", "거래량배수", "{:.1f}x", "최근 구간 평균 거래량 / 평소 거래량"),
    ("frg_eok", "외국인(억)", "{:+,.1f}", "수급 집계 기간 외국인 순매수 금액"),
    ("inst_eok", "기관(억)", "{:+,.1f}", "수급 집계 기간 기관합계 순매수 금액"),
    ("frg_days", "외국인일", "{:.0f}", "수급 집계 기간 중 외국인 순매수일 수"),
    ("inst_days", "기관일", "{:.0f}", "수급 집계 기간 중 기관 순매수일 수"),
    ("recent_vol", "최근거래량", "{:,.0f}", ""),
    ("base_vol", "평소거래량", "{:,.0f}", ""),
    ("value_eok", "거래대금(억)", "{:,.1f}", "최근 구간 평균 거래대금"),
    ("disc_flag", "공시", "{}", "최근 90일 DART 공시 중 위험(빨강) · 호재(초록). DART 키가 있으면 자동 조회"),
    ("mcap_eok", "시총(억)", "{:,.0f}", ""),
    ("box_pct", "박스폭%", "{:.1f}", "기준 구간 (최고-최저 종가)/평균"),
    ("score", "점수", "{:.1f}", "log2(거래량배수)×10 / (1 + |기간등락| + 0.5×일중변동)"),
]
NUMERIC = {c for c, *_ in COLUMNS if c not in ("ticker", "name", "market", "disc_flag")}


class ResultsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.df = pd.DataFrame(columns=[c for c, *_ in COLUMNS])
        self._bold = QFont()
        self._bold.setBold(True)

    def set_frame(self, df: pd.DataFrame):
        self.beginResetModel()
        self.df = df.reset_index(drop=True)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.df)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation != Qt.Horizontal:
            return None
        if role == Qt.DisplayRole:
            return COLUMNS[section][1]
        if role == Qt.ToolTipRole:
            return COLUMNS[section][3] or None
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col, _, fmt, _ = COLUMNS[index.column()]
        v = self.df[col].iat[index.row()] if col in self.df else ""
        if role == Qt.DisplayRole:
            if isinstance(v, float) and math.isnan(v):
                return "–"
            return fmt.format(v)
        if role == Qt.UserRole:  # 정렬용 원값 (numpy 스칼라는 QVariant 로 변환되지 않음)
            return v.item() if hasattr(v, "item") else v
        if role == Qt.TextAlignmentRole:
            return int((Qt.AlignRight if col in NUMERIC else Qt.AlignLeft) | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if col in ("chg_pct", "frg_eok", "inst_eok") and isinstance(v, float):
                return QColor(UP if v > 0 else DOWN if v < 0 else MUTED)
            if col == "vol_ratio":
                return QColor(WARN if v >= 5 else ACCENT)
            if col == "market":
                return QColor(MUTED)
            if col == "disc_flag" and isinstance(v, str) and v:
                return QColor(UP if v.startswith("위험") else GOOD)
            if col == "score":
                return QColor(GOOD)
        if role == Qt.FontRole and col in ("name", "vol_ratio", "score"):
            return self._bold
        return None

    def row(self, r: int) -> pd.Series:
        return self.df.iloc[r]


class ResultsProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSortRole(Qt.UserRole)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.setFilterKeyColumn(-1)  # 모든 컬럼(코드/종목명)에서 검색


# ============================================================================
# screener/gui/workers.py
# ============================================================================
import datetime as dt
import traceback

from PySide6.QtCore import QThread, Signal


class LoadWorker(QThread):
    """시세 패널 로딩 (네트워크) 을 UI 스레드 밖에서 수행."""

    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, source: str, end: dt.date, days: int, krx_id: str = "", krx_pw: str = "",
                 db_path: str | None = None):
        super().__init__()
        self.source, self.end, self.days = source, end, days
        self.krx_id, self.krx_pw = krx_id, krx_pw
        self.db_path = db_path

    def run(self):
        try:
            if self.source == "demo":
                panel = demo_load_panel(self.end, self.days, progress=self.progress.emit)
            else:
                db = StockDB(self.db_path)
                if db.stats()["days"] == 0:  # 예전 버전 캐시(.pkl)가 있으면 DB 로 옮겨 재다운로드 방지
                    self.progress.emit(0, 1, "이전 캐시를 DB 로 옮기는 중…")
                    n = db.import_pickle_cache(LEGACY_CACHE_DIR)
                    if n:
                        print(f"이전 캐시 {n}개 파일을 DB 로 옮김")
                self.progress.emit(0, 1, "KRX 로그인 중…")
                krx_login(self.krx_id, self.krx_pw)
                panel = krx_load_panel(self.end, self.days, progress=self.progress.emit, db=db)
            self.done.emit(panel)
        except Exception as e:  # noqa: BLE001 — 사용자에게 그대로 보여준다
            traceback.print_exc()
            self.failed.emit(str(e))


class DartWorker(QThread):
    done = Signal(str, object)   # ticker, DataFrame
    failed = Signal(str, str)

    def __init__(self, client, ticker: str):
        super().__init__()
        self.client, self.ticker = client, ticker

    def run(self):
        try:
            self.done.emit(self.ticker, self.client.disclosures(self.ticker))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(self.ticker, str(e))


class DartBatchWorker(QThread):
    """여러 종목의 최근 공시 위험/호재 표시를 한 번에 조회."""

    progress = Signal(int, int)
    done = Signal(object)  # {ticker: flag}

    def __init__(self, client, tickers):
        super().__init__()
        self.client, self.tickers = client, list(tickers)

    def run(self):

        out = {}
        for k, t in enumerate(self.tickers, 1):
            try:
                out[t] = summarize_flag(self.client.disclosures(t))
            except Exception as e:  # noqa: BLE001 — 한 종목 실패는 건너뜀
                print(f"DART {t} 조회 실패: {e}")
            self.progress.emit(k, len(self.tickers))
        self.done.emit(out)


# ============================================================================
# screener/gui/account.py
# ============================================================================
"""계정 화면: 비밀번호 잠금 · 초기 비밀번호 강제 변경 · KRX/DART 계정 편집."""

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QVBoxLayout,
)


MAX_FAILS = 5
LOCK_SECONDS = 30


def _busy(fn, *args):
    """키 유도(PBKDF2)가 0.5초가량 걸리므로 대기 커서 표시."""
    QGuiApplication.setOverrideCursor(Qt.WaitCursor)
    try:
        return fn(*args)
    finally:
        QGuiApplication.restoreOverrideCursor()


def _pw_edit(placeholder: str = "") -> QLineEdit:
    e = QLineEdit()
    e.setEchoMode(QLineEdit.Password)
    e.setPlaceholderText(placeholder)
    return e


class _Base(QDialog):
    def __init__(self, title: str, heading: str, hint: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        lay.setSpacing(12)
        self.card = Card(heading, hint)
        lay.addWidget(self.card)
        self.error = QLabel("")
        self.error.setStyleSheet("color: #ff5c6c;")
        self.error.setWordWrap(True)
        self.error.hide()
        lay.addWidget(self.error)
        self.buttons_row = QHBoxLayout()
        lay.addLayout(self.buttons_row)

    def show_error(self, msg: str):
        self.error.setText(msg)
        self.error.setVisible(bool(msg))


class PasswordDialog(_Base):
    """계정 비밀번호 입력. 성공 시 self.password / self.creds 설정."""

    _fails = 0
    _locked_until = 0.0  # 클래스 변수: 창을 다시 열어도 잠금 유지

    def __init__(self, vault: Vault, parent=None):
        first = not vault.exists()
        super().__init__(
            "계정 잠금 해제", "🔒 계정 비밀번호",
            "계정 정보(KRX·DART)는 이 비밀번호로 암호화되어 저장됩니다."
            + ("\n처음 사용: 초기 비밀번호는 0000 입니다. 접속 후 반드시 변경해야 합니다." if first else ""),
            parent)
        self.vault = vault
        self.password = ""
        self.creds: Credentials | None = None
        self.pw = _pw_edit("비밀번호")
        self.card.body.addWidget(self.pw)

        reset = QPushButton("비밀번호를 잊었어요 (초기화)", objectName="Ghost")
        reset.clicked.connect(self._reset)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("확인")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._try)
        bb.rejected.connect(self.reject)
        self.buttons_row.addWidget(reset)
        self.buttons_row.addStretch(1)
        self.buttons_row.addWidget(bb)
        self.pw.setFocus()

    def _try(self):
        wait = PasswordDialog._locked_until - time.time()
        if wait > 0:
            self.show_error(f"비밀번호를 {MAX_FAILS}회 틀려 {int(wait) + 1}초 동안 잠겼습니다.")
            return
        try:
            self.creds = _busy(self.vault.unlock, self.pw.text())
        except WrongPassword as e:
            PasswordDialog._fails += 1
            left = MAX_FAILS - PasswordDialog._fails
            if left <= 0:
                PasswordDialog._fails = 0
                PasswordDialog._locked_until = time.time() + LOCK_SECONDS
                self.show_error(f"비밀번호를 {MAX_FAILS}회 틀렸습니다. {LOCK_SECONDS}초 후 다시 시도하세요.")
            else:
                self.show_error(f"{e} (남은 시도 {left}회)")
            self.pw.selectAll()
            return
        except VaultError as e:
            self.show_error(str(e))
            return
        PasswordDialog._fails = 0
        self.password = self.pw.text()
        self.accept()

    def _reset(self):
        ok = QMessageBox.warning(
            self, "계정 초기화",
            "저장된 KRX·DART 계정 정보가 모두 삭제되고 비밀번호가 0000 으로 초기화됩니다.\n계속할까요?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ok == QMessageBox.Yes:
            self.vault.reset()
            self.show_error("초기화되었습니다. 0000 으로 접속한 뒤 새 비밀번호를 설정하세요.")


class ChangePasswordDialog(_Base):
    """비밀번호 변경. forced=True 면 초기 비밀번호 사용 중이라 변경해야만 진행 가능."""

    def __init__(self, vault: Vault, current: str | None = None, forced: bool = False, parent=None):
        super().__init__(
            "비밀번호 변경", "초기 비밀번호 변경 필요" if forced else "비밀번호 변경",
            ("초기 비밀번호(0000)로 접속했습니다. 계속하려면 새 비밀번호를 설정하세요.\n" if forced else "")
            + "4자 이상, 0000 사용 불가. 비밀번호를 잊으면 저장된 계정 정보는 복구할 수 없습니다.",
            parent)
        self.vault = vault
        self.new_password = ""
        self.creds: Credentials | None = None
        form = QFormLayout()
        form.setSpacing(10)
        self.cur = _pw_edit()
        if current is not None:
            self.cur.setText(current)
        else:
            form.addRow("현재 비밀번호", self.cur)
        self.new = _pw_edit()
        self.confirm = _pw_edit()
        form.addRow("새 비밀번호", self.new)
        form.addRow("새 비밀번호 확인", self.confirm)
        self.card.body.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("변경")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._apply)
        bb.rejected.connect(self.reject)
        self.buttons_row.addStretch(1)
        self.buttons_row.addWidget(bb)
        (self.new if current is not None else self.cur).setFocus()

    def _apply(self):
        err = validate_new_password(self.new.text(), self.confirm.text())
        if err:
            self.show_error(err)
            return
        try:
            self.creds = _busy(self.vault.change_password, self.cur.text(), self.new.text(),
                               self.confirm.text())
        except VaultError as e:
            self.show_error(str(e))
            return
        self.new_password = self.new.text()
        self.accept()


def unlock(vault: Vault, parent=None) -> tuple[str, Credentials] | None:
    """비밀번호 확인(+ 초기 비밀번호면 강제 변경). 취소/실패 시 None."""
    dlg = PasswordDialog(vault, parent)
    if dlg.exec() != QDialog.Accepted:
        return None
    password, creds = dlg.password, dlg.creds
    if vault.must_change_password:
        ch = ChangePasswordDialog(vault, current=password, forced=True, parent=parent)
        if ch.exec() != QDialog.Accepted:
            QMessageBox.information(parent, "계정", "비밀번호를 변경해야 계정을 사용할 수 있습니다.")
            return None
        password, creds = ch.new_password, ch.creds
        QMessageBox.information(parent, "계정", "비밀번호가 변경되었습니다.")
    return password, creds


class AccountDialog(_Base):
    """KRX / DART 계정 정보 편집 (잠금 해제 후에만 열림)."""

    def __init__(self, vault: Vault, password: str, creds: Credentials, parent=None):
        super().__init__("계정 설정", "KRX 정보데이터시스템",
                         "data.krx.co.kr 로그인 계정 — 시세·투자자별 순매수 수집에 사용", parent)
        self.vault, self.password = vault, password
        self.setMinimumWidth(520)

        krx_form = QFormLayout()
        krx_form.setSpacing(10)
        self.krx_id = QLineEdit(creds.krx_id)
        self.krx_pw = _pw_edit()
        self.krx_pw.setText(creds.krx_pw)
        krx_form.addRow("KRX ID", self.krx_id)
        krx_form.addRow("KRX 비밀번호", self.krx_pw)
        self.card.body.addLayout(krx_form)

        dart = Card("OpenDART", "API 호출에는 '인증키'만 사용됩니다. "
                                "opendart.fss.or.kr 로그인 → 인증키 신청/관리에서 40자리 키를 발급받으세요.")
        dart_form = QFormLayout()
        dart_form.setSpacing(10)
        self.dart_id = QLineEdit(creds.dart_id)
        self.dart_pw = _pw_edit()
        self.dart_pw.setText(creds.dart_pw)
        self.dart_key = _pw_edit("40자리 인증키")
        self.dart_key.setText(creds.dart_key)
        dart_form.addRow("DART ID", self.dart_id)
        dart_form.addRow("DART 비밀번호", self.dart_pw)
        dart_form.addRow("API 인증키", self.dart_key)
        dart.body.addLayout(dart_form)
        self.layout().insertWidget(1, dart)

        tg = Card("텔레그램 알림 (선택)", "자동 실행(daily_run) 결과를 받을 봇. 텔레그램 @BotFather → /newbot 으로 토큰 발급 → "
                                       "만든 봇에게 아무 말이나 보낸 뒤 '채팅 ID 찾기'.")
        tg_form = QFormLayout()
        tg_form.setSpacing(10)
        self.tg_token = _pw_edit("123456:ABC-...")
        self.tg_token.setText(creds.telegram_token)
        self.tg_chat = QLineEdit(creds.telegram_chat)
        find = QPushButton("채팅 ID 찾기", objectName="Ghost")
        find.clicked.connect(self._find_chat)
        test = QPushButton("테스트 전송", objectName="Ghost")
        test.clicked.connect(self._test_send)
        tg_form.addRow("봇 토큰", self.tg_token)
        tg_form.addRow("채팅 ID", self.tg_chat)
        tg.body.addLayout(tg_form)
        row = QHBoxLayout()
        row.addWidget(find)
        row.addWidget(test)
        row.addStretch(1)
        tg.body.addLayout(row)
        self.layout().insertWidget(2, tg)

        self.show_btn = QPushButton("비밀번호 보기", objectName="Ghost", checkable=True)
        self.show_btn.toggled.connect(self._toggle_echo)
        change = QPushButton("계정 비밀번호 변경")
        change.clicked.connect(self._change_password)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Save).setText("저장")
        bb.button(QDialogButtonBox.Cancel).setText("취소")
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        self.buttons_row.addWidget(change)
        self.buttons_row.addWidget(self.show_btn)
        self.buttons_row.addStretch(1)
        self.buttons_row.addWidget(bb)

    def _toggle_echo(self, on: bool):
        mode = QLineEdit.Normal if on else QLineEdit.Password
        for e in (self.krx_pw, self.dart_pw, self.dart_key, self.tg_token):
            e.setEchoMode(mode)
        self.show_btn.setText("비밀번호 숨기기" if on else "비밀번호 보기")

    def creds(self) -> Credentials:
        return Credentials(self.krx_id.text().strip(), self.krx_pw.text(), self.dart_id.text().strip(),
                           self.dart_pw.text(), self.dart_key.text().strip(),
                           self.tg_token.text().strip(), self.tg_chat.text().strip())

    def _find_chat(self):
        try:
            cid = _busy(find_chat_id, self.tg_token.text().strip())
        except (NotifyError, Exception) as e:  # noqa: BLE001
            self.show_error(str(e))
            return
        if cid:
            self.tg_chat.setText(cid)
            self.show_error("")
        else:
            self.show_error("봇에게 먼저 아무 메시지나 보낸 뒤 다시 눌러 주세요.")

    def _test_send(self):
        try:
            _busy(send_telegram, self.tg_token.text().strip(), self.tg_chat.text().strip(),
                  "✅ Volume Spike Screener 테스트 메시지")
        except Exception as e:  # noqa: BLE001
            self.show_error(str(e))
            return
        QMessageBox.information(self, "텔레그램", "전송했습니다. 텔레그램을 확인하세요.")

    def _change_password(self):
        dlg = ChangePasswordDialog(self.vault, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.password = dlg.new_password
            # 화면에서 수정 중이던 내용도 새 비밀번호로 함께 저장
            _busy(self.vault.save, self.password, self.creds())
            QMessageBox.information(self, "계정", "계정 비밀번호가 변경되었습니다.")

    def _save(self):
        key = self.dart_key.text().strip()
        if key and len(key) != 40:
            if QMessageBox.question(self, "DART 인증키", f"인증키 길이가 {len(key)}자입니다 (보통 40자). 그대로 저장할까요?") \
                    != QMessageBox.Yes:
                return
        try:
            _busy(self.vault.save, self.password, self.creds())
        except OSError as e:
            self.show_error(f"저장 실패: {e}")
            return
        self.accept()


# ============================================================================
# screener/gui/db_dialog.py
# ============================================================================
"""DB 위치·상태 화면."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout,
)



class DbDialog(QDialog):
    """현재 DB 경로와 저장된 데이터 범위를 보여주고, 위치를 바꿀 수 있게 한다."""

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("데이터 저장소 (DB)")
        self.setMinimumWidth(560)
        self.path = path
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 20)
        card = Card("SQLite DB", "받아온 시세 · 외국인/기관 순매수 · 저장한 스크리닝 결과가 이 파일 하나에 쌓입니다. "
                                 "이미 받은 날짜는 다시 받지 않습니다.")
        self.form = QFormLayout()
        self.form.setSpacing(8)
        self.lbl_path = QLabel(wordWrap=True)
        self.lbl_path.setTextInteractionFlags(self.lbl_path.textInteractionFlags() |
                                              self.lbl_path.textInteractionFlags().TextSelectableByMouse)
        self.lbl_range = QLabel()
        self.lbl_rows = QLabel()
        self.lbl_runs = QLabel()
        self.lbl_size = QLabel()
        for k, w in (("위치", self.lbl_path), ("시세 기간", self.lbl_range), ("데이터", self.lbl_rows),
                     ("저장한 결과", self.lbl_runs), ("파일 크기", self.lbl_size)):
            self.form.addRow(k, w)
        card.body.addLayout(self.form)
        lay.addWidget(card)

        row = QHBoxLayout()
        change = QPushButton("위치 변경…")
        change.clicked.connect(self._change)
        reset = QPushButton("기본 위치로", objectName="Ghost")
        reset.clicked.connect(lambda: self._set(str(default_db_path())))
        opendir = QPushButton("폴더 열기", objectName="Ghost")
        opendir.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(self.path).parent))))
        bb = QDialogButtonBox(QDialogButtonBox.Ok)
        bb.button(QDialogButtonBox.Ok).setText("닫기")
        bb.accepted.connect(self.accept)
        for w in (change, reset, opendir):
            row.addWidget(w)
        row.addStretch(1)
        row.addWidget(bb)
        lay.addLayout(row)
        self._refresh()

    def _refresh(self):
        try:
            st = StockDB(self.path).stats()
        except Exception as e:  # noqa: BLE001
            self.lbl_path.setText(f"{self.path}\n⚠ 열 수 없음: {e}")
            return
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else "–"  # noqa: E731
        self.lbl_path.setText(st["path"])
        self.lbl_range.setText(f"{fmt(st['first'])} ~ {fmt(st['last'])}  ({st['days']}영업일)")
        self.lbl_rows.setText(f"시세 {st['ohlcv_rows']:,}행 · 수급 {st['flow_days']}영업일")
        self.lbl_runs.setText(f"{st['runs']}회")
        self.lbl_size.setText(f"{st['size_mb']:.1f} MB")

    def _set(self, path: str):
        self.path = path
        self._refresh()

    def _change(self):
        start = str(Path(self.path).parent / DB_FILENAME)
        path, _ = QFileDialog.getSaveFileName(
            self, "DB 파일 위치 (기존 파일을 고르면 그 DB 를 사용)", start, "SQLite DB (*.db)",
            options=QFileDialog.DontConfirmOverwrite)
        if not path:
            return
        if not path.lower().endswith(".db"):
            path += ".db"
        try:
            StockDB(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "DB", f"이 위치에 DB 를 만들 수 없습니다:\n{e}")
            return
        self._set(path)


# ============================================================================
# screener/gui/performance_view.py
# ============================================================================
"""저장한 신호의 이후 수익률 화면."""

import json
import math

import pandas as pd
from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QSplitter, QTableView, QVBoxLayout,
)


# (컬럼, 헤더, 형식) — 형식 "pct" 는 수익률(색 표시)
DETAIL_COLUMNS = [
    ("asof", "신호일", "date"), ("ticker", "코드", "str"), ("name", "종목명", "str"),
    ("market", "시장", "str"), ("vol_ratio", "거래량배수", "{:.1f}x"), ("entry", "진입가", "{:,.0f}"),
    ("r1", "+1일", "pct"), ("r5", "+5일", "pct"), ("r10", "+10일", "pct"), ("r20", "+20일", "pct"),
    ("max_gain", "20일 최고", "pct"), ("max_loss", "20일 최저", "pct"),
    ("last_ret", "현재까지", "pct"), ("held", "경과일", "{:.0f}"),
]


def _params_summary(raw: str) -> str:
    try:
        p = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    s = f"거래량≥{p.get('vol_ratio_min', 0):g}배 · 등락±{p.get('price_chg_max', 0):g}% · 최근{p.get('recent_days', 1)}일"
    if p.get("flow_enabled"):
        s += " · 수급"
    return s


class FrameModel(QAbstractTableModel):
    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.df = pd.DataFrame()

    def set_frame(self, df: pd.DataFrame):
        self.beginResetModel()
        self.df = df.reset_index(drop=True)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.df)

    def columnCount(self, parent=QModelIndex()):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.columns[section][1]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        col, _, fmt = self.columns[index.column()]
        v = self.df[col].iat[index.row()] if col in self.df else None
        missing = v is None or (isinstance(v, float) and math.isnan(v))
        if role == Qt.DisplayRole:
            if missing:
                return "–"
            if fmt == "pct":
                return f"{v * 100:+.2f}%"
            if fmt == "date":
                return f"{v[:4]}-{v[4:6]}-{v[6:]}"
            if fmt == "str":
                return str(v)
            return fmt.format(v)
        if role == Qt.UserRole:
            if missing:
                return -1e18  # 정렬 시 맨 아래로
            return v.item() if hasattr(v, "item") else v
        if role == Qt.TextAlignmentRole:
            return int((Qt.AlignLeft if fmt in ("str", "date") else Qt.AlignRight) | Qt.AlignVCenter)
        if role == Qt.ForegroundRole:
            if fmt == "pct" and not missing:
                return QColor(UP if v > 0 else DOWN if v < 0 else MUTED)
            if missing or col == "market":
                return QColor(MUTED)
        return None


class PerformanceDialog(QDialog):
    def __init__(self, db_path: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("신호 성과 — 저장한 스크리닝 결과의 이후 수익률")
        self.resize(1280, 820)
        self.db = StockDB(db_path)
        self.detail = pd.DataFrame()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        top = QHBoxLayout()
        self.run_combo = QComboBox()
        self.run_combo.setMinimumWidth(420)
        self.entry_combo = QComboBox()
        for k, v in ENTRY_MODES.items():
            self.entry_combo.addItem(v, k)
        refresh = QPushButton("새로고침")
        refresh.clicked.connect(self.reload)
        export = QPushButton("CSV 내보내기", objectName="Ghost")
        export.clicked.connect(self.export_csv)
        top.addWidget(QLabel("대상"))
        top.addWidget(self.run_combo)
        top.addWidget(QLabel("진입가"))
        top.addWidget(self.entry_combo)
        top.addStretch(1)
        top.addWidget(refresh)
        top.addWidget(export)
        lay.addLayout(top)

        self.hint = QLabel("", objectName="CardHint")
        self.hint.setWordWrap(True)
        lay.addWidget(self.hint)

        chips = QHBoxLayout()
        chips.setSpacing(10)
        self.chip_n = StatChip("신호 수", color=TEXT)
        chips.addWidget(self.chip_n)
        self.chips = {}
        for h in HORIZONS:
            c = StatChip(f"+{h}일 평균 · 승률 · 시장대비", color=TEXT)
            self.chips[h] = c
            chips.addWidget(c)
        chips.addStretch(1)
        lay.addLayout(chips)

        split = QSplitter(Qt.Vertical)
        split.setHandleWidth(10)
        self.model = FrameModel(DETAIL_COLUMNS, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setShowGrid(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for i, (c, *_r) in enumerate(DETAIL_COLUMNS):
            self.table.setColumnWidth(i, {"asof": 96, "ticker": 70, "name": 130, "market": 70}.get(c, 86))
        self.table.doubleClicked.connect(self._open_naver)
        self.table.setToolTip("더블클릭: 네이버 금융")
        split.addWidget(self.table)
        self.chart = ChartWidget()
        split.addWidget(self.chart)
        split.setSizes([480, 260])
        lay.addWidget(split, 1)

        self._fill_runs()
        self.run_combo.currentIndexChanged.connect(self.reload)
        self.entry_combo.currentIndexChanged.connect(self.reload)
        self.reload()

    def _fill_runs(self):
        self.run_combo.clear()
        runs = self.db.list_runs()
        self.run_combo.addItem(f"전체 저장 결과 ({len(runs)}회, 같은 날 같은 종목은 1번만)", None)
        for r in runs.itertuples():
            d = r.asof
            self.run_combo.addItem(
                f"{d[:4]}-{d[4:6]}-{d[6:]} · {r.n_hits}종목 · {_params_summary(r.params)}", int(r.run_id))

    def reload(self):
        run_id = self.run_combo.currentData()
        entry = self.entry_combo.currentData() or "close"
        detail, summary, path = from_db(
            self.db, run_ids=None if run_id is None else [run_id], entry=entry)
        self.detail = detail
        self.model.set_frame(detail)
        self.table.sortByColumn(0, Qt.DescendingOrder)
        self.chip_n.set(f"{len(detail):,}")
        if summary.empty:
            for c in self.chips.values():
                c.set("–")
            self.hint.setText("저장된 결과가 없습니다. 메인 화면에서 KRX 데이터로 스크리닝한 뒤 "
                              "‘결과 DB 저장’을 누르면, 이후 매일 ‘데이터 불러오기’를 할 때마다 수익률이 채워집니다.")
            self.chart.show_message("저장된 신호가 없습니다")
            return
        for row in summary.itertuples():
            if row.n == 0:
                self.chips[row.horizon].set("데이터 대기")
                continue
            self.chips[row.horizon].set(
                f"{row.mean * 100:+.1f}% · {row.win_rate * 100:.0f}% · {row.excess * 100:+.1f}%p")
        last = self.db.stats()["last"] or ""
        self.hint.setText(
            "수익률 = N영업일 뒤 종가 ÷ 진입가 − 1. 시장대비 = 같은 기간 전종목(KOSPI·KOSDAQ) 동일가중 평균 수익률과의 차이. "
            f"DB 시세 마지막 날짜: {last[:4]}-{last[4:6]}-{last[6:]} — 이후 날짜는 ‘데이터 불러오기’를 하면 채워집니다. "
            "수수료·세금 미반영.")
        self._plot(path)

    def _plot(self, path: pd.DataFrame):
        p = path[path["n"] > 0]
        if p.empty:
            self.chart.show_message("신호 이후 시세가 아직 없습니다 — 다음 거래일 이후 확인하세요")
            return
        fig = self.chart.fig
        fig.clear()
        ax = fig.add_subplot(111)
        self.chart._style(ax)
        ax.axhline(0, color=BORDER, linewidth=1)
        ax.plot(p["day"], p["signal"] * 100, color=ACCENT, marker="o", markersize=3, label="신호 평균")
        ax.plot(p["day"], p["bench"] * 100, color=MUTED, linestyle="--", label="시장 평균")
        ax.fill_between(p["day"], p["signal"] * 100, p["bench"] * 100,
                        where=p["signal"] >= p["bench"], color=UP, alpha=0.12, interpolate=True)
        ax.fill_between(p["day"], p["signal"] * 100, p["bench"] * 100,
                        where=p["signal"] < p["bench"], color=DOWN, alpha=0.12, interpolate=True)
        ax.set_xlabel("신호 이후 영업일", color=MUTED, fontsize=9)
        ax.set_ylabel("평균 누적수익률 (%)", color=MUTED, fontsize=9)
        ax.set_xticks(list(p["day"]))
        leg = ax.legend(facecolor=SURFACE, edgecolor=BORDER, fontsize=9)
        for t in leg.get_texts():
            t.set_color(TEXT)
        fig.subplots_adjust(left=0.07, right=0.98, top=0.9, bottom=0.18)
        fig.suptitle("신호 이후 평균 누적수익률 vs 시장", color=TEXT, fontsize=11, x=0.07, ha="left")
        self.chart.draw_idle()

    def _open_naver(self, idx):
        ticker = self.proxy.index(idx.row(), 1).data()
        QDesktopServices.openUrl(QUrl(f"https://finance.naver.com/item/main.naver?code={ticker}"))

    def export_csv(self):
        if self.detail.empty:
            return
        path, _ = QFileDialog.getSaveFileName(self, "CSV 저장", "signal_performance.csv", "CSV (*.csv)")
        if path:
            cols = [c for c, *_ in DETAIL_COLUMNS if c in self.detail]
            out = self.detail[cols].copy()
            for c, _h, f in DETAIL_COLUMNS:
                if f == "pct" and c in out:
                    out[c] = (out[c] * 100).round(2)
            out.rename(columns={c: h for c, h, _ in DETAIL_COLUMNS}).to_csv(
                path, index=False, encoding="utf-8-sig")


# ============================================================================
# screener/gui/backtest_view.py
# ============================================================================
"""백테스트 창: 과거 데이터 수집 · 매매 시뮬레이션 · 조건 최적화 · 요인 분석 · 모의투자."""

import json
import traceback
from dataclasses import replace

import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
from PySide6.QtCore import QDate, QSortFilterProxyModel, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTableView, QTabWidget, QVBoxLayout, QWidget,
)


TRADE_COLUMNS = [
    ("entry_date", "진입일", "date"), ("exit_date", "청산일", "date"), ("ticker", "코드", "str"),
    ("name", "종목명", "str"), ("market", "시장", "str"), ("entry", "진입가", "{:,.0f}"),
    ("exit", "청산가", "{:,.0f}"), ("ret", "수익률", "pct"), ("days", "보유일", "{:.0f}"),
    ("reason", "청산 사유", "str"),
]
FACTOR_COLUMNS = [
    ("label", "지표", "str"), ("ic", "수익 관련성(IC)", "{:+.3f}"), ("weight", "학습 가중치", "{:+.4f}"),
    ("days", "분석 일수", "{:.0f}"),
]


class TaskWorker(QThread):
    """fn(progress, cancelled) 을 백그라운드에서 실행."""

    progress = Signal(int, int, str)
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            self.done.emit(self.fn(self.progress.emit, lambda: self._cancel))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.failed.emit(str(e))


def _table(model) -> QTableView:
    proxy = QSortFilterProxyModel()
    proxy.setSourceModel(model)
    proxy.setSortRole(Qt.UserRole)
    t = QTableView()
    t.setModel(proxy)
    t._proxy = proxy  # 참조 유지
    t.setSortingEnabled(True)
    t.setAlternatingRowColors(True)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.verticalHeader().setVisible(False)
    t.verticalHeader().setDefaultSectionSize(26)
    t.setShowGrid(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def _pct(v, signed=True):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "–"
    return f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%"


class BacktestWindow(QDialog):
    def __init__(self, main):
        super().__init__(main, Qt.Window)
        self.main = main
        self.setWindowTitle("백테스트 · 최적화 · 요인 분석 · 모의투자")
        self.resize(1440, 900)
        self._worker: TaskWorker | None = None
        self._panel = None
        self._cands = None
        self._cands_key = None
        self._grid = None
        self._factor = None

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.addWidget(self._build_side())
        right = QVBoxLayout()
        right.addLayout(self._build_top())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_result_tab(), "결과")
        self.tabs.addTab(self._build_grid_tab(), "조건 최적화")
        self.tabs.addTab(self._build_factor_tab(), "요인 분석 · 학습 점수")
        right.addWidget(self.tabs, 1)
        root.addLayout(right, 1)
        self.refresh_db_info()

    # ------------------------------------------------------------------ UI
    def _build_top(self):
        lay = QHBoxLayout()
        self.db_info = QLabel("", objectName="CardHint")
        self.progress = QProgressBar()
        self.progress.setFixedWidth(260)
        self.progress.hide()
        self.status = QLabel("", objectName="CardHint")
        self.stop_btn = QPushButton("중지", objectName="Ghost")
        self.stop_btn.clicked.connect(self._cancel)
        self.stop_btn.hide()
        lay.addWidget(self.db_info, 1)
        lay.addWidget(self.status)
        lay.addWidget(self.progress)
        lay.addWidget(self.stop_btn)
        return lay

    def _build_side(self):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(340)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(12)

        dc = Card("① 과거 데이터 수집", "KRX 에서 과거 전종목 시세(상장폐지 종목 포함)를 DB 에 받습니다. "
                                      "이미 받은 날짜는 건너뛰어 중간에 멈춰도 이어받습니다.")
        self.years = QSpinBox()
        self.years.setRange(1, 10)
        self.years.setValue(3)
        self.years.setSuffix(" 년")
        self.with_flows = QCheckBox("외국인·기관 순매수 포함 (시간 약 3배)")
        self.bf_btn = QPushButton("과거 데이터 수집")
        self.bf_btn.clicked.connect(self.run_backfill)
        dc.body.addLayout(hbox(QLabel("기간", objectName="ParamLabel"), None, self.years))
        dc.body.addWidget(self.with_flows)
        dc.body.addWidget(self.bf_btn)
        lay.addWidget(dc)

        P = ParamSlider
        rc = Card("② 매매 규칙", "신호 다음날 시가에 사서 아래 규칙으로 판다고 가정합니다.")
        self.r_stop = P("손절 (0=안 함)", 0, 30, 0.5, 5, "%", 1)
        self.r_target = P("익절 (0=안 함)", 0, 100, 1, 15, "%", 0)
        self.r_hold = P("최대 보유", 1, 120, 1, 20, "일", 0)
        self.r_cost = P("왕복 비용", 0, 2, 0.05, 0.25, "%", 2, "수수료 + 매도 세금 + 체결 오차(슬리피지)")
        self.r_pos = P("동시 보유 종목", 1, 20, 1, 5, "개", 0, "자금을 N 등분해서 투자")
        self.r_day = P("하루 신규 진입", 1, 10, 1, 2, "개", 0, "같은 날 신호가 많으면 점수 상위부터")
        self.r_regime = QCheckBox("하락장 진입 금지 (지수 < 이동평균)")
        self.r_regime.setChecked(True)
        self.r_ma = P("이동평균", 20, 200, 5, 60, "일", 0)
        self.r_regime.toggled.connect(self.r_ma.setEnabled)
        for w in (self.r_stop, self.r_target, self.r_hold, self.r_cost, self.r_pos, self.r_day,
                  self.r_regime, self.r_ma):
            rc.body.addWidget(w)
        lay.addWidget(rc)

        sc = Card("③ 실행", "조건은 메인 화면 왼쪽에 지금 설정된 값을 그대로 사용합니다.")
        self.source = QComboBox()
        self.source.addItem("과거 백테스트 (현재 조건으로 매일 스크리닝했다면)", "backtest")
        self.source.addItem("모의투자 (DB 에 저장된 실제 신호로 매매했다면)", "paper")
        self.start = QDateEdit(calendarPopup=True)
        self.end = QDateEdit(calendarPopup=True)
        for d in (self.start, self.end):
            d.setDisplayFormat("yyyy-MM-dd")
        self.params_lbl = QLabel("", objectName="CardHint")
        self.params_lbl.setWordWrap(True)
        self.run_btn = QPushButton("백테스트 실행", objectName="Primary")
        self.run_btn.clicked.connect(self.run_backtest)
        sc.body.addWidget(self.source)
        sc.body.addLayout(hbox(QLabel("시작", objectName="ParamLabel"), None, self.start))
        sc.body.addLayout(hbox(QLabel("종료", objectName="ParamLabel"), None, self.end))
        sc.body.addWidget(self.params_lbl)
        sc.body.addWidget(self.run_btn)
        lay.addWidget(sc)
        lay.addStretch(1)
        scroll.setWidget(inner)
        return scroll

    def _build_result_tab(self):
        w = QWidget(objectName="Root")
        lay = QVBoxLayout(w)
        chips = QHBoxLayout()
        self.k = {}
        for key, label in (("trades", "거래 수"), ("win_rate", "승률"), ("avg_ret", "거래당 평균"),
                           ("total", "누적 수익"), ("cagr", "연환산"), ("mdd", "최대 낙폭"),
                           ("bench_total", "시장(전종목 평균)")):
            c = StatChip(label, color=TEXT)
            c.setMinimumWidth(112)
            self.k[key] = c
            chips.addWidget(c)
        chips.addStretch(1)
        lay.addLayout(chips)
        self.result_note = QLabel("", objectName="CardHint")
        self.result_note.setWordWrap(True)
        lay.addWidget(self.result_note)
        split = QSplitter(Qt.Vertical)
        self.eq_chart = ChartWidget()
        self.eq_chart.show_message("왼쪽에서 ‘백테스트 실행’")
        split.addWidget(self.eq_chart)
        self.trade_model = FrameModel(TRADE_COLUMNS, self)
        self.trade_table = _table(self.trade_model)
        for i, (c, *_r) in enumerate(TRADE_COLUMNS):
            self.trade_table.setColumnWidth(i, {"name": 130, "reason": 90}.get(c, 90))
        split.addWidget(self.trade_table)
        split.setSizes([420, 300])
        lay.addWidget(split, 1)
        return w

    def _build_grid_tab(self):
        w = QWidget(objectName="Root")
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.g_h = QComboBox()
        for h in FWD_HORIZONS:
            self.g_h.addItem(f"{h}일 보유 수익", h)
        self.g_h.setCurrentIndex(1)
        self.g_top = QSpinBox()
        self.g_top.setRange(0, 20)
        self.g_top.setValue(0)
        self.g_top.setSpecialValueText("전체 신호")
        self.g_top.setPrefix("날짜별 상위 ")
        self.g_top.setToolTip("0 = 조건에 맞는 모든 신호, N = 날짜별 점수 상위 N개만 (실제 매매처럼)")
        run = QPushButton("최적화 실행", objectName="Primary")
        run.clicked.connect(self.run_grid)
        self.g_apply = QPushButton("추천 조합을 메인 화면에 적용")
        self.g_apply.clicked.connect(self.apply_grid)
        self.g_apply.setEnabled(False)
        for x in (QLabel("평가 기준"), self.g_h, self.g_top, run):
            top.addWidget(x)
        top.addStretch(1)
        top.addWidget(self.g_apply)
        lay.addLayout(top)
        self.g_note = QLabel("거래량 배수 × 허용 등락률 조합별로, 앞쪽 2/3 기간(학습)과 뒤쪽 1/3 기간(검증)의 "
                             "신호당 평균 수익(비용 차감)을 비교합니다. 학습 기간에서 고른 조합이 검증 기간에서도 "
                             "좋아야 믿을 만합니다.", objectName="CardHint")
        self.g_note.setWordWrap(True)
        lay.addWidget(self.g_note)
        self.g_chart = ChartWidget()
        self.g_chart.show_message("‘최적화 실행’ — 처음에는 후보 계산에 수십 초 걸립니다")
        lay.addWidget(self.g_chart, 1)
        return w

    def _build_factor_tab(self):
        w = QWidget(objectName="Root")
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.f_h = QComboBox()
        for h in FWD_HORIZONS:
            self.f_h.addItem(f"{h}일 보유 수익", h)
        self.f_h.setCurrentIndex(1)
        run = QPushButton("분석 실행", objectName="Primary")
        run.clicked.connect(self.run_factor)
        self.f_apply = QPushButton("학습 점수를 메인 화면 점수로 사용")
        self.f_apply.clicked.connect(self.apply_factor)
        self.f_apply.setEnabled(False)
        for x in (QLabel("평가 기준"), self.f_h, run):
            top.addWidget(x)
        top.addStretch(1)
        top.addWidget(self.f_apply)
        lay.addLayout(top)
        self.f_note = QLabel("후보 신호의 각 지표가 이후 수익과 얼마나 관련 있었는지(IC: 날짜별 순위상관 평균, "
                             "±0.03 이상이면 의미 있는 편)와, 학습 기간 데이터로 정한 가중치 점수가 "
                             "검증 기간에서 기본 점수보다 나았는지 보여줍니다.", objectName="CardHint")
        self.f_note.setWordWrap(True)
        lay.addWidget(self.f_note)
        self.f_result = QLabel("")
        self.f_result.setWordWrap(True)
        self.f_result.setStyleSheet("font-size: 14px;")
        lay.addWidget(self.f_result)
        self.f_model = FrameModel(FACTOR_COLUMNS, self)
        self.f_table = _table(self.f_model)
        self.f_table.setColumnWidth(0, 260)
        self.f_table.setSortingEnabled(False)  # 관련성 절대값 순 유지
        lay.addWidget(self.f_table, 1)
        return w

    # ------------------------------------------------------------------ 공통
    @property
    def db(self) -> StockDB:
        return StockDB(self.main.db_path)

    def refresh_db_info(self):
        try:
            st = self.db.stats()
        except Exception as e:  # noqa: BLE001
            self.db_info.setText(f"DB 오류: {e}")
            return
        fmt = lambda d: f"{d[:4]}-{d[4:6]}-{d[6:]}" if d else "–"  # noqa: E731
        self.db_info.setText(f"DB: {fmt(st['first'])} ~ {fmt(st['last'])} · {st['days']}영업일 · "
                             f"수급 {st['flow_days']}일 · 저장 결과 {st['runs']}회 · {st['size_mb']:.0f}MB")
        if st["first"]:
            first = QDate.fromString(st["first"], "yyyyMMdd")
            last = QDate.fromString(st["last"], "yyyyMMdd")
            for d in (self.start, self.end):
                d.setDateRange(first, last)
            if self.start.date() <= first or self.start.date() > last:
                self.start.setDate(first.addDays(120))  # 지표 계산용 앞부분 여유
            self.end.setDate(last)
        p = self.main.params()
        self.params_lbl.setText(
            f"현재 조건: 거래량 ≥{p.vol_ratio_min:g}배 · 등락 ±{p.price_chg_max:g}% · 최근 {p.recent_days}일 · "
            f"거래대금 ≥{p.min_value_eok:g}억" + (" · 수급" if p.flow_enabled else "")
            + (" · 바닥권" if p.pos_enabled else "") + (" · OBV" if p.obv_enabled else "")
            + (" · 학습점수" if p.score_weights else ""))

    def rules(self) -> TradeRules:
        return TradeRules(
            stop_pct=self.r_stop.value(), target_pct=self.r_target.value(), max_hold=int(self.r_hold.value()),
            cost_pct=self.r_cost.value(), max_positions=int(self.r_pos.value()), per_day=int(self.r_day.value()),
            regime_filter=self.r_regime.isChecked(), regime_ma=int(self.r_ma.value()))

    def _busy(self, on: bool, msg: str = ""):
        for b in (self.run_btn, self.bf_btn):
            b.setEnabled(not on)
        self.progress.setVisible(on)
        self.stop_btn.setVisible(on)
        self.status.setText(msg)

    def _start(self, fn, on_done, msg):
        if self._worker is not None and self._worker.isRunning():
            QMessageBox.information(self, "백테스트", "다른 작업이 진행 중입니다.")
            return
        self._busy(True, msg)
        self._worker = TaskWorker(fn)
        self._worker.progress.connect(self._on_progress)
        self._worker.done.connect(lambda r: (self._busy(False), on_done(r)))
        self._worker.failed.connect(lambda m: (self._busy(False), QMessageBox.critical(self, "오류", m)))
        self._worker.start()

    def _on_progress(self, i, n, msg):
        self.progress.setMaximum(max(n, 1))
        self.progress.setValue(i)
        self.status.setText(msg)

    def _cancel(self):
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("중지하는 중…")

    def shutdown(self):
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(10000)

    def _date_str(self, w: QDateEdit) -> str:
        return w.date().toString("yyyyMMdd")

    def _ensure_candidates(self, progress, cancelled):
        """현재 조건·기간의 후보 풀 (같은 설정이면 재사용)."""
        p = self.main.params()
        if self._panel is None:
            progress(0, 1, "DB 에서 과거 시세 읽는 중…")
            self._panel = load_history(self.db)
        key = json.dumps([loose_params(p).to_dict(), p.pos_days, self._date_str(self.start),
                          self.r_cost.value()], sort_keys=True, default=str)
        if self._cands is None or self._cands_key != key:
            cands = build_candidates(self._panel, p, start=self._date_str(self.start), progress=progress,
                                        cancelled=cancelled, cost_pct=self.r_cost.value())
            if cancelled():
                raise RuntimeError("중지했습니다.")
            self._cands, self._cands_key = cands, key
        end = pd.Timestamp(self.end.date().toPython())
        cands = self._cands
        return cands[cands["date"] <= end] if not cands.empty else cands

    # ------------------------------------------------------------------ 과거 데이터 수집
    def run_backfill(self):
        m = self.main
        if not m.ensure_unlocked():
            return
        if not (m.creds.krx_id and m.creds.krx_pw):
            QMessageBox.information(self, "계정", "메인 화면 ‘계정’에서 KRX ID/비밀번호를 먼저 입력하세요.")
            return
        years, flows = self.years.value(), self.with_flows.isChecked()
        calls = years * 248 * 2 * (3 if flows else 1)
        if QMessageBox.question(
                self, "과거 데이터 수집",
                f"{years}년치 약 {calls:,}회 KRX 조회 (이미 받은 날짜 제외)\n"
                f"예상 시간: {calls * 0.8 / 60:.0f}~{calls * 1.5 / 60:.0f}분.\n"
                "중간에 ‘중지’하거나 창을 닫아도 다음에 이어서 받습니다. 시작할까요?") != QMessageBox.Yes:
            return
        db_path, krx_id, krx_pw = m.db_path, m.creds.krx_id, m.creds.krx_pw

        def job(progress, cancelled):
            progress(0, 1, "KRX 로그인…")
            krx_login(krx_id, krx_pw)
            return krx_backfill(StockDB(db_path), years=years, with_flows=flows,
                                progress=progress, cancelled=cancelled)

        def done(stats):
            self._panel = self._cands = None
            self.refresh_db_info()
            QMessageBox.information(
                self, "과거 데이터 수집",
                ("중지했습니다. 다시 누르면 이어서 받습니다.\n" if stats["cancelled"] else "완료했습니다.\n")
                + f"새로 받음 {stats['fetched']:,} · 이미 있음 {stats['skipped']:,} · 오류 {stats['errors']:,}")

        self._start(job, done, "과거 데이터 수집 중…")

    # ------------------------------------------------------------------ 백테스트
    def run_backtest(self):
        self.refresh_db_info()
        rules, source = self.rules(), self.source.currentData()
        end = pd.Timestamp(self.end.date().toPython())

        def job(progress, cancelled):
            if source == "paper":
                if self._panel is None:
                    progress(0, 1, "DB 에서 시세 읽는 중…")
                    self._panel = load_history(self.db)
                sig = saved_signals(self.db, self._panel)
            else:
                cands = self._ensure_candidates(progress, cancelled)
                sig = select(cands, self.main.params())
            sig = sig[sig["di"] < int(np.searchsorted(self._panel.dates, end, side="right"))] if len(sig) else sig
            progress(1, 1, "매매 시뮬레이션…")
            return simulate(sig, self._panel, rules) + (len(sig),)

        self._start(job, self._show_result, "백테스트 실행 중…")

    def _show_result(self, res):
        trades, equity, m, n_sig = res
        if equity.empty or not m.get("trades"):
            for c in self.k.values():
                c.set("–")
            self.eq_chart.show_message("신호가 없습니다 — 기간·조건을 확인하세요" if self.source.currentData() == "backtest"
                                       else "저장된 실제 신호가 없습니다 — 메인 화면에서 ‘결과 DB 저장’ 또는 자동 실행 후 확인")
            self.trade_model.set_frame(pd.DataFrame())
            self.result_note.setText(f"신호 {n_sig:,}개 · 매매 0건")
            return
        self.k["trades"].set(f"{m['trades']:,}")
        self.k["win_rate"].set(_pct(m.get("win_rate"), signed=False))
        self.k["avg_ret"].set(_pct(m.get("avg_ret")))
        self.k["total"].set(_pct(m["total"]))
        self.k["cagr"].set(_pct(m["cagr"]))
        self.k["mdd"].set(_pct(m["mdd"]))
        self.k["bench_total"].set(_pct(m["bench_total"]))
        for key in ("avg_ret", "total", "cagr"):
            v = m.get(key, 0) or 0
            self.k[key].value_lbl.setStyleSheet(f"color: {UP if v > 0 else DOWN};")
        self.result_note.setText(
            f"{m['start']:%Y-%m-%d} ~ {m['end']:%Y-%m-%d} · 신호 {n_sig:,}개 중 {m['trades']:,}건 매매 · "
            f"평균 보유 {m.get('avg_days', 0):.1f}일 · 평균 이익 {_pct(m.get('avg_win'))} / 평균 손실 {_pct(m.get('avg_loss'))} · "
            f"평균 투자 비중 {m['exposure'] * 100:.0f}% · 비용 {self.r_cost.value():.2f}% 차감 · "
            "과거 성과가 미래를 보장하지 않습니다.")
        t = trades.copy()
        for c in ("entry_date", "exit_date"):
            t[c] = pd.to_datetime(t[c]).dt.strftime("%Y%m%d")
        self.trade_model.set_frame(t)
        self.trade_table.sortByColumn(0, Qt.DescendingOrder)
        self._plot_equity(equity)

    def _plot_equity(self, eq: pd.DataFrame):
        fig = self.eq_chart.fig
        fig.clear()
        gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08, left=0.07, right=0.98, top=0.9, bottom=0.1)
        ax = fig.add_subplot(gs[0])
        axd = fig.add_subplot(gs[1], sharex=ax)
        for a in (ax, axd):
            self.eq_chart._style(a)
        ax.plot(eq["date"], (eq["equity"] - 1) * 100, color=ACCENT, linewidth=1.6, label="전략")
        ax.plot(eq["date"], (eq["bench"] - 1) * 100, color=MUTED, linewidth=1, linestyle="--", label="시장 평균")
        ax.axhline(0, color=BORDER, linewidth=1)
        ax.set_ylabel("누적 수익률 (%)", color=MUTED, fontsize=9)
        leg = ax.legend(facecolor=SURFACE, edgecolor=BORDER, fontsize=9, loc="upper left")
        for t in leg.get_texts():
            t.set_color(TEXT)
        dd = (eq["equity"] / eq["equity"].cummax() - 1) * 100
        axd.fill_between(eq["date"], dd, 0, color=DOWN, alpha=0.35)
        axd.set_ylabel("낙폭 (%)", color=MUTED, fontsize=9)
        ax.tick_params(labelbottom=False)
        fig.suptitle("누적 수익률 · 최대 낙폭", color=TEXT, fontsize=11, x=0.07, ha="left")
        self.eq_chart.draw_idle()

    # ------------------------------------------------------------------ 최적화
    def run_grid(self):
        h, top = self.g_h.currentData(), self.g_top.value()
        p = self.main.params()

        def job(progress, cancelled):
            cands = self._ensure_candidates(progress, cancelled)
            progress(1, 1, "조합별 성과 계산…")
            return grid_search(cands, p, horizon=h, top_per_day=top)

        self._start(job, self._show_grid, "최적화 중…")

    def _show_grid(self, grid: pd.DataFrame):
        self._grid = grid
        p = self.main.params()
        best = best_cell(grid)
        self.g_apply.setEnabled(best is not None)
        vols, chgs = list(GRID_VOL), list(GRID_CHG)
        mat = grid.pivot(index="price_chg_max", columns="vol_ratio_min", values="train_mean").reindex(
            index=chgs, columns=vols).to_numpy() * 100
        fig = self.g_chart.fig
        fig.clear()
        ax = fig.add_subplot(111)
        self.g_chart._style(ax)
        ax.grid(False)
        lim = np.nanmax(np.abs(mat)) if np.isfinite(mat).any() else 1
        im = ax.imshow(mat, cmap="RdBu_r", vmin=-lim, vmax=lim, aspect="auto", origin="lower")
        ax.set_xticks(range(len(vols)), [f"{v:g}배" for v in vols])
        ax.set_yticks(range(len(chgs)), [f"±{c:g}%" for c in chgs])
        ax.set_xlabel("거래량 배수 ≥", color=MUTED)
        ax.set_ylabel("허용 등락률", color=MUTED)
        for r in grid.itertuples():
            i, j = chgs.index(r.price_chg_max), vols.index(r.vol_ratio_min)
            txt = (f"{r.train_mean * 100:+.2f}%\n검증 {r.test_mean * 100:+.2f}%\n{int(r.train_n)}건"
                   if r.train_n else "신호 없음")
            light = r.train_n and abs(r.train_mean * 100) < lim * 0.55  # 밝은 칸은 어두운 글씨
            ax.text(j, i, txt, ha="center", va="center", fontsize=7.5,
                    color="#1b2033" if light else TEXT)
        if best is not None:
            ax.add_patch(mpatches.Rectangle(
                (vols.index(best.vol_ratio_min) - 0.5, chgs.index(best.price_chg_max) - 0.5), 1, 1,
                fill=False, edgecolor=GOOD, linewidth=2.5))
        cb = fig.colorbar(im, ax=ax, fraction=0.03)
        cb.ax.tick_params(colors=MUTED, labelsize=8)
        fig.subplots_adjust(left=0.08, right=0.95, top=0.9, bottom=0.12)
        h = self.g_h.currentData()
        fig.suptitle(f"학습 기간 신호당 평균 {h}일 수익 (색) · 검증 기간 결과 · 초록 테두리 = 추천",
                     color=TEXT, fontsize=11, x=0.08, ha="left")
        self.g_chart.draw_idle()

        cur = grid[(grid.vol_ratio_min == min(vols, key=lambda v: abs(v - p.vol_ratio_min)))
                   & (grid.price_chg_max == min(chgs, key=lambda c: abs(c - p.price_chg_max)))]
        msg = f"검증 시작일 {grid.attrs['split']:%Y-%m-%d}. "
        if best is not None:
            msg += (f"추천: 거래량 ≥{best.vol_ratio_min:g}배 · 등락 ±{best.price_chg_max:g}% → 학습 "
                    f"{best.train_mean * 100:+.2f}% ({int(best.train_n)}건, 승률 {best.train_win * 100:.0f}%), "
                    f"검증 {_pct(best.test_mean)} ({int(best.test_n)}건). ")
        else:
            msg += "학습 기간 신호 30건 이상인 조합이 없어 추천하지 않습니다. "
        if not cur.empty:
            c = cur.iloc[0]
            msg += f"현재 조건과 가장 가까운 칸: 학습 {_pct(c.train_mean)}, 검증 {_pct(c.test_mean)}."
        self.g_note.setText(msg)

    def apply_grid(self):
        best = best_cell(self._grid) if self._grid is not None else None
        if best is None:
            return
        p = self.main.params()
        self.main.set_params(replace(p, vol_ratio_min=float(best.vol_ratio_min),
                                     price_chg_max=float(best.price_chg_max),
                                     daily_chg_max=max(p.daily_chg_max, float(best.price_chg_max))))
        self.refresh_db_info()
        QMessageBox.information(self, "적용", "메인 화면 조건에 적용했습니다. ‘결과’ 탭에서 다시 백테스트해 보세요.")

    # ------------------------------------------------------------------ 요인 분석
    def run_factor(self):
        h = self.f_h.currentData()
        p = self.main.params()

        def job(progress, cancelled):
            cands = self._ensure_candidates(progress, cancelled)
            progress(1, 1, "요인 분석…")
            return factor_analysis(cands, replace(p, score_weights=None), horizon=h)

        self._start(job, self._show_factor, "요인 분석 중…")

    def _show_factor(self, fa: dict):
        if "error" in fa:
            self.f_result.setText(fa["error"])
            self.f_apply.setEnabled(False)
            return
        self._factor = fa
        self.f_model.set_frame(fa["table"])
        t = fa["test"]
        if t:
            better = (t["learned_top"]["mean"] or 0) > (t["base_top"]["mean"] or 0)
            self.f_result.setText(
                f"검증 기간({fa['split']:%Y-%m-%d}~) 날짜별 상위 3개 평균 {fa['horizon']}일 수익 — "
                f"<b>학습 점수 {_pct(t['learned_top']['mean'], True)}</b> (승률 {_pct(t['learned_top']['win'], False)}) vs "
                f"기본 점수 {_pct(t['base_top']['mean'])} (승률 {_pct(t['base_top']['win'], False)}) · "
                f"후보 전체 평균 {_pct(t['all']['mean'])}. "
                + ("<span style='color:%s'>학습 점수가 더 나았습니다.</span>" % GOOD if better else
                   "<span style='color:%s'>학습 점수가 낫지 않았습니다 — 적용을 권하지 않습니다.</span>" % WARN))
            self.f_apply.setEnabled(True)
        else:
            self.f_result.setText("검증 기간 데이터가 없습니다.")
            self.f_apply.setEnabled(False)

    def apply_factor(self):
        if not self._factor:
            return
        self.main._set_weights(self._factor["weights"])
        self.refresh_db_info()
        QMessageBox.information(self, "적용", "메인 화면 점수를 학습 점수로 바꿨습니다. (⑥ 추가 필터에서 되돌릴 수 있음)")


# ============================================================================
# screener/gui/main_window.py
# ============================================================================
import datetime as dt
import json

import pandas as pd
from PySide6.QtCore import QDate, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDateEdit, QDialog, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSplitter, QTableView, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)


PRESETS: dict[str, ScreenParams] = {
    "기본 · 1일 거래량 폭발 + 보합": ScreenParams(),
    "매집 의심 · 3일 누적 급증 + 횡보": ScreenParams(
        recent_days=3, vol_ratio_min=2.5, price_chg_max=3.0, daily_chg_max=3.0, range_max=5.0),
    "강한 급증 · 5배 이상 + ±1%": ScreenParams(
        vol_ratio_min=5.0, price_chg_max=1.0, daily_chg_max=1.5, range_max=4.0),
    "매집 확인 · 급증 + 보합 + 외국인/기관 순매수": ScreenParams(
        recent_days=1, vol_ratio_min=3.0, price_chg_max=2.0,
        flow_enabled=True, flow_mode="either", flow_days=3, flow_min_eok=1.0, flow_min_buy_days=1),
    "박스권 내 거래량 급증": ScreenParams(
        base_days=40, vol_ratio_min=3.0, price_chg_max=2.5, use_box=True, box_range_max=12.0),
}



class MainWindow(QMainWindow):
    def __init__(self, demo: bool = False):
        super().__init__()
        self.setWindowTitle("Volume Spike Screener — 거래량 급증 · 가격 횡보 스크리너")
        self.resize(1480, 900)
        self.settings = QSettings("PCB_LT_Analyzer", "VolumeSpikeScreener")
        self.vault = Vault()
        self.creds: Credentials | None = None   # 잠금 해제 전에는 None
        self.session_pw: str | None = None
        self.panel: Panel | None = None
        self.result = pd.DataFrame()
        self._loader: LoadWorker | None = None
        self._dart_workers: list[DartWorker] = []
        self._dart_client = None
        self._selected: str | None = None
        self._panel_source = ""
        self._score_weights: dict | None = None
        self._disc_cache: dict[str, str] = {}
        self._disc_batch: DartBatchWorker | None = None
        self._bt_win = None
        self.db_path = self.settings.value("db_path") or str(default_db_path())

        self._debounce = QTimer(self, singleShot=True, interval=150)
        self._debounce.timeout.connect(self.rescreen)

        root = QWidget(objectName="Root")
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self._build_header(demo))

        split = QSplitter(Qt.Horizontal)
        split.setHandleWidth(10)
        split.addWidget(self._build_sidebar())
        split.addWidget(self._build_main())
        split.setStretchFactor(1, 1)
        split.setSizes([360, 1100])
        wrap = QHBoxLayout()
        wrap.setContentsMargins(12, 12, 12, 12)
        wrap.addWidget(split)
        v.addLayout(wrap, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(220)
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("기준일과 데이터 소스를 고르고 ‘데이터 불러오기’를 누르세요.")

        self._restore_params()
        export = QAction("CSV 내보내기", self, shortcut=QKeySequence("Ctrl+S"), triggered=self.export_csv)
        self.addAction(export)

    def shutdown(self):
        """백그라운드 스레드가 끝날 때까지 기다린다 (종료 시 크래시 방지)."""
        if self._bt_win is not None:
            self._bt_win.shutdown()
        for w in [self._loader, self._disc_batch, *self._dart_workers]:
            if w is not None and w.isRunning():
                w.wait(5000)
        self.chart.fig.clear()

    def closeEvent(self, e):
        self.shutdown()
        super().closeEvent(e)

    # ---------------------------------------------------------------- 레이아웃
    def _build_header(self, demo: bool) -> QFrame:
        h = QFrame(objectName="Header")
        lay = QHBoxLayout(h)
        lay.setContentsMargins(20, 14, 20, 14)
        lay.setSpacing(10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(QLabel("Volume Spike Screener", objectName="Title"))
        titles.addWidget(QLabel("평소보다 거래량이 터졌는데 가격은 그대로인 종목 · KOSPI / KOSDAQ",
                                objectName="Subtitle"))
        lay.addLayout(titles)
        lay.addStretch(1)

        self.source = QComboBox()
        self.source.addItem("KRX 실데이터", "krx")
        self.source.addItem("데모 데이터(오프라인)", "demo")
        self.source.setCurrentIndex(1 if demo else 0)
        self.date_edit = QDateEdit(QDate.currentDate(), calendarPopup=True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setToolTip("기준일 (휴장일이면 직전 영업일까지 사용)")

        self.load_btn = QPushButton("데이터 불러오기", objectName="Primary")
        self.load_btn.clicked.connect(self.load_data)
        acct = QPushButton("계정", objectName="Ghost")
        acct.clicked.connect(self.edit_account)
        export = QPushButton("CSV 내보내기", objectName="Ghost")
        export.clicked.connect(self.export_csv)
        save = QPushButton("결과 DB 저장", objectName="Ghost")
        save.setToolTip("현재 스크리닝 결과와 조건을 DB 에 기록 (나중에 신호 이후 수익률 추적용)")
        save.clicked.connect(self.save_results)
        bt_btn = QPushButton("백테스트", objectName="Ghost")
        bt_btn.setToolTip("3년치 과거 데이터로 조건 검증 · 최적화 · 요인 분석 · 모의투자")
        bt_btn.clicked.connect(self.open_backtest)
        perf = QPushButton("성과", objectName="Ghost")
        perf.setToolTip("저장한 스크리닝 결과의 이후 1·5·10·20일 수익률")
        perf.clicked.connect(lambda: PerformanceDialog(self.db_path, self).exec())
        db_btn = QPushButton("DB", objectName="Ghost")
        db_btn.setToolTip("데이터 저장 위치 · 저장된 기간 보기 / 위치 변경")
        db_btn.clicked.connect(self.open_db_dialog)
        for w in (QLabel("소스"), self.source, QLabel("기준일"), self.date_edit,
                  self.load_btn, acct, db_btn, save, perf, bt_btn, export):
            lay.addWidget(w)
        return h

    def _build_sidebar(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(330)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(12)

        # 프리셋
        pc = Card("프리셋", "자주 쓰는 조건 조합을 불러오거나 현재 설정을 저장합니다.")
        self.preset_combo = QComboBox()
        self._reload_presets()
        self.preset_combo.activated.connect(self._apply_preset)
        save = QPushButton("현재 설정 저장")
        save.clicked.connect(self._save_preset)
        pc.body.addWidget(self.preset_combo)
        pc.body.addWidget(save)
        lay.addWidget(pc)

        P = ParamSlider
        vc = Card("① 거래량 조건", "‘평소’(기준 구간) 대비 ‘최근’ 구간 거래량이 몇 배인지 봅니다.")
        self.p_base_days = P("기준 구간 (평소)", 5, 120, 1, 20, "일", 0,
                             "평소 거래량을 계산할 과거 영업일 수 (최근 구간 제외)")
        self.p_recent_days = P("최근 구간 (급증)", 1, 10, 1, 1, "일", 0,
                               "거래량 급증을 확인할 최근 영업일 수. 1 = 기준일 하루")
        self.p_vol_min = P("거래량 배수 ≥", 1, 30, 0.1, 3, "배", 1)
        self.p_vol_max = P("거래량 배수 ≤", 2, 200, 1, 100, "배", 0, "이상치(신규상장·거래재개) 제거용 상한")
        self.base_stat = QComboBox()
        self.base_stat.addItem("중앙값 (이상치에 강함)", "median")
        self.base_stat.addItem("평균", "mean")
        for w in (self.p_base_days, self.p_recent_days, self.p_vol_min, self.p_vol_max):
            vc.body.addWidget(w)
        vc.body.addLayout(hbox(QLabel("평소 거래량 계산", objectName="ParamLabel"), None, self.base_stat))
        lay.addWidget(vc)

        prc = Card("② 가격 조건", "거래량이 터진 동안 가격이 얼마나 ‘안 움직였는지’를 제한합니다.")
        self.p_chg = P("기간 등락률 |±|", 0, 15, 0.1, 2, "%", 1, "최근 구간 직전 종가 대비 기준일 종가")
        self.p_daily = P("일별 등락률 |±| 최대", 0, 30, 0.1, 3, "%", 1)
        self.p_range = P("일중 변동폭 최대", 0, 30, 0.1, 6, "%", 1, "(고가-저가)/전일종가 — 장중 흔들림 제한")
        self.use_box = QCheckBox("기준 구간 박스권(횡보) 조건 사용")
        self.p_box = P("박스 폭 최대", 2, 60, 0.5, 15, "%", 1, "기준 구간 (최고종가-최저종가)/평균종가")
        for w in (self.p_chg, self.p_daily, self.p_range, self.use_box, self.p_box):
            prc.body.addWidget(w)
        lay.addWidget(prc)

        lq = Card("③ 유동성 · 규모", "잡주/초소형주의 착시를 거릅니다. 0 = 제한 없음")
        self.p_value = P("거래대금 ≥", 0, 500, 0.5, 5, "억", 1, "최근 구간 평균 거래대금")
        self.p_price = P("주가 ≥", 0, 50000, 100, 1000, "원", 0)
        self.p_mcap_min = P("시가총액 ≥", 0, 50000, 50, 0, "억", 0)
        self.p_mcap_max = P("시가총액 ≤", 0, 500000, 500, 0, "억", 0)
        for w in (self.p_value, self.p_price, self.p_mcap_min, self.p_mcap_max):
            lq.body.addWidget(w)
        lay.addWidget(lq)

        mk = Card("④ 시장 · 제외")
        self.kospi = QCheckBox("KOSPI", checked=True)
        self.kosdaq = QCheckBox("KOSDAQ", checked=True)
        self.ex_pref = QCheckBox("우선주 제외", checked=True)
        self.ex_spac = QCheckBox("스팩 제외", checked=True)
        mk.body.addLayout(hbox(self.kospi, self.kosdaq, stretch_last=True))
        mk.body.addLayout(hbox(self.ex_pref, self.ex_spac, stretch_last=True))
        lay.addWidget(mk)

        fc = Card("⑤ 수급 · 외국인/기관 순매수",
                  f"기준일 포함 최근 N일(최대 {FLOW_DAYS}일)의 순매수 금액 합계와 순매수한 날 수로 거릅니다.")
        self.flow_on = QCheckBox("수급 조건 사용")
        self.flow_mode = QComboBox()
        for key, label in FLOW_MODES.items():
            self.flow_mode.addItem(label, key)
        self.p_flow_days = P("집계 기간", 1, FLOW_DAYS, 1, 3, "일", 0)
        self.p_flow_eok = P("순매수 금액 ≥", 0, 1000, 0.5, 0, "억", 1,
                            "기간 순매수 합계 하한. 0 이면 순매수(+)이기만 하면 통과")
        self.p_flow_buy_days = P("순매수한 날 ≥", 0, FLOW_DAYS, 1, 1, "일", 0,
                                 "집계 기간 중 순매수(+)였던 날의 수")
        self.flow_warn = QLabel("", objectName="CardHint")
        self.flow_warn.setStyleSheet(f"color: {WARN};")
        self.flow_warn.setWordWrap(True)
        self.flow_warn.hide()
        fc.body.addWidget(self.flow_on)
        fc.body.addWidget(self.flow_mode)
        for w in (self.p_flow_days, self.p_flow_eok, self.p_flow_buy_days, self.flow_warn):
            fc.body.addWidget(w)
        lay.addWidget(fc)

        xc = Card("⑥ 추가 필터", "백테스트로 효과를 확인한 뒤 켜는 것을 권합니다.")
        self.pos_on = QCheckBox("바닥권만 (저점 대비 상승률 제한)")
        self.p_pos_days = P("저점 기준 기간", 20, 250, 5, 60, "일", 0)
        self.p_pos_max = P("저점 대비 상승률 ≤", 0, 200, 1, 30, "%", 0,
                           "최근 N일 최저 종가 대비 현재가 상승률 — 고점권 '물량 넘기기' 회피")
        self.obv_on = QCheckBox("매집 추세 (OBV)")
        self.p_obv = P("OBV 추세 ≥", -1, 1, 0.05, 0.05, "", 2,
                       "기준+최근 구간 (상승일 거래량 − 하락일 거래량) ÷ 전체 거래량. +면 매집, −면 분배")
        self.ex_expiry = QCheckBox("옵션 만기일(매월 둘째 목요일) 신호 제외")
        self.hide_risk = QCheckBox("DART 위험 공시 종목 숨김 (유상증자·CB·감자 등)")
        self.hide_risk.setChecked(self.settings.value("hide_risk", "false") == "true")
        self.learned_lbl = QLabel("", objectName="CardHint")
        self.learned_lbl.setWordWrap(True)
        self.learned_clear = QPushButton("기본 점수로 되돌리기", objectName="Ghost")
        self.learned_clear.clicked.connect(lambda: self._set_weights(None))
        for w in (self.pos_on, self.p_pos_days, self.p_pos_max, self.obv_on, self.p_obv,
                  self.ex_expiry, self.hide_risk, self.learned_lbl, self.learned_clear):
            xc.body.addWidget(w)
        lay.addWidget(xc)

        reset = QPushButton("기본값으로 초기화", objectName="Ghost")
        reset.clicked.connect(lambda: self.set_params(ScreenParams()))
        lay.addWidget(reset)
        lay.addStretch(1)

        for s in self._sliders():
            s.valueChanged.connect(self._schedule)
        for c in (self.use_box, self.kospi, self.kosdaq, self.ex_pref, self.ex_spac, self.flow_on,
                  self.pos_on, self.obv_on, self.ex_expiry, self.hide_risk):
            c.toggled.connect(self._schedule)
        for chk, ws in ((self.pos_on, (self.p_pos_days, self.p_pos_max)), (self.obv_on, (self.p_obv,))):
            for w in ws:
                chk.toggled.connect(w.setEnabled)
                w.setEnabled(False)
        self._set_weights(None)
        self.base_stat.currentIndexChanged.connect(self._schedule)
        self.flow_mode.currentIndexChanged.connect(self._schedule)
        for w in (self.flow_mode, self.p_flow_days, self.p_flow_eok, self.p_flow_buy_days):
            self.flow_on.toggled.connect(w.setEnabled)
            w.setEnabled(False)
        self.use_box.toggled.connect(self.p_box.setEnabled)
        self.p_box.setEnabled(False)
        scroll.setWidget(inner)
        return scroll

    def _build_main(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 0, 0, 0)
        lay.setSpacing(10)

        self.chip_total = StatChip("검색 종목", color=TEXT)
        self.chip_hits = StatChip("조건 충족", color=GOOD)
        self.chip_kospi = StatChip("KOSPI", color=ACCENT)
        self.chip_kosdaq = StatChip("KOSDAQ", color=ACCENT_2)
        self.chip_asof = StatChip("기준일 · 데이터 기간", color=TEXT)
        self.chip_regime = StatChip("시장 추세 (지수 vs 60일선)", color=TEXT)
        chips = QHBoxLayout()
        chips.setSpacing(10)
        for c in (self.chip_total, self.chip_hits, self.chip_kospi, self.chip_kosdaq, self.chip_asof,
                  self.chip_regime):
            chips.addWidget(c)
        chips.addStretch(1)
        self.search = QLineEdit(placeholderText="종목명 / 코드 검색")
        self.search.setFixedWidth(220)
        chips.addWidget(self.search)
        lay.addLayout(chips)

        vsplit = QSplitter(Qt.Vertical)
        vsplit.setHandleWidth(10)

        self.model = ResultsModel(self)
        self.proxy = ResultsProxy(self)
        self.proxy.setSourceModel(self.model)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        hh.setHighlightSections(False)
        for i, (c, *_rest) in enumerate(COLUMNS):
            self.table.setColumnWidth(i, {"ticker": 70, "name": 130, "market": 70}.get(c, 92))
        self.table.selectionModel().currentRowChanged.connect(self._on_select)
        self.table.doubleClicked.connect(self._open_naver)
        self.table.setToolTip("더블클릭: 네이버 금융 종목 페이지 열기")
        vsplit.addWidget(self.table)

        bottom = QSplitter(Qt.Horizontal)
        bottom.setHandleWidth(10)
        self.chart = ChartWidget()
        bottom.addWidget(self.chart)
        self.disc = QTreeWidget()
        self.disc.setHeaderLabels(["일자", "공시 (DART)"])
        self.disc.setRootIsDecorated(False)
        self.disc.setColumnWidth(0, 82)
        self.disc.itemDoubleClicked.connect(
            lambda it, _c: it.data(0, Qt.UserRole) and QDesktopServices.openUrl(QUrl(it.data(0, Qt.UserRole))))
        self.disc.setToolTip("빨강=희석/위험 공시, 초록=호재성 공시. 더블클릭하면 DART 원문을 엽니다.")
        bottom.addWidget(self.disc)
        bottom.setSizes([700, 380])
        vsplit.addWidget(bottom)
        vsplit.setSizes([480, 340])
        lay.addWidget(vsplit, 1)
        return w

    # ---------------------------------------------------------------- 파라미터
    def _sliders(self) -> list[ParamSlider]:
        return [self.p_base_days, self.p_recent_days, self.p_vol_min, self.p_vol_max, self.p_chg,
                self.p_daily, self.p_range, self.p_box, self.p_value, self.p_price,
                self.p_mcap_min, self.p_mcap_max, self.p_flow_days, self.p_flow_eok,
                self.p_flow_buy_days, self.p_pos_days, self.p_pos_max, self.p_obv]

    def params(self) -> ScreenParams:
        markets = tuple(m for m, cb in (("KOSPI", self.kospi), ("KOSDAQ", self.kosdaq)) if cb.isChecked())
        return ScreenParams(
            base_days=int(self.p_base_days.value()), recent_days=int(self.p_recent_days.value()),
            base_stat=self.base_stat.currentData(),
            vol_ratio_min=self.p_vol_min.value(), vol_ratio_max=self.p_vol_max.value(),
            price_chg_max=self.p_chg.value(), daily_chg_max=self.p_daily.value(),
            range_max=self.p_range.value(), use_box=self.use_box.isChecked(),
            box_range_max=self.p_box.value(), min_value_eok=self.p_value.value(),
            min_price=int(self.p_price.value()), mcap_min_eok=self.p_mcap_min.value(),
            mcap_max_eok=self.p_mcap_max.value(), markets=markets,
            exclude_preferred=self.ex_pref.isChecked(), exclude_spac=self.ex_spac.isChecked(),
            flow_enabled=self.flow_on.isChecked(), flow_mode=self.flow_mode.currentData(),
            flow_days=int(self.p_flow_days.value()), flow_min_eok=self.p_flow_eok.value(),
            flow_min_buy_days=int(self.p_flow_buy_days.value()),
            pos_enabled=self.pos_on.isChecked(), pos_days=int(self.p_pos_days.value()),
            pos_max_pct=self.p_pos_max.value(), obv_enabled=self.obv_on.isChecked(),
            obv_min=self.p_obv.value(), exclude_expiry=self.ex_expiry.isChecked(),
            score_weights=self._score_weights,
        )

    def set_params(self, p: ScreenParams):
        self._debounce.blockSignals(True)
        pairs = [(self.p_base_days, p.base_days), (self.p_recent_days, p.recent_days),
                 (self.p_vol_min, p.vol_ratio_min), (self.p_vol_max, p.vol_ratio_max),
                 (self.p_chg, p.price_chg_max), (self.p_daily, p.daily_chg_max),
                 (self.p_range, p.range_max), (self.p_box, p.box_range_max),
                 (self.p_value, p.min_value_eok), (self.p_price, p.min_price),
                 (self.p_mcap_min, p.mcap_min_eok), (self.p_mcap_max, p.mcap_max_eok),
                 (self.p_flow_days, p.flow_days), (self.p_flow_eok, p.flow_min_eok),
                 (self.p_flow_buy_days, p.flow_min_buy_days), (self.p_pos_days, p.pos_days),
                 (self.p_pos_max, p.pos_max_pct), (self.p_obv, p.obv_min)]
        for w, val in pairs:
            w.setValue(val)
        self.base_stat.setCurrentIndex(max(0, self.base_stat.findData(p.base_stat)))
        self.use_box.setChecked(p.use_box)
        self.kospi.setChecked("KOSPI" in p.markets)
        self.kosdaq.setChecked("KOSDAQ" in p.markets)
        self.ex_pref.setChecked(p.exclude_preferred)
        self.ex_spac.setChecked(p.exclude_spac)
        self.flow_on.setChecked(p.flow_enabled)
        self.flow_mode.setCurrentIndex(max(0, self.flow_mode.findData(p.flow_mode)))
        self.pos_on.setChecked(p.pos_enabled)
        self.obv_on.setChecked(p.obv_enabled)
        self.ex_expiry.setChecked(p.exclude_expiry)
        self._set_weights(p.score_weights)
        self._debounce.blockSignals(False)
        self._schedule()

    def _restore_params(self):
        raw = self.settings.value("params")
        if raw:
            try:
                self.set_params(ScreenParams.from_dict(json.loads(raw)))
            except (ValueError, TypeError):
                pass

    def _custom_presets(self) -> dict:
        try:
            return json.loads(self.settings.value("presets", "{}"))
        except ValueError:
            return {}

    def _reload_presets(self):
        self.preset_combo.clear()
        self.preset_combo.addItem("— 프리셋 선택 —", None)
        for name in PRESETS:
            self.preset_combo.addItem(name, ("builtin", name))
        for name in self._custom_presets():
            self.preset_combo.addItem(f"★ {name}", ("custom", name))

    def _apply_preset(self, idx: int):
        data = self.preset_combo.itemData(idx)
        if not data:
            return
        kind, name = data
        p = PRESETS[name] if kind == "builtin" else ScreenParams.from_dict(self._custom_presets()[name])
        self.set_params(p)

    def _save_preset(self):
        name, ok = QInputDialog.getText(self, "프리셋 저장", "프리셋 이름")
        if ok and name.strip():
            presets = self._custom_presets()
            presets[name.strip()] = self.params().to_dict()
            self.settings.setValue("presets", json.dumps(presets, ensure_ascii=False))
            self._reload_presets()

    # ---------------------------------------------------------------- 데이터
    def ensure_unlocked(self) -> bool:
        """세션 중 한 번 잠금 해제하면 계정 정보를 메모리에 둔다."""
        if self.creds is not None:
            return True
        res = unlock(self.vault, self)
        if res is None:
            return False
        self.session_pw, self.creds = res
        self._dart_client = None
        return True

    def edit_account(self, already_unlocked: bool = False):
        """계정 화면 진입: 매번 계정 비밀번호를 확인한다."""
        if not already_unlocked:
            res = unlock(self.vault, self)
            if res is None:
                return
            self.session_pw, self.creds = res
        dlg = AccountDialog(self.vault, self.session_pw, self.creds, self)
        accepted = dlg.exec() == QDialog.Accepted
        if accepted or dlg.password != self.session_pw:
            self.creds = dlg.creds()  # 비밀번호 변경 시에도 화면 내용이 함께 저장됨
        self.session_pw = dlg.password
        self._dart_client = None
        if accepted:
            self.statusBar().showMessage("계정 정보를 암호화하여 저장했습니다.", 5000)
            if self._selected:
                self._load_disclosures(self._selected)

    def load_data(self):
        source = self.source.currentData()
        if source == "krx":
            if not self.ensure_unlocked():
                return
            if not (self.creds.krx_id and self.creds.krx_pw):
                QMessageBox.information(self, "계정", "KRX ID/비밀번호를 먼저 입력하세요.")
                self.edit_account(already_unlocked=True)
                if not (self.creds.krx_id and self.creds.krx_pw):
                    return
        p = self.params()
        # 슬라이더를 늘려도 다시 받지 않도록 여유 있게 가져온다
        days = max(60, p.required_days + 5)
        end = self.date_edit.date().toPython()
        creds = self.creds or Credentials()
        self._loader = LoadWorker(source, end, days, creds.krx_id, creds.krx_pw, db_path=self.db_path)
        self._loading_source = source
        self._loader.progress.connect(self._on_progress)
        self._loader.done.connect(self._on_loaded)
        self._loader.failed.connect(self._on_failed)
        self.load_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self._loader.start()

    def _on_progress(self, i: int, n: int, msg: str):
        self.progress.setMaximum(max(n, 1))
        self.progress.setValue(i)
        self.statusBar().showMessage(msg)

    def _on_loaded(self, panel: Panel):
        self.panel = panel
        self._panel_source = self._loading_source
        self.load_btn.setEnabled(True)
        self.progress.setVisible(False)
        d = panel.dates
        self.chip_total.set(f"{panel.close.shape[1]:,}")
        self.chip_asof.set(f"{d[-1]:%m/%d} · {len(d)}일")
        try:
            reg = current_regime(panel)
            self.chip_regime.set(" · ".join(f"{m} {'▲' if ok else '▼'}" for m, ok in reg.items()))
            self.chip_regime.value_lbl.setStyleSheet(f"color: {GOOD if all(reg.values()) else WARN};")
            self.chip_regime.setToolTip("▲ 지수가 60일 이동평균 위 (상승 추세) · ▼ 아래 (하락 추세 — 신호 신뢰도 낮음)")
        except Exception as e:  # noqa: BLE001
            self.chip_regime.set("–")
            print(f"시장 추세 계산 실패: {e}")
        self.statusBar().showMessage(
            f"{d[0]:%Y-%m-%d} ~ {d[-1]:%Y-%m-%d}, {panel.close.shape[1]:,}종목 로드 완료. "
            "왼쪽 조건을 조정하면 즉시 다시 스크리닝됩니다.")
        self.rescreen()

    def _on_failed(self, msg: str):
        self.load_btn.setEnabled(True)
        self.progress.setVisible(False)
        self.statusBar().showMessage("데이터 로딩 실패")
        QMessageBox.critical(self, "데이터 로딩 실패", msg)

    # ---------------------------------------------------------------- 스크리닝
    def _schedule(self, *_):
        self._debounce.start()

    def rescreen(self):
        p = self.params()
        self.settings.setValue("params", json.dumps(p.to_dict(), ensure_ascii=False))
        if self.panel is None:
            return
        if len(self.panel.dates) < p.required_days:
            self.statusBar().showMessage(
                f"⚠ 데이터 기간({len(self.panel.dates)}일)이 조건에 필요한 {p.required_days}일보다 짧습니다. "
                "‘데이터 불러오기’를 다시 눌러주세요.")
        no_flows = not self.panel.has_flows
        self.flow_warn.setText("⚠ 투자자별 순매수 데이터가 없어 수급 조건을 만족하는 종목이 없습니다. "
                               "데이터를 다시 불러오세요." if no_flows else "")
        self.flow_warn.setVisible(no_flows and p.flow_enabled)
        self.settings.setValue("hide_risk", "true" if self.hide_risk.isChecked() else "false")
        result = run_screen(self.panel, p)
        result["disc_flag"] = result["ticker"].map(self._disc_cache).fillna("")
        if self.hide_risk.isChecked():
            result = result[~result["disc_flag"].str.startswith("위험")].reset_index(drop=True)
        self.result = result
        self._fetch_disc_flags(result["ticker"])
        self.model.set_frame(self.result)
        self.table.sortByColumn(len(COLUMNS) - 1, Qt.DescendingOrder)  # 점수 내림차순
        mk = self.result["market"].value_counts()
        self.chip_hits.set(f"{len(self.result):,}")
        self.chip_kospi.set(f"{mk.get('KOSPI', 0):,}")
        self.chip_kosdaq.set(f"{mk.get('KOSDAQ', 0):,}")
        if len(self.result):
            row = self._row_of(self._selected) if self._selected else 0
            self.table.selectRow(row if row is not None else 0)
        else:
            self.chart.show_message("조건을 만족하는 종목이 없습니다 — 조건을 완화해 보세요")
            self.disc.clear()

    def _set_weights(self, w: dict | None):
        self._score_weights = w
        self.learned_lbl.setText("점수: 백테스트 요인 분석으로 학습한 가중치 사용 중" if w else
                                 "점수: 기본 공식 (거래량 배수↑, 가격 변동↓)")
        self.learned_clear.setVisible(bool(w))
        self._schedule()

    def _fetch_disc_flags(self, tickers):
        """결과 종목의 위험/호재 공시 표시를 백그라운드로 조회 (실데이터 + DART 키 있을 때)."""
        if (self._panel_source != "krx" or self.creds is None or not self.creds.dart_key
                or (self._disc_batch is not None and self._disc_batch.isRunning())):
            return
        missing = [t for t in tickers if t not in self._disc_cache][:60]
        if not missing:
            return
        if self._dart_client is None:
            self._dart_client = DartClient(self.creds.dart_key)
        self._disc_pending = missing
        self._disc_batch = DartBatchWorker(self._dart_client, missing)
        self._disc_batch.progress.connect(
            lambda k, n: self.statusBar().showMessage(f"DART 공시 확인 {k}/{n}"))
        self._disc_batch.done.connect(self._on_disc_flags)
        self._disc_batch.start()

    def _on_disc_flags(self, flags: dict):
        for t in self._disc_pending:  # 조회 실패 종목도 '확인함'으로 기록해 무한 재조회 방지
            self._disc_cache[t] = flags.get(t, "")
        self.statusBar().showMessage(f"DART 공시 확인 완료 ({len(flags)}종목)", 4000)
        self.rescreen()

    def open_backtest(self):
        if self._bt_win is None:
            self._bt_win = BacktestWindow(self)
        self._bt_win.show()
        self._bt_win.raise_()
        self._bt_win.refresh_db_info()

    def _row_of(self, ticker: str) -> int | None:
        for r in range(self.proxy.rowCount()):
            if self.proxy.index(r, 0).data() == ticker:
                return r
        return None

    def _on_select(self, cur, _prev):
        if not cur.isValid():
            return
        src = self.proxy.mapToSource(cur)
        row = self.model.row(src.row())
        self._selected = row["ticker"]
        p = self.params()
        hist = self.panel.history(row["ticker"]).iloc[-(p.required_days + 10):]
        self.chart.plot(hist, f"{row['name']} ({row['ticker']}) · {row['market']}  "
                              f"거래량 {row['vol_ratio']:.1f}배 · {row['chg_pct']:+.2f}%"
                              + (f" · 외국인 {row['frg_eok']:+,.1f}억 · 기관 {row['inst_eok']:+,.1f}억"
                                 if pd.notna(row["frg_eok"]) else ""),
                        p.recent_days, p.base_days, row["base_vol"])
        self._load_disclosures(row["ticker"])

    def _load_disclosures(self, ticker: str):
        self.disc.clear()
        if self.creds is None:
            QTreeWidgetItem(self.disc, ["", "‘계정’에서 잠금 해제하면 DART 공시 표시"])
            return
        if not self.creds.dart_key:
            QTreeWidgetItem(self.disc, ["", "‘계정’에 DART API 인증키 입력 시 공시 표시"])
            return
        if self._dart_client is None:
            self._dart_client = DartClient(self.creds.dart_key)
        QTreeWidgetItem(self.disc, ["", "공시 불러오는 중…"])
        w = DartWorker(self._dart_client, ticker)
        w.done.connect(self._on_disc)
        w.failed.connect(lambda t, m: t == self._selected and self._disc_message(f"DART 오류: {m}"))
        w.finished.connect(lambda: self._dart_workers.remove(w))
        self._dart_workers.append(w)
        w.start()

    def _disc_message(self, msg: str):
        self.disc.clear()
        QTreeWidgetItem(self.disc, ["", msg])

    def _on_disc(self, ticker: str, df: pd.DataFrame):
        if ticker != self._selected:
            return  # 그 사이 다른 종목을 선택함
        self.disc.clear()
        if df.empty:
            QTreeWidgetItem(self.disc, ["", "최근 90일 공시 없음"])
            return
        self._disc_cache[ticker] = summarize_flag(df)
        colors = {"risk": QColor(UP), "positive": QColor(GOOD)}
        for r in df.itertuples():
            it = QTreeWidgetItem(self.disc, [f"{r.date[2:4]}.{r.date[4:6]}.{r.date[6:]}", r.title])
            it.setData(0, Qt.UserRole, r.url)
            it.setToolTip(1, f"{r.title}\n제출인: {r.submitter}")
            if r.flag in colors:
                it.setForeground(1, colors[r.flag])

    def _open_naver(self, idx):
        ticker = self.proxy.index(idx.row(), 0).data()
        QDesktopServices.openUrl(QUrl(f"https://finance.naver.com/item/main.naver?code={ticker}"))

    def open_db_dialog(self):
        dlg = DbDialog(self.db_path, self)
        dlg.exec()
        if dlg.path != self.db_path:
            self.db_path = dlg.path
            self.settings.setValue("db_path", self.db_path)
            self.statusBar().showMessage(f"DB 위치 변경: {self.db_path} — 다음 '데이터 불러오기'부터 사용", 8000)

    def save_results(self):
        if self.panel is None or self.result.empty:
            QMessageBox.information(self, "결과 저장", "저장할 스크리닝 결과가 없습니다.")
            return
        if self._panel_source == "demo":
            QMessageBox.information(self, "결과 저장", "데모 데이터 결과는 DB 에 저장하지 않습니다.")
            return
        asof = self.panel.dates[-1].strftime("%Y%m%d")
        try:
            StockDB(self.db_path).save_results(asof, self.result, self.params().to_dict(),
                                               source=self._panel_source)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "결과 저장", f"저장 실패: {e}")
            return
        self.statusBar().showMessage(
            f"{asof} 기준 {len(self.result)}종목을 DB 에 저장했습니다 ({self.db_path})", 8000)

    def export_csv(self):
        if self.result.empty:
            QMessageBox.information(self, "내보내기", "내보낼 결과가 없습니다.")
            return
        default = f"screen_{dt.date.today():%Y%m%d}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "CSV 저장", default, "CSV (*.csv)")
        if path:
            out = self.result.rename(columns={c: h for c, h, *_ in COLUMNS})
            out.to_csv(path, index=False, encoding="utf-8-sig")  # 엑셀 한글 호환
            self.statusBar().showMessage(f"저장됨: {path}", 5000)


# ============================================================================
# run_screener.py
# ============================================================================
"""거래량 급증 · 가격 횡보 스크리너 실행.

    python run_screener.py          # KRX 실데이터
    python run_screener.py --demo   # 오프라인 데모 데이터로 시작

탐색기에서 더블클릭해도 된다. 필요한 패키지가 없으면 설치 여부를 묻고,
오류가 나면 창이 바로 닫히지 않고 메시지를 보여준다.
"""
import argparse
import importlib.util
import os
import subprocess
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 어느 폴더에서 실행하든 screener 패키지를 찾을 수 있게
sys.path.insert(0, str(HERE))

# import 이름 → pip 패키지 이름
REQUIRED = {
    "pandas": "pandas", "numpy": "numpy", "requests": "requests", "pykrx": "pykrx",
    "PySide6": "PySide6", "matplotlib": "matplotlib", "cryptography": "cryptography",
}


def _has_console() -> bool:
    return sys.stdin is not None and sys.stdin.isatty()


def _pause(msg: str = "엔터를 누르면 창이 닫힙니다..."):
    if _has_console():
        try:
            input(msg)
        except EOFError:
            pass


def _check_environment() -> bool:
    """Python 버전과 필요한 패키지를 확인하고, 없으면 설치를 제안한다."""
    if sys.version_info < (3, 10):
        print(f"[오류] Python 3.10 이상이 필요합니다. 현재: {sys.version.split()[0]}")
        print("https://www.python.org/downloads/ 에서 최신 Python 을 설치하세요.")
        return False
    missing = [pip for mod, pip in REQUIRED.items() if importlib.util.find_spec(mod) is None]
    if not missing:
        return True
    print("필요한 패키지가 설치되어 있지 않습니다: " + ", ".join(missing))
    if not _has_console():
        print(f'설치 명령: "{sys.executable}" -m pip install -r requirements.txt')
        return False
    ans = input("지금 자동으로 설치할까요? 몇 분 걸립니다. [Y/n] ").strip().lower()
    if ans not in ("", "y", "yes", "ㅛ"):
        print(f'직접 설치하려면: "{sys.executable}" -m pip install -r requirements.txt')
        return False
    req = HERE / "requirements.txt"
    cmd = [sys.executable, "-m", "pip", "install"] + (["-r", str(req)] if req.exists() else missing)
    print("실행:", " ".join(cmd))
    if subprocess.call(cmd) != 0:
        print("[오류] 패키지 설치에 실패했습니다. 위 메시지를 확인하세요.")
        return False
    importlib.invalidate_caches()
    print("설치 완료. 프로그램을 시작합니다.\n")
    return True

# 실행파일(창 모드)에서는 stdout/stderr 가 없으므로 로그 파일로 보낸다
if sys.stdout is None or sys.stderr is None:

    _log = open(app_dir() / "screener.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log
# pykrx 는 import 시 환경변수 KRX_ID/KRX_PW 로 자동 로그인을 시도하므로 비워 둔다 (로그인은 앱에서 수행)
os.environ.pop("KRX_ID", None)
os.environ.pop("KRX_PW", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="데모 데이터 소스를 기본 선택")
    ap.add_argument("--autoload", action="store_true", help="시작하자마자 데이터 불러오기")
    ap.add_argument("--selftest", action="store_true",
                    help="데모 데이터로 창을 띄워 스크리닝까지 확인한 뒤 종료 (빌드 검증용)")
    args, qt_args = ap.parse_known_args()

    from PySide6.QtWidgets import QApplication


    app = QApplication([sys.argv[0], *qt_args])
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    win = MainWindow(demo=args.demo or args.selftest)
    win.show()
    if args.selftest:
        return _selftest(app, win)
    if args.autoload:
        win.load_data()
    sys.exit(_run(app, win))


def _run(app, win) -> int:
    """이벤트 루프 실행 후 창 → 앱 순서로 정리 (순서가 어긋나면 종료 시 Qt 가 비정상 종료할 수 있음)."""
    code = app.exec()
    win.shutdown()
    win.deleteLater()
    del win
    app.processEvents()
    return code


def _selftest(app, win):
    from PySide6.QtCore import QTimer


    def check():
        rows = win.model.rowCount()
        print(f"selftest: {rows} rows")
        app.exit(0 if rows > 0 else 1)

    win.load_data()
    win._loader.done.connect(lambda _p: QTimer.singleShot(500, check))
    win._loader.failed.connect(lambda m: (print("selftest failed:", m), app.exit(1)))
    QTimer.singleShot(60_000, lambda: (print("selftest timeout"), app.exit(2)))
    code = _run(app, win)
    sys.stdout.flush()
    sys.exit(code)


if __name__ == "__main__":
    if not _check_environment():
        _pause()
        sys.exit(1)
    try:
        main()
    except SystemExit:
        raise
    except Exception:  # noqa: BLE001 — 더블클릭 실행 시 창이 바로 닫히지 않도록
        print("\n[오류] 프로그램 실행 중 문제가 발생했습니다. 아래 내용을 복사해서 알려주세요.\n")
        traceback.print_exc()
        _pause()
        sys.exit(1)


if __name__ == "__main__":
    main()
