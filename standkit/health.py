"""
Health-пробы стенда: жив ли процесс, отвечает ли HTTP, открыт ли TCP-порт
(используется как поверхностная проверка живости БД/Redis).

Все пробы — быстрые и не требуют сторонних зависимостей (только stdlib:
``socket``, ``urllib``). Глубокие проверки (реальный SQL-запрос к БД, PING к
Redis по протоколу) — сознательно вынесены в TODO под опциональный флаг,
чтобы базовый health-чек оставался лёгким и не тянул psycopg2/pyodbc/redis-py
в обязательные зависимости ядра.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

from standkit.models import ProbeState, Stand, StandStatus


def process_alive(pidfile: Path) -> bool:
    """
    Проверяет, жив ли процесс, чей pid записан в ``pidfile``.

    Если файла нет или он не читается — считается, что процесс не запущен.
    Импортирует standkit.platform лениво, чтобы health.py можно было
    использовать и для проверки "чужих" процессов без завязки на lifecycle.
    """
    from standkit import platform as _platform  # локальный импорт — избегаем цикла

    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return False
    return _platform.is_alive(pid)


def process_running(
    pidfile: Optional[Path],
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> bool:
    """
    Считает процесс стенда "живым", если ЛИБО жив pidfile standkit (стенд
    поднят самим ядром), ЛИБО слушается TCP-порт стенда (стенд поднят извне
    standkit — вручную, через IIS/systemd/сторонний скрипт и т.п.).

    Это расширение process_alive: тот проверяет только pidfile, этот —
    комбинирует обе приметы живости, потому что реальные стенды часто
    поднимаются не через lifecycle.start().
    """
    if pidfile is not None and process_alive(pidfile):
        return True
    if host and port:
        return tcp_open(host, port)
    return False


def http_ok(url: str, *, timeout: float = 3.0) -> bool:
    """
    Проверяет, отвечает ли HTTP(S)-эндпоинт (любой код ответа < 500 считается
    "живым" — стенд может честно отдавать 401/403 до логина, это не авария).

    Сетевые ошибки (отказано в соединении, DNS, таймаут) → False, без исключений
    наружу — это намеренно проба, а не операция, которая должна падать.
    """
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        # Сервер ответил (пусть и ошибкой) — значит, живой.
        return exc.code < 500
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def tcp_open(host: str, port: int, *, timeout: float = 2.0) -> bool:
    """
    Проверяет, открыт ли TCP-порт (используется как поверхностная liveness-проба
    БД/Redis — не подменяет полноценный запрос к сервису).
    """
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def db_deep_check(stand: Stand) -> ProbeState:
    """
    TODO(следующая итерация): полноценная проверка БД (реальный SELECT 1 через
    psycopg2 для postgres / pyodbc для mssql). Требует опциональных
    зависимостей, которые НЕ должны стать обязательными для ядра — включать
    только по явному флагу вызывающей стороны.

    Пока — заглушка, всегда возвращающая SKIPPED, чтобы вызывающий код мог
    отличить "проверка не выполнялась" от "проверка провалилась".
    """
    return ProbeState.SKIPPED


def redis_deep_check(stand: Stand) -> ProbeState:
    """TODO(следующая итерация): полноценный PING к Redis (redis-py, опциональная зависимость)."""
    return ProbeState.SKIPPED


def check_stand(
    stand: Stand,
    *,
    pidfile: Optional[Path] = None,
    http_path: str = "/",
    deep_db: bool = False,
    deep_redis: bool = False,
) -> StandStatus:
    """
    Собирает сводный StandStatus по всем доступным быстрым пробам.

    ``pidfile`` — если не передан, процесс-проба пропускается (UNKNOWN) —
    вызывающая сторона (lifecycle) знает свой путь к pidfile лучше, чем этот
    модуль по умолчанию.
    """
    status = StandStatus(name=stand.name)

    if pidfile is not None or (stand.stand_host and stand.stand_port):
        is_up = process_running(pidfile, stand.stand_host, stand.stand_port)
        status.process = ProbeState.OK if is_up else ProbeState.DOWN
    else:
        status.process = ProbeState.UNKNOWN

    if stand.stand_host and stand.stand_port:
        url = f"http://{stand.stand_host}:{stand.stand_port}{http_path}"
        status.http = ProbeState.OK if http_ok(url) else ProbeState.DOWN
    else:
        status.http = ProbeState.UNKNOWN

    if stand.db_host and stand.db_port:
        status.db = ProbeState.OK if tcp_open(stand.db_host, stand.db_port) else ProbeState.DOWN
        if deep_db:
            status.db = db_deep_check(stand)
    else:
        status.db = ProbeState.UNKNOWN

    redis_host = stand.extra.get("redis_host")
    redis_port = stand.extra.get("redis_port")
    if redis_host and redis_port:
        status.redis = ProbeState.OK if tcp_open(redis_host, int(redis_port)) else ProbeState.DOWN
        if deep_redis:
            status.redis = redis_deep_check(stand)
    else:
        status.redis = ProbeState.UNKNOWN

    # TODO: last_deploy — задел на будущее, источник данных пока не определён
    # (кандидат — метаданные из BPMkit deploy_status/deploy_verify).
    status.last_deploy = ProbeState.UNKNOWN

    return status
