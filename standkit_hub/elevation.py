"""
Перезапуск диспетчера «от имени администратора» (Windows/UAC) — кнопка
«Перезапустить с правами администратора» на дашборде.

ЗАЧЕМ. Управление IIS идёт через ``appcmd.exe``, который без elevation не
читает даже собственный ``redirection.config``: любая операция «Старт/Стоп»
над стендом-сайтом IIS падает, а диспетчер честно пишет «не хватает прав
администратора, запустите standkit-hub от имени администратора» (см.
``standkit.hosting.ELEVATION_HINT``). Выполнить этот совет вручную было
неочевидно: повторный запуск ярлыка «от имени администратора» НИЧЕГО не менял
— single-instance проверка видела уже работающий на 8770 экземпляр, новый
процесс просто открывал браузер на СТАРОМ (не elevated) и выходил (см.
``standkit_hub.server.HubAlreadyRunning`` и ``standkit_hub.instance``).

КАК ЭТО РАБОТАЕТ:
  1. хаб пишет в ``run_dir`` файл передачи сессии с ТЕКУЩИМ сессионным
     токеном (``write_handoff``);
  2. ``ShellExecuteW(..., "runas", ...)`` просит Windows поднять новый
     процесс хаба с elevated-токеном — это единственный штатный способ
     показать запрос UAC (повысить права УЖЕ запущенного процесса нельзя);
  3. новый процесс перехватывает порт у старого (флаг ``--takeover``) и
     читает сессионный токен из файла передачи (``--session-token-file``),
     поэтому уже открытая вкладка остаётся авторизованной: её HttpOnly-cookie
     совпадает с токеном нового процесса, и достаточно перезагрузить страницу.

БЕЗОПАСНОСТЬ. Файл передачи содержит сессионный токен, поэтому: создаётся
режимом ``0o600`` в каталоге пользователя, ЖИВЁТ СЕКУНДЫ (``HANDOFF_TTL_SEC``)
и удаляется при первом же чтении — даже если чтение неуспешно (протух, битый
JSON). Токен НИКОГДА не передаётся аргументами командной строки: argv виден
любому процессу в системе, а путь к файлу — нет (тот же контракт «секреты
только ссылкой», что у ``standkit_hub.agent_control``).

STDLIB-ONLY: ``ctypes`` (ShellExecuteW), ``json``, ``os``, ``sys``, ``time``.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from standkit.platform import is_elevated
from standkit_hub.shortcut import windows_pythonw_executable

# Имя файла передачи сессии в ``run_dir`` (см. HubConfig.resolve_run_dir).
HANDOFF_FILE_NAME = "standkit-hub-handoff.json"

# Сколько секунд файл передачи считается годным. Окно должно покрывать
# показ запроса UAC и старт второго процесса, но не превращать файл в
# долгоживущий носитель токена.
HANDOFF_TTL_SEC = 180.0

# ShellExecuteW: значение > 32 — успех, всё остальное — код ошибки.
_SHELL_EXECUTE_SUCCESS_THRESHOLD = 32
# Пользователь нажал «Нет» в окне UAC — это не сбой, а осознанный отказ.
ERROR_CANCELLED = 1223
_SW_SHOWNORMAL = 1


class ElevationError(Exception):
    """Не удалось перезапустить диспетчер с правами администратора (текст пригоден для показа)."""


class ElevationCancelled(ElevationError):
    """Запрос UAC отклонён пользователем — старый процесс продолжает работать как ни в чём не бывало."""


def elevation_supported() -> bool:
    """Поддерживается ли перезапуск с повышением прав на этой ОС (пока только Windows)."""
    return sys.platform == "win32"


def can_restart_elevated() -> tuple[bool, str]:
    """
    Можно ли предлагать перезапуск прямо сейчас: ``(да/нет, причина отказа)``.

    Причина — готовый текст для интерфейса, а не код ошибки: кнопку на
    дашборде рисует фронт, и придумывать формулировку там было бы вторым
    источником правды.
    """
    if not elevation_supported():
        return False, "Перезапуск с правами администратора доступен только на Windows."
    if is_elevated():
        return False, "Диспетчер уже работает с правами администратора."
    return True, ""


# --------------------------------------------------------------------------
# Передача сессии новому процессу
# --------------------------------------------------------------------------


def handoff_path(run_dir: Path) -> Path:
    """Путь к файлу передачи сессии внутри ``run_dir``."""
    return Path(run_dir) / HANDOFF_FILE_NAME


def write_handoff(path: Path, session_token: str, *, now: Optional[float] = None) -> Path:
    """
    Пишет файл передачи сессии (перезаписывая прошлый, если он остался от
    отменённой попытки) с правами ``0o600``.

    Права выставляются В МОМЕНТ создания (``os.open`` с ``mode=0o600``), а не
    после записи: иначе между созданием и ``chmod`` существует окно, в котором
    файл с токеном доступен всем.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"session_token": session_token, "created_at": float(now if now is not None else time.time())},
        ensure_ascii=False,
    )
    # O_TRUNC, а не O_EXCL: остаток от прошлой (отменённой в UAC) попытки —
    # штатная ситуация, ронять из-за него перезапуск незачем.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return path


def discard_handoff(path: Path) -> None:
    """Удаляет файл передачи сессии, если он есть (никогда не бросает)."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def read_handoff(
    path: Path, *, ttl: float = HANDOFF_TTL_SEC, now: Optional[float] = None
) -> Optional[str]:
    """
    Читает сессионный токен из файла передачи и УДАЛЯЕТ файл — при любом
    исходе, включая протухший/битый (одноразовость важнее диагностики).

    ``None`` — файла нет, он старше ``ttl`` секунд, или содержимое не разбирается.
    Вызывающий (``standkit_hub.__main__``) в этом случае просто генерирует
    новый сессионный токен: перезапуск состоится, но вкладку придётся открыть
    заново по ярлыку.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    finally:
        discard_handoff(path)

    try:
        data = json.loads(raw)
        token = data["session_token"]
        created_at = float(data["created_at"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

    if not isinstance(token, str) or not token:
        return None
    moment = now if now is not None else time.time()
    # Отрицательная разница (часы сдвинули назад) — тоже «не доверяем».
    if not (0.0 <= moment - created_at <= ttl):
        return None
    return token


# --------------------------------------------------------------------------
# Собственно перезапуск
# --------------------------------------------------------------------------


def build_relaunch_params(
    *,
    port: int,
    handoff: Optional[Path] = None,
    config_path: Optional[Path] = None,
) -> list[str]:
    """
    Аргументы командной строки нового (elevated) процесса хаба.

    Чистая функция без побочных эффектов — ровно тот же приём, что и в
    ``standkit_hub.agent_control.build_agent_argv``: маппинг «состояние → флаги»
    должен проверяться тестом без реального запуска процесса.

    ``--takeover`` обязателен: порт ещё занят старым процессом, и без него
    новый экземпляр упёрся бы в single-instance проверку и молча вышел —
    ровно тот сценарий, из-за которого ручной «запуск ярлыка от имени
    администратора» не давал никакого эффекта.

    ``--no-browser`` тоже обязателен, и по двум причинам: вкладка у
    пользователя уже открыта (сессия переезжает через файл передачи), а
    браузер, запущенный ИЗ elevated-процесса, сам оказался бы elevated —
    этого не хочет никто.
    """
    params = ["-m", "standkit_hub", "--port", str(port), "--takeover", "--no-browser"]
    if config_path is not None:
        params += ["--config", str(config_path)]
    if handoff is not None:
        params += ["--session-token-file", str(handoff)]
    return params


def quote_params(params: Sequence[str]) -> str:
    """
    Склеивает аргументы в строку для ``ShellExecuteW`` (он принимает ОДНУ
    строку, а не argv): аргументы с пробелами — в кавычках.

    Пути с пробелами тут норма (``C:\\Program Files\\...``,
    ``C:\\Users\\Имя Фамилия\\AppData\\...``), поэтому это не украшательство.
    """
    out = []
    for p in params:
        text = str(p)
        out.append(f'"{text}"' if (" " in text or "\t" in text) else text)
    return " ".join(out)


def _default_shell_execute(executable: str, params: str, cwd: str) -> int:
    import ctypes

    return int(
        ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", executable, params, cwd, _SW_SHOWNORMAL
        )
    )


def relaunch_elevated(
    params: Sequence[str],
    *,
    executable: Optional[str] = None,
    cwd: Optional[Path] = None,
    shell_execute: Optional[Callable[[str, str, str], int]] = None,
) -> None:
    """
    Просит Windows запустить новый процесс хаба с правами администратора
    (глагол ``runas`` — именно он показывает запрос UAC).

    Возврат означает лишь «запрос UAC подтверждён и процесс создан»: дождётся
    ли новый экземпляр освобождения порта — забота его собственной
    takeover-логики. Отказ пользователя в UAC — ``ElevationCancelled``, любая
    другая беда — ``ElevationError`` с человекочитаемым текстом. Наружу не
    выходит ни одно исключение ОС «как есть».

    ``shell_execute`` подменяется в тестах: настоящий вызов показал бы окно UAC.
    """
    if not elevation_supported():
        raise ElevationError("Перезапуск с правами администратора доступен только на Windows.")

    executable = executable or windows_pythonw_executable()
    call = shell_execute or _default_shell_execute
    try:
        rc = call(executable, quote_params(params), str(cwd or Path.home()))
    except Exception as exc:  # ctypes/WinAPI — что угодно, наружу отдаём понятный текст
        raise ElevationError(f"Не удалось запросить повышение прав: {exc}") from exc

    if rc == ERROR_CANCELLED:
        raise ElevationCancelled(
            "Запрос прав администратора отклонён — диспетчер продолжает работать без них."
        )
    if rc <= _SHELL_EXECUTE_SUCCESS_THRESHOLD:
        raise ElevationError(
            f"Windows отказалась запустить диспетчер с правами администратора (код {rc})."
        )
