"""
Health-пробы стенда: жив ли процесс, отвечает ли HTTP, открыт ли TCP-порт
(используется как поверхностная проверка живости БД/Redis).

Все пробы — быстрые и не требуют сторонних зависимостей (только stdlib:
``socket``, ``urllib``, ``concurrent.futures``). Глубокие проверки (реальный
SQL-запрос к БД, PING к Redis по протоколу) — сознательно вынесены в TODO под
опциональный флаг, чтобы базовый health-чек оставался лёгким и не тянул
psycopg2/pyodbc/redis-py в обязательные зависимости ядра.

ВРЕМЯ ОТВЕТА. ``check_stand`` выполняет четыре пробы (process/http/db/redis)
ПАРАЛЛЕЛЬНО: последовательно они складывались в сумму таймаутов, и один
недоступный стенд (firewall с политикой DROP, выключенная ВМ, VPN) держал
дашборд «серым» несколько секунд. Теперь стоимость проверки одного стенда —
максимум из таймаутов, а не их сумма.
"""

from __future__ import annotations

import socket
import ssl
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from standkit.models import HostKind, ProbeState, Stand, StandStatus

# --- таймауты проб ---------------------------------------------------------
#
# Значения СОЗНАТЕЛЬНО жёсткие: дашборд опрашивает все стенды разом, и каждый
# недоступный стенд стоит ровно этот таймаут. На localhost таймаут не бьёт
# вообще (ОС отвечает RST мгновенно), а на стенде за firewall с DROP —
# упирается в него целиком.
#
# Если в вашем контуре стенды отвечают медленнее (перегруженный гипервизор,
# канал с большим RTT, VPN) — поднимите значения здесь: обе константы
# используются как default'ы ``http_ok``/``tcp_open`` и нигде не продублированы.
HTTP_PROBE_TIMEOUT_SEC = 1.5
TCP_PROBE_TIMEOUT_SEC = 0.7

# Сколько проб ОДНОГО стенда выполняется параллельно. Проб ровно четыре
# (process/http/db/redis) — больше воркеров смысла не имеет.
PROBE_MAX_WORKERS = 4


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


def http_ok(
    url: str,
    *,
    timeout: float = HTTP_PROBE_TIMEOUT_SEC,
    verify: bool = True,
) -> bool:
    """
    Проверяет, отвечает ли HTTP(S)-эндпоинт (любой код ответа < 500 считается
    "живым" — стенд может честно отдавать 401/403 до логина, это не авария).

    ``verify=False`` отключает проверку цепочки сертификатов — и ТОЛЬКО для
    ``https://``-адресов. Это осознанное послабление для дев-контуров с
    self-signed сертификатом: без него проба живого стенда за TLS падает в
    SSLCertVerificationError и показывает ложный "down". Контекст строится
    публичным ``ssl.create_default_context()`` с явным отключением проверок,
    а не приватным ``ssl._create_unverified_context()``.

    Сетевые ошибки (отказано в соединении, DNS, таймаут, невалидный
    сертификат при verify=True) → False, без исключений наружу — это намеренно
    проба, а не операция, которая должна падать.
    """
    context = None
    if not verify and url.lower().startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        # Сервер ответил (пусть и ошибкой) — значит, живой.
        return exc.code < 500
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def tcp_open(host: str, port: int, *, timeout: float = TCP_PROBE_TIMEOUT_SEC) -> bool:
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
    Пока — заглушка, всегда возвращающая SKIPPED, чтобы вызывающий код мог
    отличить "проверка не выполнялась" от "проверка провалилась".

    Полноценная проверка (реальный SELECT 1 через psycopg2/pyodbc под опциональный
    флаг) — бэклог, см. docs/ARCHITECTURE.md.
    """
    return ProbeState.SKIPPED


def redis_deep_check(stand: Stand) -> ProbeState:
    """Заглушка (SKIPPED). Полноценный PING к Redis (redis-py, опц. зависимость) — бэклог, см. docs/ARCHITECTURE.md."""
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

    Четыре пробы выполняются ПАРАЛЛЕЛЬНО (см. докстринг модуля): логика каждой
    из них — ровно та же, что была при последовательном обходе, включая
    фолбэки бэкендов хостинга и различение OK/DOWN/UNKNOWN. Исключения проб
    НЕ глушатся молча — они пробрасываются вызывающему так же, как раньше.
    """
    status = StandStatus(name=stand.name)

    def _probe_process() -> ProbeState:
        if stand.host_kind in (HostKind.IIS, HostKind.DOCKER, HostKind.K8S):
            # Проба «процесс» для iis/docker/k8s консультируется с бэкендом
            # хостинга (состояние App Pool / контейнера / деплоймента), а не с
            # pidfile standkit — у этих видов хостинга своего pidfile нет (см.
            # ADR-0001). Это единственная проба, которая может уйти в
            # subprocess (appcmd/docker/kubectl) — именно ради неё
            # параллелизация и даёт основной выигрыш.
            from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

            try:
                # Бэкенд авторитетен: он сам консультируется с appcmd/docker/
                # kubectl и при НЕОПРЕДЕЛЁННОСТИ уже делает собственный
                # TCP-фолбэк. Здесь НЕ добавляем ещё один tcp_open — иначе
                # остановленный IIS-сайт, у которого http.sys держит порт 80,
                # ложно показывался бы «up».
                backend = _hosting.get_backend(stand)
                # Если бэкенд умеет объяснять состояние (IIS: «сайт остановлен»
                # / «пул остановлен» / «порт держит http.sys, 503») — забираем
                # причину в details, чтобы UI показал её вместо голого DOWN.
                # Отдельного вызова is_running при этом НЕ делаем: describe_state
                # уже содержит вердикт, а лишний appcmd на каждый опрос — дорого.
                #
                # Запись в status.details потокобезопасна: проба «процесс»
                # единственная, кто пишет этот ключ, а сам status собирается
                # в вызывающем потоке уже после завершения всех проб.
                describe = getattr(backend, "describe_state", None)
                if describe is not None:
                    detailed = describe(stand)
                    is_up = detailed.running
                    if detailed.reason:
                        status.details["process_reason"] = detailed.reason
                else:
                    is_up = backend.is_running(stand)
            except Exception:
                # Бэкенд вовсе не смог ответить (нет appcmd/docker/kubectl) —
                # осторожный фолбэк на TCP-порт, чтобы не показать ложный DOWN.
                is_up = bool(
                    stand.stand_host
                    and stand.stand_port
                    and tcp_open(stand.stand_host, stand.stand_port)
                )
            return ProbeState.OK if is_up else ProbeState.DOWN
        if pidfile is not None or (stand.stand_host and stand.stand_port):
            is_up = process_running(pidfile, stand.stand_host, stand.stand_port)
            return ProbeState.OK if is_up else ProbeState.DOWN
        return ProbeState.UNKNOWN

    def _probe_http() -> ProbeState:
        if not (stand.stand_host and stand.stand_port):
            return ProbeState.UNKNOWN
        scheme = (stand.stand_scheme or "http").lower()
        url = f"{scheme}://{stand.stand_host}:{stand.stand_port}{http_path}"
        return (
            ProbeState.OK
            if http_ok(url, verify=stand.verify_tls)
            else ProbeState.DOWN
        )

    def _probe_db() -> ProbeState:
        if not (stand.db_host and stand.db_port):
            return ProbeState.UNKNOWN
        if deep_db:
            return db_deep_check(stand)
        return ProbeState.OK if tcp_open(stand.db_host, stand.db_port) else ProbeState.DOWN

    def _probe_redis() -> ProbeState:
        redis_host = stand.extra.get("redis_host")
        redis_port = stand.extra.get("redis_port")
        if not (redis_host and redis_port):
            return ProbeState.UNKNOWN
        if deep_redis:
            return redis_deep_check(stand)
        return ProbeState.OK if tcp_open(redis_host, int(redis_port)) else ProbeState.DOWN

    probes = (
        ("process", _probe_process),
        ("http", _probe_http),
        ("db", _probe_db),
        ("redis", _probe_redis),
    )
    with ThreadPoolExecutor(max_workers=PROBE_MAX_WORKERS, thread_name_prefix="standkit-probe") as pool:
        futures = [(field, pool.submit(fn)) for field, fn in probes]
    for field, future in futures:
        setattr(status, field, future.result())

    # last_deploy — задел на будущее, источник данных пока не определён (бэклог,
    # см. docs/ARCHITECTURE.md).
    status.last_deploy = ProbeState.UNKNOWN

    return status
