"""
Модели данных ядра standkit: описание стенда (Stand) и его состояния (StandStatus).

Поля Stand буквально повторяют схему записи в реестре projects.json (см.
projects.sample.json в корне репозитория) плюс универсальное поле транспорта
``transport``, которое определяет, как ядро должно управлять стендом:

- "local" — стенд поднимается локально текущим процессом standkit (subprocess,
  см. standkit.platform / standkit.lifecycle);
- "agent" — стенд управляется через удалённый standkit_agent по HTTP
  (используются agent_url / agent_secret_ref);
- "ssh" / "winrm" — зарезервировано под будущие транспорты, СХЕМОЙ допускается,
  логика НЕ реализована (см. TODO в lifecycle.py).
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

    # --- Процесс стенда ---
    stand_dir: str = ""
    stand_dll: str = "BPMSoft.WebHost.dll"
    dotnet: str = "dotnet"
    stand_host: str = "127.0.0.1"
    stand_port: int = 5000

    # --- База данных ---
    db_type: str = "postgres"
    db_host: str = ""
    db_port: int = 0
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""
    secret_ref_db: Optional[str] = None

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
        if self.transport == Transport.AGENT:
            if not self.agent_url:
                errors.append("transport=agent требует agent_url")
        return errors


def _coerce_transport(value: Any) -> Transport:
    if isinstance(value, Transport):
        return value
    try:
        return Transport(str(value))
    except ValueError:
        # Неизвестное будущее значение транспорта — не роняем чтение реестра,
        # оставляем как UNKNOWN-эквивалент через LOCAL с пометкой в extra.
        return Transport.LOCAL


@dataclass
class StandStatus:
    """
    Снимок состояния стенда по всем доступным пробам разом.

    Каждое поле — состояние независимой пробы (см. standkit.health):
    - process: жив ли процесс стенда (по pid-файлу);
    - http: отвечает ли web-хост стенда по HTTP;
    - db: открыт ли TCP-порт БД (не полноценный запрос — TODO);
    - redis: открыт ли TCP-порт Redis (если используется);
    - last_deploy: состояние последнего деплоя — вне ядра здоровья, задел на
      будущее для витрины GUI (TODO: источник данных).
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
