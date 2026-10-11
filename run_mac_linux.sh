#!/usr/bin/env bash
# 실행: ./run_mac_linux.sh   (데모: ./run_mac_linux.sh --demo)
# 처음 한 번은 필요한 패키지를 설치합니다.
set -e
cd "$(dirname "$0")"
PY=${PYTHON:-python3}
command -v "$PY" >/dev/null || { echo "Python 3.10 이상을 설치하세요: https://www.python.org/downloads/"; exit 1; }
if [ ! -d .venv ]; then
  echo "[1/2] 가상환경 생성 중..."
  "$PY" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
if [ ! -f .venv/installed.flag ]; then
  echo "[2/2] 패키지 설치 중... 처음 한 번만 합니다."
  python -m pip install --upgrade pip >/dev/null
  python -m pip install -r requirements.txt
  touch .venv/installed.flag
fi
exec python run_screener.py "$@"
