# -*- coding: utf-8 -*-
"""Тесты `standkit_hub.mutex` (GAP-229/GAP-284).

Контекст. Диспетчер стендов (BPMkit-hub.exe, GAP-225) до этого модуля не сигналил о своей
работе никак -- деинсталляция BPMkit при живом диспетчере молча оставляла процесс и каталог
(GAP-229), а установщик не видел его как причину занятых файлов (GAP-284). `mutex.py` --
зеркало `core.acquire_server_mutex()` поставки BPMkit (тот же приём CreateMutexW), со своим
именем (HUB_MUTEX_NAME) в другом репозитории.

Что покрыто ЗДЕСЬ (чисто Python-сторона, без установщика -- тот проверяется в bpmsoft-mcp,
tools/check_installer.py и tests/test_installer_script.py):
  1. имя мьютекса, с которым реально идёт WinAPI-вызов, берётся из ЕДИНСТВЕННОЙ константы
     HUB_MUTEX_NAME -- не литералом где-то ещё;
  2. HUB_MUTEX_NAME отличается от серверного SERVER_MUTEX_NAME -- иначе установщик не
     сможет различить "жив сервер" от "жив диспетчер" по одному и тому же объекту;
  3. на не-Windows acquire_hub_mutex() -- тихий no-op: WinAPI не вызывается вовсе;
  4. ошибка WinAPI (ctypes недоступен, отказ CreateMutexW, нет прав) НЕ роняет вызов --
     диспетчер обязан стартовать в любом случае;
  5. повторный вызов в одном процессе идемпотентен: второй WinAPI-вызов не делается.

Мокается WinAPI-обёртка _win_create_mutex_handle -- тесты не зависят от реальной ОС и
обязаны быть зелёными и на линуксовом раннере CI (ctypes.WinDLL на Linux не существует
вовсе), и на Windows (где реальный BPMkit-hub.exe мог бы случайно оказаться запущен на
машине, где гоняются тесты, и дать ложноположительный результат при обращении к
настоящему API).

Запуск: python -m pytest tests/test_hub_mutex.py -q"""
from __future__ import annotations

import ctypes
import types
import unittest
import unittest.mock
from ctypes import wintypes

from standkit_hub import mutex


class TestHubMutex(unittest.TestCase):
    def setUp(self):
        # Каждый тест стартует с чистого состояния: реальный процесс тестраннера мьютекс
        # не держит, но соседний тест мог уже "приобрести" его через мок.
        self._orig_platform = mutex.sys.platform
        self._orig_handle = mutex._hub_mutex_handle
        mutex._hub_mutex_handle = None

    def tearDown(self):
        mutex.sys.platform = self._orig_platform
        mutex._hub_mutex_handle = self._orig_handle

    def _set_platform(self, value):
        mutex.sys.platform = value

    # -- 1. имя берётся из единственной константы -----------------------------------

    def test_uses_the_single_name_constant(self):
        seen = {}

        def fake_create(name):
            seen["name"] = name
            return 12345

        self._set_platform("win32")
        with unittest.mock.patch.object(mutex, "_win_create_mutex_handle", fake_create):
            self.assertTrue(mutex.acquire_hub_mutex())
        self.assertEqual(seen["name"], mutex.HUB_MUTEX_NAME)
        self.assertEqual(mutex.HUB_MUTEX_NAME, "BPMkitHubDispatcher")

    # -- 2. отличается от серверного мьютекса ----------------------------------------

    def test_differs_from_the_server_mutex_name(self):
        """Установщик проверяет оба мьютекса раздельно (ServerMutexRunning /
        HubMutexRunning в bpmsoft-mcp) -- одинаковое имя сделало бы их неразличимыми."""
        self.assertNotEqual(mutex.HUB_MUTEX_NAME, "BPMkitMcpServer")

    # -- 3. не-Windows: тихий no-op ---------------------------------------------------

    def test_non_windows_is_a_noop(self):
        def must_not_be_called(name):
            raise AssertionError(
                "WinAPI не должен вызываться на не-Windows платформе: name=" + repr(name))

        for platform_value in ("linux", "darwin", "linux2"):
            with self.subTest(platform=platform_value):
                mutex._hub_mutex_handle = None
                self._set_platform(platform_value)
                with unittest.mock.patch.object(
                        mutex, "_win_create_mutex_handle", must_not_be_called):
                    self.assertFalse(mutex.acquire_hub_mutex())

    # -- 4. ошибка WinAPI не роняет вызов --------------------------------------------

    def test_winapi_exception_is_suppressed_not_raised(self):
        def raises(name):
            raise OSError("нет доступа к kernel32 (симуляция)")

        self._set_platform("win32")
        with unittest.mock.patch.object(mutex, "_win_create_mutex_handle", raises):
            self.assertFalse(mutex.acquire_hub_mutex())

    def test_null_handle_is_not_an_error(self):
        """CreateMutexW возвращает 0/NULL только при отказе (нет прав и т.п.) -- это тоже
        штатный неуспех детекта, не исключение."""
        self._set_platform("win32")
        with unittest.mock.patch.object(mutex, "_win_create_mutex_handle", lambda name: 0):
            self.assertFalse(mutex.acquire_hub_mutex())

    # -- 5. идемпотентность в одном процессе -----------------------------------------

    def test_second_call_in_the_same_process_does_not_call_winapi_again(self):
        calls = []

        def fake_create(name):
            calls.append(name)
            return 999

        self._set_platform("win32")
        with unittest.mock.patch.object(mutex, "_win_create_mutex_handle", fake_create):
            self.assertTrue(mutex.acquire_hub_mutex())
            self.assertTrue(mutex.acquire_hub_mutex())
        self.assertEqual(len(calls), 1, "второй вызов не обязан снова звать CreateMutexW")


class TestMutexSecurityDescriptor(unittest.TestCase):
    """GAP-311 В8: security descriptor мьютекса (SDDL, argtypes, откат при ошибке).

    Заглушки advapi32/kernel32 -- обычные объекты с методами вместо реальных
    ``ctypes.WinDLL`` (недоступен на линуксовом раннере CI) -- та же техника, что у
    `tests/test_platform.py` для `_configure_sid_winapi`/`_convert_sid_to_string` (Б2):
    проверяем состав `argtypes`/`restype` и корректную передачу указателя, не выполняя
    реального WinAPI-вызова."""

    class _FakeFn:
        """Заглушка WinAPI-функции -- обычный вызываемый объект с изменяемыми
        `argtypes`/`restype`, как у настоящей ctypes-привязки."""

        def __init__(self, impl):
            self._impl = impl
            self.argtypes = None
            self.restype = None

        def __call__(self, *args, **kwargs):
            return self._impl(*args, **kwargs)

    def _fake_dlls(self, convert_impl):
        advapi32 = types.SimpleNamespace(
            ConvertStringSecurityDescriptorToSecurityDescriptorW=self._FakeFn(convert_impl)
        )
        kernel32 = types.SimpleNamespace(LocalFree=self._FakeFn(lambda h: 0))
        return advapi32, kernel32

    # -- argtypes/restype проставлены явно (не дефолтный int32 у ctypes) -------------

    def test_configure_mutex_sd_winapi_sets_explicit_argtypes(self):
        advapi32, kernel32 = self._fake_dlls(lambda *a: True)
        mutex._configure_mutex_sd_winapi(advapi32, kernel32)

        convert_fn = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
        self.assertEqual(
            convert_fn.argtypes,
            [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(wintypes.DWORD),
            ],
        )
        self.assertEqual(convert_fn.restype, wintypes.BOOL)
        self.assertEqual(kernel32.LocalFree.argtypes, [wintypes.HLOCAL])
        self.assertEqual(kernel32.LocalFree.restype, wintypes.HLOCAL)

    # -- успешная сборка SECURITY_ATTRIBUTES из SDDL ----------------------------------

    def test_sddl_to_security_attributes_success_returns_sa_and_sd_ptr(self):
        seen = {}

        def fake_convert(sddl, revision, psd, psize):
            seen["sddl"] = sddl
            seen["revision"] = revision
            buf = ctypes.create_string_buffer(16)
            seen["buf"] = buf  # держим ссылку, чтобы GC не забрал раньше проверки
            ptr = ctypes.cast(psd, ctypes.POINTER(ctypes.c_void_p))
            ptr[0] = ctypes.addressof(buf)
            return True

        advapi32, kernel32 = self._fake_dlls(fake_convert)
        sa, sd_ptr = mutex._sddl_to_security_attributes(advapi32, kernel32, "D:(A;;GA;;;SY)")

        self.assertEqual(seen["sddl"], "D:(A;;GA;;;SY)")
        self.assertEqual(seen["revision"], 1)
        self.assertIsInstance(sd_ptr, ctypes.c_void_p)
        self.assertNotEqual(sd_ptr.value, None)
        self.assertEqual(sa.lpSecurityDescriptor, sd_ptr.value)
        self.assertFalse(sa.bInheritHandle)

    # -- отказ WinAPI (FALSE/NULL) -> OSError, без частично собранной структуры ------

    def test_sddl_to_security_attributes_failure_raises_oserror(self):
        advapi32, kernel32 = self._fake_dlls(lambda sddl, rev, psd, psize: False)
        with self.assertRaises(OSError):
            mutex._sddl_to_security_attributes(advapi32, kernel32, "D:(A;;GA;;;SY)")

    def test_sddl_to_security_attributes_null_pointer_raises_oserror(self):
        def fake_convert(sddl, revision, psd, psize):
            ptr = ctypes.cast(psd, ctypes.POINTER(ctypes.c_void_p))
            ptr[0] = 0
            return True

        advapi32, kernel32 = self._fake_dlls(fake_convert)
        with self.assertRaises(OSError):
            mutex._sddl_to_security_attributes(advapi32, kernel32, "D:(A;;GA;;;SY)")

    # -- SDDL: SY/BA/WD присутствуют, владелец -- SID пользователя или OW fallback ---

    def test_sddl_contains_system_admins_everyone_synchronize_only(self):
        sddl = mutex._mutex_security_descriptor_sddl()
        self.assertIn("(A;;GA;;;SY)", sddl)
        self.assertIn("(A;;GA;;;BA)", sddl)
        self.assertIn("(A;;0x00100000;;;WD)", sddl)

    def test_sddl_uses_current_user_sid_when_available(self):
        with unittest.mock.patch(
            "standkit.platform.current_user_sid", lambda: "S-1-5-21-111-222-333-1001"
        ):
            sddl = mutex._mutex_security_descriptor_sddl()
        self.assertIn("(A;;GA;;;S-1-5-21-111-222-333-1001)", sddl)
        self.assertNotIn("OW", sddl)

    def test_sddl_falls_back_to_ow_when_sid_unavailable(self):
        with unittest.mock.patch("standkit.platform.current_user_sid", lambda: None):
            sddl = mutex._mutex_security_descriptor_sddl()
        self.assertIn("(A;;GA;;;OW)", sddl)


if __name__ == "__main__":
    unittest.main()
