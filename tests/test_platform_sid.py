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
