"""계정 정보 금고: KRX/DART 계정·비밀번호·API 키를 '계정 비밀번호'로 암호화해 저장한다.

- 금고 파일이 없으면 초기 비밀번호는 0000 이며, 첫 접속 시 비밀번호 변경이 강제된다.
- 키 유도: PBKDF2-HMAC-SHA256(비밀번호, 랜덤 salt, 600,000회) → Fernet(AES-128-CBC + HMAC).
  비밀번호가 틀리면 복호화(무결성 검증)가 실패하므로 별도 해시를 저장하지 않는다.
- 비밀번호를 잊으면 복구할 수 없다. reset() 으로 금고를 지우고 0000 으로 되돌린다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .paths import app_dir

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
