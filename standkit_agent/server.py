"""
HTTP/RPC-сервер агента — STDLIB-ONLY (http.server, ssl), никаких сторонних
веб-фреймворков намеренно (агент должен разворачиваться на голом хосте
стенда без pip install чего-либо, кроме самого standkit).

Эндпоинты (JSON):
    GET  /stands                       — список стендов реестра агента (read)
    GET  /stand/{name}/status          — StandStatus.to_dict()             (read)
    POST /stand/{name}/start           — запустить стенд (transport=local) (control)
    POST /stand/{name}/stop            — остановить стенд                 (control)
    POST /stand/{name}/restart         — перезапустить стенд              (control)
    GET  /stand/{name}/logs?n=100      — последние n строк лога            (read)

СЕКЬЮРИТИ-МОДЕЛЬ (см. также standkit_agent/security.py, standkit_agent/audit.py):
  - Транспорт: опциональный TLS (ssl.SSLContext, минимум TLS 1.2), опциональный
    mTLS (verify_mode=CERT_REQUIRED против --tls-client-ca). Управляется
    вызывающей стороной run_server(); fail-closed bind-проверка — ДО открытия
    сокета, см. security.validate_bind_security().
  - Аутентификация: заголовок ``Authorization: Bearer <token>``, сравнение —
    ТОЛЬКО через hmac.compare_digest (security.Authenticator). Два скоупа:
    control (start/stop/restart + всё read) и readonly (только read).
  - Rate limiting/lockout: security.LockoutTracker — N неудачных
    аутентификаций подряд с одного IP в окне → 429 до окончания окна.
  - Аудит: каждый запрос (успех/отказ/ошибка) пишется в append-only
    JSON-lines лог (audit.audit_event) — без токенов и секретов.
  - Input-hardening: лимит тела запроса, кап на n логов, таймаут сокета,
    валидация имени стенда, 400 на некорректный ввод (не 500/креш).

Остаточные пункты следующих итераций (CORS для браузерного GUI, полноценный
роутинг вместо сопоставления префиксов, CN→scope маппинг для mTLS) и общий
перечень остаточной безопасности — см. SECURITY.md, раздел 6.
"""

from __future__ import annotations

import json
import re
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

from standkit import health, lifecycle
from standkit.registry import Registry, RegistryError
from standkit_agent import audit as _audit
from standkit_agent import security as _security
from standkit_agent.security import (
    Authenticator,
    LockoutTracker,
    DEFAULT_MAX_BODY_BYTES,
    DEFAULT_MAX_LOGS_N,
    DEFAULT_SOCKET_TIMEOUT,
)

_STAND_ACTION_RE = re.compile(r"^/stand/(?P<name>[^/]+)/(?P<action>start|stop|restart|status|logs)$")


class AgentAuthError(Exception):
    """Ошибка аутентификации запроса к агенту."""


def make_handler(
    registry: Registry,
    authenticator: Authenticator,
    *,
    run_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    lockout: Optional[LockoutTracker] = None,
    audit_logger=None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_logs_n: int = DEFAULT_MAX_LOGS_N,
) -> type:
    """
    Фабрика класса-обработчика запросов с "захваченными" зависимостями
    (реестр, аутентификатор, lockout, аудит-логгер) — BaseHTTPRequestHandler
    не поддерживает конструктор с доп. аргументами напрямую, поэтому
    вложенный класс.
    """
    lockout = lockout or LockoutTracker()

    class Handler(BaseHTTPRequestHandler):
        server_version = "standkit-agent/0.2"
        # Таймаут на соединение (DoS-hardening) — socketserver.StreamRequestHandler
        # применяет его к сокету в setup(), если атрибут не None.
        timeout = DEFAULT_SOCKET_TIMEOUT

        # --- вспомогательные ---

        def _client_ip(self) -> str:
            return self.client_address[0] if self.client_address else "-"

        def _peer_cn(self) -> Optional[str]:
            """CN клиентского сертификата, если соединение по mTLS (для аудита)."""
            conn = getattr(self, "connection", None)
            if conn is None or not isinstance(conn, ssl.SSLSocket):
                return None
            try:
                return _security.peer_identity_from_cert(conn.getpeercert())
            except Exception:
                return None

        def _audit(self, *, identity: str, action: str, result: str, code: int) -> None:
            if audit_logger is None:
                return
            _audit.audit_event(
                audit_logger,
                src_ip=self._client_ip(),
                identity=identity or "-",
                method=self.command,
                path=self.path,
                action=action,
                result=result,
                code=code,
            )

        def _bearer_token(self) -> Optional[str]:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return None
            return auth[len("Bearer "):].strip() or None

        def _authenticate(self, action: str) -> Optional[str]:
            """
            Полный цикл: lockout-проверка → извлечение токена → скоуп → проверка
            прав на действие → аудит отказов/локов → успех регистрируется
            (сброс счётчика неудач для этого IP).

            Возвращает скоуп ("control"/"readonly") при успехе, либо ``None``
            (и уже отправляет ответ клиенту — 429/401/403 — и пишет аудит) при
            отказе.
            """
            ip = self._client_ip()
            cn = self._peer_cn()

            if lockout.is_locked(ip):
                self._send_json(429, {"error": "too many failed attempts, try later"})
                self._audit(identity=cn or "-", action=action, result="denied", code=429)
                return None

            token = self._bearer_token()
            scope = authenticator.check(token)
            identity = scope or cn or "-"

            if scope is None:
                lockout.record_failure(ip)
                self._send_json(401, {"error": "unauthorized"})
                self._audit(identity=identity, action=action, result="denied", code=401)
                return None

            if not Authenticator.scope_allows(scope, action):
                # Валидный токен, но недостаточно прав — НЕ засчитывается как
                # неудача аутентификации (это не brute-force ситуация), но в
                # аудит попадает как отказ.
                lockout.record_success(ip)
                self._send_json(403, {"error": "forbidden: insufficient scope"})
                self._audit(identity=identity, action=action, result="denied", code=403)
                return None

            lockout.record_success(ip)
            return scope

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

        def log_message(self, fmt: str, *args) -> None:  # noqa: A003 - сигнатура BaseHTTPRequestHandler
            # Приглушаем стандартный access-лог http.server в stderr — у
            # агента есть собственный структурный аудит-лог (см. audit.py).
            pass

        def _method_not_allowed(self) -> None:
            self._send_json(405, {"error": "method not allowed"})
            self._audit(identity="-", action="unknown", result="denied", code=405)

        do_PUT = _method_not_allowed
        do_DELETE = _method_not_allowed
        do_PATCH = _method_not_allowed

        # --- маршрутизация ---

        def do_GET(self) -> None:  # noqa: N802 - сигнатура BaseHTTPRequestHandler
            parsed = urlparse(self.path)
            path = parsed.path

            if path == "/stands":
                scope = self._authenticate("stands")
                if scope is None:
                    return
                self._send_json(200, {"stands": [s.name for s in registry.list()]})
                self._audit(identity=scope, action="stands", result="ok", code=200)
                return

            m = _STAND_ACTION_RE.match(path)
            if m and m.group("action") == "status":
                name = m.group("name")
                scope = self._authenticate("status")
                if scope is None:
                    return
                if not _security.validate_stand_name(name):
                    self._send_json(400, {"error": "invalid stand name"})
                    self._audit(identity=scope, action="status", result="error", code=400)
                    return
                self._handle_status(name, scope)
                return
            if m and m.group("action") == "logs":
                name = m.group("name")
                scope = self._authenticate("logs")
                if scope is None:
                    return
                if not _security.validate_stand_name(name):
                    self._send_json(400, {"error": "invalid stand name"})
                    self._audit(identity=scope, action="logs", result="error", code=400)
                    return
                qs = parse_qs(parsed.query)
                raw_n = qs.get("n", ["100"])[0]
                try:
                    n = _security.clamp_logs_n(raw_n, max_n=max_logs_n)
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "invalid n"})
                    self._audit(identity=scope, action="logs", result="error", code=400)
                    return
                self._handle_logs(name, n, scope)
                return

            self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - сигнатура BaseHTTPRequestHandler
            m = _STAND_ACTION_RE.match(self.path)
            if not m or m.group("action") not in ("start", "stop", "restart"):
                self._send_json(404, {"error": "not found"})
                return

            # Лимит тела запроса — ДО чтения (проверяем заявленный Content-Length),
            # чтобы не читать в память произвольно большое тело.
            try:
                content_length = _security.validate_content_length(
                    self.headers.get("Content-Length"), max_bytes=max_body_bytes
                )
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if content_length:
                # Тело текущим API не используется, но должно быть вычитано
                # из сокета до отправки ответа (иначе keep-alive соединение
                # десинхронизируется). Лимит выше уже гарантирует, что это
                # не более max_body_bytes.
                try:
                    self.rfile.read(content_length)
                except Exception:
                    self._send_json(400, {"error": "failed to read request body"})
                    return

            name = m.group("name")
            action = m.group("action")

            scope = self._authenticate(action)
            if scope is None:
                return

            if not _security.validate_stand_name(name):
                self._send_json(400, {"error": "invalid stand name"})
                self._audit(identity=scope, action=action, result="error", code=400)
                return

            try:
                stand = registry.get(name)
            except RegistryError as exc:
                self._send_json(404, {"error": str(exc)})
                self._audit(identity=scope, action=action, result="error", code=404)
                return

            try:
                if action == "start":
                    pid = lifecycle.start(stand, run_dir=run_dir, log_dir=log_dir)
                    self._send_json(200, {"ok": True, "pid": pid})
                elif action == "stop":
                    ok = lifecycle.stop(stand, run_dir=run_dir)
                    self._send_json(200, {"ok": ok})
                elif action == "restart":
                    pid = lifecycle.restart(stand, run_dir=run_dir, log_dir=log_dir)
                    self._send_json(200, {"ok": True, "pid": pid})
                self._audit(identity=scope, action=action, result="ok", code=200)
            except Exception as exc:  # noqa: BLE001 - агент не должен падать на ошибке одного стенда
                self._send_json(500, {"error": str(exc)})
                self._audit(identity=scope, action=action, result="error", code=500)

        def _handle_status(self, name: str, scope: str) -> None:
            try:
                stand = registry.get(name)
            except RegistryError as exc:
                self._send_json(404, {"error": str(exc)})
                self._audit(identity=scope, action="status", result="error", code=404)
                return
            pf = lifecycle.pidfile_path(stand, run_dir)
            status = health.check_stand(stand, pidfile=pf)
            self._send_json(200, status.to_dict())
            self._audit(identity=scope, action="status", result="ok", code=200)

        def _handle_logs(self, name: str, n: int, scope: str) -> None:
            from standkit import logs as _logs

            try:
                stand = registry.get(name)
            except RegistryError as exc:
                self._send_json(404, {"error": str(exc)})
                self._audit(identity=scope, action="logs", result="error", code=404)
                return
            lp = lifecycle.log_path(stand, log_dir)
            self._send_json(200, {"lines": _logs.tail(lp, n)})
            self._audit(identity=scope, action="logs", result="ok", code=200)

    return Handler


def run_server(
    registry: Registry,
    authenticator: Authenticator,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    run_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    tls_cert: Optional[str] = None,
    tls_key: Optional[str] = None,
    tls_client_ca: Optional[str] = None,
    insecure: bool = False,
    lockout: Optional[LockoutTracker] = None,
    audit_log_path: Optional[Path] = None,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_logs_n: int = DEFAULT_MAX_LOGS_N,
) -> None:
    """
    Запускает HTTP(S)-сервер агента (блокирующий вызов — рассчитан на запуск
    в основном потоке процесса-демона, см. standkit_agent.__main__).

    Secure-defaults (fail-closed): ``host`` по умолчанию loopback; если
    вызывающая сторона передаёт non-loopback host без TLS и без
    ``insecure=True`` — старт прерывается ``InsecureBindError`` ДО открытия
    сокета (см. security.validate_bind_security).
    """
    tls_enabled = bool(tls_cert and tls_key)
    _security.validate_bind_security(host, tls_enabled=tls_enabled, insecure=insecure)

    if insecure and not tls_enabled and not _security.is_loopback_host(host):
        import sys

        print(
            "[standkit-agent] !!! ВНИМАНИЕ: --insecure — агент слушает "
            f"{host}:{port} ОТКРЫТЫМ HTTP без TLS на non-loopback адресе. "
            "Это RCE-поверхность (start/stop/restart процессов стенда) без "
            "шифрования и без аутентификации транспорта. НЕ использовать в "
            "проде/недоверенной сети. Только dev/тест за изолированным "
            "периметром.",
            file=sys.stderr,
        )

    audit_logger = _audit.build_audit_logger(audit_log_path)
    handler_cls = make_handler(
        registry,
        authenticator,
        run_dir=run_dir,
        log_dir=log_dir,
        lockout=lockout,
        audit_logger=audit_logger,
        max_body_bytes=max_body_bytes,
        max_logs_n=max_logs_n,
    )
    httpd = ThreadingHTTPServer((host, port), handler_cls)
    if tls_enabled:
        ctx = _security.build_ssl_context(tls_cert, tls_key, tls_client_ca)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
