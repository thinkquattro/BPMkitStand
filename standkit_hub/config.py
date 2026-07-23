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
            else:
                kwargs[key] = value
        return cls(**kwargs)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["agents"] = [a.to_dict() for a in self.agents]
        return result
