"""매일 장 마감 후 자동 실행: 데이터 수집 → 스크리닝(마지막으로 화면에서 쓴 조건) → DB 저장 → 텔레그램 알림.

    python daily_run.py                    # 비밀번호는 환경변수 VSS_PASSWORD 에서 (없으면 입력 요청)
    python daily_run.py --no-notify        # 알림 없이
    python daily_run.py --force            # 휴장일이어도 마지막 거래일 기준으로 실행

Windows 작업 스케줄러 등록은 register_daily_task.bat 을 더블클릭.
"""
from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import os
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ.pop("KRX_ID", None)
os.environ.pop("KRX_PW", None)


def log(msg: str):
    from screener.paths import app_dir

    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(app_dir() / "daily_run.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_settings():
    """GUI 가 마지막으로 저장한 조건 · DB 위치."""
    from PySide6.QtCore import QSettings

    from screener.core import ScreenParams
    from screener.db import default_db_path

    st = QSettings("PCB_LT_Analyzer", "VolumeSpikeScreener")
    raw = st.value("params")
    params = ScreenParams.from_dict(json.loads(raw)) if raw else ScreenParams()
    return params, st.value("db_path") or str(default_db_path())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--password-env", default="VSS_PASSWORD")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    from screener.backtest import current_regime
    from screener.core import run_screen
    from screener.data import krx
    from screener.db import StockDB
    from screener.notify import format_results, send_telegram
    from screener.vault import Vault, WrongPassword

    vault = Vault()
    pw = os.environ.get(args.password_env)
    if not pw and sys.stdin is not None and sys.stdin.isatty():
        pw = getpass.getpass("계정 비밀번호: ")
    if not pw:
        log(f"비밀번호가 없습니다. 환경변수 {args.password_env} 를 설정하세요.")
        return 2
    try:
        creds = vault.unlock(pw)
    except WrongPassword:
        log("계정 비밀번호가 틀렸습니다.")
        return 2
    if not (creds.krx_id and creds.krx_pw):
        log("KRX 계정이 비어 있습니다. 프로그램의 '계정'에서 입력하세요.")
        return 2

    params, db_path = load_settings()
    db = StockDB(db_path)
    log(f"시작 — DB {db_path}")
    krx.login(creds.krx_id, creds.krx_pw)
    today = dt.date.today()
    panel = krx.load_panel(today, max(70, params.required_days + 5), db=db,
                           progress=lambda i, n, m: None)
    asof = panel.dates[-1]
    if asof.date() != today and not args.force:
        log(f"오늘({today})은 휴장일입니다. 마지막 거래일 {asof:%Y-%m-%d}. 종료.")
        return 0
    result = run_screen(panel, params)
    regime = current_regime(panel)
    asof_s = asof.strftime("%Y%m%d")
    db.save_results(asof_s, result, params.to_dict(), source="auto")
    log(f"{asof_s} 스크리닝 {len(result)}종목 저장. 시장 추세: {regime}")

    if not args.no_notify and creds.telegram_token and creds.telegram_chat:
        send_telegram(creds.telegram_token, creds.telegram_chat, format_results(asof_s, result, regime))
        log("텔레그램 전송 완료")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001
        log("오류:\n" + traceback.format_exc())
        code = 1
    sys.exit(code)
