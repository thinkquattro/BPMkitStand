"""
Security-примитивы веб-хаба standkit_hub: сессионный токен, извлечение
предъявленного токена (cookie/заголовок), anti-CSRF/cross-origin проверка
мутаций, санитайзинг путей статики.

По образцу ``standkit_agent/security.py`` (fail-closed bind-проверка,
``hmac.compare_digest`` вместо ``==``), но адаптировано под браузерный клиент:
сессия — HttpOnly-cookie, а мутации (POST/DELETE) дополнительно требуют
дублирующий заголовок ``X-Standkit-Token`` (double-submit паттерн: сторонний
сайт может заставить браузер отправить cookie, но не может ни прочитать её,
ни подделать наш кастомный заголовок для cross-origin запроса) плюс сверку
``Origin``/``Referer`` с адресом самого хаба.

Хаб — та же RCE-поверхность, что и headless-агент (управляет процессами
через ``standkit.lifecycle``/``standkit_hub.agent_control``), поэтому
fail-closed bind-проверка (``validate_bind_security``/``InsecureBindError``/
``is_loopback_host``) переиспользуется напрямую из ``standkit_agent.security``
— дублировать эту логику здесь было бы источником рассинхронизации.

STDLIB-ONLY: ``hmac``, ``re``, ``secrets``, ``urllib.parse``.
"""

from __future__ import annotations

import hmac
import re
import secrets as _secrets_mod
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

# Реэкспорт — единственный источник fail-closed bind-логики (не дублируем).
from standkit_agent.security import (  # noqa: F401
    InsecureBindError,
    is_loopback_host,
    validate_bind_security,
)

SESSION_COOKIE_NAME = "standkit_session"
TOKEN_HEADER_NAME = "X-Standkit-Token"
TOKEN_QUERY_PARAM = "t"

DEFAULT_MAX_BODY_BYTES = 256 * 1024  # 256 КБ — лимит тела JSON-запроса хаба
DEFAULT_MAX_LOGS_N = 10_000

_COOKIE_ATTR_RE = re.compile(r"^" + re.escape(SESSION_COOKIE_NAME) + r"=([^;]*)")

# То же множество символов, что допускает реестр/агент — не дублируем правило
# отдельным regex'ом с шансом разъехаться, просто переиспользуем то же имя.
_STAND_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SECRET_REF_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")


def generate_session_token() -> str:
    """Криптостойкий сессионный токен хаба (генерируется один раз при старте процесса)."""
    return _secrets_mod.token_urlsafe(32)


def extract_cookie_token(cookie_header: str) -> Optional[str]:
    """Достаёт значение cookie ``standkit_session`` из заголовка ``Cookie`` (без внешних зависимостей)."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        part = part.strip()
        m = _COOKIE_ATTR_RE.match(part)
        if m:
            return m.group(1)
    return None


def tokens_match(presented: Optional[str], expected: str) -> bool:
    """Сравнение токенов ТОЛЬКО через ``hmac.compare_digest`` (защита от timing-атак)."""
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


def is_local_origin(value: Optional[str], *, expected_port: int) -> bool:
    """
    True, если ``Origin``/``Referer`` указывает на loopback-хост И порт,
    совпадающий с портом самого хаба.

    Anti-CSRF проверка на мутациях (в дополнение к double-submit токену) —
    сторонний сайт не может ни подделать заголовок ``Origin`` браузера, ни
    прочитать наш HttpOnly-cookie, чтобы продублировать его в заголовок.
    """
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if not is_loopback_host(parsed.hostname or ""):
        return False
    return parsed.port == expected_port


def sanitize_static_path(web_dir: Path, rel_path: str) -> Optional[Path]:
    """
    Резолвит путь внутри ``web_dir`` для статики, отклоняя traversal
    (``..``, абсолютные пути в компонентах). Возвращает ``None``, если путь
    небезопасен, не существует или указывает не на файл.
    """
    if not rel_path or rel_path.startswith("/") or ".." in Path(rel_path).parts:
        return None
    web_dir_resolved = web_dir.resolve()
    candidate = (web_dir / rel_path).resolve()
    try:
        candidate.relative_to(web_dir_resolved)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def validate_stand_name(name: str) -> bool:
    """Валидация имени стенда из URL — тот же whitelist, что и у агента."""
    return bool(name) and bool(_STAND_NAME_RE.match(name))


def validate_secret_ref(ref: str) -> bool:
    """Валидация ссылки на секрет из URL (буквы/цифры/``_.:-``, кап длины)."""
    return bool(ref) and bool(_SECRET_REF_RE.match(ref))


def clamp_logs_n(raw: str, *, max_n: int = DEFAULT_MAX_LOGS_N) -> int:
    """Парсит и капит параметр ``n`` запроса логов. Бросает ``ValueError`` на некорректный ввод."""
    n = int(raw)
    if n < 0:
        raise ValueError("n не может быть отрицательным")
    return min(n, max_n)


def validate_content_length(header_value: Optional[str], *, max_bytes: int = DEFAULT_MAX_BODY_BYTES) -> int:
    """Разбирает ``Content-Length`` и проверяет лимит тела запроса ДО его фактического чтения из сокета."""
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
