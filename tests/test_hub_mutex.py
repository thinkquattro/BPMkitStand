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

import unittest
import unittest.mock

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


if __name__ == "__main__":
    unittest.main()
