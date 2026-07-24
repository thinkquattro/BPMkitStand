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


def set_secret(ref: str, value: str) -> None:
    """
    Сохраняет значение секрета под ссылкой ``ref`` в системном keyring.

    Симметрично ``get_secret``/``has_secret`` — тот же backend (keyring,
    сервис ``standkit``). Требует опциональную зависимость ``keyring``
    (extra ``standkit[secrets]``/``standkit[gui]``); при её отсутствии или
    сбое backend'а бросает понятную ``SecretError`` (без падения импорта
    модуля — см. try/except вокруг ``import keyring`` выше).

    Значение секрета никогда не логируется и не попадает в текст ошибки.
    """
    if not _HAS_KEYRING:
        raise SecretError(
            f"Невозможно задать секрет '{ref}': пакет keyring не установлен. "
            "Установите опциональную зависимость: pip install standkit[secrets]"
        )
    try:
        keyring.set_password(_KEYRING_SERVICE, ref, value)
    except Exception as exc:
        # Значение секрета намеренно не попадает в текст исключения — только ref.
        raise SecretError(f"Не удалось сохранить секрет '{ref}' в keyring: {exc}") from exc


def delete_secret(ref: str) -> None:
    """
    Удаляет секрет по ссылке ``ref`` из системного keyring.

    Идемпотентно по духу с остальным контрактом: отсутствие backend'а —
    SecretError; отсутствие самого секрета в keyring backend обычно тоже
    трактует как ошибку (PasswordDeleteError) — она также оборачивается в
    SecretError, вызывающая сторона может считать "секрета и так не было"
    нормальным исходом при необходимости (проверить через has_secret до
    удаления).
    """
    if not _HAS_KEYRING:
        raise SecretError(
            f"Невозможно удалить секрет '{ref}': пакет keyring не установлен. "
            "Установите опциональную зависимость: pip install standkit[secrets]"
        )
    try:
        keyring.delete_password(_KEYRING_SERVICE, ref)
    except Exception as exc:
        raise SecretError(f"Не удалось удалить секрет '{ref}' из keyring: {exc}") from exc


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


# Бэклог следующих итераций (CLI-обёртка set/get/status/rotate, файловый фолбэк
# secrets.enc для машин без keyring, диагностический status() по источнику) —
# см. docs/ARCHITECTURE.md.
