"""
GAP-138: внешние консольные утилиты не должны мигать окном.

Фон. Родитель без СВОЕЙ консоли (``pythonw.exe``, служба Windows, фоновый
поллер хаба) заставляет Windows выдать новую консоль каждому консольному
ребёнку — пользователь видит всплывающее и мгновенно исчезающее чёрное окно.
18.08.2026 у владельца так мигало по два окна раз в ~12 с: поллер хаба
опрашивал IIS-стенд парой ``appcmd list site|apppool``.

Лечение — ЕДИНАЯ точка запуска ``standkit.platform.run_console``, которая на
win32 добавляет ``creationflags=CREATE_NO_WINDOW``. Здесь проверяется и сам
хелпер, и то, что через него ходят ВСЕ реальные места запуска внешних утилит,
и — статической проверкой исходников — что новый прямой ``subprocess.run`` в
пакет больше не просочится.

Тесты кроссплатформенные: реальные процессы не запускаются, ``subprocess.run``
подменяется, ``sys.platform`` — тоже (флаг проверяется по ФАКТУ передачи, а не
по тому, на какой ОС гоняется набор).
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

import pytest

from standkit import health as health_module
from standkit import hosting
from standkit import platform as platform_module
from standkit.models import HostKind, Stand
from standkit_hub import shortcut as shortcut_module

NO_WINDOW = 0x08000000


class _FakeCompleted:
    """Достаточная замена CompletedProcess для всех вызывающих."""

    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _capturing_run(captured: list):
    def _run(cmd, **kwargs):
        captured.append((cmd, kwargs))
        return _FakeCompleted()

    return _run


def _flags(kwargs: dict) -> int:
    return int(kwargs.get("creationflags") or 0)


# --------------------------------------------------------------------------
# Сам хелпер
# --------------------------------------------------------------------------


def test_run_console_adds_no_window_on_windows(monkeypatch):
    captured: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", _capturing_run(captured))

    platform_module.run_console(["appcmd", "list", "site"], capture_output=True)

    assert len(captured) == 1
    cmd, kwargs = captured[0]
    assert cmd == ["appcmd", "list", "site"]
    assert _flags(kwargs) & NO_WINDOW == NO_WINDOW
    # Остальные kwargs вызывающего доходят без изменений.
    assert kwargs["capture_output"] is True


def test_run_console_does_not_add_flag_outside_windows(monkeypatch):
    captured: list = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", _capturing_run(captured))

    platform_module.run_console(["docker", "ps"])

    _cmd, kwargs = captured[0]
    # На Linux флага нет вовсе — не 0, а именно отсутствие ключа: иначе
    # subprocess.run на POSIX получил бы неподдерживаемый аргумент.
    assert "creationflags" not in kwargs


def test_run_console_preserves_caller_creationflags(monkeypatch):
    captured: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", _capturing_run(captured))

    other_flag = 0x00000200  # CREATE_NEW_PROCESS_GROUP
    platform_module.run_console(["sc", "query", "WAS"], creationflags=other_flag)

    _cmd, kwargs = captured[0]
    assert _flags(kwargs) & NO_WINDOW == NO_WINDOW
    assert _flags(kwargs) & other_flag == other_flag


def test_run_console_returns_result_of_subprocess_run(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    sentinel = _FakeCompleted(stdout=b"OK")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: sentinel)

    assert platform_module.run_console(["whoami"]) is sentinel


# --------------------------------------------------------------------------
# Реальные места запуска внешних утилит
# --------------------------------------------------------------------------


def test_hosting_run_hides_console(monkeypatch):
    """``hosting._run`` — путь appcmd/docker/kubectl, то самое место GAP-138."""
    captured: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", _capturing_run(captured))

    hosting._run(["appcmd", "list", "site", "iis19", "/text:state"])

    _cmd, kwargs = captured[0]
    assert _flags(kwargs) & NO_WINDOW == NO_WINDOW


def test_hosting_service_state_hides_console(monkeypatch):
    """``sc query WAS`` — второй канал диагноза «службы IIS остановлены»."""
    captured: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", _capturing_run(captured))

    hosting._service_state("WAS")

    cmd, kwargs = captured[0]
    assert cmd == ["sc", "query", "WAS"]
    assert _flags(kwargs) & NO_WINDOW == NO_WINDOW


def test_iis_poll_pair_hides_console(monkeypatch):
    """
    Живой сценарий гэпа целиком: опрос IIS-стенда поллером хаба (пара команд
    ``appcmd list site|apppool``) — окон не создаёт НИ ОДНА из них.
    """
    captured: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(hosting, "_resolve_appcmd", lambda: "C:/Windows/system32/inetsrv/appcmd.exe")
    # Сети в тесте быть не должно: пустой ответ appcmd уводит вердикт в
    # TCP-фолбэк, а тот полез бы на реальный порт хоста.
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: False)
    monkeypatch.setattr(subprocess, "run", _capturing_run(captured))

    stand = Stand(
        name="iis19",
        stand_dir="C:/inetpub/iis19",
        stand_host="127.0.0.1",
        stand_port=5000,
        host_kind=HostKind.IIS,
        iis_site="iis19",
        iis_app_pool="iis19",
    )
    hosting.IisBackend().is_running(stand)

    assert captured, "ожидался хотя бы один вызов appcmd"
    for _cmd, kwargs in captured:
        assert _flags(kwargs) & NO_WINDOW == NO_WINDOW


def test_taskkill_hides_console(monkeypatch):
    captured: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "run", _capturing_run(captured))

    platform_module._taskkill(4242, force=True)

    cmd, kwargs = captured[0]
    assert cmd[0] == "taskkill"
    assert _flags(kwargs) & NO_WINDOW == NO_WINDOW


def test_tasklist_fallback_hides_console(monkeypatch):
    """
    Фолбэк ``is_alive`` на ``tasklist`` (когда ctypes-путь недоступен) — тоже
    внешняя консольная утилита. ``windll`` убирается явно: вне Windows его нет
    и так, а НА Windows основная ветка отработала бы и до фолбэка не дошла —
    тест обязан проверять одно и то же на обеих ОС.
    """
    captured: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delattr(ctypes, "windll", raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: captured.append((cmd, kw)) or _FakeCompleted(stdout="4242 standkit"),
    )

    assert platform_module._is_alive_windows(4242) is True

    cmd, kwargs = captured[0]
    assert cmd[0] == "tasklist"
    assert _flags(kwargs) & NO_WINDOW == NO_WINDOW


def test_shortcut_powershell_hides_console(tmp_path, monkeypatch):
    """Создание ярлыка зовёт PowerShell — из окна «Настройки» хаба под pythonw."""
    captured: list = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kw: captured.append((cmd, kw)) or _FakeCompleted(stdout="", stderr=""),
    )

    result = shortcut_module.install_desktop_shortcut()

    assert result.ok is True
    cmd, kwargs = captured[0]
    assert cmd[0] == "powershell"
    assert _flags(kwargs) & NO_WINDOW == NO_WINDOW


# --------------------------------------------------------------------------
# Страховка от регресса: прямой subprocess.* мимо хелпера
# --------------------------------------------------------------------------

# Единственный модуль, которому МОЖНО звать subprocess напрямую: он и есть
# OS-абстракция (spawn_hidden ставит флаг сам, run_console — тот самый хелпер).
_PLATFORM_MODULE = "standkit/platform.py"

# Документированное исключение: ветки macOS/Linux открытия файлового
# менеджера. Консоли на этих ОС не существует как явления, а Windows-ветка
# того же места идёт через ShellExecuteW/os.startfile и внешнюю утилиту не
# запускает вовсе.
_POPEN_EXCEPTIONS = {"standkit_hub/logs_browser.py"}

_FORBIDDEN = ("subprocess.run(", "subprocess.check_output(", "subprocess.call(", "subprocess.check_call(")

# Пакеты под статическим гардом. Список ЯВНЫЙ (а не «все каталоги репозитория»),
# потому что появление нового пакета обязано быть осознанным шагом: пакет,
# забытый здесь, молча выпадает из-под проверки и возвращает GAP-138.
# ``standkit_companion`` — канал обновлений издателя: он тикает из процесса без
# своей консоли (фоновый поток хаба) и запускает CLI BPMkit за лицензионным
# конвертом, то есть попадает ровно в тот класс риска, ради которого гард и есть.
_GUARDED_PACKAGES = ("standkit", "standkit_agent", "standkit_hub", "standkit_companion")


def _package_root() -> Path:
    return Path(platform_module.__file__).resolve().parent.parent


def _sources():
    root = _package_root()
    for package in _GUARDED_PACKAGES:
        package_dir = root / package
        if not package_dir.is_dir():
            # Пакет платной редакции может отсутствовать в свободной поставке —
            # это не повод ронять набор тестов ядра.
            continue
        for path in sorted(package_dir.rglob("*.py")):
            yield path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")


def test_no_direct_subprocess_run_outside_platform_module():
    """
    Прямой ``subprocess.run``/``check_output`` в пакете — это и есть GAP-138:
    один такой вызов из процесса без консоли снова даст мигающее окно.
    Новый вызов внешней утилиты добавлять ТОЛЬКО через ``run_console``.
    """
    offenders = []
    for rel, text in _sources():
        if rel == _PLATFORM_MODULE:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if any(marker in code for marker in _FORBIDDEN):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, (
        "внешние утилиты запускаются мимо standkit.platform.run_console "
        "(вернётся мигающее консольное окно, GAP-138):\n" + "\n".join(offenders)
    )


def test_no_unexpected_popen_outside_platform_module():
    """``subprocess.Popen`` — тот же класс риска; список исключений явный."""
    offenders = []
    for rel, text in _sources():
        if rel == _PLATFORM_MODULE or rel in _POPEN_EXCEPTIONS:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]
            if "subprocess.Popen(" in code:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, "неучтённый subprocess.Popen:\n" + "\n".join(offenders)


@pytest.mark.parametrize("rel", sorted(_POPEN_EXCEPTIONS))
def test_popen_exceptions_are_still_relevant(rel):
    """
    Исключение живёт, пока в файле реально есть ``subprocess.Popen``. Иначе
    список исключений тихо протухнет и однажды прикроет НАСТОЯЩИЙ регресс.
    """
    text = (_package_root() / rel).read_text(encoding="utf-8")
    assert "subprocess.Popen(" in text, f"исключение {rel} больше не нужно — убрать из _POPEN_EXCEPTIONS"
