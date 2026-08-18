"""
Модели данных ядра standkit: описание стенда (Stand) и его состояния (StandStatus).

Поля Stand буквально повторяют схему записи в реестре projects.json (см.
projects.sample.json в корне репозитория) плюс универсальное поле транспорта
``transport``, которое определяет, как ядро должно управлять стендом:

- "local" — стенд поднимается локально текущим процессом standkit (subprocess,
  см. standkit.platform / standkit.lifecycle);
- "agent" — стенд управляется через удалённый standkit_agent по HTTP
  (используются agent_url / agent_secret_ref, а для TLS-канала до агента —
  agent_ca / agent_verify_tls);
- "ssh" / "winrm" — зарезервировано под будущие транспорты, СХЕМОЙ допускается,
  логика НЕ реализована (бэклог, см. docs/ARCHITECTURE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import Enum
from typing import Any, Optional


class Transport(str, Enum):
    """Способ, которым ядро/GUI дотягивается до стенда."""

    LOCAL = "local"
    AGENT = "agent"
    # Задел на будущее — схема допускает значения, реализации пока нет.
    SSH = "ssh"
    WINRM = "winrm"


class HostKind(str, Enum):
    """
    Как стенд ХОСТИТСЯ на своей машине — ортогонально ``Transport`` (тот
    определяет, ГДЕ управлять стендом: локально или через агента).

    См. ADR-0001 (docs/adr/0001-hosting-backends.md) и standkit.hosting.
    """

    KESTREL = "kestrel"
    IIS = "iis"
    DOCKER = "docker"
    # Реализован с standkit 0.5.0 (ADR-0002, standkit.hosting.KubernetesBackend);
    # живая приёмка на кластере — 17.08.2026.
    K8S = "k8s"


class ProbeState(str, Enum):
    """Единое множество состояний для любой health-пробы."""

    OK = "ok"
    DOWN = "down"
    UNKNOWN = "unknown"
    # Проба сознательно не выполнялась (например, глубокая БД-проба выключена флагом).
    SKIPPED = "skipped"


# Поля, обязательные для валидной записи Stand (без них смысла в объекте нет).
_REQUIRED_FIELDS = ("name", "stand_dir")


@dataclass
class Stand:
    """
    Описание одного стенда BPMSoft — один к одному со схемой записи в projects.json.

    ``name`` — ключ записи в реестре (не хранится внутри самой записи в JSON,
    проставляется registry.py при чтении).
    """

    name: str

    # --- Транспорт управления стендом ---
    transport: Transport = Transport.LOCAL
    agent_url: Optional[str] = None
    agent_secret_ref: Optional[str] = None
    # Доверие к TLS-сертификату АГЕНТА — канал «хаб → агент» (standkit_hub.client).
    #
    # ЭТО НЕ ТО ЖЕ САМОЕ, что stand_scheme/verify_tls ниже: те описывают
    # health-пробу САМОГО СТЕНДА (хаб/агент стучится в web-хост BPMSoft), а
    # эта пара — управляющее HTTPS-соединение хаба с демоном standkit_agent.
    # Пути разные, сертификаты разные, и оператор, выключивший verify_tls,
    # ничего не менял для канала до агента (GAP-008: ровно на этом он и
    # спотыкается — «я же снял проверку, почему всё равно
    # CERTIFICATE_VERIFY_FAILED»).
    #
    # agent_ca — путь к сертификату агента (или CA-бандлу, которым он подписан)
    # НА МАШИНЕ, где работает хаб; штатный способ доверять самоподписанному
    # сертификату из кукбука по развёртыванию агента.
    agent_ca: Optional[str] = None
    # agent_verify_tls=False — осознанное отключение проверки цепочки для
    # канала до агента (дев-контур). Отдельным флагом, а не «пустой agent_ca
    # значит не проверять»: молчаливое отключение TLS-проверки по умолчанию —
    # худшее, что можно сделать с управляющим каналом.
    agent_verify_tls: bool = True

    # --- Хостинг стенда (см. ADR-0001, standkit.hosting) ---
    host_kind: HostKind = HostKind.KESTREL
    # iis (используются, только если host_kind == IIS)
    iis_site: Optional[str] = None
    iis_app_pool: Optional[str] = None
    iis_stdout_log_dir: Optional[str] = None
    # docker (используются, только если host_kind == DOCKER)
    docker_container: Optional[str] = None
    docker_compose_file: Optional[str] = None
    docker_compose_service: Optional[str] = None
    # k8s (используются, только если host_kind == K8S)
    k8s_namespace: str = ""
    k8s_deployment: Optional[str] = None
    k8s_context: Optional[str] = None
    k8s_container: Optional[str] = None
    k8s_replicas: int = 1

    # --- Процесс стенда ---
    stand_dir: str = ""
    # Явный каталог логов стенда. Пусто — резолвится по факту внутри stand_dir
    # (см. standkit.logs.stand_logs_dir): имя подкаталога сравнивается БЕЗ
    # УЧЁТА РЕГИСТРА, потому что BPMSoft на Linux пишет в "Logs", а на Windows
    # регистронезависимая ФС всё это время прощала жёсткое "logs".
    logs_dir: str = ""
    stand_dll: str = "BPMSoft.WebHost.dll"
    dotnet: str = "dotnet"
    stand_host: str = "127.0.0.1"
    stand_port: int = 5000
    # Схема health-пробы стенда. Стенды за TLS (типовой дев-контур BPMSoft за
    # nginx/Kestrel с HTTPS) на http:// не отвечают вовсе, и проба врала «down»
    # на живом стенде. Дефолт "http" полностью повторяет прежнее поведение.
    stand_scheme: str = "http"
    # Проверять ли цепочку сертификатов при stand_scheme=https. Для self-signed
    # на дев-контурах — false, иначе проба падает на SSLCertVerificationError.
    verify_tls: bool = True

    # --- База данных ---
    db_type: str = "postgres"
    db_host: str = ""
    db_port: int = 0
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""
    secret_ref_db: Optional[str] = None

    # --- Redis ---
    # Адрес Redis С ТОЧКИ ЗРЕНИЯ ХОСТА, где выполняется проба (ядро или агент),
    # а не с машины оператора. До 0.8.0 эти ключи жили нетипизированными в
    # ``extra`` — чтение оттуда сохранено как фолбэк для старых реестров.
    redis_host: str = ""
    redis_port: int = 0

    # --- Администратор стенда ---
    admin_user: str = "Supervisor"
    secret_ref_admin: Optional[str] = None

    # --- Прочее ---
    distrib_dir: str = ""
    description: str = ""
    customer: str = ""

    # Произвольные дополнительные поля из реестра, которые ядро не знает явно,
    # но не хочет терять при повторной записи (forward-compatibility).
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Stand":
        """
        Строит Stand из словаря записи реестра (как он приходит из JSON).

        Неизвестные поля не теряются — уходят в ``extra``, чтобы round-trip
        чтение→запись не терял данные, добавленные другими инструментами
        экосистемы (например, provision_stand из BPMkit).
        """
        known = {f.name for f in fields(cls)} - {"name", "extra"}
        kwargs: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        for key, value in data.items():
            if key == "transport":
                kwargs["transport"] = _coerce_transport(value)
            elif key == "host_kind":
                kwargs["host_kind"] = _coerce_host_kind(value)
            elif key == "stand_scheme":
                kwargs["stand_scheme"] = _coerce_scheme(value)
            elif key == "verify_tls":
                kwargs["verify_tls"] = _coerce_bool(value, default=True)
            elif key == "agent_verify_tls":
                # Тем же терпимым чтением, что verify_tls: реестр правят руками,
                # и "false" строкой должно означать false, а мусор — безопасный
                # дефолт True (проверять), а не молчаливое отключение проверки.
                kwargs["agent_verify_tls"] = _coerce_bool(value, default=True)
            elif key == "redis_port":
                # Реестр правят руками и внешние инструменты: "6379" строкой —
                # частый случай. Мусор превращается в 0, и его ловит validate().
                kwargs["redis_port"] = _coerce_int(value, default=0)
            elif key in known:
                kwargs[key] = value
            elif key == "name":
                continue
            else:
                extra[key] = value
        return cls(name=name, extra=extra, **kwargs)

    def to_dict(self) -> dict:
        """Сериализует запись обратно в словарь для записи в projects.json (без ``name``)."""
        result: dict[str, Any] = {}
        for f in fields(self):
            if f.name in ("name", "extra"):
                continue
            value = getattr(self, f.name)
            if f.name == "transport":
                value = value.value if isinstance(value, Transport) else value
            elif f.name == "host_kind":
                value = value.value if isinstance(value, HostKind) else value
            result[f.name] = value
        result.update(self.extra)
        return result

    def validate(self) -> list[str]:
        """
        Минимальная валидация записи. Возвращает список текстов ошибок
        (пустой список — запись валидна). Не бросает исключений намеренно —
        вызывающий код сам решает, насколько строго реагировать.
        """
        errors: list[str] = []
        if not self.name:
            errors.append("name не может быть пустым")
        if not self.stand_dir:
            errors.append("stand_dir не может быть пустым")
        if str(self.stand_scheme).lower() not in ("http", "https"):
            errors.append("stand_scheme должен быть http или https")
        if self.transport == Transport.AGENT:
            if not self.agent_url:
                errors.append("transport=agent требует agent_url")
        errors.extend(self._validate_agent_trust())
        if self.host_kind == HostKind.IIS:
            if not (self.iis_site or self.iis_app_pool):
                errors.append("host_kind=iis требует iis_site и/или iis_app_pool")
        if self.host_kind == HostKind.DOCKER:
            has_single = bool(self.docker_container)
            has_compose = bool(self.docker_compose_file and self.docker_compose_service)
            if not (has_single or has_compose):
                errors.append(
                    "host_kind=docker требует docker_container либо "
                    "(docker_compose_file И docker_compose_service)"
                )
        if self.host_kind == HostKind.K8S:
            if not self.k8s_deployment:
                errors.append("host_kind=k8s требует k8s_deployment")
        errors.extend(self._validate_redis())
        return errors

    def _validate_agent_trust(self) -> list[str]:
        """
        Проверки пары ``agent_ca``/``agent_verify_tls`` (доверие к сертификату
        АГЕНТА, GAP-008).

        Что здесь СОЗНАТЕЛЬНО не проверяется и почему:

        1. Существование файла ``agent_ca``. ``validate()`` — чистая проверка
           записи, она не ходит в файловую систему НИГДЕ (``stand_dir``,
           ``distrib_dir``, ``logs_dir`` тоже не проверяются на существование),
           и вызывается она в том числе на машине, где этого файла и не должно
           быть: одна и та же запись реестра живёт и на хосте агента, и у
           оператора, а путь к сертификату актуален только для машины хаба.
           Проверка тут ломала бы ``Registry.update`` на ровном месте. Отказ
           обязан быть понятным в момент РЕАЛЬНОГО обращения — за это отвечает
           ``standkit_hub.client``: он называет путь в тексте ошибки.
        2. ``agent_ca`` при ``transport != agent`` — не ошибка. Запись
           переключают между local и agent туда-обратно, и терять уже
           введённый путь (или падать из-за него) вреднее, чем держать
           неиспользуемое поле; ровно так же ведут себя ``iis_*``/``docker_*``
           при другом ``host_kind``.

        А вот сочетание «``agent_ca`` задан + ``agent_url`` без https» — именно
        ошибка, а не «просто не применится»: молча проигнорированная настройка
        это и есть механика GAP-008 (оператор задал поле, поверил, что оно
        подействовало, и ищет причину в другом месте).
        """
        errors: list[str] = []
        url = str(self.agent_url or "").strip().lower()
        if self.agent_ca and url and not url.startswith("https://"):
            errors.append(
                "agent_ca имеет смысл только при agent_url на https:// — "
                "по http канал до агента не шифруется и сертификат не проверяется"
            )
        return errors

    def _validate_redis(self) -> list[str]:
        """
        Пара ``redis_host``/``redis_port`` валидна только целиком: половина
        пары раньше давала молчаливый ``unknown`` в дашборде, и оператор не мог
        отличить «не настроено» от «настроено с опечаткой» (GAP-003).
        """
        errors: list[str] = []
        host = str(self.redis_host or "").strip()
        port = _coerce_int(self.redis_port, default=0)
        # Ветки ВЗАИМОИСКЛЮЧАЮЩИЕ и упорядочены от частного к общему: оператор
        # должен получить ОДИН точный диагноз. Раньше сочетание «redis_host
        # задан + redis_port = -1» давало сразу две пересекающиеся строки («без
        # корректного redis_port» и «должен быть в диапазоне»), и было
        # непонятно, это одна проблема или две.
        if port and not 0 < port < 65536:
            # Сам порт невалиден — это и есть диагноз, независимо от host.
            errors.append("redis_port должен быть в диапазоне 1–65535")
        elif host and port <= 0:
            errors.append("redis_host задан без корректного redis_port (1–65535)")
        elif port > 0 and not host:
            errors.append("redis_port задан без redis_host")
        return errors


def _coerce_scheme(value: Any) -> str:
    """
    Приводит схему health-пробы к "http"/"https". Мусор из реестра не роняет
    чтение — откатываемся на "http", а несоответствие поймает validate().
    """
    text = str(value).strip().lower()
    return text if text in ("http", "https") else "http"


def _coerce_bool(value: Any, *, default: bool) -> bool:
    """
    Терпимо читает булево из JSON: настоящий bool, а также строки вида
    "true"/"false"/"1"/"0" (реестр правят руками и внешние инструменты).
    """
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "y"):
        return True
    if text in ("false", "0", "no", "n"):
        return False
    return default


def _coerce_int(value: Any, *, default: int) -> int:
    """Терпимо читает целое из JSON (реестр правят руками): "6379" → 6379, мусор → default."""
    if isinstance(value, bool):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_transport(value: Any) -> Transport:
    if isinstance(value, Transport):
        return value
    try:
        return Transport(str(value))
    except ValueError:
        # Неизвестное будущее значение транспорта — не роняем чтение реестра,
        # оставляем как UNKNOWN-эквивалент через LOCAL с пометкой в extra.
        return Transport.LOCAL


def _coerce_host_kind(value: Any) -> HostKind:
    if isinstance(value, HostKind):
        return value
    try:
        return HostKind(str(value))
    except ValueError:
        # Неизвестное будущее значение хостинга — не роняем чтение реестра,
        # откатываемся на дефолтный kestrel.
        return HostKind.KESTREL


@dataclass
class StandStatus:
    """
    Снимок состояния стенда по всем доступным пробам разом.

    Каждое поле — состояние независимой пробы (см. standkit.health):
    - process: жив ли процесс стенда (по pid-файлу);
    - http: отвечает ли web-хост стенда по HTTP;
    - db: открыт ли TCP-порт БД (поверхностная проба, не полноценный запрос);
    - redis: открыт ли TCP-порт Redis (если используется);
    - last_deploy: состояние последнего деплоя — вне ядра здоровья, задел на
      будущее для витрины GUI (источник данных пока не определён).
    """

    name: str
    process: ProbeState = ProbeState.UNKNOWN
    http: ProbeState = ProbeState.UNKNOWN
    db: ProbeState = ProbeState.UNKNOWN
    redis: ProbeState = ProbeState.UNKNOWN
    last_deploy: ProbeState = ProbeState.UNKNOWN
    details: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "StandStatus":
        """Строит StandStatus из словаря (например, ответа агента по HTTP)."""
        kwargs: dict[str, Any] = {"name": data.get("name", "")}
        for key in ("process", "http", "db", "redis", "last_deploy"):
            if key in data:
                kwargs[key] = _coerce_probe_state(data[key])
        kwargs["details"] = dict(data.get("details", {}))
        return cls(**kwargs)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "process": self.process.value,
            "http": self.http.value,
            "db": self.db.value,
            "redis": self.redis.value,
            "last_deploy": self.last_deploy.value,
            "details": self.details,
        }

    @property
    def is_healthy(self) -> bool:
        """Грубая сводная оценка: процесс и HTTP в порядке (БД/Redis — опциональны)."""
        return self.process == ProbeState.OK and self.http in (ProbeState.OK, ProbeState.SKIPPED)


def _coerce_probe_state(value: Any) -> ProbeState:
    if isinstance(value, ProbeState):
        return value
    try:
        return ProbeState(str(value))
    except ValueError:
        return ProbeState.UNKNOWN
