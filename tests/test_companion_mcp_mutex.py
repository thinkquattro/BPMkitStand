# -*- coding: utf-8 -*-
"""Тесты `standkit_companion.mcp_mutex` (GAP-161).

Что доказывается. `releases.apply_staged` теперь спрашивает этот модуль ДО бэкапа и ДО
подмены файла — детект должен быть безопасным собеседником: не звонить в реальный WinAPI
на CI-раннере без Windows, не ронять цикл обновления на любой ошибке, и звать WinAPI
ровно с тем именем, что объявлено константой (а не литералом где-то ещё).

WinAPI мокается ЦЕЛИКОМ (подмена `_win_open_mutex_handle`, как в `_win_create_mutex_handle`
поставки BPMkit, `tests/test_server_mutex.py`) — тесты обязаны быть зелёными и на Linux
(`ctypes.WinDLL` там не существует вовсе), и на Windows (где реальный `bpmkit.exe` мог бы
случайно оказаться запущен на машине, где гоняются тесты, и дать ложноположительный
результат при обращении к настоящему API).
"""
from __future__ import annotations

import pytest

from standkit_companion import mcp_mutex


@pytest.fixture()
def restore_platform(monkeypatch):
    """Подменяет `mcp_mutex.sys.platform` на время теста и гарантированно возвращает его —
    иначе один упавший тест на "win32" испортил бы платформу для соседних тестов файла."""
    def _set(value: str) -> None:
        monkeypatch.setattr(mcp_mutex.sys, "platform", value)
    return _set


# ======================================================================================
# 1. Имя мьютекса — единственная константа, ничем не дублируется
# ======================================================================================
def test_uses_the_single_name_constant(restore_platform, monkeypatch):
    seen = {}

    def fake_open(name):
        seen["name"] = name
        return 12345

    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", fake_open)

    assert mcp_mutex.server_mutex_exists() is True
    assert seen["name"] == mcp_mutex.SERVER_MUTEX_NAME
    assert mcp_mutex.SERVER_MUTEX_NAME == "BPMkitMcpServer", (
        "первоисточник — SERVER_MUTEX_NAME в BPMkit/server/bpmkit/core.py поставки BPMkit; "
        "если это упало, кто-то поправил одну копию литерала и забыл вторую")


def test_explicit_name_argument_is_passed_through(restore_platform, monkeypatch):
    seen = {}
    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle",
                        lambda name: seen.setdefault("name", name) or 1)

    assert mcp_mutex.server_mutex_exists("СовсемДругоеИмя") is True
    assert seen["name"] == "СовсемДругоеИмя"


# ======================================================================================
# 2. Не-Windows: тихий no-op, WinAPI не трогается вовсе
# ======================================================================================
@pytest.mark.parametrize("platform_value", ["linux", "darwin", "linux2"])
def test_non_windows_is_a_noop(restore_platform, monkeypatch, platform_value):
    def must_not_be_called(name):
        raise AssertionError(
            f"WinAPI не должен вызываться на не-Windows платформе: name={name!r}")

    restore_platform(platform_value)
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", must_not_be_called)

    assert mcp_mutex.server_mutex_exists() is False


# ======================================================================================
# 3. Ошибка WinAPI — fail-open ДЕТЕКТА: False, а не исключение наружу
# ======================================================================================
def test_winapi_exception_is_suppressed_not_raised(restore_platform, monkeypatch):
    def raises(name):
        raise OSError("нет доступа к kernel32 (симуляция)")

    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", raises)

    # Ключевая проверка: не бросает исключение — иначе более дешёвая диагностика
    # оказалась бы МЕНЕЕ надёжной, чем то, что она должна была улучшить.
    assert mcp_mutex.server_mutex_exists() is False


def test_null_handle_means_mutex_not_found(restore_platform, monkeypatch):
    """`OpenMutexW` возвращает 0/NULL, когда объекта с таким именем не существует —
    это штатное «сервер не запущен», не ошибка."""
    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", lambda name: 0)

    assert mcp_mutex.server_mutex_exists() is False


def test_valid_handle_means_mutex_found(restore_platform, monkeypatch):
    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", lambda name: 777)

    assert mcp_mutex.server_mutex_exists() is True


# ======================================================================================
# 4. «Мьютекса нет» — только ERROR_FILE_NOT_FOUND; «не знаю» для автоматики — «есть»
# ======================================================================================
def test_access_denied_means_mutex_exists(restore_platform, monkeypatch):
    """`ERROR_ACCESS_DENIED`: объект есть, открыть нельзя — это «сервер запущен»."""
    def denied(name):
        raise OSError(5, "Access is denied")

    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", denied)

    assert mcp_mutex.server_mutex_state() == mcp_mutex.MUTEX_PRESENT
    assert mcp_mutex.server_mutex_exists() is True


def test_unknown_failure_is_absent_for_buttons_and_present_for_automation(restore_platform,
                                                                         monkeypatch):
    def broken(name):
        raise OSError(6, "The handle is invalid")

    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", broken)

    assert mcp_mutex.server_mutex_state() == mcp_mutex.MUTEX_UNKNOWN
    assert mcp_mutex.server_mutex_exists() is False, "ручная кнопка — как прежде"
    assert mcp_mutex.server_mutex_exists(strict=True) is True, "автоматика ждёт"


def test_not_found_is_absent_even_for_automation(restore_platform, monkeypatch):
    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", lambda name: 0)

    assert mcp_mutex.server_mutex_state() == mcp_mutex.MUTEX_ABSENT
    assert mcp_mutex.server_mutex_exists(strict=True) is False


def test_non_windows_is_absent_even_for_automation(restore_platform, monkeypatch):
    restore_platform("linux")

    assert mcp_mutex.server_mutex_exists(strict=True) is False


def test_pinned_server_mutex_strict(restore_platform, monkeypatch):
    """`BPMkitMcpServerPinned` (сервер, держащий сам bpmkit.exe): «нет» — только
    ERROR_FILE_NOT_FOUND; «есть» и «не знаю» — работает."""
    seen = []

    def fake_open(name):
        seen.append(name)
        return 0

    restore_platform("win32")
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", fake_open)
    assert mcp_mutex.pinned_server_running() is False
    assert seen == ["BPMkitMcpServerPinned"]

    def broken(name):
        raise OSError(6, "The handle is invalid")

    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", broken)
    assert mcp_mutex.pinned_server_running() is True
    monkeypatch.setattr(mcp_mutex, "_win_open_mutex_handle", lambda name: 5)
    assert mcp_mutex.pinned_server_running() is True
    restore_platform("linux")
    assert mcp_mutex.pinned_server_running() is False

