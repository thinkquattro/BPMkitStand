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

ПРИЧИНА ОТКАЗА. Проба, вернувшая не-OK, обязана объяснить ПОЧЕМУ: голый
``down`` в дашборде одинаково выглядит и когда порт закрыт, и когда не прошёл
сертификат, и когда проба ушла по ``http://`` в TLS-порт — оператор без чтения
исходников не различит эти случаи (GAP-002, GAP-003). Поэтому каждое
замыкание-проба внутри ``check_stand`` возвращает пару ``(состояние, причина)``,
а причина складывается в ``StandStatus.details["<проба>_reason"]``. Тексты
причин — по-русски, без секретов и без тела ответа: они уходят прямо в UI.
"""

from __future__ import annotations

import http.client
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
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

# --- подсказки оператору ---------------------------------------------------
#
# Вынесены в константы, а не собраны по месту: один и тот же текст выдают
# несколько веток классификатора, и он же — обещание из GAP-002 («оператор
# доходит до решения без чтения исходников»). Формулировка называет ИМЕНА
# ПОЛЕЙ реестра (stand_scheme/verify_tls): подсказка вида «включите TLS»
# бесполезна, если непонятно, где именно его включать.
HINT_TLS_SCHEME = (
    "похоже, стенд за TLS: задайте stand_scheme=https "
    "(и verify_tls=false для self-signed)"
)
HINT_SELF_SIGNED = (
    "self-signed сертификат на дев-контуре — снимите флаг «Проверять сертификат» "
    "(verify_tls=false)"
)

# Максимальная длина текста исключения, которую переносим в причину. Причина
# едет в UI одной строкой, а сообщения OpenSSL/urllib бывают многострочными и
# длинными — режем, оставляя главное.
_REASON_MAX_LEN = 160


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


@dataclass(frozen=True)
class HttpProbeResult:
    """
    Результат HTTP-пробы: вердикт ПЛЮС причина отказа.

    ``reason`` — человекочитаемый диагноз («соединение отклонено…»), пустой при
    ``ok=True``. ``hint`` — необязательное «что с этим делать» (какое поле
    реестра выставить); он отделён от причины, потому что причина — факт, а
    подсказка — гипотеза, и в UI их полезно уметь показать по-разному.

    Класс frozen: результат пробы никто не должен доправлять по дороге в UI.
    """

    ok: bool
    reason: str = ""
    hint: str = ""


def http_probe(
    url: str,
    *,
    timeout: float = HTTP_PROBE_TIMEOUT_SEC,
    verify: bool = True,
) -> HttpProbeResult:
    """
    Проверяет HTTP(S)-эндпоинт и ОБЪЯСНЯЕТ отказ (любой код ответа < 500
    считается "живым" — стенд может честно отдавать 401/403 до логина, это не
    авария).

    ``verify=False`` отключает проверку цепочки сертификатов — и ТОЛЬКО для
    ``https://``-адресов. Это осознанное послабление для дев-контуров с
    self-signed сертификатом: без него проба живого стенда за TLS падает в
    SSLCertVerificationError и показывает ложный "down". Контекст строится
    публичным ``ssl.create_default_context()`` с явным отключением проверок,
    а не приватным ``ssl._create_unverified_context()``.

    Исключений наружу нет вообще: это намеренно проба, а не операция, которая
    должна падать. Но, в отличие от прежнего ``http_ok``, диагноз не теряется
    вместе с исключением, а возвращается в ``reason``/``hint`` (GAP-002).

    ВАЖНО про перехват: помимо ``URLError``/``OSError``/``ValueError`` ловится
    ещё и ``http.client.HTTPException``. Он НЕ наследник OSError, и раньше
    вылетал из пробы наружу — а прилетает он ровно в самом частом сценарии
    GAP-002: запрос по ``http://`` ушёл в TLS-порт, и сервер закрыл соединение
    (``RemoteDisconnected``) либо ответил не-HTTP-строкой (``BadStatusLine``).
    """
    context = None
    if not verify and url.lower().startswith("https://"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:
            if resp.status < 500:
                return HttpProbeResult(ok=True)
            return HttpProbeResult(ok=False, reason=f"сервер ответил {resp.status}")
    except urllib.error.HTTPError as exc:
        # Сервер ответил (пусть и ошибкой) — значит, живой; 5xx считаем отказом.
        if exc.code < 500:
            return HttpProbeResult(ok=True)
        return HttpProbeResult(ok=False, reason=f"сервер ответил {exc.code}")
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,  # включает TimeoutError, ConnectionRefusedError, socket.gaierror
        ValueError,  # urlopen на мусорной схеме: "unknown url type"
    ) as exc:
        return _classify_http_failure(exc, url=url, timeout=timeout)


def http_ok(
    url: str,
    *,
    timeout: float = HTTP_PROBE_TIMEOUT_SEC,
    verify: bool = True,
) -> bool:
    """
    Булев фасад над ``http_probe`` — прежний публичный API пробы, сохранён
    как есть (сигнатура и семантика не менялись): им пользуется код, которому
    нужен только вердикт, без диагноза.
    """
    return http_probe(url, timeout=timeout, verify=verify).ok


def _classify_http_failure(
    exc: BaseException,
    *,
    url: str,
    timeout: float,
) -> HttpProbeResult:
    """
    Превращает исключение пробы в человекочитаемую причину (и, если случай
    узнаваемый, — в подсказку).

    Порядок проверок ЗНАЧИМ, потому что иерархии исключений пересекаются:
    ``SSLCertVerificationError`` — наследник и ``SSLError``, и ``ValueError``;
    ``RemoteDisconnected`` — одновременно ``HTTPException`` и
    ``ConnectionResetError``; ``gaierror``/``ConnectionRefusedError``/
    ``TimeoutError`` — все ``OSError``. Частное всегда идёт раньше общего.
    """
    plain_http = url.lower().startswith("http://")
    cause = _unwrap_url_error(exc)

    # --- TLS ---------------------------------------------------------------
    if isinstance(cause, ssl.SSLCertVerificationError):
        return HttpProbeResult(
            ok=False,
            reason=f"сертификат не прошёл проверку: {_ssl_verify_reason(cause)}",
            hint=HINT_SELF_SIGNED,
        )
    if isinstance(cause, ssl.SSLError):
        return HttpProbeResult(
            ok=False,
            reason=f"ошибка TLS-рукопожатия: {_short(cause)}",
        )

    # --- «ответ не по HTTP» — тот самый признак TLS-порта за http:// -------
    # Проверяется РАНЬШЕ ConnectionResetError: RemoteDisconnected наследует оба
    # типа, и здесь нам важнее протокольная трактовка («сервер говорит не то»),
    # чем транспортная («соединение сбросили»).
    if isinstance(cause, http.client.HTTPException):
        return HttpProbeResult(
            ok=False,
            reason="сервер ответил не по протоколу HTTP",
            hint=HINT_TLS_SCHEME if plain_http else "",
        )
    if isinstance(cause, ConnectionResetError):
        return HttpProbeResult(
            ok=False,
            reason="соединение сброшено сервером",
            hint=HINT_TLS_SCHEME if plain_http else "",
        )

    # --- транспорт ---------------------------------------------------------
    if isinstance(cause, ConnectionRefusedError):
        return HttpProbeResult(
            ok=False,
            reason=f"соединение отклонено — на {_endpoint(url)} никто не слушает",
        )
    if _is_timeout(cause):
        # ``{:g}`` — чтобы 1.5 осталось «1.5 с», а 2.0 не превратилось в «2.0 с».
        return HttpProbeResult(ok=False, reason=f"нет ответа за {timeout:g} с")
    if isinstance(cause, (socket.gaierror, socket.herror)):
        return HttpProbeResult(ok=False, reason="имя хоста не разрешается")

    if isinstance(cause, ValueError):
        # Не сеть, а сам адрес: неизвестная схема, пустой хост и т.п.
        return HttpProbeResult(ok=False, reason=f"некорректный адрес: {_short(cause)}")
    return HttpProbeResult(ok=False, reason=f"сетевая ошибка: {_short(cause)}")


def _unwrap_url_error(exc: BaseException) -> BaseException:
    """
    Достаёт исходную ошибку из ``URLError``: urllib оборачивает в него всё, что
    случилось на этапе соединения (``URLError(ConnectionRefusedError(...))``),
    и без разворачивания классифицировать нечего. Если ``reason`` — строка
    (так бывает у «unknown url type»), возвращаем сам URLError.
    """
    cause = exc
    while isinstance(cause, urllib.error.URLError) and isinstance(cause.reason, BaseException):
        cause = cause.reason
    return cause


def _is_timeout(cause: BaseException) -> bool:
    """
    Таймаут пробы. ``socket.timeout`` с Python 3.10 — псевдоним
    ``TimeoutError``, но URLError иногда несёт таймаут строкой ("timed out"),
    поэтому проверяем и текст.
    """
    if isinstance(cause, TimeoutError):
        return True
    if isinstance(cause, urllib.error.URLError):
        return "timed out" in str(cause.reason).lower()
    return False


def _ssl_verify_reason(exc: BaseException) -> str:
    """
    Краткая причина отказа сертификата: у настоящей ``SSLCertVerificationError``
    из OpenSSL есть ``verify_message`` («self-signed certificate») — он и
    читается лучше полного текста с координатами в ``_ssl.c``.
    """
    for attr in ("verify_message", "reason"):
        value = getattr(exc, attr, None)
        if value:
            return _short(value)
    return _short(exc)


def _short(value: object) -> str:
    """Однострочный и обрезанный текст ошибки — причина едет в UI одной строкой."""
    text = " ".join(str(value).split())
    if len(text) > _REASON_MAX_LEN:
        text = text[: _REASON_MAX_LEN - 1].rstrip() + "…"
    return text


def _split_netloc(netloc: str) -> tuple[str, str]:
    """
    ``(host, port)`` из ``netloc`` — БЕЗ userinfo и КАК НАПИСАНО в URL.

    Почему не ``urlsplit().hostname``: он приводит хост к нижнему регистру и
    снимает квадратные скобки у IPv6 — ``http://[::1]:8080/x`` превращался в
    ``::1:8080``, то есть в строку, которую нельзя ни прочитать, ни
    скопировать в браузер. Здесь хост берётся из ``netloc`` как есть, скобки
    сохраняются, регистр не трогается.

    ``port`` — текст (может быть пустым или мусором); валидацию делает
    вызывающий.
    """
    at = netloc.rfind("@")
    if at >= 0:
        netloc = netloc[at + 1:]  # userinfo в текст причины не тащим — там бывает пароль
    if netloc.startswith("["):  # IPv6-литерал: [::1] / [fe80::1%25eth0]
        end = netloc.find("]")
        if end < 0:
            return netloc, ""
        host = netloc[: end + 1]
        rest = netloc[end + 1:]
        return host, rest[1:] if rest.startswith(":") else ""
    host, sep, port = netloc.rpartition(":")
    if not sep:
        return netloc, ""
    return host, port


def _endpoint(url: str) -> str:
    """``host:port`` из URL (порт — явный либо дефолтный для схемы) для текста причины."""
    parts = urllib.parse.urlsplit(url)
    host, port = _split_netloc(parts.netloc)
    if not port.strip().isdigit():
        # Порта нет или он мусорный — подставляем дефолт схемы, а не роняем
        # формирование причины.
        port = "443" if parts.scheme.lower() == "https" else "80"
    return f"{host}:{port}"


def _safe_url(url: str) -> str:
    """
    URL для показа оператору: без userinfo, query и фрагмента.

    В причину отказа нельзя тащить секреты, а ``https://user:pass@host/?token=…``
    — вполне легальный вход пробы. Остаётся ровно то, что нужно для диагноза:
    схема, хост, порт, путь. Результат обязан оставаться КОПИРУЕМЫМ в адресную
    строку браузера — поэтому скобки IPv6 и регистр хоста сохраняются (см.
    ``_split_netloc``).
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.hostname:
        return url  # не разобрали — отдаём как есть, лучше так, чем пусто
    return f"{parts.scheme}://{_endpoint(url)}{parts.path}"


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

    Каждое замыкание-проба возвращает пару ``(состояние, причина)``. Причина
    (или ``None``, если объяснять нечего) НЕ пишется в ``status.details`` из
    рабочего потока: раньше это делала одна-единственная проба процесса, и
    гонки не было по построению, а сейчас пишущих проб три. Сборка деталей
    вынесена в вызывающий поток, ниже по коду.
    """
    status = StandStatus(name=stand.name)

    def _probe_process() -> tuple[ProbeState, Optional[str]]:
        if stand.host_kind in (HostKind.IIS, HostKind.DOCKER, HostKind.K8S):
            # Проба «процесс» для iis/docker/k8s консультируется с бэкендом
            # хостинга (состояние App Pool / контейнера / деплоймента), а не с
            # pidfile standkit — у этих видов хостинга своего pidfile нет (см.
            # ADR-0001). Это единственная проба, которая может уйти в
            # subprocess (appcmd/docker/kubectl) — именно ради неё
            # параллелизация и даёт основной выигрыш.
            from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

            reason: Optional[str] = None
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
                # Причина НЕ пишется в status.details прямо отсюда: замыкание
                # исполняется в рабочем потоке пула, а details собираются в
                # вызывающем — просто возвращаем текст наверх.
                describe = getattr(backend, "describe_state", None)
                if describe is not None:
                    detailed = describe(stand)
                    is_up = detailed.running
                    if detailed.reason:
                        reason = detailed.reason
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
                reason = None  # диагноз бэкенда недостоверен — он и не ответил
            return (ProbeState.OK if is_up else ProbeState.DOWN), reason
        if pidfile is not None or (stand.stand_host and stand.stand_port):
            is_up = process_running(pidfile, stand.stand_host, stand.stand_port)
            return (ProbeState.OK if is_up else ProbeState.DOWN), None
        return ProbeState.UNKNOWN, None

    def _probe_http() -> tuple[ProbeState, Optional[str]]:
        if not (stand.stand_host and stand.stand_port):
            return ProbeState.UNKNOWN, None
        scheme = (stand.stand_scheme or "http").lower()
        url = f"{scheme}://{stand.stand_host}:{stand.stand_port}{http_path}"
        result = http_probe(url, verify=stand.verify_tls)
        if result.ok:
            return ProbeState.OK, None
        # Фактический URL в тексте обязателен: чаще всего оператор ошибается
        # именно в нём (не та схема, не тот порт, localhost вместо адреса,
        # видимого с хоста агента), а из дашборда URL пробы иначе не виден.
        text = result.reason or "HTTP-проба не прошла"
        if result.hint:
            text = f"{text}. {result.hint}"
        return ProbeState.DOWN, f"{text} (URL: {_safe_url(url)})"

    def _probe_db() -> tuple[ProbeState, Optional[str]]:
        # У БД-пробы объяснять пока нечего: адрес есть — проверяем порт, нет —
        # UNKNOWN. Пару возвращает ради единого протокола сборки деталей.
        if not (stand.db_host and stand.db_port):
            return ProbeState.UNKNOWN, None
        if deep_db:
            return db_deep_check(stand), None
        state = ProbeState.OK if tcp_open(stand.db_host, stand.db_port) else ProbeState.DOWN
        return state, None

    def _probe_redis() -> tuple[ProbeState, Optional[str]]:
        # Поля модели в приоритете, ``extra`` — фолбэк: до 0.8.0 адрес Redis
        # жил только в нетипизированном extra, и реестры, заполненные раньше,
        # обязаны продолжать работать без правок (GAP-003).
        redis_host = str(stand.redis_host or stand.extra.get("redis_host") or "").strip()
        raw_port = stand.redis_port or stand.extra.get("redis_port") or 0
        try:
            redis_port = int(str(raw_port).strip())
        except (TypeError, ValueError):
            # Мусор в порту («шесть тысяч») — это «не задано», а не исключение
            # внутри пробы: проба не имеет права падать, а некорректную запись
            # ловит Stand.validate().
            redis_port = 0
        if not (redis_host and redis_port > 0):
            return ProbeState.UNKNOWN, "адрес Redis не задан в реестре (redis_host/redis_port)"
        if deep_redis:
            return redis_deep_check(stand), None
        if tcp_open(redis_host, redis_port):
            return ProbeState.OK, None
        # Про «с хоста, где выполняется проба» сказано намеренно: Redis в
        # compose без проброса наружу виден агенту и не виден оператору.
        return ProbeState.DOWN, (
            f"Redis не отвечает на {redis_host}:{redis_port} "
            "(адрес проверяется с хоста, где выполняется проба)"
        )

    probes = (
        ("process", _probe_process),
        ("http", _probe_http),
        ("db", _probe_db),
        ("redis", _probe_redis),
    )
    with ThreadPoolExecutor(max_workers=PROBE_MAX_WORKERS, thread_name_prefix="standkit-probe") as pool:
        futures = [(field, pool.submit(fn)) for field, fn in probes]
    # Единственное место записи в status.details — вызывающий поток, уже после
    # завершения всех futures. Пустую причину не пишем: ключ в details означает
    # «есть что сказать оператору», а не «проба отработала».
    for field, future in futures:
        state, reason = future.result()
        setattr(status, field, state)
        if reason:
            status.details[f"{field}_reason"] = reason

    # last_deploy — задел на будущее, источник данных пока не определён (бэклог,
    # см. docs/ARCHITECTURE.md).
    status.last_deploy = ProbeState.UNKNOWN

    return status
