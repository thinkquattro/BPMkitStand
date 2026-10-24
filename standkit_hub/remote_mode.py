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
  не читается, не печатается, в argv и в ответы API не попадает. Токен читает ``serve-http``
  из хранилища секретов BPMkit (сервис «bpmkit», ``secrets.enc``), а НЕ из хранилища standkit
  (сервис «standkit»): раньше хаб спрашивал «есть ли секрет» у своего хранилища, и круг
  замыкался -- токен, созданный предложенной хабом командой ``setup remote-token``, хаб не видел,
  а токен, заданный кнопкой «Задать…», не видел сервер (GAP-612, приёмка 13.10.2026). Теперь
  наличие проверяет ``BPMkit.exe setup remote-token --status --ref``, а задаёт/создаёт токен
  ``setup remote-token --from-env`` / ``--generate`` (значение уходит переменной окружения
  процесса, не argv). Токен в командной строке ``serve-http`` сам отвергает.
* Живость -- по ``GET /healthz`` (отвечает без авторизации и без секретов): процесс жив,
  но порт не отвечает -- это «запускается» либо «не отвечает», а не «работает».
* Резолв ``BPMkit.exe`` -- ТОТ ЖЕ, что у экрана лицензии и канала обновлений
  (``license_api.find_cli``: ``companion.mcp_cli`` -> ``BPMKIT_CLI`` -> автодетект рядом с
  поставкой -> запуск из исходников): у хаба один путь к MCP, второго поля не заводим.
"""

from __future__ import annotations

import csv
import io
import json
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from standkit.platform import (
    ProcessError, is_alive, run_console, run_console_tree, spawn_hidden, stop,
)
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

#: Имя переменной окружения, через которую значение токена передаётся ``setup remote-token
#: --from-env`` (то же, что ``remote_transport.ENV_TOKEN_VALUE`` сервера BPMkit).
TOKEN_ENV_VAR = "BPMKIT_REMOTE_TOKEN_VALUE"

#: Ответ CLI на статус токена кэшируется: статус удалённого режима опрашивается часто, а запуск
#: ``BPMkit.exe`` -- это процесс (холодный старт под антивирусом до секунд).
_TOKEN_CACHE_TTL_S = 20.0
_TOKEN_CLI_TIMEOUT_S = 15.0
_TOKEN_CACHE: dict = {}
_TOKEN_CACHE_LOCK = threading.Lock()


def invalidate_token_cache() -> None:
    """Сбросить кэш статуса токена (после создания/смены токена)."""
    with _TOKEN_CACHE_LOCK:
        _TOKEN_CACHE.clear()


def default_cli_runner(argv: list, env: Optional[dict] = None, timeout: float = _TOKEN_CLI_TIMEOUT_S) -> tuple:
    """Запуск CLI BPMkit: ``(rc, stdout, stderr)``; любой сбой запуска -- ``rc=-1``.

    ``run_console_tree``: без мигающего окна, по таймауту умирает всё дерево, stdin -- DEVNULL."""
    import os
    kwargs: dict = dict(capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=timeout)
    if env:
        merged = dict(os.environ)
        merged.update(env)
        kwargs["env"] = merged
    try:
        proc = run_console_tree(list(argv), **kwargs)
    except Exception as exc:  # noqa: BLE001 - один вид отказа наружу
        return -1, "", f"{type(exc).__name__}: {exc}"
    rc = proc.returncode if proc.returncode is not None else -1
    return int(rc), proc.stdout or "", proc.stderr or ""


def _cli_error_text(stdout: str, stderr: str) -> str:
    """Человеческий текст отказа из JSON-ответа CLI (поле ``error``) либо хвост stderr."""
    text = (stdout or "").strip()
    try:
        start, end = text.index("{"), text.rindex("}")
        data = json.loads(text[start:end + 1])
        if isinstance(data, dict) and data.get("error"):
            return str(data["error"])[:400]
    except (ValueError, TypeError):
        pass
    tail = (stderr or text or "").strip().splitlines()
    return (tail[-1] if tail else "CLI BPMkit не ответил")[:400]


#: Один замок на процесс диспетчера: контроллер создаётся на КАЖДЫЙ запрос хаба, поэтому
#: замок на экземпляре два одновременных POST не разведёт.
_CONTROL_LOCK = threading.Lock()

#: Подстроки имени образа, по которым процесс считается «нашим»: собранный BPMkit.exe
#: либо python (запуск из исходников).
_IMAGE_MARKERS = ("bpmkit", "python")


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


def default_image_of(pid: int) -> Optional[str]:
    """Имя образа процесса по pid (``BPMkit.exe``) либо ``None``, если узнать не удалось.

    Windows -- ``tasklist`` через ``run_console`` (без окна); Linux -- ``/proc/<pid>/comm``."""
    try:
        if sys.platform == "win32":
            proc = run_console(["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=10,
                               encoding="utf-8", errors="replace")
            for row in csv.reader(io.StringIO(proc.stdout or "")):
                if len(row) >= 2 and row[1].strip() == str(int(pid)):
                    return row[0].strip()
            return None
        return Path(f"/proc/{int(pid)}/comm").read_text(encoding="utf-8").strip() or None
    except Exception:  # noqa: BLE001 - «не знаю» = None
        return None


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
                 has_secret: Optional[Callable[[str], bool]] = None,
                 cli_runner: Optional[Callable] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 image_of: Callable[[int], Optional[str]] = default_image_of,
                 startup_wait: float = _STARTUP_WAIT_S):
        self.config = config
        self._cli_resolver = cli_resolver or (lambda: license_api.find_cli(config.companion))
        self._spawn = spawn
        self._alive = alive
        self._stopper = stopper
        self._probe = probe
        #: ``has_secret`` задан явно (тесты, встраивание) -- наличие токена решает он; иначе токен
        #: спрашивается у CLI BPMkit, то есть у того хранилища, которое читает serve-http.
        self._has_secret_override = has_secret
        self._has_secret = has_secret or _default_has_secret
        self._cli_runner = cli_runner or default_cli_runner
        self._sleep = sleep
        self._image_of = image_of
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

    # --- токен (хранилище BPMkit, через CLI) ---

    def _cli_or_none(self) -> Optional[list]:
        try:
            return self._cli_resolver()
        except Exception:  # noqa: BLE001
            return None

    def token_present(self, *, fresh: bool = False) -> bool:
        """Есть ли токен в хранилище, которое читает ``serve-http`` (спрашивает CLI BPMkit).

        ``fresh`` -- мимо кэша (перед запуском). CLI не найден -- ``False`` (запустить всё равно
        нечем). CLI не умеет ``remote-token --status`` (старая сборка) -- прежняя проверка по
        хранилищу standkit."""
        ref = str(self.config.remote_token_ref or "").strip()
        if not ref:
            return False
        if self._has_secret_override is not None:
            return bool(self._has_secret_override(ref))
        cli = self._cli_or_none()
        if not cli:
            return False
        key = (tuple(cli), ref)
        now = time.monotonic()
        if not fresh:
            with _TOKEN_CACHE_LOCK:
                hit = _TOKEN_CACHE.get(key)
            if hit is not None and now - hit[0] < _TOKEN_CACHE_TTL_S:
                return hit[1]
        rc, _out, _err = self._cli_runner(
            list(cli) + ["setup", "remote-token", "--status", "--ref", ref])
        if rc == 0:
            present = True
        elif rc == 1:
            present = False
        else:
            present = bool(_default_has_secret(ref))
        with _TOKEN_CACHE_LOCK:
            _TOKEN_CACHE[key] = (now, present)
        return present

    def set_token(self, value: str) -> tuple:
        """Записать токен в хранилище BPMkit (кнопка «Задать…»): ``(ok, сообщение)``.

        Значение уходит ПЕРЕМЕННОЙ ОКРУЖЕНИЯ дочернего процесса (``--from-env``), не argv; замена
        существующего токена -- явное действие человека, поэтому ``--force``. Значение нигде не
        логируется и в сообщение не попадает."""
        ref = str(self.config.remote_token_ref or "").strip()
        if not ref or not _REF_RE.match(ref):
            return False, "Не задана ссылка на токен (remote_token_ref)."
        cli = self._cli_or_none()
        if not cli:
            return False, ("Не найден BPMkit.exe: укажите путь в Настройки → Основные → «CLI BPMkit» "
                           "или установите BPMkit.")
        rc, out, err = self._cli_runner(
            list(cli) + ["setup", "remote-token", "--from-env", TOKEN_ENV_VAR, "--ref", ref, "--force"],
            {TOKEN_ENV_VAR: str(value)})
        invalidate_token_cache()
        if rc == 0:
            return True, "Токен сохранён в хранилище секретов BPMkit."
        return False, _cli_error_text(out, err)

    def generate_token(self, *, force: bool = False) -> tuple:
        """Создать токен командой CLI (``setup remote-token --generate``): ``(ok, сообщение)``.

        Значение не показывается (без ``--show``): его читают командой ``setup secrets get``.
        Существующий токен без ``force`` не перезаписывается -- ротация обрывает всех клиентов."""
        ref = str(self.config.remote_token_ref or "").strip()
        if not ref or not _REF_RE.match(ref):
            return False, "Не задана ссылка на токен (remote_token_ref)."
        cli = self._cli_or_none()
        if not cli:
            return False, ("Не найден BPMkit.exe: укажите путь в Настройки → Основные → «CLI BPMkit» "
                           "или установите BPMkit.")
        argv = list(cli) + ["setup", "remote-token", "--generate", "--ref", ref]
        if force:
            argv.append("--force")
        rc, out, err = self._cli_runner(argv)
        invalidate_token_cache()
        if rc == 0:
            return True, "Токен создан и сохранён в хранилище секретов BPMkit."
        return False, _cli_error_text(out, err)

    # --- состояние ---

    def _live_pid(self) -> Optional[int]:
        state = self._read_state()
        if state is None:
            return None
        pid = state["pid"]
        if self._alive(pid) and self._is_ours(pid, state):
            return pid
        # pid мёртв либо переиспользован ЧУЖИМ процессом: состояние устарело.
        self._clear_state()
        return None

    def _is_ours(self, pid: int, state: dict) -> bool:
        """Тот ли это процесс, что мы запускали. pid после перезагрузки/завершения может достаться
        постороннему -- остановка по одному ``is_alive`` убила бы чужое. «Наш», если на
        сохранённом адресе отвечает /healthz BPMkit либо имя образа -- BPMkit/python. Не удалось
        узнать образ -- считаем чужим (убить чужое хуже, чем показать «остановлен»)."""
        host = state.get("host") or self.config.remote_host
        port = state.get("port") or self.config.remote_port
        try:
            if self._probe(host, int(port)) is not None:
                return True
        except Exception:  # noqa: BLE001
            pass
        try:
            image = (self._image_of(pid) or "").lower()
        except Exception:  # noqa: BLE001
            image = ""
        return any(marker in image for marker in _IMAGE_MARKERS)

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
        token_present = bool(ref) and self.token_present()
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
        невалидны, нет токена/CLI, уже запущен, ОС отказала, процесс сразу завершился.
        Под общим замком: два одновременных POST не породят процесс-сироту."""
        with _CONTROL_LOCK:
            return self._start_locked()

    def _start_locked(self) -> RemoteStartResult:
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
        cli = self._cli_resolver()
        if not cli:
            raise RemoteModeError(
                "Не найден BPMkit.exe: укажите путь в Настройки → Основные → «CLI BPMkit» "
                "или установите BPMkit.")
        if not self.token_present(fresh=True):
            cmd = self.status()["token_hint"]
            raise RemoteModeError(
                f"Токен «{ref}» не найден в хранилище секретов BPMkit -- без него сервер не запустится. "
                f"Нажмите «Сгенерировать токен» либо выполните команду: {cmd}")
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
        with _CONTROL_LOCK:
            return self._stop_locked(timeout)

    def _stop_locked(self, timeout: float) -> bool:
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
