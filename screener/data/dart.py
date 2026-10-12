"""OpenDART 공시 조회.

OpenDART API 는 홈페이지 ID/PW 가 아니라 '인증키(crtfc_key)' 로 호출한다.
opendart.fss.or.kr 로그인 → 인증키 신청/관리 에서 발급받은 40자리 키를 사용.
"""
from __future__ import annotations

import datetime as dt
import io
import xml.etree.ElementTree as ET
import zipfile

import pandas as pd
import requests

from ..paths import cache_dir

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
