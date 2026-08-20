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

ПРОИЗВОДИТЕЛЬНОСТЬ (почему хаб больше не «прокси в сеть на каждый GET»):
  - фоновый поток (``standkit_hub.poller.StatusPoller``) опрашивает стенды с
    периодом ``refresh_interval_sec`` и держит снапшот в памяти; ``GET
    /api/stands`` отдаёт готовый снапшот с отметкой времени, а не лезет в сеть;
  - ``GET /api/stands?probe=0`` отдаёт слепок реестра БЕЗ единой пробы —
    фронт рисует таблицу мгновенно и дорисовывает статусы вторым запросом;
  - ``GET /api/events`` — тот же снапшот push'ем через SSE (text/event-stream),
    чтобы не опрашивать хаб таймером;
  - конфиг и реестр кэшируются в памяти с инвалидацией по mtime файла
    (раньше оба JSON перечитывались с диска на КАЖДЫЙ запрос);
  - статика (``/static/*``) отдаётся с ``Cache-Control``/``ETag``/
    ``Last-Modified`` и умеет отвечать ``304`` — раньше браузер качал
    style.css/app.js/логотипы заново при каждой загрузке страницы.
"""

from __future__ import annotations

import errno
import json
import mimetypes
import re
import threading
import time
from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from standkit import __version__ as _standkit_version
from standkit import lifecycle as _lifecycle
from standkit import logs as _logs
from standkit.hosting import HostingError
from standkit.lifecycle import AdoptionRequired, AdoptionUnavailable, LifecycleError
from standkit.models import HostKind, Stand, Transport
from standkit.registry import Registry, RegistryError, default_registry_path
from standkit.secrets import SecretError, delete_secret, has_secret, set_secret
from standkit_hub import logs_browser
from standkit_hub import redis_min
from standkit_hub import security as _security
from standkit_hub.agent_control import AgentControlError, AgentController
from standkit_hub.client import FederatedClient, RemoteCallError
from standkit_hub.config import HubConfig
from standkit_hub.poller import StatusPoller, StatusSnapshot
from standkit_hub.shortcut import install_desktop_shortcut, uninstall_desktop_shortcut

# --- ТОЧКА РАСШИРЕНИЯ РЕДАКЦИИ: канал доставки обновлений издателя ------------
#
# Редакция определяется НАЛИЧИЕМ пакета ``standkit_companion``, и ничем больше:
# ни флага в конфиге, ни имени файла, ни ключа лицензии в ядре. MIT-ядро обязано
# собираться, запускаться и проходить тесты без этого пакета — поэтому импорт
# мягкий, а весь код канала спрятан за проверкой ``companion_available()``.
#
# Ловится ``Exception``, а НЕ только ``ImportError``: пакет платной редакции
# ставится отдельно и может оказаться битым (несовместимая версия, обрезанный
# файл, падение на импорте из-за окружения). Такая установка не имеет права
# уронить весь хаб — управление стендами обязано продолжать работать, а канал
# честно покажется недоступным (``edition: "free"``). ``ImportError`` этот класс
# отказов не покрывает: сломанный модуль падает чем угодно, вплоть до
# ``SyntaxError``/``AttributeError`` на уровне модуля.
try:
    import standkit_companion as _companion
    from standkit_companion import runner as _companion_runner
except Exception:  # noqa: BLE001 - см. комментарий выше: битая платная редакция
    _companion = None
    _companion_runner = None

# Порт хаба по умолчанию. ФИКСИРОВАННЫЙ осознанно: раньше хаб стартовал на
# эфемерном порту, а origin (схема+хост+ПОРТ) — ключ браузерных хранилищ и
# HTTP-кэша. Каждый запуск давал новый origin: пустой localStorage («тема не
# запоминается»), холодный кэш статики и протухшая закладка. Если порт занят —
# см. ``bind_hub_server``: откат на эфемерный с явным сообщением, а не падение.
DEFAULT_HUB_PORT = 8770

# Заголовок-опознаватель хаба, который ставится на КАЖДЫЙ ответ (см.
# ``Handler.end_headers``) и читается ``probe_hub_instance``.
HUB_IDENTITY_HEADER = "X-Standkit-Hub"

_STAND_ACTION_RE = re.compile(
    r"^/api/stand/(?P<name>[^/]+)/(?P<action>status|logs|start|stop|restart|adopt|state|redis-clear)$"
)
# Единственный оставшийся суб-путь "логов" — открытие папки логов в
# проводнике ОС (POST). Просмотр отдельных файлов лога из UI убран (см.
# CLAUDE.md фидбэк: панель "Текущее состояние" показывает только консоль
# выбранного стенда, без выбора файла) — соответствующие эндпоинты
# /logs/list и /logs/file удалены вместе с фронтом, который их использовал.
_STAND_LOGS_SUB_RE = re.compile(r"^/api/stand/(?P<name>[^/]+)/logs/(?P<sub>open-folder)$")
_SECRET_RE = re.compile(r"^/api/secret/(?P<ref>[^/]+)$")

# Регистрация УЖЕ существующего стенда в общем реестре (кнопка "Зарегистрировать
# стенд" на дашборде) — отдельный точный путь, НЕ пересекается с _STAND_ACTION_RE
# (тот требует .../<action> после имени стенда).
_STAND_REGISTER_PATH = "/api/stand/register"

# Автоопределение IIS-сайта по каталогу/порту для кнопки «Определить
# автоматически» в форме регистрации. Работает по ЕЩЁ НЕ зарегистрированному
# стенду (данные приходят телом запроса), поэтому это отдельный путь, а не
# суб-действие /api/stand/<name>/*.
_IIS_DETECT_PATH = "/api/iis/detect"

# Поля формы регистрации, которые сервер готов принять и записать в Stand —
# белый список (всё, чего нет в этом множестве, в реестр не попадает, даже
# если клиент его пришлёт). Пароли/секреты сюда осознанно НЕ входят — только
# secret_ref_* (см. _api_stand_register).
_REGISTER_ALLOWED_FIELDS = {
    "transport",
    "host_kind",
    "stand_dir",
    "logs_dir",
    "stand_host",
    "stand_port",
    # Схема пробы/ссылки «Открыть стенд» и проверка сертификата. Появились в
    # модели в 0.7.0, но в белый список тогда сознательно не попали, и стенд за
    # TLS настраивался только правкой projects.json руками (GAP-001).
    "stand_scheme",
    "verify_tls",
    "db_type",
    "db_host",
    "db_port",
    "db_name",
    # Адрес Redis: до 0.8.0 жил нетипизированными ключами в extra, в форме его
    # не было вовсе, и «не настроено» было неотличимо от «не поддержано» (GAP-003).
    "redis_host",
    "redis_port",
    "agent_url",
    "agent_secret_ref",
    # Доверие к сертификату АГЕНТА (канал «хаб → агент»), не путать с
    # stand_scheme/verify_tls выше — те про пробу самого стенда. Без этой пары
    # агент с самоподписанным сертификатом подключался только через
    # SSL_CERT_FILE в окружении процесса хаба (GAP-008).
    "agent_ca",
    "agent_verify_tls",
    "iis_site",
    "iis_app_pool",
    "docker_container",
    "docker_compose_file",
    "docker_compose_service",
    "k8s_namespace",
    "k8s_deployment",
    "description",
    "customer",
}

# Поля, которые сервер ЯВНО отклоняет с понятной ошибкой (а не молча
# игнорирует), если клиент вдруг их пришлёт — защита от того, чтобы кто-то
# принял отсутствие ошибки за "пароль сохранён". Секреты — только через
# отдельный secretstore/secret_ref_*, никогда открытым текстом в реестре.
_REGISTER_FORBIDDEN_FIELDS = {"db_password", "admin_password", "password", "secret", "secret_value"}

# Человекочитаемые подписи источника логов для сообщений "лог недоступен".
_LOG_SOURCE_LABELS = {"stand": "Стенд", "bpmkit": "BPMkit"}

_DEFAULT_WEB_DIR = Path(__file__).parent / "web"

# --- HTTP-кэш статики --------------------------------------------------------
#
# Логика/стили правятся при каждом обновлении пакета, поэтому им — "no-cache":
# браузер ХРАНИТ файл, но всегда переспрашивает и в норме получает 304 без
# тела. Картинки/шрифты меняются редко — им можно короткий max-age.
# index.html не кэшируется вовсе: в нём сессионный токен (см. _serve_index).
_STATIC_LONG_CACHE_SUFFIXES = {".svg", ".png", ".ico", ".jpg", ".jpeg", ".gif", ".webp", ".woff", ".woff2"}
_STATIC_CACHE_CONTROL_ASSET = "public, max-age=3600, must-revalidate"
_STATIC_CACHE_CONTROL_CODE = "no-cache"

# Максимальное ожидание нового снапшота в SSE-цикле. По его истечении
# отправляется heartbeat-комментарий — он же единственный способ заметить,
# что клиент ушёл (write в закрытый сокет упадёт, поток освободится).
_SSE_WAIT_SEC = 15.0


# mimetypes ничего не знает про .webmanifest, а браузер обязан получить именно
# ``application/manifest+json``, иначе кнопка «Установить приложение» не
# появляется. Регистрируем один раз на импорт модуля.
mimetypes.add_type("application/manifest+json", ".webmanifest")

# Значения ``?view=`` для корневой страницы. ``full`` — обычный дашборд,
# ``compact`` — узкое окно-виджет: только имена стендов, точки состояния и
# старт/стоп (см. CSS-правила [data-view="compact"]). Отдельной технологии за
# этим нет — тот же UI, другой набор CSS-правил.
HUB_VIEWS = ("full", "compact")
_DEFAULT_VIEW = "full"


def normalize_view(value: object) -> str:
    """Приводит ``?view=`` к одному из ``HUB_VIEWS``; мусор молча даёт ``full``."""
    if isinstance(value, str) and value.strip().lower() in HUB_VIEWS:
        return value.strip().lower()
    return _DEFAULT_VIEW


def _static_cache_control(target: Path) -> str:
    if target.suffix.lower() in _STATIC_LONG_CACHE_SUFFIXES:
        return _STATIC_CACHE_CONTROL_ASSET
    return _STATIC_CACHE_CONTROL_CODE


def _static_etag(size: int, mtime_ns: int) -> str:
    """Слабый по смыслу, но синтаксически сильный ETag: размер + mtime файла."""
    return f'"{size:x}-{mtime_ns:x}"'


def _etag_matches(header_value: str, etag: str) -> bool:
    """Разбирает ``If-None-Match`` (список тегов, возможен ``*`` и префикс ``W/``)."""
    for raw in header_value.split(","):
        candidate = raw.strip()
        if not candidate:
            continue
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == etag:
            return True
    return False


def _is_not_modified(headers, *, etag: str, mtime: float) -> bool:
    """
    Нужно ли ответить ``304 Not Modified``.

    Приоритет за ``If-None-Match`` (RFC 9110): если клиент предъявил ETag и он
    не совпал — отдаём тело, ``If-Modified-Since`` в этом случае игнорируется.
    """
    inm = headers.get("If-None-Match")
    if inm:
        return _etag_matches(inm, etag)
    ims = headers.get("If-Modified-Since")
    if not ims:
        return False
    try:
        since = parsedate_to_datetime(ims)
    except (TypeError, ValueError):
        return False
    if since is None:
        return False
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    # Last-Modified отдаётся с секундной точностью — сравниваем так же,
    # иначе файл с дробным mtime всегда выглядел бы «изменённым».
    return int(mtime) <= int(since.timestamp())


def _redis_from_registry(stand: Stand) -> Optional[dict]:
    """
    Резолвит ``{"host", "port", "db"}`` ТОЛЬКО из реестра/``extra`` (без
    чтения конфига стенда) — первый шаг резолва, см. ``_redis_connect_params``.

    ``db`` ищется по нескольким правдоподобным ключам (плоские
    ``extra["redis_db"]``/``extra["redis_number"]``, либо вложенный
    ``extra["redis"]["db"/"number"/"redis_db"]``) — реестр BPMkit исторически
    не имеет единой строгой схемы для Redis-параметров. Возвращает ``None``,
    если ``db`` в реестре не найден (это ожидаемо в большинстве случаев —
    реестр обычно вообще не хранит Redis-параметры, они лежат в конфиге
    самого стенда, см. ``standkit_hub.redis_min.resolve_redis_from_stand_config``).
    """
    nested = stand.extra.get("redis")
    nested = nested if isinstance(nested, dict) else {}

    db: Optional[int] = None
    for key in ("redis_db", "redis_number"):
        val = stand.extra.get(key)
        if val is not None:
            try:
                db = int(val)
                break
            except (TypeError, ValueError):
                continue
    if db is None:
        for key in ("db", "number", "redis_db"):
            val = nested.get(key)
            if val is not None:
                try:
                    db = int(val)
                    break
                except (TypeError, ValueError):
                    continue
    if db is None:
        return None

    # Поля модели в приоритете, ``extra`` — фолбэк для реестров, заполненных до
    # 0.8.0 (тогда redis_host/redis_port были нетипизированными ключами extra,
    # см. GAP-003). Тот же порядок, что в standkit.health::_probe_redis.
    host = stand.redis_host or stand.extra.get("redis_host") or nested.get("host") or "127.0.0.1"
    port_raw = stand.redis_port or None
    if port_raw is None:
        port_raw = stand.extra.get("redis_port")
    if port_raw is None:
        port_raw = nested.get("port")
    try:
        port = int(port_raw) if port_raw is not None else 6379
    except (TypeError, ValueError):
        port = 6379

    return {"host": host, "port": port, "db": db}


_REDIS_MISSING_DB_MESSAGE = (
    "redis не настроен у стенда — не найден ни redis_db в реестре, ни "
    "redis-подключение в конфиге стенда"
)


def _redis_connect_params(stand: Stand) -> tuple[str, int, Optional[int]]:
    """
    Резолвит параметры подключения к Redis стенда для кнопки "Очистить Redis":
    ``host`` (дефолт ``127.0.0.1``), ``port`` (дефолт ``6379``), ``db``.

    Порядок резолва (``db`` — ОБЯЗАТЕЛЕН для очистки, см.
    ``_api_stand_redis_clear``; номер БД НИКОГДА не угадывается):
      1. реестр/``extra`` (см. ``_redis_from_registry``);
      2. best-effort резолвер по конфигу самого стенда (см.
         ``standkit_hub.redis_min.resolve_redis_from_stand_config`` —
         ``ConnectionStrings.config``/``appsettings.json``/прочие
         ``*.config``/``*.json`` в корне ``stand_dir``);
      3. ``None`` — вызывающая сторона обязана отдать 400 с понятным текстом.
    """
    from_registry = _redis_from_registry(stand)
    if from_registry is not None:
        return from_registry["host"], from_registry["port"], from_registry["db"]

    from_config = redis_min.resolve_redis_from_stand_config(stand.stand_dir)
    if from_config is not None:
        return from_config["host"], from_config["port"], from_config["db"]

    return "127.0.0.1", 6379, None


def _redis_number(stand: Stand) -> Optional[int]:
    """
    Номер БД Redis стенда — реестр в приоритете, иначе best-effort резолв из
    конфига стенда (см. ``_redis_connect_params``). ``None``, если не найден
    нигде — используется UI (``/api/stands``), чтобы дизейблить кнопку
    "Очистить Redis" только когда db реально нигде не найден.
    """
    _, _, db = _redis_connect_params(stand)
    return db


def _is_external(stand: Stand, process_state: str) -> bool:
    """
    True, если стенд ЖИВ, но поднят вне диспетчера — процесс отвечает (проба
    ``process`` = ok, она смотрит и на TCP-порт), а живого pidfile у диспетчера
    нет (см. ``standkit.lifecycle.is_managed``).

    Только для локальных kestrel-стендов: у iis/docker/k8s объект управления
    глобальный (сайт/контейнер/деплоймент), понятия «поднят не нами» там нет —
    ``docker stop`` работает независимо от того, кто запускал контейнер.
    Ошибки чтения pidfile трактуем как «не внешний»: бейдж — подсказка, он не
    имеет права ронять список стендов.
    """
    if stand.transport != Transport.LOCAL or stand.host_kind != HostKind.KESTREL:
        return False
    if process_state != "ok":
        return False
    try:
        return not _lifecycle.is_managed(stand)
    except OSError:
        return False


# --- кэш конфига и реестра ---------------------------------------------------
#
# Оба JSON'а раньше перечитывались с диска на КАЖДЫЙ запрос (а на дашборде их
# несколько в секунду). Кэшируем в памяти, инвалидируя по «отпечатку» файла
# (mtime в наносекундах + размер): файл правят и снаружи — руками, MCP BPMkit,
# другим экземпляром хаба — поэтому кэш обязан замечать чужие изменения сам, а
# не только собственные записи.

_CACHE_LOCK = threading.Lock()
_CONFIG_CACHE: dict[str, tuple[Optional[tuple[int, int]], HubConfig]] = {}
_REGISTRY_CACHE: dict[str, tuple[Optional[tuple[int, int]], Registry]] = {}


def _file_stamp(path: Path) -> Optional[tuple[int, int]]:
    """Отпечаток файла для инвалидации кэша. ``None`` — файла нет (это норма)."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def invalidate_caches() -> None:
    """
    Сбрасывает кэш конфига и реестра.

    Вызывается ПОСЛЕ собственных записей хаба (сохранение настроек,
    регистрация стенда): на грубом mtime (FAT/сетевые диски) отпечаток файла
    мог не измениться, и следующий GET отдал бы устаревшие данные.
    """
    with _CACHE_LOCK:
        _CONFIG_CACHE.clear()
        _REGISTRY_CACHE.clear()


def _load_config(config_path: Path) -> HubConfig:
    key = str(config_path)
    stamp = _file_stamp(config_path)
    with _CACHE_LOCK:
        cached = _CONFIG_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    config = HubConfig.load(config_path)
    with _CACHE_LOCK:
        _CONFIG_CACHE[key] = (stamp, config)
    return config


def _registry_path_of(config: HubConfig) -> Path:
    return Path(config.registry_path) if config.registry_path else default_registry_path()


def _load_registry(config: HubConfig, *, fresh: bool = False) -> Registry:
    """
    Реестр стендов по пути из конфига — из кэша, если файл не менялся.

    ``fresh=True`` обязателен для путей, которые собираются реестр МЕНЯТЬ
    (``add_existing``/``save``): ``Registry`` — изменяемый объект, и отдавать
    один и тот же экземпляр на мутацию и на параллельные чтения нельзя.
    """
    reg_path = _registry_path_of(config)
    key = str(reg_path)
    stamp = _file_stamp(reg_path)
    if not fresh:
        with _CACHE_LOCK:
            cached = _REGISTRY_CACHE.get(key)
            if cached is not None and cached[0] == stamp:
                return cached[1]
    registry = Registry.load(reg_path)
    if not fresh:
        with _CACHE_LOCK:
            _REGISTRY_CACHE[key] = (stamp, registry)
    return registry


# --- сборка снапшота состояния стендов ---------------------------------------

# Состояние пробы в ответе, когда пробы ЕЩЁ НЕ ВЫПОЛНЯЛИСЬ (ответ на
# ``?probe=0`` либо первый заход, пока фоновый опрос не завершил круг).
# Отдельное значение, а не "unknown": "unknown" — это честный результат
# выполненной проверки («проверить нечем»), а здесь проверки просто не было.
PENDING_PROBE_STATE = "pending"


def _stand_entry(name: str, stand: Stand, status) -> dict:
    """
    Одна строка таблицы стендов для ``GET /api/stands``.

    ``status is None`` означает «пробы не выполнялись» — все состояния
    получают ``PENDING_PROBE_STATE``, а не выдуманный OK/DOWN.
    """
    status_dict = status.to_dict() if status else None
    http_state = status.http.value if status else PENDING_PROBE_STATE
    db_state = status.db.value if status else PENDING_PROBE_STATE
    redis_state = status.redis.value if status else PENDING_PROBE_STATE
    process_state = status.process.value if status else PENDING_PROBE_STATE
    # Таблица стендов показывает каталог логов BPMkit-ПРОЕКТА
    # (<extra["docs_folder"]>/logs, scaffold, НЕ extra["logs_path"] — тот
    # указывает на каталог логов самого стенда) — источник "stand" здесь не
    # запрашивается ни query-параметром, ни выбором пользователя (тот выбор —
    # только у панели "Текущее состояние"/сплит-меню ниже).
    logs_dir = logs_browser.resolve_logs_dir(stand, source="bpmkit")
    logs_path = str(logs_dir) if logs_dir else (logs_browser.raw_logs_path(stand, "bpmkit") or None)
    # Флаг для UI: доступен ли источник логов "Логи BPMkit-проекта" у ЭТОГО
    # стенда — задан extra["docs_folder"] И каталог <docs_folder>/logs реально
    # существует (см. logs_browser.resolve_logs_dir). Используется, чтобы
    # дизейблить соответствующий пункт сплит-меню "Открыть папку логов".
    bpmkit_logs_available = logs_dir is not None
    # Схему берём из записи: стенд за TLS по http:// не откроется, а ссылка
    # «Открыть стенд» в дашборде вела бы в никуда (см. Stand.stand_scheme).
    http_url = (
        f"{(stand.stand_scheme or 'http').lower()}://{stand.stand_host}:{stand.stand_port}"
        if stand.stand_host and stand.stand_port
        else None
    )
    return {
        "name": name,
        "transport": stand.transport.value,
        "status": status_dict,
        # ``reason`` у http/redis — тот же приём, что у process.reason: без него
        # наружу уходил голый "down"/"—", и оператор не мог отличить закрытый
        # порт от TLS-ошибки, а «не настроено» от «настроено, но недоступно»
        # (GAP-002, GAP-003). Источник — StandStatus.details, см. health.check_stand.
        "http": {
            "url": http_url,
            "state": http_state,
            "reason": (status.details.get("http_reason") if status else None),
        },
        "db": {"name": stand.db_name or None, "state": db_state},
        "redis": {
            "number": _redis_number(stand),
            "state": redis_state,
            "reason": (status.details.get("redis_reason") if status else None),
        },
        "process": {
            "state": process_state,
            "transport": stand.transport.value,
            "logs_path": logs_path,
            # Стенд жив, но поднят МИМО диспетчера (нет живого pidfile) —
            # Стоп/Рестарт по нему потребуют усыновления. Показываем это
            # бейджем ДО того, как пользователь нажмёт кнопку и получит отказ.
            "external": _is_external(stand, process_state),
            # Причина состояния от бэкенда хостинга (IIS: «сайт остановлен» /
            # «пул остановлен» / «порт держит http.sys, 503»), см.
            # health.check_stand.
            "reason": (status.details.get("process_reason") if status else None),
        },
        "logs": {"bpmkit_available": bpmkit_logs_available},
    }


def build_snapshot(config_path: Path, *, probe: bool = True) -> StatusSnapshot:
    """
    Собирает снапшот состояния всех стендов.

    ``probe=False`` — БЕЗ единой сетевой пробы: только реестр (мгновенно,
    сколько бы недоступных стендов в нём ни было). ``probe=True`` — полный
    параллельный опрос через ``FederatedClient.status_all`` (может быть
    медленным — вызывается из фонового потока поллера, не из обработчика).

    ``RegistryError`` пробрасывается наружу: вызывающий (обработчик — 500,
    поллер — снапшот с ``error``) решает, как о нём сообщить.
    """
    config = _load_config(config_path)
    registry = _load_registry(config)
    statuses = FederatedClient(registry).status_all() if probe else {}
    stands = [_stand_entry(name, registry.get(name), statuses.get(name)) for name in registry.names()]
    return StatusSnapshot(
        stands=stands,
        default=registry.default,
        probed=probe,
        generated_at=time.time(),
        sources=_snapshot_sources(config_path),
    )


def _snapshot_sources(config_path: Path) -> tuple:
    """
    Отпечаток файлов, из которых собран снапшот: конфиг хаба и реестр стендов.

    Сверяется при отдаче кэшированного снапшота (см. ``_api_stands``): если
    реестр изменился, состав стендов в снапшоте уже неверен, и отдавать его
    нельзя, каким бы свежим он ни был по времени.
    """
    try:
        config = _load_config(config_path)
        return (_file_stamp(config_path), _file_stamp(_registry_path_of(config)))
    except (OSError, RegistryError):
        # Не смогли снять отпечаток — считаем источники «неизвестными».
        # Пустой кортеж не совпадёт ни с чем, и снапшот будет пересобран.
        return ()


# --- канал обновлений издателя (companion) -----------------------------------
#
# Ядро знает о канале ровно три вещи: есть ли пакет, как спросить у него статус
# и как попросить выполнить одно из шести разрешённых действий. Никакой логики
# канала (лицензия, подписи, сеть) здесь нет и быть не должно — она целиком
# живёт в ``standkit_companion``.

#: Свободная редакция — пакета канала рядом нет.
EDITION_FREE = "free"
#: Редакция с каналом обновлений издателя.
EDITION_COMPANION = "companion"

#: Маршрут → имя действия раннера (``CompanionRunner.run_action``). Имена
#: действий НЕ повторяют URL дословно: URL — часть публичного контракта фронта,
#: а имена действий принадлежат каналу, и таблица делает эту границу видимой.
COMPANION_ACTION_ROUTES = {
    "/api/companion/sync": "sync_patterns",
    "/api/companion/check-update": "check_update",
    "/api/companion/stage-update": "stage_update",
    "/api/companion/apply-update": "apply_update",
    "/api/companion/rollback": "rollback",
    "/api/companion/revocations": "refresh_revocations",
}

#: Действия, которые умеют адресоваться к конкретной версии (тело
#: ``{"version": "0.307.0"}``). Для остальных поле в теле игнорируется — молча,
#: потому что лишний ключ в JSON не повод отказать пользователю в операции.
COMPANION_VERSION_ACTIONS = frozenset({"stage_update", "rollback"})

#: ``CompanionError.kind`` → HTTP-код. Смысл группировки, а не «все ошибки 500»:
#:
#: * **402** — нужна лицензия или действие с ней: ключа нет, он отозван, истёк,
#:   не вступил в силу, конверт не распознан или его подпись не подтверждена.
#:   Единственная группа, которую чинит сам пользователь, обратившись к издателю;
#: * **503** — канал временно не может работать: рядом нет CLI BPMkit, у которого
#:   спрашивается лицензионный контекст. Ровно тот же код, что и у отсутствующего
#:   пакета канала, — обе ситуации означают «возможность сейчас недоступна»;
#: * **409** — конфликт с текущим состоянием: применять нечего, откатываться
#:   некуда. Запрос корректен, но противоречит тому, что есть на диске;
#: * **502** (умолчание) — отказ на стороне канала/бэкенда издателя: сеть,
#:   неразобранный ответ, несошедшаяся контрольная сумма, недействительная
#:   подпись артефакта. Хаб здесь — шлюз к чужой системе, и 502 честнее 500.
COMPANION_ERROR_STATUS = {
    "no_license": 402,
    "revoked": 402,
    "expired": 402,
    "invalid_envelope": 402,
    "signature_invalid": 402,
    "not_yet_valid": 402,
    "context_unavailable": 503,
    "nothing_staged": 409,
    "nothing_to_rollback": 409,
}
COMPANION_ERROR_STATUS_DEFAULT = 502

#: Текст для свободной редакции. Отвечаем 503, а НЕ 404: маршрут существует и
#: будет работать после установки платной редакции — недоступна возможность, а
#: не адрес. 404 в этом месте читается как «опечатка в URL» и уводит и
#: пользователя, и поддержку не туда.
COMPANION_UNAVAILABLE_MESSAGE = (
    "Канал обновлений недоступен: установлена свободная редакция BPMkitStand"
)

#: Текст для выключенного главного рубильника (``companion.enabled = false``).
COMPANION_DISABLED_MESSAGE = "Канал обновлений выключен в настройках"


def companion_available() -> bool:
    """Установлена ли редакция с каналом обновлений.

    Спрашивается именно ``is_available()``, а не «модуль импортировался»: пакет
    оставляет себе право честно ответить «я здесь, но в этом окружении не
    работаю». Любое исключение из чужого кода трактуется как «недоступен» — на
    этот вопрос у хаба обязан быть ответ при любом состоянии платной редакции.
    """
    if _companion is None or _companion_runner is None:
        return False
    try:
        return bool(_companion.is_available())
    except Exception:  # noqa: BLE001 - битая редакция не роняет ядро
        return False


def companion_edition() -> str:
    """``"companion"`` или ``"free"`` — то, что уходит в ``/api/version``."""
    return EDITION_COMPANION if companion_available() else EDITION_FREE


def companion_describe() -> dict:
    """Карточка канала для ``/api/version`` (пустая в свободной редакции)."""
    if not companion_available():
        return {}
    try:
        described = _companion.describe()
    except Exception:  # noqa: BLE001 - см. companion_available
        return {}
    return described if isinstance(described, dict) else {}


def companion_error_status(kind: str) -> int:
    """HTTP-код по ``CompanionError.kind`` (см. ``COMPANION_ERROR_STATUS``)."""
    return COMPANION_ERROR_STATUS.get(kind or "", COMPANION_ERROR_STATUS_DEFAULT)


def _is_companion_error(exc: BaseException) -> bool:
    """Типизированный ли это отказ канала.

    Проверка через ``getattr``-импорт, а не через глобальный ``except
    CompanionError``: класса может не существовать вовсе (свободная редакция), и
    ссылаться на него в теле обработчика — значит уронить ядро ``NameError`` там,
    где оно обязано работать. Импорт локальный и защищённый по той же причине,
    что и импорт самого пакета в шапке модуля.
    """
    if _companion is None:
        return False
    try:
        from standkit_companion.errors import CompanionError
    except Exception:  # noqa: BLE001 - битая редакция не роняет обработку ошибки
        return False
    return isinstance(exc, CompanionError)


def companion_error_payload(exc) -> tuple[int, dict]:
    """``CompanionError`` → (HTTP-код, тело ответа).

    Ключ ``error`` обязателен и обязан быть человеческим текстом: именно его
    читает фронт (``handleResponse`` в app.js) и показывает пользователю. Машинные
    поля (``kind``/``retriable``/``user_visible``) идут рядом — по ним UI решает,
    предлагать ли повтор и поднимать ли отказ как проблему, а не как строку в
    подробностях. Решение принимает канал, а не хаб: подменять его здесь своей
    таблицей значило бы завести вторую, расходящуюся.
    """
    info = exc.to_dict() if hasattr(exc, "to_dict") else {}
    title = str(info.get("title") or "Ошибка канала обновлений")
    message = str(info.get("message") or "")
    text = f"{title}: {message}" if message and message != title else title
    return companion_error_status(str(info.get("kind") or "")), {
        "error": text,
        "kind": info.get("kind") or "unknown",
        "detail": info.get("detail") or "",
        "retriable": bool(info.get("retriable")),
        "user_visible": bool(info.get("user_visible")),
    }


def companion_status(config_path: Path, runner=None) -> dict:
    """Карточка канала: у живого раннера — ``status()``, иначе — снимок с диска.

    Раннера может не быть по двум ПОЛНОСТЬЮ разным причинам: канал выключен
    настройкой либо его поток не поднялся. В обоих случаях UI обязан показать
    состояние, а не пустоту, — за это отвечает ``status_snapshot`` (та же форма
    ответа, ``running: false``, сроки следующего запуска отсутствуют).
    """
    if runner is not None:
        return runner.status()
    return _companion_runner.status_snapshot(config_path)


def merge_companion_section(current: dict, incoming: object) -> dict:
    """Слить присланную секцию ``companion`` поверх текущей, НЕ теряя непереданное.

    Зачем отдельная функция. ``_api_settings_post`` мержит тело в конфиг обычным
    ``dict.update`` — он ПЛОСКИЙ, и вложенная секция заменяется целиком. Для
    ``agents`` это правильно (список — атомарное значение), а для ``companion``
    губительно: форма, приславшая только ``{"companion": {"enabled": true}}``,
    молча обнулила бы адрес бэкенда, путь к CLI и все три интервала, потому что
    ``CompanionSettings.from_dict`` достроит недостающее ДЕФОЛТАМИ. Пользователь
    при этом увидел бы «Настройки сохранены».

    Мержим два уровня: сама секция и вложенные циклы (``patterns``/``releases``/
    ``revocations``) — глубже структуры нет. Валидация и КЛАМП интервала остаются
    за ``CompanionSettings.from_dict``: свою вторую границу здесь не заводим,
    иначе они разойдутся (в конфиге границы уже есть — см. ``CompanionCycle``).
    """
    merged = dict(current or {})
    if not isinstance(incoming, dict):
        return merged
    for key, value in incoming.items():
        if key in _companion_cycle_names() and isinstance(value, dict):
            cycle = dict(merged.get(key) or {})
            cycle.update(value)
            merged[key] = cycle
        else:
            merged[key] = value
    return merged


def _companion_cycle_names() -> tuple:
    """Имена циклов канала. Берутся у самого канала, если он установлен, — там
    они и объявлены (``runner.CYCLES``). В свободной редакции секция всё равно
    присутствует в конфиге (см. ``HubConfig.companion``), поэтому имена нужны и
    без пакета — держим их дословной копией с явной ссылкой на источник."""
    if _companion_runner is not None:
        try:
            return tuple(_companion_runner.CYCLES)
        except Exception:  # noqa: BLE001 - битая редакция не роняет настройки
            pass
    return ("patterns", "releases", "revocations")


def build_companion_runner(config_path: Path):
    """Собрать и запустить планировщик канала. Возвращает ``(раннер, ошибка)``.

    Отказ старта НЕ поднимается наружу: хаб — про управление стендами, и канал
    обновлений не имеет права помешать ему подняться. Текст отказа возвращается
    вторым элементом и живёт на сервере (``companion_error``), чтобы попасть в
    статус, а не потеряться.
    """
    try:
        runner = _companion_runner.build_runner(config_path)
        runner.start()
        return runner, ""
    except Exception as exc:  # noqa: BLE001 - см. докстринг
        return None, f"канал обновлений не запущен: {type(exc).__name__}: {exc}"


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

        def end_headers(self) -> None:
            # Метка «здесь живой standkit-hub» на КАЖДОМ ответе, включая 401 и
            # 404. Нужна single-instance проверке (см. probe_hub_instance):
            # второй запуск по ярлыку должен отличить свой уже работающий
            # экземпляр от чужого сервиса, занявшего порт, НЕ предъявляя
            # токена — а значит, судить можно только по неаутентифицированному
            # ответу. Версия в значении — диагностика, не контракт: сравнивать
            # её не нужно, важен сам факт заголовка.
            self.send_header(HUB_IDENTITY_HEADER, _standkit_version)
            super().end_headers()

        def _send_json(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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

        def _serve_index(
            self,
            inject_token: Optional[str] = None,
            set_cookie: bool = False,
            view: str = _DEFAULT_VIEW,
        ) -> None:
            index_path = web_dir / "index.html"
            if not index_path.is_file():
                self._send_json(500, {"error": "index.html не найден в пакете хаба"})
                return
            # Токен инжектим в <meta> ТОЛЬКО аутентифицированному запросу (см.
            # _handle_root). Неаутентифицированному — плейсхолдер очищается в пустоту,
            # токен не утекает.
            text = index_path.read_text(encoding="utf-8")
            text = text.replace("__STANDKIT_TOKEN__", inject_token or "")
            # Тема подставляется прямо в data-theme <html> — она применяется
            # ДО загрузки/выполнения JS, поэтому у пользователя тёмной темы нет
            # «вспышки» светлой. Источник правды — конфиг хаба, а не
            # localStorage браузера (см. HubConfig.theme).
            try:
                theme = _load_config(config_path).theme
            except OSError:
                theme = "auto"
            text = text.replace("__STANDKIT_THEME__", theme)
            # Режим отображения — тем же приёмом, что и тема: атрибут на <html>
            # проставлен ДО выполнения JS, поэтому компактное окно не успевает
            # мигнуть полноразмерным дашбордом.
            text = text.replace("__STANDKIT_VIEW__", normalize_view(view))
            body = text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # index.html НЕ кэшируем: в нём сессионный токен в <meta> и
            # подставленная тема — обе величины меняются между запусками.
            self.send_header("Cache-Control", "no-store")
            if set_cookie:
                self.send_header(
                    "Set-Cookie",
                    f"{_security.SESSION_COOKIE_NAME}={session_token}; HttpOnly; SameSite=Strict; Path=/",
                )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _serve_static(self, rel_path: str) -> None:
            """
            Отдаёт файл из ``web/`` с валидаторами кэша (``ETag`` +
            ``Last-Modified``) и умеет отвечать ``304 Not Modified`` на
            ``If-None-Match``/``If-Modified-Since``.

            Санитайзинг пути (traversal) — прежний, в ``sanitize_static_path``.
            """
            target = _security.sanitize_static_path(web_dir, rel_path)
            if target is None:
                self._send_json(404, {"error": "not found"})
                return
            try:
                st = target.stat()
            except OSError:
                self._send_json(404, {"error": "not found"})
                return

            etag = _static_etag(st.st_size, st.st_mtime_ns)
            cache_control = _static_cache_control(target)
            last_modified = formatdate(st.st_mtime, usegmt=True)

            if _is_not_modified(self.headers, etag=etag, mtime=st.st_mtime):
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Last-Modified", last_modified)
                self.send_header("Cache-Control", cache_control)
                self.end_headers()
                return

            content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_modified)
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def _handle_root(self, parsed) -> None:
            qs = parse_qs(parsed.query)
            token = (qs.get(_security.TOKEN_QUERY_PARAM) or [None])[0]
            authed_query = bool(token and _security.tokens_match(token, session_token))
            cookie_tok = _security.extract_cookie_token(self.headers.get("Cookie", ""))
            authed_cookie = _security.tokens_match(cookie_tok, session_token)
            view = normalize_view((qs.get("view") or [_DEFAULT_VIEW])[0])
            if authed_query or authed_cookie:
                # Аутентифицированный запрос: отдаём index с токеном в <meta>, чтобы
                # JS мог класть X-Standkit-Token в мутации (cookie HttpOnly, JS её не
                # читает). Cookie ставим, если пришли по ссылке ?t=. Без редиректа —
                # иначе токен теряется до загрузки JS (был баг 403 на мутациях).
                self._serve_index(
                    inject_token=session_token, set_cookie=authed_query, view=view
                )
            else:
                self._serve_index(view=view)

        # --- API: стенды ---

        def _poller(self) -> Optional[StatusPoller]:
            """
            Фоновый поллер сервера, если он есть.

            ``make_handler`` умышленно не требует поллера: класс-обработчик
            можно поднять и без него (тесты, встраивание) — тогда ``GET
            /api/stands`` честно опрашивает стенды синхронно, как раньше.
            """
            return getattr(self.server, "status_poller", None)

        # Тело ответа /api/stands: снапшот + интервал автообновления, который
        # фронт обязан применять (настройка refresh_interval_sec долго висела
        # в форме, ни на что не влияя, — см. app.js).
        def _stands_payload(self, snapshot: StatusSnapshot) -> dict:
            payload = snapshot.to_payload()
            try:
                payload["refresh_interval_sec"] = _load_config(config_path).refresh_interval_sec
            except OSError:
                pass
            return payload

        def _api_stands(self, parsed) -> None:
            qs = parse_qs(parsed.query)
            raw_probe = (qs.get("probe") or ["1"])[0]
            if raw_probe not in ("0", "1"):
                self._send_json(400, {"error": "invalid probe (ожидается 0 или 1)"})
                return
            want_probe = raw_probe == "1"

            poller = self._poller()
            if want_probe:
                if poller is not None:
                    snapshot = poller.snapshot()
                    # Снапшот годится, только если реестр и конфиг с момента
                    # его сборки не менялись. Иначе состав стендов в нём уже
                    # неверен (стенд зарегистрировали или удалили — в том
                    # числе мимо хаба), и отдавать его нельзя: пользователь
                    # увидел бы старый список до следующего тика поллера.
                    if snapshot is not None and snapshot.sources == _snapshot_sources(config_path):
                        self._send_json(200, self._stands_payload(snapshot))
                        return
                    if snapshot is not None:
                        # Источники разошлись — просим поллер пересобраться
                        # вне очереди, а сейчас отдаём мгновенный слепок с
                        # актуальным составом и probed=false.
                        poller.poke()
                    # Первый круг фонового опроса ещё не завершён — не держим
                    # браузер, отдаём мгновенный слепок реестра с probed=false.
                    # Фронт увидит флаг и переспросит.
                else:
                    # Поллера нет вовсе — синхронный опрос (прежнее поведение).
                    try:
                        self._send_json(200, self._stands_payload(build_snapshot(config_path, probe=True)))
                    except RegistryError as exc:
                        self._send_json(500, {"error": str(exc)})
                    return

            try:
                self._send_json(200, self._stands_payload(build_snapshot(config_path, probe=False)))
            except RegistryError as exc:
                self._send_json(500, {"error": str(exc)})

        def _api_events(self) -> None:
            """
            SSE-поток обновлений снапшота (``text/event-stream``).

            Заменяет опрос ``GET /api/stands`` таймером: сервер сам присылает
            новое состояние, как только фоновый поллер его собрал. Соединение
            держит один поток ``ThreadingHTTPServer`` — при остановке хаба
            поллер выставляет флаг остановки, и цикл ниже выходит.

            Авторизация — та же, что у прочих ``GET /api/*`` (``EventSource``
            не умеет слать кастомные заголовки, поэтому проверка проходит по
            session-cookie; она HttpOnly+SameSite=Strict, cross-origin поток
            открыть нельзя).
            """
            poller = self._poller()
            if poller is None:
                self._send_json(
                    503,
                    {"error": "фоновый опрос не запущен — используйте периодический GET /api/stands"},
                )
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()

            version = 0
            try:
                # Рекомендация клиенту переподключаться не чаще, чем раз в
                # 5 секунд (браузер применяет её сам при обрыве).
                self.wfile.write(b"retry: 5000\n\n")
                self.wfile.flush()
                while not poller.is_stopping():
                    new_version, snapshot = poller.wait_for_change(version, _SSE_WAIT_SEC)
                    if poller.is_stopping():
                        break
                    if new_version == version or snapshot is None:
                        # Таймаут ожидания — heartbeat-комментарий: он же
                        # способ УЗНАТЬ об уходе клиента (write упадёт).
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        continue
                    version = new_version
                    data = json.dumps(self._stands_payload(snapshot), ensure_ascii=False)
                    self.wfile.write(f"event: stands\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError, ValueError):
                # Клиент закрыл вкладку/оборвал соединение — штатный выход.
                return

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
                # Для transport=agent каталог живёт на ЧУЖОЙ файловой системе,
                # и локальная проверка «не найден» технически верна, но
                # бесполезна — logs_unavailable_reason говорит это прямо
                # (GAP-006, п.4).
                detail = logs_browser.logs_unavailable_reason(stand, source)
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
            # «Отрезаем всё старше сегодня»: логи IIS/.NET по дням бывают очень
            # тяжёлыми — сначала берём самый свежий файл ЗА СЕГОДНЯ. Если сегодня
            # логов нет (стенд не писал сегодня) — фолбэк на самый свежий вообще,
            # чтобы всегда показать последнюю сессию.
            primary = logs_browser.pick_primary_log(
                logs_dir, since_mtime=logs_browser.start_of_today_ts()
            )
            if primary is None:
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
            # Хвост берём щедрым (4000 строк), чтобы гарантированно захватить
            # ВСЮ последнюю сессию (от "=== START pid="/"Application starting"
            # до конца файла), даже если она сама по себе длинная — затем
            # extract_current_session() отрезает всё, что относится к прошлым
            # запускам, и уже результат капается до разумного размера для UI.
            # Читаем только хвост файла (до 4 МБ) — даже дневной IIS-лог в сотни
            # МБ не грузим целиком в память ради последних строк.
            raw_lines = _logs.tail(primary, 4000, max_bytes=4_000_000)
            raw_text = "\n".join(raw_lines)
            session_text = _logs.extract_current_session(raw_text) if raw_text else ""
            if session_text:
                session_lines = session_text.split("\n")
                if len(session_lines) > 1000:
                    session_lines = session_lines[-1000:]
                session_text = "\n".join(session_lines)
            self._send_json(
                200,
                {
                    "available": True,
                    "text": session_text if session_text else "(лог пуст)",
                    "file": primary.name,
                    "source": source,
                },
            )

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

        def _force_from_qs(self, parsed) -> bool:
            """
            ``?force=1`` на stop/restart — ЯВНОЕ согласие пользователя взять под
            управление стенд, поднятый вне диспетчера, и остановить его (см.
            ``standkit.lifecycle._kestrel_stop``). Любое другое значение — нет.
            """
            raw = (parse_qs(parsed.query).get("force") or ["0"])[0]
            return raw in ("1", "true", "yes")

        def _api_stand_action(self, name: str, action: str, *, force: bool = False) -> None:
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            client = FederatedClient(registry)
            try:
                if action in ("stop", "restart"):
                    result = getattr(client, action)(name, force=force)
                else:
                    result = getattr(client, action)(name)
            except (RemoteCallError, SecretError) as exc:
                self._send_json(502, {"error": str(exc)})
                return
            except AdoptionRequired as exc:
                # Стенд поднят вне диспетчера, найден валидный кандидат — НЕ
                # убиваем ничего молча: отдаём кандидата фронту (409), тот
                # показывает подтверждение и повторяет запрос с ?force=1.
                self._send_json(
                    409,
                    {
                        "error": str(exc),
                        "adopt_required": True,
                        "candidate": exc.candidate.to_dict(),
                    },
                )
                return
            except AdoptionUnavailable as exc:
                self._send_json(404, {"error": str(exc)})
                return
            except LifecycleError as exc:
                # Понятная причина отказа (dotnet не найден в PATH, процесс
                # умер сразу после старта, стенд запущен не диспетчером и т.п.)
                # — фронт обязан показать текст пользователю, а не просто "ошибка".
                self._send_json(400, {"error": str(exc)})
                return
            except HostingError as exc:
                # Отказ бэкенда хостинга (appcmd/docker/kubectl не найден, нет
                # прав, App Pool/контейнер/деплоймент не остановился и т.п.) —
                # честный текст ошибки пользователю (включая stderr команды),
                # а не молчаливый 500.
                self._send_json(400, {"error": str(exc)})
                return
            except NotImplementedError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            payload: dict = {"ok": True}
            if action in ("start", "restart") and isinstance(result, int):
                payload["pid"] = result
            # Состояние стенда только что изменилось — просим фоновый поллер
            # не досыпать интервал. Сам опрос идёт в его потоке, ответ на
            # мутацию им НЕ задерживается.
            poller = self._poller()
            if poller is not None:
                poller.poke()
            self._send_json(200, payload)

        def _api_stand_adopt(self, name: str) -> None:
            """
            ``POST /api/stand/<name>/adopt`` — взять под управление стенд,
            поднятый вне диспетчера: найти владельца порта, проверить, что это
            действительно процесс ЭТОГО стенда, и записать pidfile.

            Процесс НЕ останавливается — это отдельный шаг (кнопки Стоп/Рестарт
            после усыновления работают обычным путём). Ответы:
              - 200 ``{"ok": true, "candidate": {...}}`` — усыновлён;
              - 404 ``{"error"}`` — владельца порта определить не удалось;
              - 400 ``{"error"}`` — владелец найден, но это не процесс стенда
                (порт занят чужим процессом) либо host_kind не kestrel.
            """
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            client = FederatedClient(registry)
            try:
                candidate = client.adopt(name)
            except (RemoteCallError, SecretError) as exc:
                self._send_json(502, {"error": str(exc)})
                return
            except AdoptionUnavailable as exc:
                self._send_json(404, {"error": str(exc)})
                return
            except LifecycleError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except NotImplementedError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, {"ok": True, "candidate": candidate})

        def _api_iis_detect(self) -> None:
            """
            ``POST /api/iis/detect`` — автоопределение IIS-сайта/пула стенда по
            каталогу (physical path) и порту (биндинг) для кнопки «Определить
            автоматически» формы регистрации.

            Тело: ``{"stand_dir": "...", "stand_port": 5000}``. Ответы:
              - 200 ``{"ok": true, "match": {...}}`` — сайт найден;
              - 404 ``{"error"}`` — сопоставить не удалось;
              - 400 ``{"error", "elevation_required"?}`` — appcmd недоступен /
                нет прав администратора (диагноз, а не общий текст ошибки).
            """
            body = self._read_json_body(max_bytes=max_body_bytes)
            if body is None:
                return
            stand_dir = body.get("stand_dir")
            if not isinstance(stand_dir, str) or not stand_dir.strip():
                self._send_json(
                    400, {"error": "поле 'stand_dir' обязательно", "fields": ["stand_dir"]}
                )
                return
            try:
                port = int(body.get("stand_port") or 0)
            except (TypeError, ValueError):
                self._send_json(
                    400, {"error": "stand_port должен быть числом", "fields": ["stand_port"]}
                )
                return

            from standkit import hosting as _hosting

            probe = Stand(
                name="__detect__",
                host_kind=HostKind.IIS,
                stand_dir=stand_dir.strip(),
                stand_port=port,
            )
            try:
                match = _hosting.detect_iis_site(probe)
            except _hosting.IisElevationError as exc:
                self._send_json(400, {"error": str(exc), "elevation_required": True})
                return
            except HostingError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            if match is None:
                self._send_json(
                    404,
                    {
                        "error": (
                            "не нашли IIS-сайт с таким каталогом или портом — проверьте "
                            "stand_dir/stand_port либо укажите iis_site/iis_app_pool вручную"
                        )
                    },
                )
                return
            self._send_json(200, {"ok": True, "match": match.to_dict()})

        def _api_stand_redis_clear(self, name: str) -> None:
            """
            Очищает БД Redis стенда (``SELECT <db>`` + ``FLUSHDB``, см.
            ``standkit_hub.redis_min``) — кнопка "Очистить Redis" в таблице
            стендов. Требует явно заданный ``redis_db`` в реестре/``extra``
            (см. ``_redis_connect_params``) — номер БД НИКОГДА не угадывается.
            """
            config = _load_config(config_path)
            registry = _load_registry(config)
            if name not in registry:
                self._send_json(404, {"error": f"стенд '{name}' не найден"})
                return
            stand = registry.get(name)
            host, port, db = _redis_connect_params(stand)
            if db is None:
                self._send_json(400, {"error": _REDIS_MISSING_DB_MESSAGE})
                return
            result = redis_min.flush_db(host, port, db)
            if result.ok:
                self._send_json(200, {"ok": True, "message": result.message})
            else:
                # "error" (не только "message") — чтобы фронт (handleResponse
                # в app.js, которая читает data.error на не-2xx-ответах) показал
                # содержательный текст, а не голое "HTTP 502".
                self._send_json(502, {"ok": False, "error": result.message})

        def _api_stand_register(self) -> None:
            """
            ``POST /api/stand/register`` — кнопка "Зарегистрировать стенд" на
            дашборде. Регистрирует УЖЕ существующий стенд (каталог/БД/дистрибутив
            предполагаются готовыми — это НЕ провижининг, см. docstring
            ``Registry.add_existing``) в том же реестре, который резолвит
            ``_load_registry`` (тот же ``registry_path`` конфига хаба /
            ``default_registry_path()``).

            Пароли/секреты в теле запроса не принимаются — только ``secret_ref_*``
            (см. ``_REGISTER_FORBIDDEN_FIELDS``). Ответы:
              - 400 ``{"error", "fields"}`` — невалидное тело/запись;
              - 409 ``{"error"}`` — имя уже занято (не перезаписываем молча);
              - 200 ``{"ok": true, "name"}`` — успех.
            """
            body = self._read_json_body(max_bytes=max_body_bytes)
            if body is None:
                return

            forbidden = sorted(k for k in body if k in _REGISTER_FORBIDDEN_FIELDS)
            if forbidden:
                self._send_json(
                    400,
                    {
                        "error": (
                            f"поля {', '.join(forbidden)} не принимаются — пароли/секреты "
                            "задаются только через secret_ref_* (secretstore), не в теле запроса"
                        ),
                        "fields": forbidden,
                    },
                )
                return

            raw_name = body.get("name")
            if not isinstance(raw_name, str) or not raw_name.strip():
                self._send_json(400, {"error": "поле 'name' обязательно", "fields": ["name"]})
                return
            name = raw_name.strip()
            if not _security.validate_stand_name(name):
                self._send_json(
                    400,
                    {"error": "недопустимое имя стенда (допустимы буквы/цифры/._-)", "fields": ["name"]},
                )
                return

            errors: list[str] = []
            bad_fields: list[str] = []

            data: dict = {}
            for key in _REGISTER_ALLOWED_FIELDS:
                if key not in body:
                    continue
                value = body[key]
                if isinstance(value, str) and not value.strip():
                    continue  # пустые строки не пишем поверх дефолтов Stand
                data[key] = value

            for int_field in ("stand_port", "db_port", "redis_port"):
                if int_field in data:
                    try:
                        data[int_field] = int(data[int_field])
                    except (TypeError, ValueError):
                        errors.append(f"{int_field} должен быть числом")
                        bad_fields.append(int_field)
                        data.pop(int_field, None)

            # Булевы поля формы (обе «галочки проверки сертификата»: verify_tls
            # — проба СТЕНДА, agent_verify_tls — канал до АГЕНТА). Чекбокс
            # присылает настоящий JSON-bool (см. app.js::collectRegisterPayload),
            # но реестр правят и внешние инструменты: принимаем "true"/"false"
            # строкой, а всё остальное — явная ошибка, а не молчаливый дефолт
            # true (иначе оператор решит, что проверку он выключил, а он нет).
            for bool_field in ("verify_tls", "agent_verify_tls"):
                if bool_field not in data:
                    continue
                raw_verify = data[bool_field]
                if isinstance(raw_verify, bool):
                    continue
                if isinstance(raw_verify, str) and raw_verify.strip().lower() in (
                    "true", "false", "1", "0", "yes", "no", "y", "n",
                ):
                    data[bool_field] = raw_verify.strip().lower() in ("true", "1", "yes", "y")
                else:
                    errors.append(f"{bool_field} должен быть true или false")
                    bad_fields.append(bool_field)
                    data.pop(bool_field, None)

            if "stand_scheme" in data and str(data["stand_scheme"]).lower() not in ("http", "https"):
                errors.append(f"недопустимое значение stand_scheme: {data['stand_scheme']!r}")
                bad_fields.append("stand_scheme")
                data.pop("stand_scheme", None)

            valid_transports = {t.value for t in Transport}
            if "transport" in data and data["transport"] not in valid_transports:
                errors.append(f"недопустимое значение transport: {data['transport']!r}")
                bad_fields.append("transport")

            valid_host_kinds = {h.value for h in HostKind}
            if "host_kind" in data and data["host_kind"] not in valid_host_kinds:
                errors.append(f"недопустимое значение host_kind: {data['host_kind']!r}")
                bad_fields.append("host_kind")

            if errors:
                self._send_json(400, {"error": "; ".join(errors), "fields": bad_fields})
                return

            stand = Stand.from_dict(name, data)
            validation_errors = stand.validate()
            if validation_errors:
                self._send_json(400, {"error": "; ".join(validation_errors), "fields": []})
                return

            config = _load_config(config_path)
            try:
                # fresh=True — реестр сейчас будут МЕНЯТЬ, кэшированный
                # экземпляр отдавать на мутацию нельзя (см. _load_registry).
                registry = _load_registry(config, fresh=True)
            except RegistryError as exc:
                self._send_json(500, {"error": str(exc)})
                return

            if name in registry:
                self._send_json(409, {"error": f"стенд '{name}' уже есть в реестре"})
                return

            try:
                registry.add_existing(stand)
                registry.save()
            except RegistryError as exc:
                self._send_json(400, {"error": str(exc)})
                return

            invalidate_caches()
            poller = self._poller()
            if poller is not None:
                poller.poke()
            self._send_json(200, {"ok": True, "name": name})

        # --- API: версия ---

        def _api_version(self) -> None:
            """
            Версия ядра ``standkit`` (для модалки «О программе» на фронте) —
            read-only, тот же ``_authorize_read``, что у прочих ``GET /api/*``.

            Плюс ``edition`` — единственное место, где фронт узнаёт редакцию.
            Ключи ``version``/``name`` не трогаем: их уже читает модалка «О
            программе», и переименование сломало бы её молча.
            """
            payload = {"version": _standkit_version, "name": "BPMkitStand",
                       "edition": companion_edition()}
            described = companion_describe()
            if described.get("companion_version"):
                payload["companion_version"] = described["companion_version"]
            self._send_json(200, payload)

        # --- API: настройки ---

        def _api_settings_get(self) -> None:
            config = _load_config(config_path)
            payload = config.to_dict()
            # Отдельным ключом — фактические значения по умолчанию (HubConfig без
            # аргументов). Форма показывает их в placeholder'ах: пустое поле
            # само по себе не говорит пользователю, что подставится, если он
            # так его и оставит.
            payload["defaults"] = HubConfig().to_dict()
            self._send_json(200, payload)

        def _api_settings_post(self) -> None:
            body = self._read_json_body(max_bytes=max_body_bytes)
            if body is None:
                return
            current = _load_config(config_path)
            data = current.to_dict()
            data.update(body)
            # ``defaults`` — справочное поле ответа GET (см. _api_settings_get),
            # не часть конфига. Если форма вернула его назад — молча выбрасываем.
            data.pop("defaults", None)
            if "companion" in body:
                # Вложенная секция канала: плоский update выше заменил бы её
                # целиком, потеряв всё, чего форма не прислала (см.
                # merge_companion_section). Валидация и кламп интервалов — на
                # CompanionSettings.from_dict внутри HubConfig.from_dict.
                data["companion"] = merge_companion_section(
                    current.companion.to_dict(), body.get("companion"))
            new_config = HubConfig.from_dict(data)
            new_config.save(config_path)
            # Сброс кэша сразу после собственной записи — не полагаемся на
            # разрешение mtime файловой системы (см. invalidate_caches).
            invalidate_caches()
            # Мог измениться refresh_interval_sec — пусть поллер перечитает
            # его немедленно, а не после текущего (возможно, длинного) сна.
            poller = self._poller()
            if poller is not None:
                poller.poke()
            # Тот же смысл для канала: человек только что включил цикл и ждёт
            # первого прогона сейчас, а не через сутки (интервал релизов).
            # poke() заодно снимает блокировку не-retriable отказа — правка
            # настроек и есть сообщение «я починил то, на что вы жаловались».
            companion = self._companion_scheduler()
            if companion is not None:
                try:
                    companion.poke()
                except Exception:  # noqa: BLE001 - канал не роняет сохранение настроек
                    pass
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

        # --- API: канал обновлений издателя ---
        #
        # Шесть действий и один статус. Все действия — мутации (ходят в сеть,
        # пишут файлы, подменяют исполняемый файл MCP), поэтому проходят
        # ``_authorize_mutation`` наравне со start/stop стенда: double-submit
        # заголовок плюс локальный Origin/Referer. Послаблений «это же
        # служебный вызов» здесь нет и быть не может (SECURITY.md §4.1).

        def _companion_scheduler(self):
            """Планировщик канала этого сервера (``server.companion_runner``), если он поднят.

            Тем же способом, что и ``_poller``: атрибут сервера, а не глобаль.
            ``None`` означает три разные вещи — свободная редакция, выключенный
            настройкой канал, не поднявшийся поток; их разводит ``_api_companion_*``.

            Назван ``_scheduler``, а не ``_runner``, чтобы не затенять модульное
            имя ``_companion_runner`` — это МОДУЛЬ канала, и путать объект
            планировщика с модулем в одном методе слишком легко.
            """
            return getattr(self.server, "companion_runner", None)

        def _companion_guard(self) -> bool:
            """Свободная редакция → 503 и явный ``edition``. False = ответ отправлен."""
            if companion_available():
                return True
            self._send_json(503, {"error": COMPANION_UNAVAILABLE_MESSAGE,
                                  "edition": EDITION_FREE})
            return False

        def _companion_enabled(self) -> bool:
            """Главный рубильник ``companion.enabled`` из свежего конфига.

            Битый/недоступный конфиг трактуется как «выключен»: предлагать
            действия канала, не сумев прочитать его настройки, — способ сходить
            в сеть с чужим ключом, а не удобство.
            """
            try:
                return bool(_load_config(config_path).companion.enabled)
            except (OSError, ValueError, AttributeError):
                return False

        def _companion_status_dict(self) -> dict:
            """Статус канала для ответа. Отказ канала не имеет права уронить ответ
            хаба, поэтому ошибка превращается в поле ``last_error`` той же формы."""
            try:
                return companion_status(config_path, self._companion_scheduler())
            except Exception as exc:  # noqa: BLE001 - см. докстринг
                return {"running": False, "edition": EDITION_COMPANION,
                        "last_error": f"статус канала не получен: {type(exc).__name__}: {exc}"}

        def _api_companion_status(self) -> None:
            """Карточка канала. Выключенный канал — тоже 200.

            Отвечать ошибкой на статус выключенного канала нельзя: UI обязан
            показать, ПОЧЕМУ ничего не происходит, и дать включить обратно. Отказ
            в этом месте оставил бы пользователя с пустой вкладкой и без объяснения.
            """
            if not self._companion_guard():
                return
            status = self._companion_status_dict()
            status["edition"] = EDITION_COMPANION
            status["enabled"] = self._companion_enabled()
            self._send_json(200, status)

        def _api_companion_action(self, action: str) -> None:
            """Одно явное действие человека (кнопка UI) → ``runner.run_action``.

            Возвращаем и результат, и СВЕЖИЙ статус: после ``apply_update``
            меняется всё разом — доступные действия, флаг перезапуска, текущая
            версия, — и второй запрос за статусом успел бы разъехаться с первым.

            Конверт лицензии в ответ не попадает ни при каком исходе: тело
            собирается только из результата канала и его же типизированной
            ошибки; присланное клиентом тело наружу не возвращается вовсе.
            """
            if not self._companion_guard():
                return
            body = self._read_json_body(max_bytes=max_body_bytes)
            if body is None:
                return
            if not self._companion_enabled():
                # 409, а не 403: запрос корректен и разрешён, он противоречит
                # текущему состоянию — рубильнику, который выключил сам человек.
                self._send_json(409, {"error": COMPANION_DISABLED_MESSAGE,
                                      "edition": EDITION_COMPANION, "enabled": False})
                return

            version = None
            if action in COMPANION_VERSION_ACTIONS:
                raw = body.get("version")
                if raw is not None and not isinstance(raw, str):
                    self._send_json(400, {"error": "поле 'version' должно быть строкой"})
                    return
                version = (raw or "").strip() or None

            try:
                runner = self._companion_scheduler()
                if runner is None:
                    # Канал включён, но планировщика нет (поток не поднялся). Работаем
                    # одноразовым раннером: ручное действие человека не должно зависеть
                    # от фонового потока — иначе диагностика «почему не работает»
                    # требовала бы перезапуска хаба. Сборка внутри try намеренно:
                    # исключение отсюда обязано стать ответом, а не оборванным
                    # соединением с трейсбеком в консоли (http.server на не пойманном
                    # исключении не отвечает вовсе).
                    runner = _companion_runner.build_runner(config_path)
                result = runner.run_action(action, version=version)
            except Exception as exc:  # noqa: BLE001 - разбор ниже
                # Ловится всё, а разбирается по типу. Отдельной ветки под
                # ValueError здесь НЕТ намеренно: единственный ValueError,
                # который умеет бросить канал, — «неизвестное действие», а это
                # ошибка маршрутизации ХАБА (таблица маршрутов разошлась с
                # ACTIONS), то есть честные 500, а не 400 «клиент неправ».
                # Отвечать 400 на ValueError вообще опасно: любой ValueError из
                # глубины канала (разбор чужого JSON) стал бы «неверным запросом».
                if _is_companion_error(exc):
                    code, payload = companion_error_payload(exc)
                    self._send_json(code, payload)
                    return
                # Незнакомое исключение: наружу — только класс и текст, без
                # трейсбека. Трейсбек в браузере — это пути установки и куски
                # чужого кода в ответе, который пользователь пришлёт в поддержку.
                self._send_json(500, {
                    "error": f"Внутренняя ошибка канала обновлений: {type(exc).__name__}: {exc}",
                    "kind": "unknown"})
                return

            self._send_json(200, {"ok": True, "result": result,
                                  "status": self._companion_status_dict()})

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
                self._api_stands(parsed)
                return

            if path == "/api/events":
                # SSE-поток. EventSource не умеет слать кастомные заголовки —
                # авторизация проходит по session-cookie (HttpOnly+SameSite=
                # Strict), т.е. ровно тот же _authorize_read, что и у прочих
                # GET /api/*, без послаблений.
                if not self._authorize_read():
                    return
                self._api_events()
                return

            if path == "/api/settings":
                if not self._authorize_read():
                    return
                self._api_settings_get()
                return

            if path == "/api/version":
                if not self._authorize_read():
                    return
                self._api_version()
                return

            if path == "/api/agent/status":
                if not self._authorize_read():
                    return
                self._api_agent_status()
                return

            if path == "/api/companion/status":
                # Чтение статуса канала — обычный GET /api/*: токен из cookie
                # ИЛИ заголовка. Отвечает и при выключенном канале (см.
                # _api_companion_status), и в свободной редакции (503).
                if not self._authorize_read():
                    return
                self._api_companion_status()
                return

            m = _SECRET_RE.match(path)
            if m:
                if not self._authorize_read():
                    return
                self._api_secret_get(m.group("ref"))
                return

            m = _STAND_LOGS_SUB_RE.match(path)
            if m:
                # Единственный суб-путь — "open-folder", он только POST
                # (мутация: запускает процесс на хосте). GET сюда — 404.
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

            action = COMPANION_ACTION_ROUTES.get(path)
            if action is not None:
                # Все действия канала — мутации без исключений: даже «просто
                # проверить обновление» ходит в сеть с лицензионным конвертом.
                if not self._authorize_mutation():
                    return
                self._api_companion_action(action)
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

            if path == _STAND_REGISTER_PATH:
                if not self._authorize_mutation():
                    return
                self._api_stand_register()
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

            if path == _IIS_DETECT_PATH:
                if not self._authorize_mutation():
                    return
                self._api_iis_detect()
                return

            m = _STAND_ACTION_RE.match(path)
            if m and m.group("action") in ("start", "stop", "restart"):
                if not self._authorize_mutation():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                self._api_stand_action(
                    m.group("name"), m.group("action"), force=self._force_from_qs(parsed)
                )
                return
            if m and m.group("action") == "adopt":
                # Усыновление — мутация (пишет pidfile и открывает диспетчеру
                # возможность убить найденный процесс), поэтому та же связка
                # CSRF-заголовок + локальный Origin, что у stop/restart.
                if not self._authorize_mutation():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                self._api_stand_adopt(m.group("name"))
                return
            if m and m.group("action") == "redis-clear":
                if not self._authorize_mutation():
                    return
                if not _security.validate_stand_name(m.group("name")):
                    self._send_json(400, {"error": "invalid stand name"})
                    return
                self._api_stand_redis_clear(m.group("name"))
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


def poll_interval_of(config_path: Path) -> float:
    """
    Желаемый период фонового опроса в секундах — ``refresh_interval_sec`` из
    конфига хаба.

    Читается ПЕРЕД каждым ожиданием поллера (см. ``StatusPoller``), поэтому
    правка настройки применяется без перезапуска хаба. Нижняя граница —
    забота поллера (``MIN_POLL_INTERVAL_SEC``), здесь её не дублируем.
    Битый/недоступный конфиг не имеет права уронить фоновый поток: в этом
    случае отдаём дефолт.
    """
    try:
        return float(_load_config(config_path).refresh_interval_sec)
    except (OSError, TypeError, ValueError):
        return float(HubConfig().refresh_interval_sec)


class HubHTTPServer(ThreadingHTTPServer):
    """
    ``ThreadingHTTPServer`` + фоновый опрос стендов + канал обновлений издателя.

    Поллер живёт ровно столько же, сколько сам сервер: стартует после
    успешного bind (если бы bind упал, лишний поток вообще не создавался бы) и
    останавливается в ``server_close()``. Обработчики достают его через
    ``self.server.status_poller`` (см. ``Handler._poller``) — атрибут может
    быть ``None``, если сервер подняли с ``poll=False``.

    Планировщик канала (``companion_runner``) поднимается там же и по тем же
    правилам, но при ДВУХ дополнительных условиях: установлена редакция с
    каналом (пакет ``standkit_companion``) и включён главный рубильник
    ``companion.enabled``. Выключенный настройкой канал не поднимает поток
    вовсе — «выключен» обязано означать «ничего не тикает», а не «тикает, но
    молчит»; статус в этом случае отдаётся снимком с диска
    (``runner.status_snapshot``).
    """

    daemon_threads = True

    def __init__(self, server_address, handler_cls, *, config_path: Path, poll: bool = True):
        super().__init__(server_address, handler_cls)
        self.config_path = config_path
        self.status_poller: Optional[StatusPoller] = None
        # Канал обновлений: атрибуты существуют ВСЕГДА, включая свободную
        # редакцию, — обработчики читают их через getattr и не обязаны знать,
        # какая редакция установлена.
        self.companion_runner = None
        self.companion_error = ""
        # Канал поднимается ДО поллера намеренно. Он читает конфиг и файл
        # состояния, то есть занимает несколько миллисекунд, — и делай он это
        # ПОСЛЕ старта поллера, эти миллисекунды растянули бы промежуток между
        # первым фоновым опросом и первым запросом клиента. Промежуток не
        # безобидный: ``_api_stands`` отдаёт готовый снапшот поллера, как только
        # тот появился, и «свежесть» ответа сразу после старта зависела бы от
        # того, сколько работы успело набежать между ними.
        if companion_available():
            try:
                enabled = bool(HubConfig.load(config_path).companion.enabled)
            except Exception as exc:  # noqa: BLE001 - битый конфиг не роняет хаб
                enabled = False
                self.companion_error = f"настройки канала не прочитаны: {exc}"
            if enabled:
                # Отказ старта канала НЕ роняет сервер: хаб — про управление
                # стендами, и они обязаны работать, даже если канал обновлений
                # не поднялся. Причина оседает в состоянии сервера и уходит в
                # статус вкладки «Обновления», а не в трейсбек при запуске.
                self.companion_runner, error = build_companion_runner(config_path)
                if error:
                    self.companion_error = error
        if poll:
            self.status_poller = StatusPoller(
                build=lambda: build_snapshot(config_path, probe=True),
                interval=lambda: poll_interval_of(config_path),
            )
            self.status_poller.start()

    def server_close(self) -> None:
        # Сначала гасим фоновые потоки, потом закрываем сокет: иначе поллер мог
        # бы продолжать пробы, а канал — тик уже после «остановки» хаба. Для
        # канала это к тому же испорченные соседние тесты: забытый поток
        # продолжает дёргать модульные функции, которые следующий тест
        # подменяет через monkeypatch (ровно та история, что у поллера).
        poller = getattr(self, "status_poller", None)
        if poller is not None:
            poller.stop()
            self.status_poller = None
        companion = getattr(self, "companion_runner", None)
        if companion is not None:
            try:
                companion.stop()
            except Exception:  # noqa: BLE001 - уборка не имеет права упасть
                pass
            self.companion_runner = None
        super().server_close()


def create_hub_server(
    host: str,
    port: int,
    *,
    config_path: Path,
    session_token: str,
    web_dir: Optional[Path] = None,
    insecure: bool = False,
    poll: bool = True,
) -> HubHTTPServer:
    """
    Биндит и возвращает готовый ``HubHTTPServer`` (БЕЗ ``serve_forever``)
    — вынесено отдельно от блокирующего запуска, чтобы вызывающая сторона
    (``standkit_hub.__main__``) могла узнать реальный порт (важно при
    ``port=0`` — эфемерный порт) ДО того, как открыть браузер, и чтобы тесты
    могли поднимать сервер в отдельном потоке без дублирования bind-логики.

    Fail-closed bind-проверка (см. standkit_hub.security.validate_bind_security)
    выполняется ДО открытия сокета — как и у headless-агента.

    ``poll=False`` поднимает сервер БЕЗ фонового опроса — тогда ``GET
    /api/stands`` честно опрашивает стенды синхронно (прежнее поведение), а
    ``GET /api/events`` отвечает 503.
    """
    _security.validate_bind_security(host, tls_enabled=False, insecure=insecure)
    handler_cls = make_handler(config_path=config_path, session_token=session_token, web_dir=web_dir)
    return HubHTTPServer((host, port), handler_cls, config_path=config_path, poll=poll)


# Коды ошибок bind'а, которые означают «порт занят/недоступен» и оправдывают
# откат на эфемерный порт. EACCES — Windows отдаёт его, когда порт попал в
# исключённый диапазон (netsh interface portproxy, Hyper-V) — для пользователя
# это ровно то же «порт занять нельзя».
_PORT_BUSY_ERRNOS = frozenset({errno.EADDRINUSE, errno.EACCES})


def probe_hub_instance(host: str, port: int, *, timeout: float = 1.5) -> Optional[str]:
    """
    Отвечает ли на ``host:port`` уже работающий standkit-hub?

    Возвращает строку версии из заголовка ``X-Standkit-Hub`` либо ``None``,
    если порт занят чем-то другим (или не отвечает вовремя).

    ЗАЧЕМ. Ярлык на рабочем столе запускает хаб через ``pythonw.exe`` — без
    консоли. Закрытие окна браузера НЕ останавливает процесс (сервер живёт в
    ``serve_forever``, idle-shutdown нет), поэтому повторный клик по ярлыку
    раньше поднимал ВТОРОЙ экземпляр: порт 8770 занят → откат на эфемерный →
    два фоновых поллера дёргают health-пробы и ``subprocess`` бэкендов
    хостинга над одним ``projects.json``, а origin у второго окна другой, то
    есть своя копия localStorage. Сообщение об откате уходило в ``stderr``,
    которого при ``pythonw`` никто не видит.

    Запрос НАМЕРЕННО неаутентифицированный: сессионного токена работающего
    экземпляра у нас нет и быть не может. Поэтому опознание идёт по
    заголовку, который хаб ставит на любой ответ, включая 401 — отдавать
    токен или иную чувствительную информацию в обмен на «постучались с
    loopback» нельзя, это был бы обход аутентификации.

    Сетевые ошибки здесь не исключение, а нормальный ответ «не хаб»: чужой
    сервис может рвать соединение, отвечать мусором или молчать. Любой сбой
    трактуем консервативно — ``None``, то есть «не наш», и вызывающий уходит
    в прежний откат на эфемерный порт.
    """
    import http.client

    conn = None
    try:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        # HEAD, а не GET: опознавательный заголовок есть и здесь, а тело
        # (index.html целиком) читать незачем.
        conn.request("HEAD", "/")
        resp = conn.getresponse()
        value = resp.getheader(HUB_IDENTITY_HEADER)
        return value or None
    except Exception:
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


class HubAlreadyRunning(RuntimeError):
    """
    На запрошенном порту уже работает standkit-hub.

    Не ошибка в привычном смысле, а сигнал вызывающему (CLI): второй процесс
    поднимать не нужно, надо открыть браузер на уже работающем экземпляре.
    """

    def __init__(self, host: str, port: int, version: Optional[str] = None) -> None:
        self.host = host
        self.port = port
        self.version = version
        super().__init__(f"диспетчер уже работает на {host}:{port}")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


def bind_hub_server(
    host: str,
    port: int,
    *,
    config_path: Path,
    session_token: str,
    web_dir: Optional[Path] = None,
    insecure: bool = False,
    poll: bool = True,
    on_fallback: Optional[Callable[[int, OSError], None]] = None,
    single_instance: bool = True,
) -> HubHTTPServer:
    """
    ``create_hub_server`` + откат на эфемерный порт, если запрошенный занят.

    ЗАЧЕМ. Хаб слушает ФИКСИРОВАННЫЙ порт (``DEFAULT_HUB_PORT``), потому что
    origin (схема+хост+порт) — ключ браузерного localStorage и HTTP-кэша:
    на эфемерном порту каждый запуск давал новый origin, пустое хранилище и
    холодный кэш статики. Но фиксированный порт можно и не получить (второй
    экземпляр хаба, чужой сервис) — падать из-за этого нельзя, поэтому
    занятый порт — не ошибка, а откат на эфемерный.

    ``on_fallback(requested_port, exc)`` вызывается ПЕРЕД повторным bind'ом,
    чтобы вызывающий (CLI) мог сообщить пользователю причину. Ошибки, не
    связанные с занятостью порта, пробрасываются как есть — «честный отказ».

    ``single_instance=True`` (по умолчанию): перед bind'ом проверяем, не занял
    ли порт НАШ ЖЕ работающий хаб (см. ``probe_hub_instance``) — если да,
    второй экземпляр не поднимается, бросается ``HubAlreadyRunning``, и CLI
    просто открывает браузер на уже работающем. Откат на эфемерный остаётся
    для случая «порт занял чужой сервис»: тогда второй хаб действительно нужен.
    ``single_instance=False`` возвращает прежнее безусловное поведение и
    нужен тестам, которым надо поднять два хаба подряд.
    """
    # Проверка ДО bind'а, а не в обработчике OSError — и это принципиально.
    # ``ThreadingHTTPServer.allow_reuse_address = 1`` (SO_REUSEADDR), а на
    # Windows SO_REUSEADDR разрешает второму процессу занять УЖЕ слушаемый
    # порт: bind проходит успешно, EADDRINUSE не возникает, и два хаба
    # оказываются на одном 8770 — входящие соединения распределяются между
    # ними непредсказуемо. То есть на Windows отката на эфемерный порт даже не
    # случалось, было тихое раздвоение (обнаружено тестом
    # tests/test_hub_single_instance.py). Единственный переносимый способ
    # узнать правду — постучаться и посмотреть, кто ответит.
    if single_instance and port != 0:
        running = probe_hub_instance(host, port)
        if running is not None:
            raise HubAlreadyRunning(host, port, running)

    try:
        return create_hub_server(
            host,
            port,
            config_path=config_path,
            session_token=session_token,
            web_dir=web_dir,
            insecure=insecure,
            poll=poll,
        )
    except OSError as exc:
        if port == 0 or exc.errno not in _PORT_BUSY_ERRNOS:
            raise
        if on_fallback is not None:
            on_fallback(port, exc)
        return create_hub_server(
            host,
            0,
            config_path=config_path,
            session_token=session_token,
            web_dir=web_dir,
            insecure=insecure,
            poll=poll,
        )
