"""
Конфиг веб-дашборда standkit (``standkit-hub.json``) — ВСЕ параметры,
которые иначе пришлось бы задавать флагами ``--host``/``--port``/... в
терминале при запуске headless-агента, плюс параметры самого хаба (реестр,
каталоги, интервал автообновления, список удалённых агентов федерации).

Намеренно НЕ импортирует ничего из ``http.server``/веб-слоя — модуль должен
быть тестируемым в изоляции (см. tests/test_hub_config.py). Отдаётся/
принимается фронтендом хаба через ``GET/POST /api/settings`` (см.
standkit_hub/server.py).

Путь конфига — та же папка BPMkit, что и реестр стендов (см.
standkit.registry.bpmkit_config_dir):
    Windows: %APPDATA%\\BPMkit\\standkit-hub.json
    POSIX:   ~/.config/BPMkit/standkit-hub.json  (или $XDG_CONFIG_HOME/BPMkit/...)

Секреты (control/readonly-токены агентов) в конфиге хранятся ТОЛЬКО как ссылки
(``*_ref``) на standkit.secrets — значения самих секретов сюда никогда не
попадают.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from standkit.registry import bpmkit_config_dir, default_registry_path

_CONFIG_FILE_NAME = "standkit-hub.json"

# Значения по умолчанию для параметров запуска локального агента (совпадают
# с default'ами standkit_agent/__main__.py — см. DEFAULT_LOCKOUT_* там же).
_DEFAULT_AGENT_HOST = "127.0.0.1"
_DEFAULT_AGENT_PORT = 8765
_DEFAULT_LOCKOUT_MAX_FAILURES = 5
_DEFAULT_LOCKOUT_WINDOW_SEC = 300.0
_DEFAULT_REFRESH_INTERVAL_SEC = 10

# Тема оформления дашборда. Источник правды — ИМЕННО конфиг, а не
# localStorage браузера: localStorage привязан к origin (включая порт), а хаб
# исторически стартовал на эфемерном порту — каждый запуск давал новый origin
# и, как следствие, пустое хранилище («тема не запоминается»). localStorage
# остаётся лишь клиентским кэшем, чтобы тема применилась до ответа /api/settings.
HUB_THEMES = ("light", "dark", "auto")
_DEFAULT_THEME = "auto"


def normalize_theme(value: object) -> str:
    """
    Приводит значение темы к одному из ``HUB_THEMES``.

    Неизвестное/битое значение (в т.ч. из руками правленого конфига) молча
    откатывается на ``auto`` — тема не тот параметр, ради которого стоит
    ронять запуск дашборда.
    """
    if isinstance(value, str) and value.strip().lower() in HUB_THEMES:
        return value.strip().lower()
    return _DEFAULT_THEME


@dataclass
class RemoteAgent:
    """Одна запись федерации удалённых агентов (мульти-агентная панель хаба)."""

    name: str = ""
    url: str = ""
    # Ссылка на секрет токена (standkit.secrets), НЕ сам токен.
    token_ref: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "RemoteAgent":
        return cls(
            name=str(data.get("name", "")),
            url=str(data.get("url", "")),
            token_ref=str(data.get("token_ref", "")),
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "token_ref": self.token_ref}


@dataclass
class CompanionCycle:
    """Один цикл канала обновлений: включён ли и как часто опрашивать.

    Интервал хранится в СЕКУНДАХ, а в форме настроек показывается в минутах/часах:
    единица измерения — вопрос представления, а конфиг обязан быть однозначным.
    """

    enabled: bool = False
    interval_sec: int = 1800

    @classmethod
    def from_dict(cls, data: object, *, default_enabled: bool, default_interval: int,
                  min_interval: int) -> "CompanionCycle":
        if not isinstance(data, dict):
            return cls(enabled=default_enabled, interval_sec=default_interval)
        enabled = data.get("enabled", default_enabled)
        raw = data.get("interval_sec", default_interval)
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = default_interval
        # Нижняя граница — не вкусовщина: слишком частый опрос лицензированного
        # эндпоинта выглядит на стороне издателя как перебор ключа и бессмысленно
        # нагружает единственный синхронный воркер бэкенда.
        return cls(enabled=bool(enabled), interval_sec=max(min_interval, interval))

    def to_dict(self) -> dict:
        return {"enabled": bool(self.enabled), "interval_sec": int(self.interval_sec)}


# Дефолты и нижние границы циклов канала. Паттерны включены (markdown, не исполняемый
# код — sha256 достаточно); релизы ВЫКЛЮЧЕНЫ до выпуска ключа подписи артефактов и
# переезда бэкенда на HTTPS (ADR-0022, блокеры Б1/Б2) и включаются владельцем явно.
_COMPANION_PATTERNS_INTERVAL = 1800        # 30 минут
_COMPANION_PATTERNS_MIN_INTERVAL = 300     # 5 минут
_COMPANION_RELEASES_INTERVAL = 86400       # раз в сутки
_COMPANION_RELEASES_MIN_INTERVAL = 3600    # час
_COMPANION_REVOCATIONS_INTERVAL = 1800     # вместе с паттернами
_COMPANION_REVOCATIONS_MIN_INTERVAL = 300


@dataclass
class CompanionSettings:
    """Настройки канала доставки обновлений издателя (пакет ``standkit_companion``).

    Секция живёт в конфиге ХАБА, а не в отдельном файле: канал поднимает процесс хаба,
    и раздвоение источников настроек — гарантированный источник расхождений «в UI одно,
    в службе другое».

    Чего здесь НЕТ и не будет:

    * лицензионного конверта и любых токенов — конверт резолвится у CLI самого MCP на
      каждый тик, в конфиге ему не место (инвариант SECURITY.md: в конфиге только ``*_ref``);
    * флага «не проверять подпись бинаря». Политика бинаря — строго fail-closed БЕЗ
      возможности отключения: автоапдейтер тянет и ИСПОЛНЯЕТ код. Отключаемым сделан
      только строгий режим для ПАТТЕРНОВ (``require_pattern_signature``), потому что там
      подписи ещё нет на стороне издателя и дефолт «не требовать» — единственный, при
      котором канал вообще работает.
    """

    # Главный рубильник: False — канал не поднимается вовсе, тиков нет.
    enabled: bool = True
    # Пусто — берётся дефолт клиентского MCP (env BPMKIT_BACKEND_URL поверх адреса
    # издателя). Своей второй константы адреса канал не заводит (GAP-73).
    backend_url: str = ""
    # Путь к CLI BPMkit (bpmkit.exe / python -m bpmkit). Пусто — автодетект рядом с
    # поставкой. Нужен ТОЛЬКО чтобы спросить лицензионный контекст, не для логики канала.
    mcp_cli: str = ""
    patterns: CompanionCycle = field(default_factory=lambda: CompanionCycle(
        enabled=True, interval_sec=_COMPANION_PATTERNS_INTERVAL))
    releases: CompanionCycle = field(default_factory=lambda: CompanionCycle(
        enabled=False, interval_sec=_COMPANION_RELEASES_INTERVAL))
    revocations: CompanionCycle = field(default_factory=lambda: CompanionCycle(
        enabled=True, interval_sec=_COMPANION_REVOCATIONS_INTERVAL))
    # Скачанный релиз кладётся в стейджинг автоматически (но НЕ применяется: подмена
    # бинаря — всегда явное действие человека, см. SECURITY.md §4.1 «никакого тихого
    # действия»).
    auto_stage_release: bool = False
    # Требовать подпись у КАЖДОГО паттерна. Дефолт False: механизма подписи markdown-тела
    # у издателя ещё нет, включение сегодня просто выключит канал целиком.
    require_pattern_signature: bool = False

    @classmethod
    def from_dict(cls, data: object) -> "CompanionSettings":
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=bool(data.get("enabled", True)),
            backend_url=str(data.get("backend_url", "") or ""),
            mcp_cli=str(data.get("mcp_cli", "") or ""),
            patterns=CompanionCycle.from_dict(
                data.get("patterns"), default_enabled=True,
                default_interval=_COMPANION_PATTERNS_INTERVAL,
                min_interval=_COMPANION_PATTERNS_MIN_INTERVAL),
            releases=CompanionCycle.from_dict(
                data.get("releases"), default_enabled=False,
                default_interval=_COMPANION_RELEASES_INTERVAL,
                min_interval=_COMPANION_RELEASES_MIN_INTERVAL),
            revocations=CompanionCycle.from_dict(
                data.get("revocations"), default_enabled=True,
                default_interval=_COMPANION_REVOCATIONS_INTERVAL,
                min_interval=_COMPANION_REVOCATIONS_MIN_INTERVAL),
            auto_stage_release=bool(data.get("auto_stage_release", False)),
            require_pattern_signature=bool(data.get("require_pattern_signature", False)),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "backend_url": self.backend_url,
            "mcp_cli": self.mcp_cli,
            "patterns": self.patterns.to_dict(),
            "releases": self.releases.to_dict(),
            "revocations": self.revocations.to_dict(),
            "auto_stage_release": bool(self.auto_stage_release),
            "require_pattern_signature": bool(self.require_pattern_signature),
        }


@dataclass
class HubConfig:
    """
    Все настраиваемые пользователем параметры веб-дашборда, чтобы не лазить
    в PowerShell/``--help``.

    Поля сгруппированы по смыслу:
    - реестр/каталоги/автообновление — сам хаб;
    - agents — федерация удалённых standkit-агентов, которых показывает хаб;
    - agent_* / tls_* / lockout_* / insecure / audit_log — дефолты для запуска
      ЛОКАЛЬНОГО агента из хаба (зеркалят флаги standkit_agent/__main__.py
      один в один, чтобы форма настроек их полностью покрывала).
    """

    # --- Хаб ---
    registry_path: str = field(default_factory=lambda: str(default_registry_path()))
    run_dir: str = ""
    log_dir: str = ""
    refresh_interval_sec: int = _DEFAULT_REFRESH_INTERVAL_SEC
    # light | dark | auto (см. normalize_theme). Подставляется сервером прямо
    # в атрибут data-theme отдаваемого index.html — тема применяется ДО
    # выполнения JS, без «мигания» светлой темой у любителей тёмной.
    theme: str = _DEFAULT_THEME

    # --- Федерация удалённых агентов ---
    agents: list[RemoteAgent] = field(default_factory=list)

    # --- Дефолты запуска локального агента (standkit_agent) ---
    agent_host: str = _DEFAULT_AGENT_HOST
    agent_port: int = _DEFAULT_AGENT_PORT
    token_ref: str = ""
    readonly_token_ref: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    tls_client_ca: str = ""
    insecure: bool = False
    audit_log: str = ""
    lockout_max_failures: int = _DEFAULT_LOCKOUT_MAX_FAILURES
    lockout_window_sec: float = _DEFAULT_LOCKOUT_WINDOW_SEC

    # --- Канал доставки обновлений издателя (пакет standkit_companion) ---
    # Секция присутствует в конфиге ВСЕГДА, даже в free-редакции без пакета: так форма
    # настроек и файл конфига не меняют форму при установке платной редакции, и
    # пользователь видит, что именно включится.
    companion: CompanionSettings = field(default_factory=CompanionSettings)

    # --- чтение/запись ---

    @classmethod
    def config_path(cls) -> Path:
        """Канонический путь к файлу конфига хаба (та же папка, что и реестр кита)."""
        return bpmkit_config_dir() / _CONFIG_FILE_NAME

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "HubConfig":
        """
        Читает конфиг из ``path`` (по умолчанию — ``config_path()``).

        Если файла нет — возвращает конфиг с дефолтами (в т.ч.
        ``registry_path = default_registry_path()``); это нормальная ситуация
        при первом запуске хаба. Файл читается как ``utf-8-sig`` (терпим к BOM
        — тот же принцип, что и в standkit.registry.Registry.load).
        """
        p = Path(path) if path is not None else cls.config_path()
        if not p.exists():
            return cls()

        raw = p.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            # Битый конфиг хаба не должен ронять запуск диспетчера — тихо
            # откатываемся на дефолты (в отличие от реестра, это не
            # критичные для управления стендами данные).
            return cls()

        return cls.from_dict(data)

    def save(self, path: Optional[str | Path] = None) -> None:
        """Пишет конфиг в ``path`` (по умолчанию — ``config_path()``), создавая папку при необходимости.

        Запись АТОМАРНАЯ (временный файл рядом → ``os.replace``). Поводом стал канал
        обновлений: его тик правит конфиг (например, включает override-корень паттернов) в
        тот же момент, когда пользователь жмёт «Сохранить» в форме настроек. Обычный
        ``write_text`` в этой гонке оставляет обрезанный JSON, а битый конфиг ``load()``
        молча заменяет дефолтами — то есть настройки исчезают без единого сообщения.
        Временный файл создаётся в ТОЙ ЖЕ папке: ``os.replace`` атомарен только в пределах
        одного тома.
        """
        p = Path(path) if path is not None else self.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    def ensure_registry_dir(self) -> Path:
        """Создаёт папку файла реестра проектов (``registry_path``), если её ещё
        нет, и возвращает её.

        Нужно при ПЕРВОМ запуске диспетчера: на свежей машине папки
        ``%APPDATA%\\BPMkit`` может не существовать, и показываемый в интерфейсе
        путь к ``projects.json`` указывает «в никуда» — попытка открыть его в
        проводнике даёт «Windows не удаётся найти …». Ранее папка появлялась
        только после первой записи (регистрация стенда / сохранение настроек)."""
        reg_parent = Path(self.registry_path).parent if self.registry_path else default_registry_path().parent
        reg_parent.mkdir(parents=True, exist_ok=True)
        return reg_parent

    # --- сериализация ---

    @classmethod
    def from_dict(cls, data: dict) -> "HubConfig":
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key == "agents":
                kwargs[key] = [RemoteAgent.from_dict(a) for a in value]
            elif key == "companion":
                kwargs[key] = CompanionSettings.from_dict(value)
            elif key == "theme":
                kwargs[key] = normalize_theme(value)
            else:
                kwargs[key] = value
        return cls(**kwargs)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["agents"] = [a.to_dict() for a in self.agents]
        result["companion"] = self.companion.to_dict()
        # Тему нормализуем и на выходе: конфиг мог быть собран напрямую
        # конструктором (в обход from_dict), а фронт обязан получать только
        # значение из HUB_THEMES.
        result["theme"] = normalize_theme(result.get("theme"))
        return result
