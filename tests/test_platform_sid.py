# -*- coding: utf-8 -*-
"""
Тесты `standkit.platform._configure_sid_winapi`/`_convert_sid_to_string`
(GAP-311 Б2).

Контекст. Ревью нашло, что без явных ``argtypes``/``restype`` у
``OpenProcessToken``/``GetTokenInformation``/``ConvertSidToStringSidW`` ctypes
передаёт Python ``int`` по умолчанию как 32-битное значение — указатель SID и
хендлы на x64 Windows обрубаются, ``current_user_sid()`` в лучшем случае
возвращает ``None``, в худшем — мусор без единого исключения. Оба
исправленных места вынесены в отдельные функции ИМЕННО для тестируемости на
линуксовом раннере (реальный ``ctypes.windll`` недоступен вовсе) — здесь
подставляются заглушки ``advapi32``/``kernel32`` (обычные объекты с
изменяемыми ``argtypes``/``restype``, как у настоящей ctypes-привязки) и
проверяется:
  1. состав ``argtypes``/``restype`` для каждого задействованного вызова;
  2. что указатель SID передаётся в ``ConvertSidToStringSidW`` ОБЁРНУТЫМ в
     ``ctypes.c_void_p`` (не голым ``int``) -- ровно то, что и обрубалось бы
     без явного ``argtypes``.

Запуск: python -m pytest tests/test_platform_sid.py -q
"""
from __future__ import annotations

import ctypes
import types
from ctypes import wintypes

from standkit import platform as _platform


class _FakeFn:
    """Заглушка WinAPI-функции — обычный вызываемый объект с изменяемыми
    `argtypes`/`restype`, как у настоящей ctypes-привязки (тот же приём, что
    в tests/test_hub_mutex.py::TestMutexSecurityDescriptor)."""

    def __init__(self, impl):
        self._impl = impl
        self.argtypes = None
        self.restype = None

    def __call__(self, *args, **kwargs):
        return self._impl(*args, **kwargs)


def _fake_dlls(**impls):
    names = (
        "GetCurrentProcess",
        "OpenProcessToken",
        "GetTokenInformation",
        "ConvertSidToStringSidW",
        "LocalFree",
        "CloseHandle",
    )
    fns = {name: _FakeFn(impls.get(name, lambda *a, **kw: None)) for name in names}
    advapi32 = types.SimpleNamespace(
        OpenProcessToken=fns["OpenProcessToken"],
        GetTokenInformation=fns["GetTokenInformation"],
        ConvertSidToStringSidW=fns["ConvertSidToStringSidW"],
    )
    kernel32 = types.SimpleNamespace(
        GetCurrentProcess=fns["GetCurrentProcess"],
        LocalFree=fns["LocalFree"],
        CloseHandle=fns["CloseHandle"],
    )
    return advapi32, kernel32


# --- 1. argtypes/restype проставлены явно для каждого вызова --------------------


def test_configure_sid_winapi_sets_explicit_argtypes_for_every_call():
    advapi32, kernel32 = _fake_dlls()
    _platform._configure_sid_winapi(advapi32, kernel32)

    assert kernel32.GetCurrentProcess.argtypes == []
    assert kernel32.GetCurrentProcess.restype == wintypes.HANDLE

    assert advapi32.OpenProcessToken.argtypes == [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    assert advapi32.OpenProcessToken.restype == wintypes.BOOL

    assert advapi32.GetTokenInformation.argtypes == [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    assert advapi32.GetTokenInformation.restype == wintypes.BOOL

    # Пункт, который реально нашло ревью: PSID — c_void_p, НЕ дефолтный int.
    assert advapi32.ConvertSidToStringSidW.argtypes == [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)
    ]
    assert advapi32.ConvertSidToStringSidW.restype == wintypes.BOOL

    assert kernel32.LocalFree.argtypes == [ctypes.c_void_p]
    assert kernel32.LocalFree.restype == wintypes.HANDLE

    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]
    assert kernel32.CloseHandle.restype == wintypes.BOOL


# --- 2. sid_ptr передаётся в ConvertSidToStringSidW обёрнутым в c_void_p --------


def test_convert_sid_to_string_passes_c_void_p_not_bare_int():
    seen = {}

    def fake_convert(sid_arg, out_ptr):
        seen["sid_arg"] = sid_arg
        seen["type"] = type(sid_arg)
        out_ptr._obj.value = "S-1-5-21-111-222-333-1001"
        return True

    def fake_local_free(p):
        seen["freed"] = p
        return 0

    advapi32, kernel32 = _fake_dlls(
        ConvertSidToStringSidW=fake_convert, LocalFree=fake_local_free
    )

    result = _platform._convert_sid_to_string(advapi32, kernel32, sid_ptr=0xDEADBEEF)

    assert result == "S-1-5-21-111-222-333-1001"
    assert isinstance(seen["sid_arg"], ctypes.c_void_p)
    assert seen["sid_arg"].value == 0xDEADBEEF
    assert "freed" in seen  # LocalFree вызван на результате Convert


def test_convert_sid_to_string_returns_none_for_null_pointer_without_calling_winapi():
    called = []

    def fake_convert(*a, **kw):
        called.append(True)
        return True

    advapi32, kernel32 = _fake_dlls(ConvertSidToStringSidW=fake_convert)

    result = _platform._convert_sid_to_string(advapi32, kernel32, sid_ptr=0)

    assert result is None
    assert called == []


def test_convert_sid_to_string_returns_none_on_winapi_failure():
    advapi32, kernel32 = _fake_dlls(ConvertSidToStringSidW=lambda *a, **kw: False)

    result = _platform._convert_sid_to_string(advapi32, kernel32, sid_ptr=0xABCDEF)

    assert result is None


# --- GAP-311 Н1: process_create_time (используется для подтверждения "тот же процесс") ---


def test_linux_process_create_time_reads_stat_and_btime(tmp_path):
    proc_root = tmp_path
    pid_dir = proc_root / "42"
    pid_dir.mkdir()
    # 20 полей после ")" -> starttime (20-е, индекс 19) = 500 тиков.
    fields_after_comm = ["S"] + ["0"] * 18 + ["500"]
    (pid_dir / "stat").write_text("42 (bash) " + " ".join(fields_after_comm) + "\n")
    (proc_root / "stat").write_text("btime 1000000\nsome other line\n")

    result = _platform._linux_process_create_time(42, proc_root=proc_root)

    clk_tck = ctypes_sysconf_fallback()
    assert result == pytest_approx(1000000 + 500 / clk_tck)


def ctypes_sysconf_fallback():
    import os as _os
    try:
        return _os.sysconf("SC_CLK_TCK")
    except (ValueError, AttributeError, OSError):
        return 100


def pytest_approx(value):
    import pytest as _pytest
    return _pytest.approx(value)


def test_linux_process_create_time_handles_comm_with_spaces_and_parens(tmp_path):
    """``comm`` в скобках может содержать пробелы/скобки само по себе — резать
    нужно по ПОСЛЕДНЕЙ ")" в строке, а не по первому пробелу."""
    proc_root = tmp_path
    pid_dir = proc_root / "42"
    pid_dir.mkdir()
    fields_after_comm = ["S"] + ["0"] * 18 + ["777"]
    (pid_dir / "stat").write_text("42 (my (weird) proc) " + " ".join(fields_after_comm) + "\n")
    (proc_root / "stat").write_text("btime 2000000\n")

    result = _platform._linux_process_create_time(42, proc_root=proc_root)

    clk_tck = ctypes_sysconf_fallback()
    assert result == pytest_approx(2000000 + 777 / clk_tck)


def test_linux_process_create_time_returns_none_when_pid_missing(tmp_path):
    assert _platform._linux_process_create_time(99999, proc_root=tmp_path) is None


def test_linux_process_create_time_returns_none_without_btime_line(tmp_path):
    proc_root = tmp_path
    pid_dir = proc_root / "42"
    pid_dir.mkdir()
    fields_after_comm = ["S"] + ["0"] * 17 + ["500"] + ["0"]
    (pid_dir / "stat").write_text("42 (bash) " + " ".join(fields_after_comm) + "\n")
    (proc_root / "stat").write_text("no btime here\n")

    assert _platform._linux_process_create_time(42, proc_root=proc_root) is None


def test_process_create_time_uses_linux_path_on_linux(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "linux")
    monkeypatch.setattr(_platform, "_linux_process_create_time", lambda pid: 12345.0)

    assert _platform.process_create_time(4242) == 12345.0


def test_process_create_time_returns_none_off_supported_platforms(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "darwin")

    assert _platform.process_create_time(4242) is None


def test_windows_process_create_time_converts_filetime_and_closes_handle():
    calls = []

    def fake_open_process(access, inherit, pid):
        calls.append(("open", access, pid))
        return 777

    def fake_get_process_times(handle, creation, exit_t, kernel_t, user_t):
        # Значение самого FILETIME здесь не важно — конвертация в Unix epoch
        # подменена отдельно (см. mock.patch.object ниже), проверяем только
        # что GetProcessTimes реально вызван и что-то записал по указателю.
        creation._obj.dwLowDateTime = 12345
        creation._obj.dwHighDateTime = 0
        return True

    kernel32 = _fake_dlls()[1]  # переиспользуем заглушку kernel32 из _fake_dlls
    kernel32.OpenProcess = _FakeFn(fake_open_process)
    kernel32.GetProcessTimes = _FakeFn(fake_get_process_times)
    closed = []
    kernel32.CloseHandle = _FakeFn(lambda h: closed.append(h))

    def fake_filetime_to_unix(ft):
        return 999999999.0

    import unittest.mock as mock
    with mock.patch.object(_platform, "_filetime_to_unix", fake_filetime_to_unix):
        result = _platform._windows_process_create_time(kernel32, 4242)

    assert result == 999999999.0
    assert calls == [("open", 0x1000, 4242)]
    assert closed == [777]


def test_windows_process_create_time_returns_none_when_open_process_fails():
    kernel32 = _fake_dlls()[1]
    kernel32.OpenProcess = _FakeFn(lambda *a: 0)
    kernel32.GetProcessTimes = _FakeFn(lambda *a: False)

    assert _platform._windows_process_create_time(kernel32, 4242) is None


def test_windows_process_create_time_returns_none_when_get_process_times_fails():
    kernel32 = _fake_dlls()[1]
    kernel32.OpenProcess = _FakeFn(lambda *a: 777)
    kernel32.GetProcessTimes = _FakeFn(lambda *a: False)
    closed = []
    kernel32.CloseHandle = _FakeFn(lambda h: closed.append(h))

    result = _platform._windows_process_create_time(kernel32, 4242)

    assert result is None
    assert closed == [777]  # хендл закрыт даже при неудаче GetProcessTimes


def test_filetime_to_unix_matches_known_reference():
    """FILETIME 116444736000000000 (=1970-01-01T00:00:00Z, ровно эпоха Unix)."""
    ft = wintypes.FILETIME()
    value = _platform._FILETIME_UNIX_EPOCH_DELTA_100NS
    ft.dwLowDateTime = value & 0xFFFFFFFF
    ft.dwHighDateTime = value >> 32

    assert _platform._filetime_to_unix(ft) == pytest_approx(0.0)
