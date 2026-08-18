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
) -> bool:
    """
    Останавливает процесс по pid с эскалацией «мягко → таймаут → жёстко».

    Порядок:
      1. мягкое завершение — POSIX: ``SIGTERM``; Windows: ``CTRL_BREAK_EVENT``
         (процесс стенда спавнится в собственной группе, см. ``spawn_hidden``)
         плюс ``taskkill /T`` БЕЗ ``/F``;
      2. ожидание до ``timeout`` секунд с опросом раз в ``poll_interval``;
      3. если не завершился — жёстко: ``SIGKILL`` / ``taskkill /T /F``.

    ``timeout=0`` пропускает ожидание и эскалирует сразу (используется в тестах,
    чтобы не ждать реальное время).

    Возвращает True, если процесс на момент возврата считается остановленным
    (уже не был жив, либо остановлен успешно).
    """
    if not is_alive(pid):
        return True

    if sys.platform == "win32":
        return _stop_windows(pid, timeout=timeout, poll_interval=poll_interval)
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


def _taskkill(pid: int, *, force: bool) -> None:
    """``taskkill /PID <pid> /T`` (дерево процессов), с ``/F`` — жёстко."""
    args = ["taskkill", "/PID", str(pid), "/T"]
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


def _stop_windows(pid: int, *, timeout: float, poll_interval: float) -> bool:
    # Мягко: CTRL_BREAK (если консоль общая) + taskkill без /F — тот шлёт
    # WM_CLOSE и даёт процессу отработать штатное завершение.
    _send_ctrl_break(pid)
    _taskkill(pid, force=False)
    if wait_for_exit(pid, timeout, poll_interval):
        return True

    # Не успел — жёстко.
    _taskkill(pid, force=True)
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
