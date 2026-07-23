"""
Тесты standkit.secrets: set_secret/get_secret/has_secret/delete_secret
round-trip на monkeypatch-подмене keyring backend'а (in-memory, без реального
системного keyring/DBus — тест должен быть детерминирован в CI/headless).

Также проверяем, что значение секрета не утекает в текст исключений/repr.
"""

from __future__ import annotations

import pytest

import standkit.secrets as secrets_module
from standkit.secrets import SecretError, delete_secret, get_secret, has_secret, set_secret


class _FakeKeyring:
    """In-memory подмена модуля keyring — тот же публичный интерфейс, что и настоящий."""

    def __init__(self):
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, ref: str):
        return self._store.get((service, ref))

    def set_password(self, service: str, ref: str, value: str) -> None:
        self._store[(service, ref)] = value

    def delete_password(self, service: str, ref: str) -> None:
        try:
            del self._store[(service, ref)]
        except KeyError as exc:
            raise Exception(f"PasswordDeleteError: {ref}") from exc


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    monkeypatch.setattr(secrets_module, "keyring", fake)
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", True)
    # env не должен маскировать backend keyring в этих тестах.
    monkeypatch.delenv("STANDKIT_SECRET__TEST_REF", raising=False)
    return fake


def test_set_get_has_delete_roundtrip(fake_keyring):
    ref = "standkit:test-stand:agent-token"

    assert has_secret(ref) is False

    set_secret(ref, "super-secret-value")
    assert has_secret(ref) is True
    assert get_secret(ref) == "super-secret-value"

    delete_secret(ref)
    assert has_secret(ref) is False


def test_get_secret_after_delete_raises_without_fallback(fake_keyring):
    ref = "standkit:test-stand:agent-token"
    set_secret(ref, "value")
    delete_secret(ref)

    with pytest.raises(SecretError):
        get_secret(ref)


def test_set_secret_without_keyring_backend_raises_secret_error(monkeypatch):
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", False)
    monkeypatch.setattr(secrets_module, "keyring", None)

    with pytest.raises(SecretError):
        set_secret("standkit:x:token", "value")


def test_delete_secret_without_keyring_backend_raises_secret_error(monkeypatch):
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", False)
    monkeypatch.setattr(secrets_module, "keyring", None)

    with pytest.raises(SecretError):
        delete_secret("standkit:x:token")


def test_set_secret_value_does_not_leak_into_exception_text(fake_keyring, monkeypatch):
    ref = "standkit:test-stand:agent-token"
    secret_value = "extremely-sensitive-payload-should-not-leak"

    def _boom(service, r, v):
        raise RuntimeError("backend failure")

    monkeypatch.setattr(fake_keyring, "set_password", _boom)

    with pytest.raises(SecretError) as excinfo:
        set_secret(ref, secret_value)

    assert secret_value not in str(excinfo.value)
    assert ref in str(excinfo.value)


def test_delete_secret_value_does_not_leak_and_missing_ref_wrapped(fake_keyring):
    ref = "standkit:missing:token"

    with pytest.raises(SecretError) as excinfo:
        delete_secret(ref)

    assert ref in str(excinfo.value)
