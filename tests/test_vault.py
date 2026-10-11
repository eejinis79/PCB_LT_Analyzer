import json

import pytest

from screener.vault import Credentials, Vault, VaultError, WrongPassword


@pytest.fixture
def vault(tmp_path):
    return Vault(tmp_path / "vault.json")


def test_initial_password_is_0000_and_must_change(vault):
    assert vault.must_change_password
    assert vault.unlock("0000") == Credentials()
    with pytest.raises(WrongPassword):
        vault.unlock("1234")


def test_change_from_initial_then_store_encrypted(vault):
    vault.change_password("0000", "s3cret!")
    assert not vault.must_change_password
    with pytest.raises(WrongPassword):
        vault.unlock("0000")
    creds = Credentials("krxid", "krxpw", "dartid", "dartpw", "k" * 40)
    vault.save("s3cret!", creds)
    assert vault.unlock("s3cret!") == creds
    raw = vault.path.read_text()
    assert "krxpw" not in raw and "dartpw" not in raw  # 평문 저장 안 됨
    assert set(json.loads(raw)) >= {"salt", "token"}


def test_change_password_keeps_credentials(vault):
    vault.change_password("0000", "first1")
    creds = Credentials(krx_id="a", krx_pw="b", dart_key="c")
    vault.save("first1", creds)
    assert vault.change_password("first1", "second2") == creds
    assert vault.unlock("second2") == creds
    with pytest.raises(WrongPassword):
        vault.unlock("first1")


@pytest.mark.parametrize("new,confirm", [("abc", "abc"), ("0000", "0000"), ("abcd", "abce")])
def test_password_rules(vault, new, confirm):
    with pytest.raises(VaultError):
        vault.change_password("0000", new, confirm)


def test_wrong_current_password_on_change(vault):
    vault.change_password("0000", "first1")
    with pytest.raises(WrongPassword):
        vault.change_password("nope", "second2")


def test_reset(vault):
    vault.change_password("0000", "first1")
    vault.reset()
    assert vault.unlock("0000") == Credentials() and vault.must_change_password
