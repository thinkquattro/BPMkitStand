# -*- coding: utf-8 -*-
"""Именованный Windows-мьютекс диспетчера стендов BPMkit-hub.exe (GAP-229/GAP-284).

Контекст. Диспетчер (GAP-225, PyInstaller onefile) до этого модуля не сигналил о своей
работе никак: `standkit_companion/mcp_mutex.py` умеет только ЧИТАТЬ серверный мьютекс
(SERVER_MUTEX_NAME -- первоисточник BPMkit/server/bpmkit/core.py, отдельный репозиторий
`bpmsoft-mcp`), а собственного мьютекса у диспетчера не было вовсе. Два живых замера это
подтвердили:
  * GAP-229 (04.09.2026): деинсталляция BPMkit при ЗАПУЩЕННОМ диспетчере возвращала rc=0 и
    оставляла на диске единственный файл `BPMkit-hub.exe` -- живой процесс держал его
    открытым, а деинсталлятор об этом ничего не знал (у СЕРВЕРА такой гард уже был принят
    живьём -- GAP-155б в поставке BPMkit, мьютекс SERVER_MUTEX_NAME, отказ ДО удаления
    файлов).
  * GAP-284 (14.09.2026, приёмка 1.1.1 на VM): установка поверх РАБОТАЮЩЕГО диспетчера не
    блокировалась гейтом мастера (тот проверял только серверный мьютекс) -- занятый
    `BPMkit-hub.exe` доезжал до копирования, и Restart Manager показывал собственный диалог
    Windows вместо понятного сообщения BPMkit.

Этот модуль -- зеркало `BPMkit/server/bpmkit/core.py::acquire_server_mutex()` (тот же
приём CreateMutexW, тот же паттерн монки-патчинга `_win_create_mutex_handle` для тестов на
Linux-раннере), но со своим именем и в другом репозитории: BPMkitStand -- отдельный git-
репозиторий (submodule корневого `bpmsoft-mcp`), импортировать константу оттуда нельзя.
HUB_MUTEX_NAME -- ПЕРВОИСТОЧНИК для диспетчера (в отличие от `mcp_mutex.py`, где
SERVER_MUTEX_NAME -- вторая копия чужого первоисточника): у мьютекса диспетчера другого
дома нет. Копия строки для установщика (packaging/installer/bpmkit_installer.iss,
константа HubMutexName, репозиторий bpmsoft-mcp) неизбежна по той же причине, что у
ServerMutexName там же -- Pascal не читает чужой .py на этапе компиляции мастера;
расхождение -- best-effort сверка `tools/check_installer.py::_hub_mutex_name` (пропускается,
если указатель submodule в bpmsoft-mcp ещё не обновлён до коммита с этим файлом).

Без "Global\\"-префикса -- та же причина, что у серверного мьютекса: диспетчер, установщик
и деинсталлятор BPMkit всегда работают в одной интерактивной сессии рабочего стола обычного
пользователя (per-user установка по умолчанию, PrivilegesRequired=lowest); "Global\\"
потребовал бы SeCreateGlobalPrivilege, которого у него нет.
"""
from __future__ import annotations

import sys

__all__ = ["HUB_MUTEX_NAME", "acquire_hub_mutex"]

#: ПЕРВОИСТОЧНИК имени -- других копий в BPMkitStand быть не должно (искать перед правкой:
#: grep -r HUB_MUTEX_NAME). Копия для установщика -- packaging/installer/
#: bpmkit_installer.iss в репозитории bpmsoft-mcp, константа HubMutexName.
HUB_MUTEX_NAME = "BPMkitHubDispatcher"

# Хендл живёт в модульной переменной, чтобы GC не закрыл его раньше конца процесса (та же
# оговорка, что у core._server_mutex_handle в поставке BPMkit: CPython не гарантирует
# немедленную финализацию временных ctypes-хендлов, но держать ссылку явно -- дешевле, чем
# на это полагаться).
_hub_mutex_handle = None


def _win_create_mutex_handle(name):
    """Сырой вызов WinAPI CreateMutexW -- вынесен ОТДЕЛЬНО от acquire_hub_mutex(), чтобы
    тесты подменяли (monkeypatch/mock.patch.object) именно эту функцию и не зависели от
    реальной ОС: CI гоняет линуксовый раннер, где ctypes.WinDLL не существует вовсе, а тест
    «ошибка WinAPI не роняет старт диспетчера» обязан быть зелёным и там (см.
    tests/test_hub_mutex.py, конвенция -- tests/test_server_mutex.py поставки BPMkit)."""
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [wintypes.LPCVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    return kernel32.CreateMutexW(None, False, name)


def acquire_hub_mutex():
    """Создаёт именованный Windows-мьютекс HUB_MUTEX_NAME, живущий до конца процесса
    (GAP-229/GAP-284) -- сигнал установщику/деинсталлятору BPMkit (Inno Setup,
    ServerMutexRunning/HubMutexRunning в packaging/installer/bpmkit_installer.iss
    репозитория bpmsoft-mcp), что диспетчер стендов запущен. `standkit_hub.__main__.main()`
    зовёт эту функцию непосредственно перед началом обслуживания HTTP -- ПОСЛЕ того, как
    `bind_hub_server` подтвердил, что этот процесс действительно поднимает сервер (а не
    просто открывает браузер на УЖЕ работающем экземпляре через `HubAlreadyRunning` --
    тот путь мьютекс не трогает: второй мьютекс от процесса, который тут же завершится,
    создавать незачем).

    Только Windows -- на других ОС тихий no-op (сам Inno Setup существует только на
    Windows, дублировать проверку больше негде). ЛЮБАЯ ошибка (ctypes недоступен, WinAPI
    отказала, нет прав и т.п.) ПОДАВЛЯЕТСЯ -- диспетчер обязан подняться, даже если мьютекс
    создать не удалось: диагностика установщика не входит в контракт запуска диспетчера
    (тот же принцип, что у core.acquire_server_mutex в поставке BPMkit).

    Идемпотентна: повторный вызов в том же процессе возвращает True немедленно (хендл уже
    в _hub_mutex_handle, второй CreateMutexW не делается).

    Возвращает True, если мьютекс создан/уже удерживается этим процессом; False -- на
    не-Windows или при ошибке (в обоих случаях диспетчер продолжает работать)."""
    global _hub_mutex_handle
    if _hub_mutex_handle is not None:
        return True
    if not sys.platform.startswith("win"):
        return False
    try:
        handle = _win_create_mutex_handle(HUB_MUTEX_NAME)
    except Exception:
        return False
    if not handle:
        return False
    _hub_mutex_handle = handle
    return True
