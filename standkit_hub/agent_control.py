"""
Запуск/остановка ЛОКАЛЬНОГО ``standkit_agent`` как дочернего процесса из хаба
(``standkit_hub/server.py`` — ``POST /api/agent/start``/``POST /api/agent/stop``).

Логика спавна намеренно БЕЗ веб-слоя (чистые функции + один класс-контроллер
поверх ``standkit.platform``), чтобы:
  - её можно было тестировать без реального HTTP-сервера (см.
    tests/test_hub_agent_control.py);
  - агент по-прежнему запускался как самостоятельный процесс через
    ``sys.executable -m standkit_agent`` (не импортом в тот же процесс хаба) —
    падение агента не должно ронять диспетчер, и наоборот.

Секреты (``token_ref``/``readonly_token_ref``) передаются агенту ТОЛЬКО как
ссылки (``--token-ref``/``--readonly-token-ref``) — сам standkit_agent
резолвит их через свой Secret-first контракт (standkit.secrets). Значения
секретов через argv никогда не передаются.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from standkit.platform import ProcessError, is_alive, spawn_hidden, stop
from standkit_hub.config import HubConfig

_PID_FILE_NAME = "standkit-hub-agent.pid"
_LOG_FILE_NAME = "standkit-hub-agent.log"


class AgentControlError(Exception):
    """Ошибка запуска/остановки локального агента (сообщение пригодно для показа пользователю)."""


def build_agent_argv(config: HubConfig, *, python_executable: Optional[str] = None) -> list[str]:
    """
    Собирает argv команды запуска ``standkit_agent`` по полям ``HubConfig``.

    Чистая функция без побочных эффектов — вся логика маппинга полей в флаги
    вынесена сюда специально, чтобы её можно было проверить в тесте без
    реального subprocess.Popen.
    """
    python_executable = python_executable or sys.executable
    argv = [
        python_executable,
        "-m",
        "standkit_agent",
        "--host",
        config.agent_host,
        "--port",
        str(config.agent_port),
    ]

    if config.registry_path:
        argv += ["--registry", config.registry_path]

    # token_ref обязателен для standkit_agent (required=True в его argparse) —
    # но здесь мы не форсируем это молча: validate_agent_config() отдельно
    # проверяет обязательность ДО спавна, чтобы дать понятную ошибку в хабе, а
    # не "агент упал сразу после старта" без объяснений.
    if config.token_ref:
        argv += ["--token-ref", config.token_ref]
    if config.readonly_token_ref:
        argv += ["--readonly-token-ref", config.readonly_token_ref]

    if config.run_dir:
        argv += ["--run-dir", config.run_dir]
    if config.log_dir:
        argv += ["--log-dir", config.log_dir]
    if config.audit_log:
        argv += ["--audit-log", config.audit_log]

    if config.tls_cert:
        argv += ["--tls-cert", config.tls_cert]
    if config.tls_key:
        argv += ["--tls-key", config.tls_key]
    if config.tls_client_ca:
        argv += ["--tls-client-ca", config.tls_client_ca]

    if config.insecure:
        argv.append("--insecure")

    argv += ["--lockout-max-failures", str(config.lockout_max_failures)]
    argv += ["--lockout-window", str(config.lockout_window_sec)]

    return argv


def validate_agent_config(config: HubConfig) -> list[str]:
    """
    Возвращает список понятных проблем конфигурации ДО попытки запуска
    (пустой список — можно запускать). Не бросает исключений — вызывающий
    код (хаб) сам решает, как показать список пользователю.
    """
    problems: list[str] = []
    if not config.token_ref:
        problems.append(
            "Не задана ссылка на control-токен агента (token_ref) — заполните её на "
            "панели «Агент по умолчанию» и задайте сам секрет через редактор секретов."
        )
    if not config.registry_path:
        problems.append("Не задан путь к реестру стендов (registry_path).")
    return problems


@dataclass
class AgentStartResult:
    pid: int
    log_path: str


class AgentController:
    """
    Управляет ОДНИМ локальным процессом ``standkit_agent``, запущенным из хаба.

    pid запущенного процесса персистится в pid-файле (в ``run_dir`` конфига,
    либо ``~/.standkit/run``), чтобы диспетчер мог обнаружить уже запущенный
    им ранее процесс и после собственного перезапуска (не только в течение
    жизни одного объекта Python).
    """

    def __init__(self, config: HubConfig):
        self.config = config
        self._pid: Optional[int] = None

    # --- пути ---

    def _run_dir(self) -> Path:
        # Единый резолв каталога с runtime-файлами (см. HubConfig.resolve_run_dir):
        # туда же кладут своё состояние сам хаб и перезапуск с правами
        # администратора.
        return self.config.resolve_run_dir()

    def _log_dir(self) -> Path:
        return Path(self.config.log_dir) if self.config.log_dir else Path.home() / ".standkit" / "logs"

    def _pid_file(self) -> Path:
        return self._run_dir() / _PID_FILE_NAME

    def _log_file(self) -> Path:
        return self._log_dir() / _LOG_FILE_NAME

    def _load_pid(self) -> Optional[int]:
        if self._pid is not None:
            return self._pid
        pid_file = self._pid_file()
        if not pid_file.exists():
            return None
        try:
            self._pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None
        return self._pid

    # --- состояние ---

    def is_running(self) -> bool:
        """Проверяет, жив ли процесс агента, ранее запущенный этим контроллером (по pid-файлу)."""
        pid = self._load_pid()
        if pid is None:
            return False
        return is_alive(pid)

    # --- управление ---

    def start(self) -> AgentStartResult:
        """
        Запускает локальный агент. Бросает ``AgentControlError`` с понятным
        текстом, если конфигурация невалидна (см. ``validate_agent_config``),
        агент уже запущен, либо ОС отказала в спавне процесса — никогда не
        роняет вызывающий код хаба исключением ОС "как есть".
        """
        problems = validate_agent_config(self.config)
        if problems:
            raise AgentControlError("; ".join(problems))
        if self.is_running():
            raise AgentControlError("Локальный агент уже запущен")

        argv = build_agent_argv(self.config)
        log_path = self._log_file()

        try:
            pid = spawn_hidden(argv, Path.cwd(), log_path)
        except ProcessError as exc:
            raise AgentControlError(f"Не удалось запустить локальный агент: {exc}") from exc

        pid_file = self._pid_file()
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(pid), encoding="utf-8")
        self._pid = pid

        return AgentStartResult(pid=pid, log_path=str(log_path))

    def stop(self, *, timeout: float = 10.0) -> bool:
        """
        Останавливает процесс агента (если он был запущен этим контроллером).
        Возвращает True, если процесс на момент вызова считается остановленным
        (в т.ч. если он и так уже не был жив).
        """
        pid = self._load_pid()
        if pid is None:
            return True

        try:
            stopped = stop(pid, timeout=timeout)
        except ProcessError as exc:
            raise AgentControlError(f"Не удалось остановить локальный агент: {exc}") from exc

        pid_file = self._pid_file()
        if pid_file.exists():
            try:
                pid_file.unlink()
            except OSError:
                pass
        self._pid = None

        return stopped
