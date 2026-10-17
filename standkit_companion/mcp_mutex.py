# -*- coding: utf-8 -*-
"""Детект запущенного MCP-сервера BPMkit по именованному Windows-мьютексу.

Зачем. `releases.apply_staged` до этого узнавал, что сервер работает, только КОСВЕННО —
по неудачной попытке `fsutil.replace_with_retry` (Windows не даёт подменить файл, который
держит открытым другой процесс). Это не ложь, но и не диагноз: пользователь видит
«локальная ошибка файловой системы» уже ПОСЛЕ того, как сделан бэкап. Этот модуль даёт
более дешёвый и более честный сигнал — «сервер работает» — раньше и без файловых операций
вовсе, если сервер это заранее объявил.

⚠️ КОНТРАКТ ИМЕНИ (не дублировать вторым способом). Первоисточник строки — константа
`SERVER_MUTEX_NAME` в `BPMkit/server/bpmkit/core.py` поставки **BPMkit** (репозиторий
другой репозиторий; там же — `acquire_server_mutex()`, вызывается непосредственно
перед `mcp.run()`). BPMkitStand — ОТДЕЛЬНАЯ поставка со своим git-репозиторием:
импортировать константу оттуда нельзя, поэтому здесь неизбежна ВТОРАЯ копия того же
литерала. Если имя мьютекса там когда-нибудь изменится — поправить `SERVER_MUTEX_NAME`
НИЖЕ, это единственное место в BPMkitStand, где оно живёт (второй копии в репозитории быть
не должно — искать перед правкой: `grep -r BpmkitMcpServer` было бы неверно из-за
регистра, искать `SERVER_MUTEX_NAME`/`BpmkitMcpServer`/`server_mutex`).

Мьютекс сессионно-локальный (без `Global\\`-префикса) — сервер и Companion всегда
работают в одной интерактивной сессии рабочего стола, см. обоснование у первоисточника.

Fail-open ИМЕННО ДЛЯ ДЕТЕКТА (не для подмены файла). Любая ошибка WinAPI — недоступный
`kernel32`, отказ вызова, отсутствие прав — ловится здесь целиком и даёт `False`. «Не
смогли определить» — это НЕ «сервер точно не работает»: последнее слово всё равно за
файловой пробой (`fsutil.probe_writable`) и за самой подменой (`replace_with_retry`),
которые ловят и такой случай. Отказ детекта не имеет права остановить цикл обновления
собственным исключением — иначе более дешёвая диагностика оказалась бы менее надёжной,
чем то, что она должна была улучшить.

«Мьютекса нет» — ТОЛЬКО `ERROR_FILE_NOT_FOUND`. `OpenMutexW` отказывает и тогда, когда
объект есть, но открыть его нельзя (`ERROR_ACCESS_DENIED` — мьютекс создан процессом с
другими правами): это «есть», а не «нет». Прочие сбои — «не знаю» (`server_mutex_state`
→ `unknown`). Ручная кнопка трактует «не знаю» как прежде (fail-open детекта, решает
файловая проба); автоматическое обновление — как «сервер работает» (`strict=True`):
поставить версию поверх живого процесса из-за сбоя детекта хуже, чем подождать.
"""
from __future__ import annotations

import sys

__all__ = ["SERVER_MUTEX_NAME", "server_mutex_exists", "server_mutex_state",
           "MUTEX_PRESENT", "MUTEX_ABSENT", "MUTEX_UNKNOWN"]

#: ПЕРВОИСТОЧНИК — `SERVER_MUTEX_NAME` в `BPMkit/server/bpmkit/core.py` поставки BPMkit
#: (другой репозиторий). Вторая копия литерала здесь неизбежна (отдельный
#: репозиторий, импорт невозможен) — при смене имени там поправить ТОЛЬКО эту строку.
SERVER_MUTEX_NAME = "BPMkitMcpServer"

#: `SYNCHRONIZE` — минимальные права доступа, достаточные, чтобы открыть мьютекс и
#: проверить сам факт его существования; ждать (`WaitForSingleObject`) здесь не нужно.
_SYNCHRONIZE = 0x00100000

#: Коды `GetLastError` после неудачного `OpenMutexW`.
_ERROR_FILE_NOT_FOUND = 2
_ERROR_ACCESS_DENIED = 5

#: Исходы детекта (`server_mutex_state`).
MUTEX_PRESENT = "present"
MUTEX_ABSENT = "absent"
MUTEX_UNKNOWN = "unknown"


def _win_open_mutex_handle(name: str):
    """Сырой вызов WinAPI `OpenMutexW` — вынесен ОТДЕЛЬНО от `server_mutex_exists()`,
    чтобы тесты подменяли (monkeypatch) именно эту функцию и не зависели от реальной ОС:
    `ctypes.WinDLL` на Linux не существует вовсе, а тесты обязаны быть зелёными и там
    (см. `tests/test_companion_mcp_mutex.py`, конвенция — `tests/test_server_mutex.py`
    поставки BPMkit).

    Возвращает хендл (усечённый до `bool` в вызывающем) и сразу его закрывает: держать
    открытый хендл нам незачем — важен только факт, что `OpenMutexW` его выдал. Пустой
    хендл возвращается только при `ERROR_FILE_NOT_FOUND` («мьютекса нет»); любой другой
    отказ — `OSError` с кодом `GetLastError` (разбирает `server_mutex_state`).
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, name)
    if handle:
        kernel32.CloseHandle(handle)
        return handle
    error = ctypes.get_last_error()
    if error == _ERROR_FILE_NOT_FOUND:
        return 0
    raise OSError(error, f"OpenMutexW: код {error}", None, error)


def server_mutex_state(name: str = SERVER_MUTEX_NAME) -> str:
    """`present` / `absent` / `unknown` — что известно о мьютексе сервера.

    Не-Windows — `absent` (детекта нет, решают файловая проба и сама подмена). Хендл
    выдан — `present`; `ERROR_FILE_NOT_FOUND` — `absent`; `ERROR_ACCESS_DENIED` — объект
    есть — `present`; любой другой сбой — `unknown`.
    """
    if not sys.platform.startswith("win"):
        return MUTEX_ABSENT
    try:
        handle = _win_open_mutex_handle(name)
    except OSError as exc:
        code = getattr(exc, "winerror", None) or exc.errno
        return MUTEX_PRESENT if code == _ERROR_ACCESS_DENIED else MUTEX_UNKNOWN
    except Exception:
        return MUTEX_UNKNOWN
    return MUTEX_PRESENT if handle else MUTEX_ABSENT


def server_mutex_exists(name: str = SERVER_MUTEX_NAME, *, strict: bool = False) -> bool:
    """`True`, если именованный мьютекс сервера сейчас существует (сервер запущен).

    Только Windows — на прочих платформах тихий no-op (`False`), `OpenMutexW` не
    вызывается вовсе: поведение канала обновлений там не меняется, финальная защита —
    файловая проба и сама подмена.

    «Не знаю» (сбой WinAPI, кроме «объекта нет» и «нет прав на объект») при
    `strict=False` даёт `False` (fail-open ДЕТЕКТА — см. докстринг модуля: вызывающий
    обязан трактовать `False` как «мьютекса не видно», а не как доказательство простоя
    сервера), при `strict=True` — `True` (автоматическое обновление: не знаю — жду).
    """
    state = server_mutex_state(name)
    if state == MUTEX_UNKNOWN:
        return bool(strict)
    return state == MUTEX_PRESENT
