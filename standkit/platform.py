"""
OS-абстракция запуска процессов: скрытый (headless, без консольного окна)
процесс на Windows, отсоединённый (setsid) процесс на Linux.

Никакого хардкода путей вида ``C:\\...`` — все пути принимаются и возвращаются
как ``pathlib.Path``. Модуль не знает ничего про BPMSoft — только про то, как
корректно поднять/остановить/проверить произвольный процесс кроссплатформенно.

Остаточные пункты следующих итераций (Windows Job Object для остановки дерева
процессов, полноценный double-fork на Linux, graceful stop с эскалацией
SIGTERM→таймаут→SIGKILL / CTRL_BREAK_EVENT) — см. docs/ARCHITECTURE.md,
раздел «Что уже реализовано в каркасе vs TODO».
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Optional, Sequence


class ProcessError(Exception):
    """Ошибки запуска/остановки/проверки процесса."""


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
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
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


def stop(pid: int, *, timeout: float = 10.0) -> bool:
    """
    Останавливает процесс по pid.

    Сейчас — безусловное завершение (SIGTERM/taskkill), без эскалации до SIGKILL
    по таймауту (параметр ``timeout`` зарезервирован под будущую graceful-эскалацию;
    бэклог — см. docs/ARCHITECTURE.md).

    Возвращает True, если процесс на момент вызова считался остановленным
    (уже не был жив, либо остановлен успешно).
    """
    if not is_alive(pid):
        return True

    if sys.platform == "win32":
        return _stop_windows(pid)
    return _stop_posix(pid)


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
        try:
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}"], text=True, stderr=subprocess.DEVNULL
            )
            return str(pid) in out
        except Exception:
            return False


def _stop_windows(pid: int) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        raise ProcessError(f"Не удалось остановить процесс {pid}: {exc}") from exc
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


def _stop_posix(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        raise ProcessError(f"Не удалось остановить процесс {pid}: {exc}") from exc
    return not is_alive(pid)
