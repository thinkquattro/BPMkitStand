"""
OS-абстракция запуска процессов: скрытый (headless, без консольного окна)
процесс на Windows, отсоединённый (setsid) процесс на Linux.

Два входа наружу, и оба скрывают консольное окно на Windows:
``spawn_hidden`` — ДОЛГОЖИВУЩИЙ фоновый процесс (стенд, агент), ``run_console``
— КОРОТКАЯ внешняя консольная утилита (appcmd/sc/docker/kubectl/taskkill/
powershell), результат которой нужен здесь и сейчас. Прямой ``subprocess.run``
в остальных модулях пакета запрещён (GAP-138): без ``CREATE_NO_WINDOW``
родитель без собственной консоли — ``pythonw``, служба — рождает мигающее
чёрное окно на каждый вызов.

Никакого хардкода путей вида ``C:\\...`` — все пути принимаются и возвращаются
как ``pathlib.Path``. Модуль не знает ничего про BPMSoft — только про то, как
корректно поднять/остановить/проверить произвольный процесс кроссплатформенно.

Остановка — с ЭСКАЛАЦИЕЙ (см. ``stop``): сначала мягкое завершение
(SIGTERM / CTRL_BREAK + ``taskkill`` без ``/F``), ожидание в пределах таймаута,
и только затем жёсткое (SIGKILL / ``taskkill /F``). Это существенно с тех пор,
как диспетчер умеет «усыновлять» стенды, поднятые вне его (см.
``standkit.adopt``): жёсткое убийство перестало быть редким случаем, а
BPMSoft-стенду нужно дать закрыть соединения с БД/Redis.

Остаточные пункты следующих итераций (Windows Job Object для остановки дерева
процессов, полноценный double-fork на Linux) — см. docs/ARCHITECTURE.md,
раздел «Что уже реализовано в каркасе vs TODO».
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, Sequence

# Сколько ждать мягкого завершения процесса, прежде чем эскалировать до
# жёсткого убийства. Значение с запасом: BPMSoft.WebHost на завершении
# закрывает пул соединений с БД и сбрасывает кэши, доли секунды ему мало.
DEFAULT_STOP_TIMEOUT = 10.0

# Шаг опроса «жив ли ещё процесс» в ожидании мягкого завершения.
DEFAULT_STOP_POLL_INTERVAL = 0.25

# Кап ожидания ПОСЛЕ жёсткого убийства — оно почти мгновенно, ждать столько же,
# сколько мягкого, бессмысленно.
_HARD_KILL_WAIT = 5.0


class ProcessError(Exception):
    """Ошибки запуска/остановки/проверки процесса."""


# Флаг Windows: дочерний процесс НЕ получает своей консоли. Продублирован
# числом, потому что вне Windows атрибута ``subprocess.CREATE_NO_WINDOW`` нет
# вовсе (до реального запуска с этим флагом там дело всё равно не доходит —
# см. ``run_console``), а ветка кода должна оставаться проверяемой тестом.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def run_console(cmd: Sequence[str], **kwargs) -> subprocess.CompletedProcess:
    """
    ЕДИНАЯ точка запуска ВНЕШНИХ КОНСОЛЬНЫХ утилит (``appcmd``, ``sc``,
    ``docker``, ``kubectl``, ``taskkill``, ``tasklist``, ``powershell``): на
    Windows всегда добавляет ``creationflags=CREATE_NO_WINDOW``.

    Зачем. Родитель, у которого своей консоли НЕТ (``pythonw.exe``, служба,
    фоновый поллер хаба), заставляет Windows выдать консоль каждому
    консольному ребёнку — на экране это всплывающее и тут же исчезающее чёрное
    окно (плюс `conhost.exe`/`OpenConsole.exe` в списке процессов). Из
    обычного терминала дефект не виден: там ребёнок наследует консоль
    родителя. Ровно так GAP-138 и дожил до владельца: поллер хаба раз в ~12 с
    опрашивал IIS-стенд двумя ``appcmd`` — два мигающих окна.

    Поэтому НИ ОДИН модуль пакета не зовёт ``subprocess.run`` напрямую: флаг
    ставится здесь, в одном месте. Регресс стережёт тест
    ``tests/test_no_window.py`` (статическая проверка исходников).

    ``creationflags`` вызывающего не затирается, а дополняется битом.
    Возвращает то же, что ``subprocess.run`` (тесты подменяют именно его).
    """
    if sys.platform == "win32":
        kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | CREATE_NO_WINDOW
    return subprocess.run(cmd, **kwargs)


def spawn_hidden(cmd: Sequence[str], cwd: Path, log_path: Path) -> int:
    """
    Запускает процесс в фоне, без видимого консольного окна (Windows) /
    в новой сессии (Linux), с перенаправлением stdout+stderr в log_path.

    Возвращает pid запущенного процесса. Не блокирует вызывающий поток.
    """
    cwd = Path(cwd)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Открываем лог в режиме дозаписи — предыдущие запуски не теряются молча.
    log_file = open(log_path, "ab", buffering=0)

    popen_kwargs: dict = {
        "cwd": str(cwd),
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
    }

    if sys.platform == "win32":
        # Скрытое окно + отдельная группа процессов, чтобы CTRL-C консоли
        # родителя (если он в консоли) не убивал дочерний процесс стенда.
        creationflags = 0
        creationflags |= CREATE_NO_WINDOW
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        popen_kwargs["creationflags"] = creationflags
    else:
        # Новая сессия — процесс переживает завершение управляющего терминала
        # и получает свою группу для последующего управляемого stop().
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(list(cmd), **popen_kwargs)
    except OSError as exc:
        raise ProcessError(f"Не удалось запустить процесс {cmd!r}: {exc}") from exc
    finally:
        log_file.close()

    return proc.pid


def is_elevated() -> Optional[bool]:
    """
    Запущен ли ТЕКУЩИЙ процесс с правами администратора (Windows).

    ``None`` — выяснить не удалось: не Windows (там понятия «elevated» в этом
    смысле нет) либо ctypes недоступен. Именно ``None``, а не ``False``:
    «не знаю» и «точно без прав» — разные ответы, и вызывающий код
    (диагностика appcmd в ``standkit.hosting``, кнопка перезапуска в хабе)
    ведёт себя по-разному.

    Живёт здесь, а не в ``standkit.hosting``, потому что это OS-примитив того
    же класса, что ``is_alive``/``stop``: его спрашивают и бэкенд хостинга
    (честная классификация отказа appcmd), и веб-хаб (показать индикатор и
    предложить перезапуск с правами администратора). Две копии одного
    ``IsUserAnAdmin`` разъехались бы.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except Exception:
        # Любой сбой ctypes/WinAPI — честное «не знаю», а не «точно нет».
        return None


def _configure_sid_winapi(advapi32, kernel32) -> None:
    """
    Выставляет ``argtypes``/``restype`` для WinAPI-вызовов, участвующих в
    ``current_user_sid()``.

    ЗАЧЕМ ЭТО ОБЯЗАТЕЛЬНО (а не «для порядка»). Без явных ``argtypes`` ctypes
    конвертирует переданный Python ``int`` по умолчанию как обычный ``int``
    (32 бита), а НЕ как указатель/хендл (64 бита на x64 Windows) — верхняя
    половина адреса SID/хендла токена молча обрубается, и вызов либо падает,
    либо (хуже) отдаёт мусорный SID без единого исключения. Ревью GAP-311
    поймало это именно на ``ConvertSidToStringSidW`` — токен и хендлы страдают
    от того же класса ошибки.

    Вынесена в отдельную функцию: тестируется на Linux подменой
    ``advapi32``/``kernel32`` объектами-заглушками, без реального ctypes/WinAPI.
    """
    import ctypes
    from ctypes import wintypes

    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL

    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL

    # PSID — это указатель НЕ на фиксированную структуру, а на переменной
    # длины блок байт; c_void_p — единственный корректный тип для него.
    advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def _convert_sid_to_string(advapi32, kernel32, sid_ptr: int) -> Optional[str]:
    """
    Оборачивает ``ConvertSidToStringSidW`` + освобождение результата
    (``LocalFree``) — вынесено отдельно от ``current_user_sid``, чтобы шаг
    «передать указатель SID в WinAPI» проверялся тестом изолированно от
    OpenProcessToken/GetTokenInformation.

    ``sid_ptr`` ЯВНО заворачивается в ``ctypes.c_void_p`` перед вызовом:
    голый Python ``int`` в вызове без argtypes ctypes отправил бы как 32-битный
    ``int`` (см. ``_configure_sid_winapi``) — на x64 адрес обрубился бы.
    """
    import ctypes

    if not sid_ptr:
        return None
    str_sid_ptr = ctypes.c_wchar_p()
    if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr), ctypes.byref(str_sid_ptr)):
        return None
    try:
        return str_sid_ptr.value
    finally:
        kernel32.LocalFree(str_sid_ptr)


def current_user_sid() -> Optional[str]:
    """
    SID ТЕКУЩЕГО пользователя Windows (строка вида ``S-1-5-21-...``).

    ЗАЧЕМ. Запрос UAC (``standkit_hub.elevation.relaunch_elevated``) может
    быть подтверждён ЛЮБОЙ учётной записью из группы «Администраторы» —
    Windows не требует, чтобы это была та же учётка, что запустила исходный
    процесс. Реестр стендов, ключи шифрования секретов и файлы диспетчера в
    ``run_dir``/``%APPDATA%`` привязаны к профилю КОНКРЕТНОГО пользователя
    Windows, поэтому повышение прав «не под собой» для диспетчера означает не
    ускорение, а потерю доступа к собственным данным (GAP-311 п.4). SID, а не
    имя — потому что имя переименовывается, а SID пользователя неизменен.

    ``None`` — не Windows либо ЛЮБОЙ сбой (WinAPI недоступен, ctypes упал):
    в этом случае сверку SID делать не с чем, и вызывающий код (elevation,
    ``standkit_hub.instance.should_takeover``) трактует это как «не проверяем»,
    а не как отказ.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        # ПРИВАТНЫЙ WinDLL (не глобальный ctypes.windll.*, GAP-311 M6):
        # `_configure_sid_winapi` мутирует `argtypes`/`restype` функций на
        # объекте DLL — на глобальном `ctypes.windll.advapi32`/`kernel32`
        # это меняло бы поведение ЛЮБОГО другого кода пакета, который зовёт
        # те же функции (напр. `LocalFree`/`CloseHandle`) с другими
        # ожиданиями относительно типов аргументов. Свой хендл — своя,
        # изолированная настройка (тот же приём, что в `standkit_hub.mutex`).
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_sid_winapi(advapi32, kernel32)

        TOKEN_QUERY = 0x0008
        TokenUser = 1

        htoken = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(htoken)):
            return None
        try:
            size = wintypes.DWORD(0)
            # Первый вызов — только чтобы узнать нужный размер буфера.
            advapi32.GetTokenInformation(htoken, TokenUser, None, 0, ctypes.byref(size))
            if size.value == 0:
                return None
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(htoken, TokenUser, buf, size, ctypes.byref(size)):
                return None
            # TOKEN_USER — это { SID_AND_ATTRIBUTES User }, первое поле —
            # указатель PSID (не встроенный SID, поэтому просто читаем указатель).
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            return _convert_sid_to_string(advapi32, kernel32, sid_ptr)
        finally:
            kernel32.CloseHandle(htoken)
    except Exception:
        # Любой сбой ctypes/WinAPI — честное «не знаю», проверку SID пропускаем.
        return None


def current_user_name() -> Optional[str]:
    """
    Человекочитаемое имя ТЕКУЩЕГО пользователя ОС — для текста отказа при
    повышении прав под другой учётной записью (``standkit_hub.elevation``,
    GAP-311 п.4) и для поля ``user`` в ``GET /api/hub/elevation``.

    На Windows предпочитаем ``ДОМЕН\\Имя`` из ``USERDOMAIN``/``USERNAME`` —
    ровно так Windows подписывает учётку в самом диалоге UAC, поэтому текст
    отказа узнаваем. ``getpass.getuser()`` — переносимый фолбэк (и основной
    путь вне Windows). ``None`` — не удалось определить ничем.
    """
    if sys.platform == "win32":
        name = os.environ.get("USERNAME")
        if name:
            domain = os.environ.get("USERDOMAIN")
            return f"{domain}\\{name}" if domain else name
    try:
        import getpass

        return getpass.getuser()
    except Exception:
        return None


def _configure_session_id_winapi(kernel32) -> None:
    """``argtypes``/``restype`` для ``ProcessIdToSessionId`` (GAP-445) --
    вынесена отдельно для тестируемости на Linux заглушками, тем же приёмом,
    что ``_configure_sid_winapi``/``_configure_process_time_winapi``."""
    from ctypes import wintypes

    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, wintypes.PDWORD]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL


def current_session_id(pid: Optional[int] = None) -> Optional[int]:
    """
    Номер сеанса служб терминалов (Terminal Services session id) процесса
    ``pid`` (по умолчанию -- ТЕКУЩЕГО процесса), через WinAPI
    ``ProcessIdToSessionId`` (GAP-445).

    ЗАЧЕМ. Именованный мьютекс диспетчера (``standkit_hub.mutex.HUB_MUTEX_NAME``)
    и Windows SID (``current_user_sid`` выше) отвечают на РАЗНЫЕ вопросы:
    мьютекс -- «есть ли диспетчер В ЭТОМ сеансе рабочего стола», SID -- «под
    какой учётной записью». Ни один не отвечает на «в КАКОМ сеансе (RDP,
    сеанс 0, вторая учётка на той же машине) работает диспетчер, которого я
    вижу занимающим порт 8770, но чей мьютекс я НЕ вижу» -- установщик
    (``bpmkit_installer.iss``, ``CheckForMutexes``/``HubMutexRunning``)
    опрашивает мьютекс ТОЛЬКО в своём сеансе (без ``Global\\``-префикса,
    решение владельца 21.09.2026 -- сеансовая модель мьютекса остаётся), и
    расхождение «порт занят, мьютекс не виден» означает ровно это: диспетчер
    жив, но в ДРУГОМ сеансе. ``ProcessIdToSessionId`` -- единственный публичный
    WinAPI-вызов, который называет сеанс ЛЮБОГО живого pid на машине без
    открытия хендла процесса (в отличие от ``OpenProcess``/токена выше) --
    работает даже для процесса другого пользователя, если у вызывающего есть
    право видеть сам pid (``tasklist``/``netstat -ano`` его уже показали).

    ``standkit_hub.instance.HubInstanceState.session_id`` хранит РЕЗУЛЬТАТ
    этого вызова для СВОЕГО процесса -- второй, независимый от мьютекса
    источник «в каком сеансе я живу», который устанавливающий читает из
    файла состояния хаба (``standkit-hub.json``) рядом с ``pid``.

    ``None`` -- не Windows либо ЛЮБОЙ сбой (WinAPI недоступен, чужой pid не
    виден и т.п.): честное «не знаю», а не 0 (сеанс 0 -- РЕАЛЬНЫЙ, отличимый
    результат: служба/RDP-сессия без интерактивного рабочего стола).
    """
    if sys.platform != "win32":
        return None
    target_pid = int(pid) if pid is not None else os.getpid()
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _configure_session_id_winapi(kernel32)

        session_id = wintypes.DWORD(0)
        if not kernel32.ProcessIdToSessionId(wintypes.DWORD(target_pid), ctypes.byref(session_id)):
            return None
        return int(session_id.value)
    except Exception:
        return None


def _configure_process_time_winapi(kernel32) -> None:
    """
    ``argtypes``/``restype`` для ``OpenProcess``/``GetProcessTimes``/
    ``CloseHandle`` (GAP-311 Н1) — вынесена отдельно для тестируемости на
    Linux заглушками, тем же приёмом, что ``_configure_sid_winapi``.
    """
    import ctypes
    from ctypes import wintypes

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


# Разница (в 100-наносекундных интервалах FILETIME) между эпохой Windows
# (1601-01-01) и эпохой Unix (1970-01-01) — константа, не вычисление.
_FILETIME_UNIX_EPOCH_DELTA_100NS = 116444736000000000


def _filetime_to_unix(filetime) -> float:
    """``FILETIME`` (100-наносекундные интервалы с 1601-01-01) → Unix epoch секунды."""
    value = (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime
    return (value - _FILETIME_UNIX_EPOCH_DELTA_100NS) / 10_000_000.0


def _windows_process_create_time(kernel32, pid: int) -> Optional[float]:
    """
    Читает время СОЗДАНИЯ процесса через ``OpenProcess`` (только
    ``PROCESS_QUERY_LIMITED_INFORMATION`` — минимум прав, достаточный даже
    для чужой учётки/сервиса) + ``GetProcessTimes`` (GAP-311 Н1). Вынесена
    отдельно от ``process_create_time``, чтобы принимать готовый ``kernel32``
    в тестах (заглушка вместо реального ``ctypes.WinDLL``).
    """
    import ctypes
    from ctypes import wintypes

    _configure_process_time_winapi(kernel32)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_time),
            ctypes.byref(kernel_time), ctypes.byref(user_time),
        )
        if not ok:
            return None
        return _filetime_to_unix(creation)
    finally:
        kernel32.CloseHandle(handle)


def _linux_process_create_time(pid: int, *, proc_root: Optional[Path] = None) -> Optional[float]:
    """
    Время СОЗДАНИЯ процесса из ``/proc/<pid>/stat`` (поле 22, ``starttime`` —
    в тиках с момента загрузки системы) + ``btime`` из ``/proc/stat`` (момент
    загрузки, Unix epoch) — GAP-311 Н1. ``proc_root`` — точка подмены для
    тестов (реальный ``/proc`` на CI недетерминирован).
    """
    root = Path(proc_root) if proc_root else Path("/proc")
    try:
        stat_text = (root / str(pid) / "stat").read_text()
        # ``comm`` (имя процесса) — в круглых скобках и МОЖЕТ содержать
        # пробелы/скобки само по себе, поэтому режем по ПОСЛЕДНЕЙ ")" в
        # строке, а не по первому пробелу.
        rparen = stat_text.rindex(")")
        fields_after_comm = stat_text[rparen + 2:].split()
        starttime_ticks = int(fields_after_comm[19])  # 20-е поле после comm — starttime

        btime = None
        for line in (root / "stat").read_text().splitlines():
            if line.startswith("btime "):
                btime = int(line.split()[1])
                break
        if btime is None:
            return None

        try:
            clk_tck = os.sysconf("SC_CLK_TCK")
        except (ValueError, AttributeError, OSError):
            clk_tck = 100
        return float(btime) + starttime_ticks / float(clk_tck)
    except (OSError, ValueError, IndexError):
        return None


def process_create_time(pid: int) -> Optional[float]:
    """
    Unix epoch секунд, когда процесс ``pid`` был СОЗДАН ОС — не когда мы его
    впервые увидели (GAP-311 Н1).

    ЗАЧЕМ. Единственный надёжный признак «это тот же самый процесс между
    двумя проверками», устойчивый к переиспользованию pid: сверка «файл
    состояния сам с собой» (см. ``standkit_hub.instance._same_process_as_recorded``,
    исходный баг ревью — старый хаб завершился, ОС отдала его pid левому
    ``sleep 600``, а сверка "текущий файл состояния всё ещё описывает этот
    pid" тривиально совпадала САМА С СОБОЙ и подтверждала подмену). Время
    создания процесса читается заново из ОС при каждой проверке, а не из
    файла, который сам процесс не переписывает после переиспользования pid.

    ``None`` — платформа не поддерживается (macOS и прочее не-Windows/не-Linux)
    либо ЛЮБОЙ сбой (WinAPI недоступен, ``/proc`` недоступен, permission
    denied, процесс уже завершился между вызовами) — вызывающий код честно
    трактует это как «не подтверждено», а НЕ как «подтверждено, что тот же».
    """
    try:
        if sys.platform == "win32":
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            return _windows_process_create_time(kernel32, pid)
        if sys.platform.startswith("linux"):
            return _linux_process_create_time(pid)
        return None
    except Exception:
        return None


def is_alive(pid: int) -> bool:
    """Проверяет, жив ли процесс с данным pid (кроссплатформенно)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _is_alive_windows(pid)
    return _is_alive_posix(pid)


def stop(
    pid: int,
    *,
    timeout: float = DEFAULT_STOP_TIMEOUT,
    poll_interval: float = DEFAULT_STOP_POLL_INTERVAL,
    tree: bool = True,
) -> bool:
    """
    Останавливает процесс по pid с эскалацией «мягко → таймаут → жёстко».

    Порядок:
      1. мягкое завершение — POSIX: ``SIGTERM`` (ТОЛЬКО этому pid, группа
         процессов никогда не трогается — см. ``_stop_posix``); Windows:
         ``CTRL_BREAK_EVENT`` плюс ``taskkill`` БЕЗ ``/F``;
      2. ожидание до ``timeout`` секунд с опросом раз в ``poll_interval``;
      3. если не завершился — жёстко: ``SIGKILL`` / ``taskkill /F``.

    ``tree`` (Windows-специфично, GAP-311 Б1) — включать ли ``/T`` (дерево
    процессов) в ``taskkill``. ``True`` (по умолчанию) — прежнее поведение,
    нужное для остановки самого СТЕНДА (kestrel вместе со своими детьми).
    ``False`` — убить ТОЛЬКО указанный pid: обязателен, когда таргет — сам
    процесс диспетчера standkit-hub, а не стенд, потому что kestrel-стенды и
    локальный агент — прямые дети хаба (``spawn_hidden``), и ``/T`` вместе с
    хабом гасит их все. На POSIX параметр принимается для симметрии сигнатуры,
    но ни на что не влияет — здесь дерево и так никогда не убивалось
    (``os.kill(pid, ...)`` бьёт точно в указанный pid, а не в группу).

    ``timeout=0`` пропускает ожидание и эскалирует сразу (используется в тестах,
    чтобы не ждать реальное время).

    Возвращает True, если процесс на момент возврата считается остановленным
    (уже не был жив, либо остановлен успешно).
    """
    if not is_alive(pid):
        return True

    if sys.platform == "win32":
        return _stop_windows(pid, timeout=timeout, poll_interval=poll_interval, tree=tree)
    return _stop_posix(pid, timeout=timeout, poll_interval=poll_interval)


def wait_for_exit(pid: int, timeout: float, poll_interval: float = DEFAULT_STOP_POLL_INTERVAL) -> bool:
    """
    Ждёт завершения процесса не дольше ``timeout`` секунд.

    Возвращает True, если процесс завершился. При ``timeout <= 0`` делает ровно
    одну проверку и не спит вовсе.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    step = max(0.01, poll_interval)
    while True:
        if not is_alive(pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(step, remaining))


# --- Windows-специфика ---

def _is_alive_windows(pid: int) -> bool:
    try:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            STILL_ACTIVE = 259
            ok = ctypes.windll.kernel32.GetExitCodeProcess(  # type: ignore[attr-defined]
                handle, ctypes.byref(exit_code)
            )
            return bool(ok) and exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    except Exception:
        # Фолбэк на tasklist, если ctypes-путь недоступен по какой-то причине.
        # Через run_console — иначе фолбэк сам мигал бы консольным окном
        # (GAP-138); проверка кода возврата не нужна, важно лишь наличие pid
        # в выводе.
        try:
            proc = run_console(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
            )
            return str(pid) in (proc.stdout or "")
        except Exception:
            return False


def _send_ctrl_break(pid: int) -> None:
    """
    Best-effort мягкий сигнал консольному процессу на Windows
    (``GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)``).

    Работает только когда вызывающий процесс разделяет консоль с целевым —
    для хаба/агента это обычно НЕ так, и вызов тихо не проходит. Это осознанно:
    попытка бесплатная, а следом всё равно идёт ``taskkill``. pid<=0 не
    передаём никогда — нулевая группа означала бы «сигнал самому себе».
    """
    if pid <= 0:
        return
    try:
        import ctypes

        CTRL_BREAK_EVENT = 1
        ctypes.windll.kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid)  # type: ignore[attr-defined]
    except Exception:
        pass


def _taskkill(pid: int, *, force: bool, tree: bool = True) -> None:
    """``taskkill /PID <pid>`` (``/T`` — дерево процессов, ``/F`` — жёстко)."""
    args = ["taskkill", "/PID", str(pid)]
    if tree:
        args.append("/T")
    if force:
        args.append("/F")
    try:
        run_console(
            args,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ProcessError(f"Не удалось остановить процесс {pid}: {exc}") from exc


def _stop_windows(pid: int, *, timeout: float, poll_interval: float, tree: bool = True) -> bool:
    # Мягко: CTRL_BREAK (если консоль общая) + taskkill без /F — тот шлёт
    # WM_CLOSE и даёт процессу отработать штатное завершение.
    _send_ctrl_break(pid)
    _taskkill(pid, force=False, tree=tree)
    if wait_for_exit(pid, timeout, poll_interval):
        return True

    # Не успел — жёстко.
    _taskkill(pid, force=True, tree=tree)
    wait_for_exit(pid, min(timeout, _HARD_KILL_WAIT), poll_interval)
    return not is_alive(pid)


# --- Linux/POSIX-специфика ---

def _is_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Процесс существует, но принадлежит другому пользователю — жив.
        return True


def _stop_posix(pid: int, *, timeout: float, poll_interval: float) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        raise ProcessError(f"Не удалось остановить процесс {pid}: {exc}") from exc

    if wait_for_exit(pid, timeout, poll_interval):
        return True

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as exc:
        raise ProcessError(f"Не удалось принудительно завершить процесс {pid}: {exc}") from exc

    wait_for_exit(pid, min(timeout, _HARD_KILL_WAIT), poll_interval)
    return not is_alive(pid)
