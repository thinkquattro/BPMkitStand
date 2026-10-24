"""
Автозапуск удалённого режима BPMkit при входе пользователя в Windows: задача Планировщика
(``schtasks``) «при входе» от ТЕКУЩЕЙ учётной записи, без прав администратора.

Зачем. ``remote_autostart`` поднимает ``BPMkit.exe serve-http`` вместе с диспетчером, но после
перезагрузки хоста диспетчер сам не стартует. Задача при входе запускает ``serve-http`` напрямую,
пока диспетчер не открыт. Это не служба Windows: служба потребовала бы администратора.

Что делает модуль.

* Задача создаётся импортом XML (``schtasks /Create /XML``): триггер ``LogonTrigger`` с
  ``UserId`` текущего пользователя и ``RunLevel=LeastPrivilege``, ``LogonType=InteractiveToken``.
  Так обычный пользователь регистрирует задачу без повышения; ключ ``/SC ONLOGON /RL LIMITED``
  без ``/RU`` на части систем требует администратора.
* В командной строке задачи -- те же аргументы, что строит ``remote_mode.build_serve_argv``
  (хост, порт, профиль, ``--token-ref``). Значение токена в задачу не попадает: ``serve-http``
  читает его из хранилища секретов BPMkit сам.
* Задача не заменяет кнопку «Запустить»: если ``serve-http`` уже работает (поднят диспетчером),
  повторный экземпляр отказывается на занятом порту и тихо завершается.
* Вне Windows возможности нет: ``supported=False``.
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Optional
from xml.sax.saxutils import escape

from standkit.platform import run_console_tree
from standkit_hub.config import HubConfig
from standkit_hub.remote_mode import (
    RemoteModeController,
    build_serve_argv,
    validate_remote_config,
)

TASK_NAME = "BPMkit Remote MCP (serve-http)"
_SCHTASKS_TIMEOUT_S = 20.0
#: Код «задача ещё ни разу не запускалась» (SCHED_S_TASK_HAS_NOT_RUN, 0x41303).
_NOT_RUN_RESULT = "267011"


class LogonTaskError(Exception):
    """Отказ создания/удаления задачи (текст пригоден для показа пользователю)."""


def default_runner(argv: list, timeout: float = _SCHTASKS_TIMEOUT_S) -> tuple:
    """Запуск ``schtasks``: ``(rc, stdout, stderr)``; сбой запуска -- ``rc=-1``. Без окна."""
    try:
        # schtasks печатает в OEM-кодировке консоли (cp866 и т.п.), не в UTF-8.
        proc = run_console_tree(list(argv), capture_output=True, text=True,
                                encoding="oem" if sys.platform == "win32" else "utf-8",
                                errors="replace", timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return -1, "", f"{type(exc).__name__}: {exc}"
    rc = proc.returncode if proc.returncode is not None else -1
    return int(rc), proc.stdout or "", proc.stderr or ""


def current_user_id() -> str:
    """``ДОМЕН\\пользователь`` текущей учётной записи (для триггера задачи)."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    domain = os.environ.get("USERDOMAIN") or ""
    return f"{domain}\\{user}" if domain and user else user


def split_command(argv: list) -> tuple:
    """``(исполняемый файл, строка аргументов)`` для действия задачи."""
    return str(argv[0]), subprocess.list2cmdline([str(a) for a in argv[1:]])


def build_task_xml(command: str, arguments: str, user_id: str, working_dir: str = "") -> str:
    """XML задачи (Task Scheduler 1.2): при входе ``user_id``, обычные права, без лимита времени."""
    wd = f"      <WorkingDirectory>{escape(working_dir)}</WorkingDirectory>\n" if working_dir else ""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>BPMkit: удалённый режим MCP (serve-http) при входе пользователя. "
        "Создано диспетчером BPMkitStand.</Description>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        f"      <UserId>{escape(user_id)}</UserId>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{escape(user_id)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <Hidden>true</Hidden>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(command)}</Command>\n"
        f"      <Arguments>{escape(arguments)}</Arguments>\n"
        f"{wd}"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _xml_field(xml: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", xml, re.S)
    if not m:
        return ""
    return (m.group(1).replace("&lt;", "<").replace("&gt;", ">")
            .replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&"))


def parse_query_csv(text: str) -> dict:
    """Разбор ``schtasks /Query /FO CSV /V /NH``: позиции столбцов не зависят от языка ОС
    (6-й -- время прошлого запуска, 7-й -- результат прошлого запуска)."""
    for row in csv.reader(io.StringIO(text or "")):
        if len(row) >= 7:
            last_run = row[5].strip()
            last_result = row[6].strip()
            never = last_result == _NOT_RUN_RESULT or last_run.upper() in ("N/A", "")
            return {
                "last_run": "" if never else last_run,   # «30.11.1999» -- заглушка Планировщика
                "last_result": last_result,
                "never_run": never,
            }
    return {}


class LogonTaskController:
    """Создаёт/удаляет/показывает задачу «при входе» для ``serve-http``."""

    def __init__(self, config: HubConfig, *,
                 remote: Optional[RemoteModeController] = None,
                 runner: Optional[Callable] = None,
                 platform: Optional[str] = None,
                 user_id: Optional[Callable[[], str]] = None,
                 xml_dir: Optional[Path] = None):
        self.config = config
        self.remote = remote or RemoteModeController(config)
        self._run = runner or default_runner
        self._platform = platform or sys.platform
        self._user_id = user_id or current_user_id
        self._xml_dir = xml_dir

    @property
    def supported(self) -> bool:
        return self._platform == "win32"

    def _expected(self) -> Optional[tuple]:
        cli = self.remote._cli_or_none()
        if not cli:
            return None
        return split_command(build_serve_argv(self.config, cli))

    def status(self) -> dict:
        """Снимок для UI: есть ли задача, актуальна ли её команда, последний запуск."""
        payload: dict = {"supported": self.supported, "task_name": TASK_NAME, "present": False}
        if not self.supported:
            payload["note"] = "Задача планировщика доступна только в Windows."
            return payload
        rc, out, _err = self._run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "CSV", "/V", "/NH"])
        if rc != 0:
            return payload
        payload["present"] = True
        payload.update(parse_query_csv(out))
        rc, xml, _err = self._run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"])
        if rc == 0:
            command, arguments = _xml_field(xml, "Command"), _xml_field(xml, "Arguments")
            payload["command"] = f"{command} {arguments}".strip()
            expected = self._expected()
            if expected is not None:
                payload["up_to_date"] = (command, arguments) == expected
        return payload

    def enable(self) -> dict:
        """Создать (или пересоздать с текущими настройками) задачу. ``LogonTaskError`` -- отказ."""
        if not self.supported:
            raise LogonTaskError("Задача планировщика доступна только в Windows.")
        problems = validate_remote_config(self.config)
        if problems:
            raise LogonTaskError(" ".join(problems))
        expected = self._expected()
        if expected is None:
            raise LogonTaskError("Не найден BPMkit.exe: укажите путь в Настройки → Основные → "
                                 "«CLI BPMkit» или установите BPMkit.")
        if not self.remote.token_present(fresh=True):
            raise LogonTaskError(
                f"Токен «{self.config.remote_token_ref}» не найден в хранилище секретов BPMkit -- "
                "задача при входе не поднимет сервер. Сначала нажмите «Сгенерировать токен».")
        user = self._user_id()
        if not user:
            raise LogonTaskError("Не удалось определить текущего пользователя Windows.")
        xml = build_task_xml(expected[0], expected[1], user)
        base = self._xml_dir or Path(tempfile.gettempdir())
        base.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="bpmkit-logon-", suffix=".xml", dir=str(base))
        os.close(fd)
        path = Path(name)
        try:
            path.write_bytes(b"\xff\xfe" + xml.encode("utf-16-le"))
            rc, out, err = self._run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(path), "/F"])
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if rc != 0:
            detail = (err or out or "").strip().splitlines()
            raise LogonTaskError("Не удалось создать задачу планировщика: "
                                 + (detail[-1][:300] if detail else f"rc={rc}"))
        return self.status()

    def disable(self) -> dict:
        """Удалить задачу. Её отсутствие -- не ошибка."""
        if not self.supported:
            raise LogonTaskError("Задача планировщика доступна только в Windows.")
        if not self.status().get("present"):
            return self.status()
        rc, out, err = self._run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        if rc != 0:
            detail = (err or out or "").strip().splitlines()
            raise LogonTaskError("Не удалось удалить задачу планировщика: "
                                 + (detail[-1][:300] if detail else f"rc={rc}"))
        return self.status()


__all__ = ["LogonTaskController", "LogonTaskError", "TASK_NAME", "build_task_xml",
           "parse_query_csv"]
