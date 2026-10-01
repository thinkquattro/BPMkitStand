"""
Удалённый режим BPMkit в диспетчере: запуск и остановка ``BPMkit.exe serve-http``
(``POST /api/remote/start``, ``POST /api/remote/stop``, ``GET /api/remote/status``).

``serve-http`` -- транспорт Streamable HTTP с Bearer-токеном, который сервер BPMkit
поднимает для потребителей, подключающих MCP по адресу (платформа ИИ-агентов BPMSoft 2.0,
облачные агентные платформы, IDE с MCP по URL). Раньше его запускали командой в консоли;
здесь -- кнопка в диспетчере.

Что делает модуль и чего НЕ делает.

* Процесс -- ДОЧЕРНИЙ процесс пользователя (как локальный агент, см.
  ``standkit_hub.agent_control``): ``standkit.platform.spawn_hidden`` (CREATE_NO_WINDOW,
  без консоли), pid в pid-файле ``run_dir`` -- процесс находится и после перезапуска
  диспетчера. Службу Windows модуль не регистрирует (нужны права администратора, ``BPMkit.exe``
  не является службой) -- «поднимать само после перезагрузки хоста» закрывает флаг
  ``remote_autostart`` (старт вместе с диспетчером), а настоящая служба -- отдельный остаток.
* Токен -- ТОЛЬКО ссылка (``remote_token_ref`` -> ``--token-ref``). Значение секрета
  не читается, не печатается, в argv и в ответы API не попадает; модуль спрашивает
  лишь «есть ли секрет» (``standkit.secrets.has_secret``). Токен в командной строке
  ``serve-http`` сам отвергает.
* Живость -- по ``GET /healthz`` (отвечает без авторизации и без секретов): процесс жив,
  но порт не отвечает -- это «запускается» либо «не отвечает», а не «работает».
* Резолв ``BPMkit.exe`` -- ТОТ ЖЕ, что у экрана лицензии и канала обновлений
  (``license_api.find_cli``: ``companion.mcp_cli`` -> ``BPMKIT_CLI`` -> автодетект рядом с
  поставкой -> запуск из исходников): у хаба один путь к MCP, второго поля не заводим.
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from standkit.platform import ProcessError, is_alive, spawn_hidden, stop
from standkit.secrets import has_secret as _default_has_secret
from standkit_hub import license_api
from standkit_hub.config import (
    DEFAULT_REMOTE_PATH,
    DEFAULT_REMOTE_TOKEN_REF,
    HubConfig,
)

_PID_FILE_NAME = "standkit-hub-remote.json"
_LOG_FILE_NAME = "standkit-hub-remote.log"

#: Ответ ``/healthz`` ждём недолго: проба идёт из потока запроса хаба, а опрос статуса
#: частый. Живой локальный сервер отвечает за миллисекунды.
_HEALTH_TIMEOUT_S = 1.5

#: Сколько start() ждёт, пока процесс либо ответит на /healthz, либо умрёт (например,
#: сразу отказал: нет токена, порт занят). Дольше -- UI не должен висеть на кнопке.
_STARTUP_WAIT_S = 6.0
_STARTUP_POLL_S = 0.25

#: Подмножество допустимых значений ``--profile`` для проверки ДО запуска: формат, а не список
#: (список профилей живёт в ``toolsets.PROFILES`` сервера BPMkit, хаб его не импортирует).
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.:\[\]_-]{1,255}$")

_WILDCARD_HOSTS = ("0.0.0.0", "::", "[::]", "")


class RemoteModeError(Exception):
    """Отказ запуска/остановки удалённого режима (текст пригоден для показа пользователю)."""


@dataclass
class RemoteStartResult:
    pid: int
    log_path: str
    url: str
    healthy: bool


def validate_remote_config(config: HubConfig) -> list[str]:
    """Список понятных проблем настроек ДО спавна (пустой -- можно запускать).

    Не бросает: хаб сам решает, как показать список."""
    problems: list[str] = []
    host = str(config.remote_host or "").strip()
    if not host or not _HOST_RE.match(host):
        problems.append("Адрес удалённого режима (remote_host) не задан или содержит недопустимые символы.")
    port = config.remote_port
    if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
        problems.append("Порт удалённого режима (remote_port) должен быть целым числом от 1 до 65535.")
    profile = str(config.remote_profile or "").strip()
    if not _PROFILE_RE.match(profile):
        problems.append("Профиль (remote_profile) -- латиница, цифры, «_» и «-», например default, cursor, all, auto.")
    ref = str(config.remote_token_ref or "").strip()
    if not ref or not _REF_RE.match(ref):
        problems.append("Не задана ссылка на токен (remote_token_ref).")
    return problems


def build_serve_argv(config: HubConfig, cli_argv: list[str]) -> list[str]:
    """argv запуска: ``<CLI BPMkit> serve-http --host .. --port .. --profile .. --token-ref ..``.

    Чистая функция. Секретов в argv нет -- только ref (сам ``serve-http`` отказывается
    от токена в командной строке)."""
    argv = list(cli_argv) + [
        "serve-http",
        "--host", str(config.remote_host).strip(),
        "--port", str(int(config.remote_port)),
        "--profile", str(config.remote_profile).strip(),
        "--token-ref", str(config.remote_token_ref).strip(),
    ]
    if config.remote_allow_query_token:
        argv.append("--allow-query-token")
    return argv


def _probe_host(host: str) -> str:
    """Адрес, по которому диспетчер сам стучится в /healthz: wildcard -> loopback."""
    host = str(host or "").strip()
    if host in _WILDCARD_HOSTS:
        return "127.0.0.1"
    return host


def _url_host(host: str) -> str:
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def connection_url(config: HubConfig) -> str:
    """Адрес подключения ``http://<хост>:<порт>/mcp``. Для «все интерфейсы» подставляется
    имя этой машины: wildcard-адрес клиенту ничего не говорит."""
    host = str(config.remote_host or "").strip()
    if host in _WILDCARD_HOSTS:
        try:
            host = socket.gethostname() or "<имя-сервера>"
        except Exception:  # noqa: BLE001
            host = "<имя-сервера>"
    return f"http://{_url_host(host)}:{int(config.remote_port)}{DEFAULT_REMOTE_PATH}"


def default_health_probe(host: str, port: int, *, timeout: float = _HEALTH_TIMEOUT_S) -> Optional[dict]:
    """``GET /healthz`` -> разобранный JSON либо ``None`` (не отвечает / не JSON / не 200).

    Прокси окружения обходим: пробуем локальный/корпоративный адрес напрямую."""
    url = f"http://{_url_host(_probe_host(host))}:{int(port)}/healthz"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as resp:  # noqa: S310 - http, адрес из настроек
            if resp.status != 200:
                return None
            data = json.loads(resp.read(65536).decode("utf-8", errors="replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("ok") else None


class RemoteModeController:
    """Управляет ОДНИМ процессом ``BPMkit.exe serve-http``, запущенным из диспетчера.

    Состояние между запусками диспетчера -- JSON-файл в ``run_dir`` (pid, host, port,
    время старта): процесс, поднятый прошлым экземпляром диспетчера, виден и управляем.
    Живость pid сверяется ещё и по /healthz на сохранённом адресе: pid мог достаться
    постороннему процессу."""

    def __init__(self, config: HubConfig, *,
                 cli_resolver: Optional[Callable[[], Optional[list]]] = None,
                 spawn: Callable = spawn_hidden,
                 alive: Callable[[int], bool] = is_alive,
                 stopper: Callable = stop,
                 probe: Callable[[str, int], Optional[dict]] = default_health_probe,
                 has_secret: Callable[[str], bool] = _default_has_secret,
                 sleep: Callable[[float], None] = time.sleep,
                 startup_wait: float = _STARTUP_WAIT_S):
        self.config = config
        self._cli_resolver = cli_resolver or (lambda: license_api.find_cli(config.companion))
        self._spawn = spawn
        self._alive = alive
        self._stopper = stopper
        self._probe = probe
        self._has_secret = has_secret
        self._sleep = sleep
        self._startup_wait = startup_wait

    # --- пути ---

    def _state_file(self) -> Path:
        return self.config.resolve_run_dir() / _PID_FILE_NAME

    def _log_file(self) -> Path:
        base = Path(self.config.log_dir) if self.config.log_dir else Path.home() / ".standkit" / "logs"
        return base / _LOG_FILE_NAME

    def _read_state(self) -> Optional[dict]:
        try:
            data = json.loads(self._state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("pid"), int):
            return None
        return data

    def _write_state(self, pid: int) -> None:
        path = self._state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": pid,
            "host": str(self.config.remote_host).strip(),
            "port": int(self.config.remote_port),
            "started": time.time(),
        }
        path.write_text(json.dumps(payload), encoding="utf-8")

    def _clear_state(self) -> None:
        try:
            self._state_file().unlink()
        except OSError:
            pass

    def _log_tail(self, lines: int = 6) -> str:
        """Хвост журнала процесса (stderr serve-http: причина отказа). Сервер не пишет в журнал
        токен -- параметр ``token`` в адресе вырезается до access-лога, ref не секрет."""
        try:
            raw = self._log_file().read_bytes()[-4000:].decode("utf-8", errors="replace")
        except OSError:
            return ""
        tail = [ln.strip() for ln in raw.splitlines() if ln.strip()][-lines:]
        return " | ".join(tail)[:600]

    # --- состояние ---

    def _live_pid(self) -> Optional[int]:
        state = self._read_state()
        if state is None:
            return None
        pid = state["pid"]
        if self._alive(pid):
            return pid
        self._clear_state()
        return None

    def status(self) -> dict:
        """Снимок состояния для UI. Секретов нет: токен -- только факт наличия.

        ``state``: ``stopped`` | ``starting`` | ``running`` | ``unresponsive``
        (процесс жив, порт не отвечает ``/healthz``)."""
        cfg = self.config
        pid = self._live_pid()
        state_row = self._read_state() if pid is not None else None
        # Адрес для пробы -- тот, с которым процесс ЗАПУЩЕН (настройки могли поменяться после старта).
        probe_host = (state_row or {}).get("host") or cfg.remote_host
        probe_port = (state_row or {}).get("port") or cfg.remote_port
        health = self._probe(probe_host, int(probe_port)) if pid is not None else None
        if pid is None:
            state = "stopped"
        elif health is not None:
            state = "running"
        else:
            started = float((state_row or {}).get("started") or 0)
            state = "starting" if started and time.time() - started < 15 else "unresponsive"
        ref = str(cfg.remote_token_ref or "").strip()
        token_present = bool(ref) and bool(self._has_secret(ref))
        cli = None
        try:
            cli = self._cli_resolver()
        except Exception:  # noqa: BLE001 - статус не падает из-за резолва
            cli = None
        cli_prefix = " ".join(f'"{p}"' if " " in p else p for p in cli) if cli else "BPMkit.exe"
        token_cmd = f"{cli_prefix} setup remote-token --generate --show"
        if ref and ref != DEFAULT_REMOTE_TOKEN_REF:
            token_cmd += f" --ref {ref}"
        payload = {
            "state": state,
            "running": pid is not None,
            "healthy": health is not None,
            "pid": pid,
            "url": connection_url(cfg),
            "host": str(cfg.remote_host).strip(),
            "port": int(cfg.remote_port),
            "profile": str(cfg.remote_profile).strip(),
            "auth": (health or {}).get("auth"),
            "version": (health or {}).get("version"),
            "tools": (health or {}).get("tools"),
            "token_ref": ref,
            "token_present": token_present,
            "token_hint": token_cmd,
            "cli_found": bool(cli),
            "autostart": bool(cfg.remote_autostart),
            "log_path": str(self._log_file()),
        }
        if state in ("unresponsive", "stopped"):
            tail = self._log_tail()
            if tail:
                payload["log_tail"] = tail
        return payload

    # --- управление ---

    def start(self) -> RemoteStartResult:
        """Запускает ``serve-http``. ``RemoteModeError`` -- с понятным текстом: настройки
        невалидны, нет токена/CLI, уже запущен, ОС отказала, процесс сразу завершился."""
        problems = validate_remote_config(self.config)
        if problems:
            raise RemoteModeError(" ".join(problems))
        if self._live_pid() is not None:
            raise RemoteModeError("Удалённый режим уже запущен.")
        # Порт может держать процесс, поднятый вне диспетчера (консоль, NSSM, планировщик).
        if self._probe(self.config.remote_host, int(self.config.remote_port)) is not None:
            raise RemoteModeError(
                f"По адресу {self.config.remote_host}:{self.config.remote_port} уже отвечает "
                "сервер BPMkit, запущенный не из диспетчера, -- остановите его или смените порт.")
        ref = str(self.config.remote_token_ref).strip()
        if not self._has_secret(ref):
            cmd = self.status()["token_hint"]
            raise RemoteModeError(
                f"Токен «{ref}» не найден в хранилище секретов -- без него сервер не запустится. "
                f"Создайте токен командой: {cmd}")
        cli = self._cli_resolver()
        if not cli:
            raise RemoteModeError(
                "Не найден BPMkit.exe: укажите путь в Настройки → Основные → «CLI BPMkit» "
                "или установите BPMkit.")
        argv = build_serve_argv(self.config, cli)
        log_path = self._log_file()
        try:
            pid = self._spawn(argv, Path.cwd(), log_path)
        except ProcessError as exc:
            raise RemoteModeError(f"Не удалось запустить удалённый режим: {exc}") from exc
        self._write_state(pid)

        healthy = False
        waited = 0.0
        while waited <= self._startup_wait:
            if self._probe(self.config.remote_host, int(self.config.remote_port)) is not None:
                healthy = True
                break
            if not self._alive(pid):
                break
            self._sleep(_STARTUP_POLL_S)
            waited += _STARTUP_POLL_S
        if not healthy and not self._alive(pid):
            self._clear_state()
            tail = self._log_tail()
            raise RemoteModeError(
                "Сервер удалённого режима сразу завершился." + (f" Журнал: {tail}" if tail else ""))
        return RemoteStartResult(pid=pid, log_path=str(log_path),
                                 url=connection_url(self.config), healthy=healthy)

    def stop(self, *, timeout: float = 10.0) -> bool:
        """Останавливает процесс (мягко, с эскалацией -- ``standkit.platform.stop``).
        ``True``, если на момент возврата процесса нет (в т.ч. его и не было)."""
        pid = self._live_pid()
        if pid is None:
            self._clear_state()
            return True
        try:
            stopped = bool(self._stopper(pid, timeout=timeout))
        except ProcessError as exc:
            raise RemoteModeError(f"Не удалось остановить удалённый режим: {exc}") from exc
        if stopped or not self._alive(pid):
            self._clear_state()
        return stopped


def autostart(config: HubConfig, **kwargs) -> Optional[str]:
    """Старт вместе с диспетчером (``remote_autostart``). Возвращает текст отказа или ``None``.

    Не бросает: вызывается из фонового потока запуска хаба, отказ -- строка для журнала.
    Уже запущенный (в т.ч. прошлым экземпляром диспетчера) процесс не трогается."""
    if not config.remote_autostart:
        return None
    controller = RemoteModeController(config, **kwargs)
    try:
        if controller._live_pid() is not None:
            return None
        controller.start()
    except RemoteModeError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - автозапуск не роняет диспетчер
        return f"{type(exc).__name__}: {exc}"
    return None
