"""screener 패키지 + run_screener.py 를 파일 하나(volume_spike_screener.py)로 합친다.

    python tools/build_single_py.py

다른 PC 에 파일 하나만 복사해 실행·수정할 수 있게 하기 위한 배포본이다.
원본(패키지)의 모듈 경계를 없애면서 생기는 참조를 아래 규칙으로 바꾼다.
  - 상대 import / screener.* import 제거 (같은 파일 안에 정의가 있음)
  - theme 모듈 별칭 T.XXX → XXX
  - demo.load_panel / krx.load_panel 이름 충돌 → demo_load_panel / krx_load_panel
  - krx.login → krx_login, account.unlock → unlock, account.AccountDialog → AccountDialog
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "volume_spike_screener.py"

# 의존 순서대로
MODULES = [
    "screener/paths.py",
    "screener/core.py",
    "screener/vault.py",
    "screener/data/demo.py",
    "screener/data/krx.py",
    "screener/data/dart.py",
    "screener/gui/theme.py",
    "screener/gui/widgets.py",
    "screener/gui/chart.py",
    "screener/gui/models.py",
    "screener/gui/workers.py",
    "screener/gui/account.py",
    "screener/gui/main_window.py",
    "run_screener.py",
]

# (모듈, 정규식, 치환) — 모듈이 None 이면 전체 적용
RULES = [
    (None, r"^from __future__ import annotations\n", ""),
    (None, r"^[ \t]*from \.+[\w.]* import [^\n]*\n", ""),          # 상대 import (함수 안 lazy import 포함)
    (None, r"^[ \t]*from screener[\w.]* import [^\n]*\n", ""),
    (None, r"\bT\.([A-Z][A-Z_0-9]*)\b", r"\1"),
    ("screener/data/demo.py", r"\bdef load_panel\(", "def demo_load_panel("),
    ("screener/data/krx.py", r"\bdef load_panel\(", "def krx_load_panel("),
    ("screener/data/krx.py", r"\bdef login\(", "def krx_login("),
    ("screener/gui/workers.py", r"\bdemo\.load_panel\(", "demo_load_panel("),
    ("screener/gui/workers.py", r"\bkrx\.load_panel\(", "krx_load_panel("),
    ("screener/gui/workers.py", r"\bkrx\.login\(", "krx_login("),
    ("screener/gui/main_window.py", r"\baccount\.(unlock|AccountDialog)\(", r"\1("),
    # 실행 진입점: 모듈 맨 아래에서 main() 호출은 파일 끝에 한 번만
    ("run_screener.py", r'\nif __name__ == "__main__":\n    main\(\)\n', "\n"),
]

HEADER = '''#!/usr/bin/env python3
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

'''


def transform(rel: str, src: str) -> str:
    for mod, pat, rep in RULES:
        if mod is None or mod == rel:
            src = re.sub(pat, rep, src, flags=re.M)
    return src.strip("\n")


def build() -> str:
    parts = [HEADER]
    for rel in MODULES:
        body = transform(rel, (ROOT / rel).read_text(encoding="utf-8"))
        parts.append(f"\n\n# {'=' * 76}\n# {rel}\n# {'=' * 76}\n{body}\n")
    parts.append('\n\nif __name__ == "__main__":\n    main()\n')
    return "".join(parts)


def main():
    code = build()
    compile(code, str(OUT), "exec")  # 문법 확인
    leftovers = [ln for ln in code.splitlines()
                 if re.search(r"^\s*from \.|\bT\.[A-Z]|\b(demo|krx|account)\.\w+\(", ln)]
    if leftovers:
        sys.exit("변환되지 않은 참조:\n" + "\n".join(leftovers))
    OUT.write_text(code, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(code.splitlines())} lines)")


if __name__ == "__main__":
    main()
