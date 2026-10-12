"""알림: 텔레그램 봇으로 메시지 보내기.

설정: 텔레그램에서 @BotFather → /newbot → 봇 토큰 발급.
      만든 봇에게 아무 메시지나 보낸 뒤 '채팅 ID 찾기'(find_chat_id)로 채팅 ID 확인.
"""
from __future__ import annotations

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
