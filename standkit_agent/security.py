"""
Security-примитивы headless-агента: fail-closed валидация bind-параметров,
TLS/mTLS-контекст, аутентификация по скоупам (control/readonly) с
защитой от timing-атак, rate limiting/lockout по source-IP.

Агент — RCE-поверхность по дизайну (управляет процессами на хосте стенда:
start/stop/restart произвольного дистрибутива). Все функции этого модуля
написаны так, чтобы secure-defaults были ОТКАЗ, а не разрешение (fail-closed):
там, где нет однозначного подтверждения безопасности конфигурации — агент не
стартует, а не "стартует и предупреждает".

STDLIB-ONLY: ``ssl``, ``hmac``, ``socket``, ``threading``, ``time``, ``re``.
Никаких сторонних зависимостей.
"""

from __future__ import annotations

import hmac
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# --- Константы secure-defaults ---

# Минимум TLS 1.2 — TLS 1.0/1.1 запрещены протоколом (POODLE/BEAST-класс атак).
MIN_TLS_VERSION = ssl.TLSVersion.TLSv1_2

# Разумный набор современных AEAD-шифров (без RC4/3DES/статического RSA-обмена
# ключами, без экспортных наборов). Список сознательно консервативен —
# приоритет ECDHE (forward secrecy) + AEAD.
RECOMMENDED_CIPHERS = (
    "ECDHE-ECDSA-AES256-GCM-SHA384:"
    "ECDHE-RSA-AES256-GCM-SHA384:"
    "ECDHE-ECDSA-AES128-GCM-SHA256:"
    "ECDHE-RSA-AES128-GCM-SHA256:"
    "ECDHE-ECDSA-CHACHA20-POLY1305:"
    "ECDHE-RSA-CHACHA20-POLY1305"
)

DEFAULT_MAX_BODY_BYTES = 64 * 1024  # 64 КБ — лимит тела HTTP-запроса
DEFAULT_MAX_LOGS_N = 10_000  # кап на "n" в GET /stand/{name}/logs
DEFAULT_SOCKET_TIMEOUT = 30.0  # секунд — таймаут на одно соединение

DEFAULT_LOCKOUT_MAX_FAILURES = 5
DEFAULT_LOCKOUT_WINDOW_SECONDS = 300.0

_LOOPBACK_EXACT = {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1"}

# Валидное имя стенда: то же множество символов, что типично для ключей
# реестра projects.json (без "/", без пробелов, без управляющих символов).
_STAND_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# --- Скоупы аутентификации ---

SCOPE_CONTROL = "control"
SCOPE_READONLY = "readonly"

READ_ACTIONS = frozenset({"stands", "status", "logs"})
# "adopt" — взятие под управление стенда, поднятого вне диспетчера (пишет
# pidfile по найденному владельцу порта, см. standkit.adopt). Это операция
# управления процессом, поэтому только control-скоуп.
CONTROL_ACTIONS = frozenset({"start", "stop", "restart", "adopt"})


class InsecureBindError(Exception):
    """
    Отказ старта агента: fail-closed проверка bind-параметров не пройдена.

    Бросается ДО открытия сокета — старт процесса агента прерывается с
    ненулевым кодом возврата, а не "стартует и уязвим".
    """


def is_loopback_host(host: str) -> bool:
    """
    True, если ``host`` — loopback-адрес/имя (127.0.0.1, ::1, localhost,
    127.x.x.x). Используется для fail-closed решения — держим проверку
    консервативной (белый список известных loopback-форм), а не пытаемся
    резолвить произвольные DNS-имена (это была бы сетевая операция внутри
    чистой функции валидации).
    """
    h = (host or "").strip().lower()
    if h in _LOOPBACK_EXACT:
        return True
    if h.startswith("127."):
        return True
    return False


def validate_bind_security(host: str, *, tls_enabled: bool, insecure: bool) -> None:
    """
    Fail-closed проверка перед стартом сервера. Чистая функция — НЕ открывает
    сокет, поэтому тестируется без реальной сети.

    Правила:
      - loopback-хост — открытый HTTP разрешён (обычный dev-сценарий за
        локальным туннелем/управляющим контуром на той же машине);
      - non-loopback хост БЕЗ TLS — отказ (``InsecureBindError``), если не
        передан явный ``insecure=True`` (осознанный обход, только для
        dev/тестовых сценариев — вызывающая сторона обязана громко
        предупредить в stderr, см. ``standkit_agent.server.run_server``);
      - TLS включён — non-loopback разрешён без дополнительных условий
        (транспорт уже защищён).
    """
    if tls_enabled:
        return
    if is_loopback_host(host):
        return
    if insecure:
        return
    raise InsecureBindError(
        f"Отказ старта: host={host!r} не loopback, TLS не настроен "
        "(--tls-cert/--tls-key) — headless-агент управляет процессами на "
        "хосте стенда (RCE-поверхность), открытый HTTP наружу по умолчанию "
        "запрещён (fail-closed). Варианты: (1) слушать loopback (127.0.0.1) "
        "за управляющим контуром/VPN/SSH-туннелем; (2) настроить TLS/mTLS "
        "(--tls-cert/--tls-key[/--tls-client-ca]); (3) если это осознанный "
        "dev-сценарий — передать --insecure (НЕ для прод, агент выведет "
        "громкое предупреждение)."
    )


def _apply_tls_hardening(context: ssl.SSLContext, *, require_client_cert: bool) -> None:
    """
    Настраивает флаги ``SSLContext`` (минимальная версия протокола, шифры,
    verify_mode) БЕЗ обращения к файловой системе — специально вынесено
    отдельно от ``build_ssl_context``, чтобы это можно было протестировать
    юнит-тестом без реальных сертификатов (см. tests/test_agent_security.py).
    """
    context.minimum_version = MIN_TLS_VERSION
    try:
        context.set_ciphers(RECOMMENDED_CIPHERS)
    except ssl.SSLError:
        # Набор шифров недоступен в конкретной сборке OpenSSL — не роняем
        # запуск агента из-за этого, минимальная версия протокола важнее.
        pass
    # Явно выключаем сжатие (CRIME-класс атак) и устаревшие опции, если они
    # доступны в текущей сборке OpenSSL.
    context.options |= getattr(ssl, "OP_NO_COMPRESSION", 0)
    if require_client_cert:
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        context.verify_mode = ssl.CERT_NONE


def build_ssl_context(
    tls_cert: str,
    tls_key: str,
    tls_client_ca: Optional[str] = None,
) -> ssl.SSLContext:
    """
    Строит серверный ``SSLContext`` для оборачивания сокета агента.

    Если задан ``tls_client_ca`` — включается mTLS: клиенты БЕЗ сертификата,
    подписанного этим CA, отклоняются на уровне TLS-хендшейка, до того как
    запрос доходит до обработчика (``CERT_REQUIRED`` +
    ``load_verify_locations``).
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    _apply_tls_hardening(context, require_client_cert=bool(tls_client_ca))
    context.load_cert_chain(certfile=tls_cert, keyfile=tls_key)
    if tls_client_ca:
        context.load_verify_locations(cafile=tls_client_ca)
    return context


def peer_identity_from_cert(peer_cert: Optional[dict]) -> Optional[str]:
    """
    Извлекает CN клиентского сертификата из результата
    ``SSLSocket.getpeercert()`` — для аудита (кто предъявил mTLS-сертификат).

    Возвращает ``None``, если сертификата нет или CN не найден (не бросает
    исключений — это вспомогательная функция для логирования, а не для
    решений о доступе).
    """
    if not peer_cert:
        return None
    for rdn in peer_cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


class Authenticator:
    """
    Проверка Bearer-токена запроса со скоупом control/readonly.

    Сравнение — ТОЛЬКО через ``hmac.compare_digest`` (защита от timing-атак
    по времени сравнения строк символ-за-символом).
    """

    def __init__(self, control_token: Optional[str], readonly_token: Optional[str] = None):
        self._control_token = control_token or None
        self._readonly_token = readonly_token or None

    def check(self, presented_token: Optional[str]) -> Optional[str]:
        """
        Возвращает ``SCOPE_CONTROL``/``SCOPE_READONLY`` при совпадении токена,
        иначе ``None``. Пустой/отсутствующий предъявленный токен — всегда
        отказ (fail-closed), без обращения к ``hmac.compare_digest``.
        """
        if not presented_token:
            return None
        if self._control_token and hmac.compare_digest(presented_token, self._control_token):
            return SCOPE_CONTROL
        if self._readonly_token and hmac.compare_digest(presented_token, self._readonly_token):
            return SCOPE_READONLY
        return None

    @staticmethod
    def scope_allows(scope: Optional[str], action: str) -> bool:
        """
        Проверяет, достаточно ли ``scope`` для выполнения ``action``.

        control-скоуп разрешает всё; readonly — только действия из
        ``READ_ACTIONS``; отсутствие скоупа (``None``) не разрешает ничего.
        """
        if scope == SCOPE_CONTROL:
            return True
        if scope == SCOPE_READONLY:
            return action in READ_ACTIONS
        return False


@dataclass
class LockoutTracker:
    """
    Потокобезопасный счётчик неудачных аутентификаций per source-IP.

    После ``max_failures`` неудач в скользящем окне ``window_seconds`` —
    IP считается заблокированным (``is_locked`` → True) до истечения окна с
    момента последней неудачи, входящей в счёт. Рассчитан на использование
    из ``ThreadingHTTPServer`` (несколько потоков-обработчиков одновременно).
    """

    max_failures: int = DEFAULT_LOCKOUT_MAX_FAILURES
    window_seconds: float = DEFAULT_LOCKOUT_WINDOW_SECONDS
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)
    _failures: dict = field(default_factory=dict, repr=False, compare=False)

    def _prune(self, ip: str, now: float) -> list:
        hits = [t for t in self._failures.get(ip, []) if now - t < self.window_seconds]
        self._failures[ip] = hits
        return hits

    def is_locked(self, ip: str) -> bool:
        with self._lock:
            hits = self._prune(ip, time.monotonic())
            return len(hits) >= self.max_failures

    def record_failure(self, ip: str) -> None:
        with self._lock:
            now = time.monotonic()
            hits = self._prune(ip, now)
            hits.append(now)
            self._failures[ip] = hits

    def record_success(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)


def validate_stand_name(name: str) -> bool:
    """Валидация имени стенда из URL — консервативный whitelist символов, кап длины."""
    return bool(name) and bool(_STAND_NAME_RE.match(name))


def clamp_logs_n(raw: str, *, max_n: int = DEFAULT_MAX_LOGS_N) -> int:
    """
    Парсит и капит параметр ``n`` запроса логов.

    Бросает ``ValueError`` на некорректный ввод (не число, отрицательное) —
    вызывающая сторона обязана поймать это и вернуть 400, а не 500/креш.
    """
    n = int(raw)
    if n < 0:
        raise ValueError("n не может быть отрицательным")
    return min(n, max_n)


def validate_content_length(header_value: Optional[str], *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> int:
    """
    Разбирает заголовок ``Content-Length`` и проверяет лимит тела запроса
    ДО его фактического чтения из сокета (input-hardening/DoS-защита).

    Бросает ``ValueError`` на некорректное, отрицательное или превышающее
    лимит значение — вызывающая сторона обязана поймать это и вернуть 400
    клиенту, а не 500/креш процесса.
    """
    raw = header_value if header_value not in (None, "") else "0"
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"некорректный Content-Length: {raw!r}") from exc
    if n < 0:
        raise ValueError("Content-Length не может быть отрицательным")
    if n > max_bytes:
        raise ValueError(f"тело запроса превышает лимит {max_bytes} байт")
    return n
