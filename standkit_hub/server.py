"""
HTTP-сервер веб-дашборда standkit — STDLIB-ONLY (http.server), никаких
сторонних веб-фреймворков намеренно (тот же принцип, что и у
standkit_agent.server: хаб должен разворачиваться без pip install чего-либо,
кроме самого standkit/standkit_agent).

Отдаёт:
    - статический фронтенд (vanilla JS/CSS, web/index.html + /static/*);
    - JSON API под ``/api/*`` (агрегированный статус стендов, start/stop/
      restart, настройки хаба, секреты, локальный агент, ярлык).

СЕКЬЮРИТИ-МОДЕЛЬ (см. standkit_hub/security.py, docstring там подробнее):
  - Bind ТОЛЬКО на loopback по умолчанию (fail-closed, см.
    standkit_agent.security.validate_bind_security — переиспользуется
    напрямую, не дублируется).
  - Сессионный токен генерируется один раз при старте процесса
    (``secrets.token_urlsafe(32)``). Первый переход по ``/?t=<token>``
    ставит HttpOnly+SameSite=Strict cookie и редиректит на ``/`` без токена
    в URL. Далее ``GET /api/*`` требует совпадения токена (cookie ИЛИ
    заголовок ``X-Standkit-Token``) — иначе 401.
  - Мутации (``POST``/``DELETE`` под ``/api/*``) ДОПОЛНИТЕЛЬНО требуют явный
    заголовок ``X-Standkit-Token`` (double-submit — сторонний сайт не может
    ни прочитать HttpOnly-cookie, ни продублировать его в заголовок) И
    совпадающий по loopback-хосту и порту ``Origin``/``Referer`` — иначе 403.
  - Никакого CORS (same-origin по дизайну).
  - Статика (``/``, ``/static/*``) отдаётся БЕЗ авторизации — это только
    HTML/JS/CSS-оболочка без данных стенда, авторизация нужна исключительно
    для ``/api/*``.
  - Input-hardening: лимит тела запроса, кап на ``n`` логов, таймаут сокета,
    валидация имени стенда/ссылки на секрет, санитайзинг статических путей
    (защита от traversal), 400/404 без стектрейсов.
  - Секреты (``POST /api/secret/{ref}``) никогда не логируются и не попадают
    в аудит/ответ — только статус ``has_secret``.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from standkit import logs as _logs
from standkit.lifecycle import LifecycleError
from standkit.models import Stand
from standkit.registry import Registry, RegistryError, bpmkit_config_dir, default_registry_path
from standkit.secrets import SecretError, delete_secret, has_secret, set_secret
from standkit_hub import logs_browser
from standkit_hub import security as _security
from standkit_hub.agent_control import AgentControlError, AgentController
from standkit_hub.client import FederatedClient, RemoteCallError
from standkit_hub.config import HubConfig
from standkit_hub.shortcut import install_desktop_shortcut, uninstall_desktop_shortcut

_STAND_ACTION_RE = re.compile(
    r"^/api/stand/(?P<name>[^/]+)/(?P<action>status|logs|start|stop|restart|state)$"
)
_STAND_LOGS_SUB_RE = re.compile(r"^/api/stand/(?P<name>[^/]+)/logs/(?P<sub>list|file|open-folder)$")
_SECRET_RE = re.compile(r"^/api/secret/(?P<ref>[^/]+)$")

# Человекочитаемые подписи источника логов для сообщений "лог недоступен".
_LOG_SOURCE_LABELS = {"stand": "Стенд", "bpmkit": "BPMkit"}

_DEFAULT_WEB_DIR = Path(__file__).parent / "web"

# env, которую может задать инсталляция клиентского MCP BPMkit, чтобы хаб
# нашёл его manifest.json напрямую, без угадывания путей.
_MCP_MANIFEST_ENV = "BPMKIT_MANIFEST"


def _redis_number(stand: Stand) -> Optional[int]:
    """
    Номер БД Redis стенда, ЕСЛИ он найден в реестре/``extra``.

    Реестр BPMkit (projects.json) обычно НЕ хранит Redis-параметры — номер БД
    Redis лежит в конфиге самого стенда (appsettings), не в реестре стендов.
    Поэтому в подавляющем большинстве случаев эта функция вернёт ``None`` —
    это ожидаемо, а не ошибка; ищем по нескольким правдоподобным ключам на
    случай, если значение всё же было добавлено вручную/другим инструментом.
    """
    for key in ("redis_db", "redis_number"):
        val = stand.extra.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    nested = stand.extra.get("redis")
    if isinstance(nested, dict):
        for key in ("db", "number", "redis_db"):
            val = nested.get(key)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    continue
    return None


def _find_mcp_manifest_path(config: "HubConfig") -> Optional[Path]:
    """
    Ищет ``manifest.json`` клиентского MCP BPMkit (для карточки версии/
    Companion на дашборде). Порядок поиска — от явного к угадыванию:

    1. env ``BPMKIT_MANIFEST`` (путь к самому файлу манифеста);
    2. рядом с реестром стендов (``registry_path`` хаба указывает в общую
       папку BPMkit, где обычно лежит и manifest.json установленного MCP);
    3. каноническая папка конфигов BPMkit (``%APPDATA%\\BPMkit`` и т.п.);
    4. стандартное относительное расположение при поставке рядом со
       standkit (``../BPMkit/manifest.json`` от корня пакета).

    Не находит — не ошибка, вызывающая сторона отдаёт ``version: null``.
    """
    env_value = os.environ.get(_MCP_MANIFEST_ENV)
    if env_value:
        p = Path(env_value)
        if p.is_file():
            return p

    candidates = []
    if config.registry_path:
        candidates.append(Path(config.registry_path).parent / "manifest.json")
    candidates.append(bpmkit_config_dir() / "manifest.json")
    candidates.append(Path(__file__).resolve().parents[2] / "BPMkit" / "manifest.json")

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _load_config(config_path: Path) -> HubConfig:
    return HubConfig.load(config_path)


def _load_registry(config: HubConfig) -> Registry:
    reg_path = Path(config.registry_path) if config.registry_path else default_registry_path()
    return Registry.load(reg_path)


def make_handler(
    *,
    config_path: Path,
    session_token: str,
    web_dir: Optional[Path] = None,
    max_body_bytes: int = _security.DEFAULT_MAX_BODY_BYTES,
    max_logs_n: int = _security.DEFAULT_MAX_LOGS_N,
) -> type:
    """
    Фабрика класса-обработчика запросов хаба с "захваченными" зависимостями
    (путь конфига, сессионный токен, каталог статики) — по тому же принципу,
    что ``standkit_agent.server.make_handler``.
    """
    web_dir = web_dir or _DEFAULT_WEB_DIR

    class Handler(BaseHTTPRequestHandler):
        server_version = "standkit-hub/0.1"
        timeout = 30.0

        # --- вспомогательные ---

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - сигнатура BaseHTTPRequestHandler
            # У хаба нет отдельного аудит-лога (в отличие от агента) — но
            # стандартный access-лог http.server в stderr всё равно приглушаем,
            # чтобы не шуметь секретными путями (/api/secret/<ref>) в консоли.
            pass

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _hub_port(self) -> int:
            return int(self.server.server_address[1])

        def _presented_token(self) -> Optional[str]:
            header = self.headers.get(_security.TOKEN_HEADER_NAME)
            if header:
                return header
            return _security.extract_cookie_token(self.headers.get("Cookie", ""))

        def _authorize_read(self) -> bool:
            """GET /api/* — токен из cookie ИЛИ заголовка. 401 при несовпадении."""
            if not _security.tokens_match(self._presented_token(), session_token):
                self._send_json(401, {"error": "unauthorized"})
                return False
            return True

        def _authorize_mutation(self) -> bool:
            """POST/DELETE /api/* — явный заголовок + локальный Origin/Referer. 403 при несовпадении."""
            header_token = self.headers.get(_security.TOKEN_HEADER_NAME)
            if not _security.tokens_match(header_token, session_token):
                self._send_json(403, {"error": "forbidden: missing or invalid X-Standkit-Token header"})
                return False
            origin = self.headers.get("Origin") or self.headers.get("Referer")
            if not _security.is_local_origin(origin, expected_port=self._hub_port()):
                self._send_json(403, {"error": "forbidden: origin/referer is not the local hub"})
                return False
            return True

        def _log_source_from_qs(self, parsed) -> Optional[str]:
            """
            Разбирает query-параметр ``source`` (какой источник логов стенда
            использовать — "stand" — логи самого стенда, "bpmkit" — логи,
            которые пишет BPMkit MCP). Дефолт — "stand" (см.
            ``logs_browser.DEFAULT_LOG_SOURCE``).

            При некорректном значении сразу отправляет 400 и возвращает
            ``None`` — вызывающая сторона обязана прервать обработку запроса.
            """
            qs = parse_qs(parsed.query)
            raw = (qs.get("source") or [logs_browser.DEFAULT_LOG_SOURCE])[0]
            if raw not in logs_browser.LOG_SOURCES:
                self._send_json(
                    400,
                    {"error": f"invalid source: {raw!r} (ожидается 'stand' или 'bpmkit')"},
                )
                return None
            return raw

        def _read_json_body(self, *, max_bytes: int) -> Optional[dict]:
            try:
                length = _security.validate_content_length(
                    self.headers.get("Content-Length"), max_bytes=max_bytes
                )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return None
            raw = self.rfile.read(length) if length else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid JSON body"})
                return None
            if not isinstance(data, dict):
                self._send_json(400, {"error": "JSON body must be an object"})
                return None
            return data

        # --- статика ---

        def _serve_index(self, inject_token: Optional[str] = None, set_cookie: bool = False) -> None:
            index_path = web_dir / "index.html"
            if not index_path.is_file():
                self._send_json(500, {"error": "index.html не найден в пакете хаба"})
                return
            # Токен инжектим в <meta> ТОЛЬКО аутентифицированному запросу (см.
            # _handle_root). Неаутентифицированному — плейсхолдер очищается в пустоту,
            # токен не утекает.
            text = index_path.read_text(encoding="utf-8")
            text = text.replace("__STANDKIT_TOKEN__", inject_token or "")
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if set_cookie:
                self.send_header(
                    "Set-Cookie",
                    f"{_security.SESSION_COOKIE_NAME}={session_token}; HttpOnly; SameSite=Strict; Path=/",
                )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _serve_static(self, rel_path: str) -> None:
            target = _security.sanitize_static_path(web_dir, rel_path)
            if target is None:
                self._send_json(404, {"error": "not found"})
                return
            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _handle_root(self, parsed) -> None:
            qs = parse_qs(parsed.query)
            token = (qs.get(_security.TOKEN_QUERY_PARAM) or [None])[0]
            authed_query = bool(token and _security.tokens_match(token, session_token))
            cookie_tok = _security.extract_cookie_token(self.headers.get("Cookie", ""))
            authed_cookie = _security.tokens_match(cookie_tok, session_token)
            if authed_query or authed_cookie:
                # Аутентифицированный запрос: отдаём index с токеном в <meta>, чтобы
                # JS мог класть X-Standkit-Token в мутации (cookie HttpOnly, JS её не
                # читает). Cookie ставим, если пришли по ссылке ?t=. Без редиректа —
                # иначе токен теряется до загрузки JS (был баг 403 на мутациях).
                self._serve_index(inject_token=session_token, set_cookie=authed_query)
            else:
                self._serve_index()

        # --- API: стенды ---

        def _api_stands(self) -> None:
            config = _load_config(config_path)
            try:
                registry = _load_registry(config)
            except RegistryError as exc:
                self._send_json(500, {"error": str(exc)})
                return
            client = FederatedClient(registry)
            statuses = client.status_all()
            stands = []
            for name in registry.names():
                stand = registry.get(name)
                status = statuses.get(name)
                status_dict = status.to_dict() if status else None
                http_state = status.http.value if status else "unknown"
                db_state = status.db.value if status else "unknown"
                redis_state = status.redis.value if status else "unknown"
                process_state = status.process.value if status else "unknown"
                # Таблица стендов исторически показывает именно BPMkit-каталог
                # логов (extra["logs_path"]) — источник "stand" здесь не
                # запрашивается ни query-параметром, ни выбором пользователя
                # (тот выбор — только у панели "Текущее состояние" ниже).
                logs_dir = logs_browser.resolve_logs_dir(stand, source="bpmkit")
                logs_path = str(logs_dir) if logs_dir else (stand.extra.get("logs_path") or None)
                http_url = (
                    f"http://{stand.stand_host}:{stand.stand_port}"
                    if stand.stand_host and stand.stand_port
                    else None
                )
                stands.append(
                    {
                        "name": name,
                        "transport": stand.transport.value,
                        "status": status_dict,
                        "http": {"url": http_url, "state": http_state},
                        "db": {"name": stand.db_name or None, "state": db_state},
                        "redis": {"number": _redis_number(stand), "state": redis_state},
                        "process": {
                            "state": process_state,
                            "transport": stand.transport.value,
                            "logs_path": logs_path,
                        },
                    }
                )
            self._send_json(200, {"stands": stands, "default": registry.default})

        def _api_stand_status(self, name: str) -> None:
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            client = FederatedClient(registry)
            try:
                status = client.status(name)
            except (RemoteCallError, SecretError) as exc:
                self._send_json(502, {"error": str(exc)})
                return
            except NotImplementedError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, status.to_dict())

        def _api_stand_logs(self, name: str, parsed) -> None:
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            qs = parse_qs(parsed.query)
            raw_n = (qs.get("n") or ["100"])[0]
            try:
                n = _security.clamp_logs_n(raw_n, max_n=max_logs_n)
            except (ValueError, TypeError):
                self._send_json(400, {"error": "invalid n"})
                return
            client = FederatedClient(registry)
            try:
                lines = client.logs(name, n)
            except (RemoteCallError, SecretError) as exc:
                self._send_json(502, {"error": str(exc)})
                return
            except NotImplementedError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"lines": lines})

        def _api_stand_state(self, name: str, parsed) -> None:
            """
            Текущее состояние стенда (то, что видно в консоли/PS-окне стенда) —
            tail основного лог-файла из выбранного источника (``source``,
            дефолт "stand" — см. ``logs_browser``). Не путать с /logs
            (standkit-managed лог для transport=local через lifecycle) — это
            отдельный источник, специфичный для того, как реально запущен
            стенд (зачастую — вне standkit).
            """
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            source = self._log_source_from_qs(parsed)
            if source is None:
                return
            stand = registry.get(name)
            label = _LOG_SOURCE_LABELS[source]
            logs_dir = logs_browser.resolve_logs_dir(stand, source=source)
            if logs_dir is None:
                raw = logs_browser.raw_logs_path(stand, source)
                detail = "путь не задан" if not raw else f"каталог не найден — {raw}"
                self._send_json(
                    200,
                    {
                        "available": False,
                        "text": f"лог недоступен (источник «{label}»: {detail})",
                        "file": None,
                        "source": source,
                    },
                )
                return
            primary = logs_browser.pick_primary_log(logs_dir)
            if primary is None:
                self._send_json(
                    200,
                    {
                        "available": False,
                        "text": f"лог недоступен (источник «{label}»: в каталоге {logs_dir} нет файлов)",
                        "file": None,
                        "source": source,
                    },
                )
                return
            lines = _logs.tail(primary, 200)
            self._send_json(
                200,
                {
                    "available": True,
                    "text": "\n".join(lines) if lines else "(лог пуст)",
                    "file": primary.name,
                    "source": source,
                },
            )

        def _api_stand_logs_list(self, name: str, parsed) -> None:
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            source = self._log_source_from_qs(parsed)
            if source is None:
                return
            stand = registry.get(name)
            logs_dir = logs_browser.resolve_logs_dir(stand, source=source)
            if logs_dir is None:
                raw = logs_browser.raw_logs_path(stand, source)
                self._send_json(200, {"files": [], "logs_path": raw, "source": source})
                return
            files = logs_browser.list_log_files(logs_dir)
            self._send_json(200, {"files": files, "logs_path": str(logs_dir), "source": source})

        def _api_stand_logs_file(self, name: str, parsed) -> None:
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            source = self._log_source_from_qs(parsed)
            if source is None:
                return
            stand = registry.get(name)
            logs_dir = logs_browser.resolve_logs_dir(stand, source=source)
            if logs_dir is None:
                self._send_json(404, {"error": "источник логов не задан или недоступен"})
                return
            qs = parse_qs(parsed.query)
            raw_name = (qs.get("name") or [None])[0]
            if not raw_name:
                self._send_json(400, {"error": "параметр 'name' обязателен"})
                return
            target = logs_browser.sanitize_log_filename(logs_dir, raw_name)
            if target is None:
                self._send_json(404, {"error": "файл не найден"})
                return
            raw_n = (qs.get("n") or ["500"])[0]
            try:
                n = _security.clamp_logs_n(raw_n, max_n=max_logs_n)
            except (ValueError, TypeError):
                self._send_json(400, {"error": "invalid n"})
                return
            lines = _logs.tail(target, n)
            self._send_json(200, {"lines": lines, "name": target.name, "source": source})

        def _api_stand_logs_open_folder(self, name: str, parsed) -> None:
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            source = self._log_source_from_qs(parsed)
            if source is None:
                return
            stand = registry.get(name)
            logs_dir = logs_browser.resolve_logs_dir(stand, source=source)
            if logs_dir is None:
                self._send_json(400, {"error": "источник логов не задан или недоступен"})
                return
            result = logs_browser.open_folder(logs_dir)
            self._send_json(
                200 if result.ok else 400,
                {"ok": result.ok, "message": result.message, "source": source},
            )

        # --- API: MCP / Companion ---

        def _api_mcp_version(self) -> None:
            config = _load_config(config_path)
            manifest_path = _find_mcp_manifest_path(config)
            version = None
            if manifest_path is not None:
                try:
                    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    version = data.get("version")
                except (OSError, json.JSONDecodeError):
                    version = None
            self._send_json(
                200,
                {
                    "version": version,
                    "manifest_path": str(manifest_path) if manifest_path else None,
                },
            )

        def _api_stand_action(self, name: str, action: str) -> None:
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            client = FederatedClient(registry)
            try:
                getattr(client, action)(name)
            except (RemoteCallError, SecretError) as exc:
                self._send_json(502, {"error": str(exc)})
                return
            except LifecycleError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except NotImplementedError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True})

        # --- API: настройки ---

        def _api_settings_get(self) -> None:
            config = _load_config(config_path)
            self._send_json(200, config.to_dict())

        def _api_settings_post(self) -> None:
            body = self._read_json_body(max_bytes=max_body_bytes)
            if body is None:
                return
            current = _load_config(config_path)
            data = current.to_dict()
            data.update(body)
            new_config = HubConfig.from_dict(data)
            new_config.save(config_path)
            self._send_json(200, new_config.to_dict())

        # --- API: секреты ---

        def _api_secret_get(self, ref: str) -> None:
            if not _security.validate_secret_ref(ref):
                self._send_json(400, {"error": "invalid secret ref"})
                return
            self._send_json(200, {"ref": ref, "has_secret": has_secret(ref)})

        def _api_secret_post(self, ref: str) -> None:
            if not _security.validate_secret_ref(ref):
                self._send_json(400, {"error": "invalid secret ref"})
                return
            body = self._read_json_body(max_bytes=max_body_bytes)
            if body is None:
                return
            value = body.get("value")
            if not isinstance(value, str) or not value:
                self._send_json(400, {"error": "поле 'value' обязательно и должно быть непустой строкой"})
                return
            try:
                set_secret(ref, value)
            except SecretError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "ref": ref})

        def _api_secret_delete(self, ref: str) -> None:
            if not _security.validate_secret_ref(ref):
                self._send_json(400, {"error": "invalid secret ref"})
                return
            try:
                delete_secret(ref)
            except SecretError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "ref": ref})

        # --- API: локальный агент ---

        def _api_agent_status(self) -> None:
            config = _load_config(config_path)
            controller = AgentController(config)
            self._send_json(200, {"running": controller.is_running()})

        def _api_agent_start(self) -> None:
            config = _load_config(config_path)
            controller = AgentController(config)
            try:
                result = controller.start()
            except AgentControlError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "pid": result.pid, "log_path": result.log_path})

        def _api_agent_stop(self) -> None:
            config = _load_config(config_path)
            controller = AgentController(config)
            try:
                stopped = controller.stop()
            except AgentControlError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": stopped})

        # --- API: ярлык ---

        def _api_shortcut_install(self) -> None:
            result = install_desktop_shortcut()
            self._send_json(200 if result.ok else 400, {"ok": result.ok, "path": result.path, "message": result.message})

        def _api_shortcut_uninstall(self) -> None:
            result = uninstall_desktop_shortcut()
            self._send_json(200 if result.ok else 400, {"ok": result.ok, "path": result.path, "message": result.message})

        # --- маршрутизация ---

        def do_GET(self) -> None:  # noqa: N802 - сигнатура BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/":
                self._handle_root(parsed)
                return

            if path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
                return

            if not path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return

            if path == "/api/stands":
                if not self._authorize_read():
                    return
                self._api_stands()
                return

            if path == "/api/settings":
                if not self._authorize_read():
                    return
                self._api_settings_get()
                return

            if path == "/api/agent/status":
                if not self._authorize_read():
                    return
                self._api_agent_status()
                return

            if path == "/api/mcp/version":
                if not self._authorize_read():
                    return
                self._api_mcp_version()
                return

            m = _SECRET_RE.match(path)
            if m:
                if not self._authorize_read():
                    return
                self._api_secret_get(m.group("ref"))
                return

            m = _STAND_LOGS_SUB_RE.match(path)
            if m:
                if not self._authorize_read():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                if m.group("sub") == "list":
                    self._api_stand_logs_list(m.group("name"), parsed)
                    return
                if m.group("sub") == "file":
                    self._api_stand_logs_file(m.group("name"), parsed)
                    return
                # "open-folder" — только POST (мутация: запускает процесс на хосте).
                self._send_json(404, {"error": "not found"})
                return

            m = _STAND_ACTION_RE.match(path)
            if m and m.group("action") == "status":
                if not self._authorize_read():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                self._api_stand_status(m.group("name"))
                return
            if m and m.group("action") == "logs":
                if not self._authorize_read():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                self._api_stand_logs(m.group("name"), parsed)
                return
            if m and m.group("action") == "state":
                if not self._authorize_read():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                self._api_stand_state(m.group("name"), parsed)
                return

            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - сигнатура BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            path = parsed.path

            if not path.startswith("/api/"):
                self._send_json(404, {"error": "not found"})
                return

            if path == "/api/settings":
                if not self._authorize_mutation():
                    return
                self._api_settings_post()
                return

            if path == "/api/agent/start":
                if not self._authorize_mutation():
                    return
                self._api_agent_start()
                return
            if path == "/api/agent/stop":
                if not self._authorize_mutation():
                    return
                self._api_agent_stop()
                return

            if path == "/api/shortcut/install":
                if not self._authorize_mutation():
                    return
                self._api_shortcut_install()
                return
            if path == "/api/shortcut/uninstall":
                if not self._authorize_mutation():
                    return
                self._api_shortcut_uninstall()
                return

            m = _SECRET_RE.match(path)
            if m:
                if not self._authorize_mutation():
                    return
                self._api_secret_post(m.group("ref"))
                return

            m = _STAND_LOGS_SUB_RE.match(path)
            if m and m.group("sub") == "open-folder":
                if not self._authorize_mutation():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                self._api_stand_logs_open_folder(m.group("name"), parsed)
                return

            m = _STAND_ACTION_RE.match(path)
            if m and m.group("action") in ("start", "stop", "restart"):
                if not self._authorize_mutation():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                self._api_stand_action(m.group("name"), m.group("action"))
                return

            self._send_json(404, {"error": "not found"})

        def do_DELETE(self) -> None:  # noqa: N802 - сигнатура BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            path = parsed.path

            m = _SECRET_RE.match(path)
            if m:
                if not self._authorize_mutation():
                    return
                self._api_secret_delete(m.group("ref"))
                return

            self._send_json(404, {"error": "not found"})

        def do_PUT(self) -> None:  # noqa: N802 - сигнатура BaseHTTPRequestHandler
            self._send_json(405, {"error": "method not allowed"})

        do_PATCH = do_PUT

    return Handler


def create_hub_server(
    host: str,
    port: int,
    *,
    config_path: Path,
    session_token: str,
    web_dir: Optional[Path] = None,
    insecure: bool = False,
) -> ThreadingHTTPServer:
    """
    Биндит и возвращает готовый ``ThreadingHTTPServer`` (БЕЗ ``serve_forever``)
    — вынесено отдельно от блокирующего запуска, чтобы вызывающая сторона
    (``standkit_hub.__main__``) могла узнать реальный порт (важно при
    ``port=0`` — эфемерный порт) ДО того, как открыть браузер, и чтобы тесты
    могли поднимать сервер в отдельном потоке без дублирования bind-логики.

    Fail-closed bind-проверка (см. standkit_hub.security.validate_bind_security)
    выполняется ДО открытия сокета — как и у headless-агента.
    """
    _security.validate_bind_security(host, tls_enabled=False, insecure=insecure)
    handler_cls = make_handler(config_path=config_path, session_token=session_token, web_dir=web_dir)
    return ThreadingHTTPServer((host, port), handler_cls)
