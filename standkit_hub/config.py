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
        """Пишет конфиг в ``path`` (по умолчанию — ``config_path()``), создавая папку при необходимости."""
        p = Path(path) if path is not None else self.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        p.write_text(text, encoding="utf-8")

    def resolve_run_dir(self) -> Path:
        """
        Каталог runtime-файлов диспетчера: ``run_dir`` из конфига, иначе
        ``~/.standkit/run``.

        Один ответ на всех, кто туда пишет: pid-файл локального агента
        (``standkit_hub.agent_control``), файл состояния экземпляра хаба и
        файл передачи сессии при перезапуске с правами администратора
        (``standkit_hub.instance`` / ``standkit_hub.elevation``). Раньше
        дефолт был захардкожен в agent_control — вторая копия неизбежно
        разъехалась бы с первой.

        Папку НЕ создаёт: решение «создавать ли» принимает тот, кто пишет.
        """
        return Path(self.run_dir) if self.run_dir else Path.home() / ".standkit" / "run"

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
            elif key == "theme":
                kwargs[key] = normalize_theme(value)
            else:
                kwargs[key] = value
        return cls(**kwargs)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["agents"] = [a.to_dict() for a in self.agents]
        # Тему нормализуем и на выходе: конфиг мог быть собран напрямую
        # конструктором (в обход from_dict), а фронт обязан получать только
        # значение из HUB_THEMES.
        result["theme"] = normalize_theme(result.get("theme"))
        return result
