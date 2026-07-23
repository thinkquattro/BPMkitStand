"""
Secret-first доступ к секретам стенда (пароли БД, токены агентов и т.п.).

Контракт разрешения секрета (по духу совпадает с secretstore из BPMkit, но без
зависимости от него — ядро standkit самодостаточно):

  1. Переменная окружения ``STANDKIT_SECRET__<REF_UPPER>`` (двойное подчёркивание
     как разделитель, символы, недопустимые в имени переменной окружения,
     заменяются на "_");
  2. системный keyring (опциональная зависимость — импортируется в try/except,
     ядро НЕ требует пакет ``keyring`` для работы);
  3. явный фолбэк, переданный вызывающей стороной (например, открытое поле
     ``db_password`` из записи реестра — сознательно менее приоритетно, чем
     секрет);
  4. если ничего не найдено — SecretError.

Секрет никогда не логируется и не попадает в текстовое представление ошибки
целиком (в сообщении об ошибке фигурирует только ref, не значение).
"""

from __future__ import annotations

import os
import re
from typing import Optional

try:
    import keyring  # type: ignore

    _HAS_KEYRING = True
except ImportError:  # keyring — опциональная зависимость
    keyring = None  # type: ignore
    _HAS_KEYRING = False

_KEYRING_SERVICE = "standkit"
_ENV_PREFIX = "STANDKIT_SECRET__"


class SecretError(Exception):
    """Секрет не найден ни в одном из источников."""


def _env_var_name(ref: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]", "_", ref).upper()
    return f"{_ENV_PREFIX}{safe}"


def has_secret(ref: str) -> bool:
    """Быстрая проверка наличия секрета (без исключений) — env либо keyring."""
    if os.environ.get(_env_var_name(ref)):
        return True
    if _HAS_KEYRING:
        try:
            return keyring.get_password(_KEYRING_SERVICE, ref) is not None
        except Exception:
            # Бэкенд keyring недоступен на этой машине (нет DBus/Credential
            # Manager и т.п.) — не считаем это фатальной ошибкой на этапе has_secret.
            return False
    return False


def get_secret(ref: str, *, fallback: Optional[str] = None) -> str:
    """
    Возвращает значение секрета по ссылке ``ref`` согласно Secret-first контракту.

    ``fallback`` — необязательное значение "последней надежды" (например,
    открытое поле реестра) — используется только если ни env, ни keyring не
    дали ответа. Если и фолбэка нет — SecretError.
    """
    env_val = os.environ.get(_env_var_name(ref))
    if env_val:
        return env_val

    if _HAS_KEYRING:
        try:
            val = keyring.get_password(_KEYRING_SERVICE, ref)
        except Exception:
            val = None
        if val:
            return val

    if fallback:
        return fallback

    raise SecretError(
        f"Секрет '{ref}' не найден ни в переменной окружения {_env_var_name(ref)}, "
        f"ни в keyring, ни в переданном fallback"
    )


# --- TODO(следующая итерация) ---
# - CLI-обёртка (set/get/status/rotate/delete/list) по аналогии с
#   BPMkit/server/secretstore.py, но для сервиса "standkit";
# - файловый фолбэк secrets.enc с мастер-ключом из переменной окружения — для
#   машин без доступного системного keyring (headless Linux без dbus/libsecret);
# - явный `status()` с диагностикой источника (env/keyring/fallback/not found)
#   для онбординга новой машины без раскрытия самого значения.
