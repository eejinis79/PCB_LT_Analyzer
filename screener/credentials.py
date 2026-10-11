"""계정 정보: 환경변수 → 프로젝트 루트 .env 순으로 읽는다. (.env 는 .gitignore 대상)"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
KEYS = ("KRX_ID", "KRX_PW", "DART_API_KEY")


@dataclass
class Credentials:
    krx_id: str = ""
    krx_pw: str = ""
    dart_key: str = ""


def _read_env_file(path: Path) -> dict[str, str]:
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def load(path: Path = ENV_PATH) -> Credentials:
    file_vals = _read_env_file(path)
    get = lambda k: os.environ.get(k) or file_vals.get(k, "")  # noqa: E731
    return Credentials(get("KRX_ID"), get("KRX_PW"), get("DART_API_KEY"))


def save(c: Credentials, path: Path = ENV_PATH) -> None:
    vals = _read_env_file(path)
    vals.update({"KRX_ID": c.krx_id, "KRX_PW": c.krx_pw, "DART_API_KEY": c.dart_key})
    path.write_text("".join(f"{k}={v}\n" for k, v in vals.items()), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
