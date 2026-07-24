"""
Минимальный STDLIB-ONLY RESP-клиент для очистки БД Redis стенда (``SELECT`` +
``FLUSHDB``) — используется кнопкой "Очистить Redis" в таблице стендов.

Намеренно НЕ тянет ``redis``/``aioredis`` и т.п. — хаб (как и ядро/агент)
разворачивается без ``pip install`` чего-либо стороннего. Протокол Redis
(RESP) в объёме, нужном для двух команд, тривиален: массив bulk-строк на
запрос, однострочный ``+OK``/``-ERR ...`` на ответ — велосипед оправдан
отсутствием стороннего клиента в рантайме хаба.

Намеренно НЕ разбирает произвольные RESP-ответы (bulk/array/integer) — только
однострочные (``+``/``-``), этого достаточно для ``SELECT``/``FLUSHDB``.

Также здесь живёт ``resolve_redis_from_stand_config`` — BEST-EFFORT резолвер
параметров подключения к Redis ИЗ КОНФИГА САМОГО СТЕНДА (не реестра), на
случай, когда ``extra["redis_db"]`` не задан в реестре BPMkit (частый
случай — реестр вообще не хранит Redis-параметры). Используется как второй
шаг резолва в ``standkit_hub.server._redis_connect_params``: реестр в
приоритете, конфиг стенда — фолбэк.
"""

from __future__ import annotations

import json
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_DEFAULT_TIMEOUT_SEC = 5.0


class RedisClearError(Exception):
    """Ошибка обращения к Redis (недоступен, таймаут, протокольная ошибка)."""


@dataclass
class RedisClearResult:
    """Результат попытки очистки БД Redis — никогда не бросает исключение наружу."""

    ok: bool
    message: str


def _encode_command(*args: str) -> bytes:
    """Кодирует команду в формате RESP (массив bulk-строк) — то, что понимает redis-server."""
    parts = [f"*{len(args)}\r\n".encode("ascii")]
    for arg in args:
        raw = str(arg).encode("utf-8")
        parts.append(f"${len(raw)}\r\n".encode("ascii"))
        parts.append(raw)
        parts.append(b"\r\n")
    return b"".join(parts)


def _read_line(sock) -> bytes:
    """
    Читает одну CRLF-терминированную строку ответа Redis (побайтово — простая,
    не самая быстрая реализация, но ответы ``+OK``/``-ERR ...`` короткие,
    производительность не критична для разовой административной операции).
    """
    buf = b""
    while not buf.endswith(b"\r\n"):
        chunk = sock.recv(1)
        if not chunk:
            raise RedisClearError("соединение с Redis закрыто неожиданно (до получения ответа)")
        buf += chunk
    return buf[:-2]


def flush_db(host: str, port: int, db: int, *, timeout: float = _DEFAULT_TIMEOUT_SEC) -> RedisClearResult:
    """
    Подключается к ``host:port``, выполняет ``SELECT <db>`` затем ``FLUSHDB``.

    Никогда не бросает исключение наружу (сетевые/протокольные ошибки
    заворачиваются в ``RedisClearResult(ok=False, ...)``) — вызывающая сторона
    (HTTP-слой хаба) должна показать понятный текст пользователю, а не 500-ю.
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return RedisClearResult(False, f"не удалось подключиться к Redis {host}:{port}: {exc}")

    try:
        sock.settimeout(timeout)

        sock.sendall(_encode_command("SELECT", str(db)))
        reply = _read_line(sock)
        if not reply.startswith(b"+"):
            return RedisClearResult(
                False, f"Redis отклонил SELECT {db}: {reply.decode('utf-8', errors='replace')}"
            )

        sock.sendall(_encode_command("FLUSHDB"))
        reply = _read_line(sock)
        if not reply.startswith(b"+"):
            return RedisClearResult(
                False, f"Redis отклонил FLUSHDB: {reply.decode('utf-8', errors='replace')}"
            )

        return RedisClearResult(True, f"Redis db={db} очищен ({host}:{port})")
    except (OSError, RedisClearError) as exc:
        return RedisClearResult(False, f"ошибка обращения к Redis {host}:{port}: {exc}")
    finally:
        try:
            sock.close()
        except OSError:
            pass


# --- best-effort резолвер Redis-параметров из конфига стенда (фолбэк реестра) ---
#
# Типичные для BPMSoft/.NET места: <stand_dir>\ConnectionStrings.config
# (классический .NET connectionStrings-XML), <stand_dir>\appsettings.json
# (JSON-конфиг ASP.NET Core), либо любой другой *.config/*.json в КОРНЕ
# каталога стенда (без рекурсии в подпапки — не сканируем весь стенд).
#
# Best-effort: любая ошибка чтения/парсинга конкретного файла — файл просто
# пропускается, никогда не бросаем исключение наружу. Формат Redis-строки у
# BPMSoft заранее не задокументирован нам достоверно — резолвер ищет
# правдоподобные пары ключ=значение (host/port/db/$db) в тексте рядом со
# словом "redis", это эвристика, а не парсер конкретного формата.

_REDIS_CONFIG_FILENAMES = ("ConnectionStrings.config", "appsettings.json")
_REDIS_CONFIG_EXTENSIONS = (".config", ".json")
# Не читаем большие файлы целиком без нужды — конфиги стенда обычно ↓ 200KB,
# 2MB — щедрый потолок на случай нетипичного файла.
_MAX_CONFIG_FILE_BYTES = 2 * 1024 * 1024

_REDIS_KV_RE = re.compile(r"(?i)(\$db|\bdb\b|\bhost\b|\bport\b)\s*=\s*([^;,\s\"']+)")


def _parse_redis_connection_string(text: str) -> Optional[dict]:
    """
    Ищет пары ``host=``/``port=``/``db=``/``$db=`` в произвольном тексте
    (типично — значение атрибута ``connectionString`` или кусок JSON-строки).

    ``db`` обязателен — без него результат бесполезен для очистки (нельзя
    угадывать номер БД), при отсутствии возвращает ``None``. ``host``/``port``
    — опциональны, дефолты ``127.0.0.1``/``6379`` (та же дисциплина, что и у
    резолвера из реестра, см. ``standkit_hub.server._redis_from_registry``).
    """
    if not text:
        return None
    found: dict[str, Any] = {}
    for m in _REDIS_KV_RE.finditer(text):
        key = m.group(1).lower().lstrip("$")
        val = m.group(2).strip().rstrip(";,")
        if key == "db" and "db" not in found:
            try:
                found["db"] = int(val)
            except ValueError:
                continue
        elif key == "host" and "host" not in found:
            found["host"] = val
        elif key == "port" and "port" not in found:
            try:
                found["port"] = int(val)
            except ValueError:
                continue
    if "db" not in found:
        return None
    return {"host": found.get("host", "127.0.0.1"), "port": found.get("port", 6379), "db": found["db"]}


def _extract_connection_string_from_config_xml(text: str) -> Optional[str]:
    """
    Ищет ``<add name="..." connectionString="..."/>`` записи, чьё имя/тег
    содержит "redis" (регистронезависимо) — стандартный вид ``<connectionStrings>``
    секции .NET-конфига (``ConnectionStrings.config``/``Web.config``/``App.config``).
    """
    for m in re.finditer(r"<add\b[^>]*/?>", text, re.IGNORECASE):
        tag = m.group(0)
        if "redis" not in tag.lower():
            continue
        cs_m = re.search(r'connectionString\s*=\s*"([^"]*)"', tag, re.IGNORECASE)
        if cs_m and cs_m.group(1):
            return cs_m.group(1)
    return None


def _windowed_redis_search(text: str) -> Optional[dict]:
    """Запасной вариант для произвольного текста: окно ~350 символов вокруг первого упоминания "redis"."""
    lower = text.lower()
    idx = lower.find("redis")
    if idx == -1:
        return None
    window = text[max(0, idx - 50): idx + 300]
    return _parse_redis_connection_string(window)


def _extract_host_port_db_from_json_dict(d: dict) -> Optional[dict]:
    """
    Достаёт host/port/db из JSON-объекта, ПОХОЖЕГО на настройки redis
    (напр. ``{"Host": "127.0.0.1", "Port": 6379, "Db": 2}``), либо, если это
    обёртка над строкой подключения (``{"ConnectionString": "host=..;db=.."}``),
    делегирует в ``_parse_redis_connection_string``.
    """
    lower = {k.lower(): v for k, v in d.items() if isinstance(k, str)}
    db_val = None
    for key in ("db", "database", "number", "redisdb", "redis_db"):
        if key in lower:
            try:
                db_val = int(lower[key])
                break
            except (TypeError, ValueError):
                continue
    if db_val is None:
        for key in ("connectionstring", "connection_string", "value", "url"):
            if key in lower and isinstance(lower[key], str):
                parsed = _parse_redis_connection_string(lower[key])
                if parsed:
                    return parsed
        return None
    host = lower.get("host") or lower.get("hostname") or "127.0.0.1"
    port_raw = lower.get("port")
    try:
        port = int(port_raw) if port_raw is not None else 6379
    except (TypeError, ValueError):
        port = 6379
    return {"host": str(host), "port": port, "db": db_val}


def _find_redis_in_json(data: Any) -> Optional[dict]:
    """
    Рекурсивно ищет "redis"-ключ (регистронезависимо) в разобранном JSON
    (dict/list произвольной вложенности — ``appsettings.json`` обычно
    группирует секции, поэтому Redis может лежать не в корне) и пытается
    извлечь host/port/db из значения (строка-подключение ИЛИ объект).
    """
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and "redis" in key.lower():
                if isinstance(value, str):
                    parsed = _parse_redis_connection_string(value)
                    if parsed:
                        return parsed
                elif isinstance(value, dict):
                    parsed = _extract_host_port_db_from_json_dict(value)
                    if parsed:
                        return parsed
        for value in data.values():
            result = _find_redis_in_json(value)
            if result:
                return result
    elif isinstance(data, list):
        for item in data:
            result = _find_redis_in_json(item)
            if result:
                return result
    return None


def _try_extract_redis_from_file(path: Path) -> Optional[dict]:
    """Best-effort извлечение Redis-параметров из ОДНОГО файла. Никогда не бросает исключение."""
    try:
        if path.stat().st_size > _MAX_CONFIG_FILE_BYTES:
            return None
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return None

    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError, RecursionError):
            return None
        return _find_redis_in_json(data)

    # .config и прочее текстовое — сначала штатный вид <add .../>, иначе
    # запасной поиск "redis" по окну текста.
    cs = _extract_connection_string_from_config_xml(text)
    if cs:
        parsed = _parse_redis_connection_string(cs)
        if parsed:
            return parsed
    return _windowed_redis_search(text)


def resolve_redis_from_stand_config(stand_dir: Optional[str]) -> Optional[dict]:
    """
    Best-effort резолвер параметров подключения к Redis ИЗ КОНФИГА СТЕНДА —
    фолбэк, когда реестр BPMkit не содержит ``extra["redis_db"]`` (частый
    случай, см. docstring модуля). Возвращает ``{"host": str, "port": int,
    "db": int}`` либо ``None``, если ничего похожего на redis-подключение с
    номером БД не нашлось (или ``stand_dir`` пуст/не существует).

    Порядок поиска в ``stand_dir`` (без рекурсии в подпапки):
      1. ``ConnectionStrings.config``, ``appsettings.json`` (типичные для
         BPMSoft/.NET имена) — в этом порядке;
      2. остальные ``*.config``/``*.json`` файлы в корне каталога стенда.

    Никогда не бросает исключение — отсутствующий/битый/нечитаемый файл
    просто пропускается (следующий кандидат).
    """
    if not stand_dir:
        return None
    root = Path(stand_dir)
    if not root.is_dir():
        return None

    candidates: list[Path] = []
    for name in _REDIS_CONFIG_FILENAMES:
        p = root / name
        if p.is_file():
            candidates.append(p)

    try:
        entries = sorted(root.iterdir())
    except OSError:
        entries = []
    for p in entries:
        try:
            if not p.is_file() or p in candidates:
                continue
        except OSError:
            continue
        if p.suffix.lower() in _REDIS_CONFIG_EXTENSIONS:
            candidates.append(p)

    for path in candidates:
        result = _try_extract_redis_from_file(path)
        if result:
            return result
    return None
