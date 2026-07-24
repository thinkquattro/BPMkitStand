"""
Реестр стендов — чтение/запись projects.json.

Формат совпадает со схемой BPMkit (см. projects.sample.json в корне репозитория):
верхний уровень ``{"default": <имя>, "stands": {<имя>: {...}}}``. Файл читается как
``utf-8-sig`` (терпим к BOM, который оставляют некоторые редакторы на Windows) и
пишется БЕЗ BOM, чтобы не плодить дифф на ровном месте между инструментами.

Важно: этот модуль НЕ занимается провижинингом стендов (созданием каталогов,
БД, установкой дистрибутива) — только чтением/записью записей реестра для уже
существующих стендов. Провижининг — зона ответственности платного продукта
(BPMkit: provision_stand и т.п.).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Iterable, Optional

from standkit.models import Stand

_DEFAULT_SCHEMA_VERSION = 1

# Имя папки с общими файлами экосистемы BPMkit (реестр стендов, конфиг GUI и
# т.п.) — единая точка правды между standkit и BPMkit MCP.
_BPMKIT_DIR_NAME = "BPMkit"
_REGISTRY_FILE_NAME = "projects.json"
_ENV_REGISTRY_VAR = "BPMSOFT_PROJECTS_FILE"


class RegistryError(Exception):
    """Ошибки чтения/записи/валидации реестра стендов."""


def bpmkit_config_dir() -> Path:
    """
    Каталог общих файлов экосистемы BPMkit (реестр стендов, конфиг GUI и т.п.)
    — та же папка, которую резолвит клиентский MCP BPMkit:

    - Windows: ``%APPDATA%\\BPMkit``;
    - POSIX: ``$XDG_CONFIG_HOME/BPMkit`` либо, если переменная не задана,
      ``~/.config/BPMkit``.

    Функция ничего не создаёт на диске — только считает путь.
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / _BPMKIT_DIR_NAME

    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / _BPMKIT_DIR_NAME


def default_registry_path() -> Path:
    """
    Резолвит путь к реестру стендов ТОЙ ЖЕ цепочкой, что использует BPMkit MCP,
    чтобы standkit и кит смотрели в один и тот же ``projects.json`` без
    дополнительной настройки:

    1. env ``BPMSOFT_PROJECTS_FILE`` — если задана и путь существует, он в
       приоритете;
    2. канонический путь кита: ``%APPDATA%\\BPMkit\\projects.json`` (Windows)
       или ``$XDG_CONFIG_HOME/BPMkit/projects.json`` / ``~/.config/BPMkit/
       projects.json`` (POSIX);
    3. фолбэк ``./projects.json`` в текущей рабочей директории — для
       standalone-запуска standkit без установленного кита.

    Возвращает первый СУЩЕСТВУЮЩИЙ файл из цепочки; если ни один не
    существует — возвращает канонический путь п.2 (куда реестр следовало бы
    положить), чтобы вызывающий код мог использовать его как путь для
    первого создания реестра.
    """
    env_value = os.environ.get(_ENV_REGISTRY_VAR)
    if env_value:
        env_path = Path(env_value)
        if env_path.exists():
            return env_path

    canonical = bpmkit_config_dir() / _REGISTRY_FILE_NAME
    if canonical.exists():
        return canonical

    fallback = Path.cwd() / _REGISTRY_FILE_NAME
    if fallback.exists():
        return fallback

    return canonical


class Registry:
    """
    Реестр стендов поверх JSON-файла.

    Пример:
        reg = Registry.load("projects.json")
        stand = reg.get("my-stand")
        reg.add_existing(Stand(name="another", stand_dir="/opt/bpmsoft/another"))
        reg.save()
    """

    def __init__(self, path: Path, default: str = "", stands: Optional[dict[str, Stand]] = None,
                 extra_top: Optional[dict] = None):
        self.path = Path(path)
        self.default = default
        self._stands: dict[str, Stand] = stands or {}
        # Прочие top-level ключи исходного файла (у реестра BPMkit: _comment,
        # scaffold_root, shared_docs_root, default_locked и т.п.) — сохраняются,
        # чтобы save() не затирал их и не портил общий с китом projects.json.
        self._extra_top: dict = extra_top or {}

    # --- чтение/запись ---

    @classmethod
    def load(cls, path: str | Path) -> "Registry":
        """
        Читает реестр из файла. Если файла нет — возвращает ПУСТОЙ реестр
        (это нормальная ситуация при первом запуске; см. projects.sample.json
        как образец для ручного заполнения).
        """
        p = Path(path)
        if not p.exists():
            return cls(path=p, default="", stands={})

        raw = p.read_text(encoding="utf-8-sig")
        try:
            data = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError as exc:
            raise RegistryError(f"Некорректный JSON реестра {p}: {exc}") from exc

        default = data.get("default", "")
        # Реестр BPMkit (и кита) хранит стенды под ключом "projects"; поддерживаем
        # и "stands" для обратной совместимости со старым форматом standkit.
        stands_raw = data.get("projects")
        if stands_raw is None:
            stands_raw = data.get("stands", {})
        extra_top = {k: v for k, v in data.items() if k not in ("projects", "stands", "default")}
        stands = {name: Stand.from_dict(name, rec) for name, rec in stands_raw.items()}
        return cls(path=p, default=default, stands=stands, extra_top=extra_top)

    def save(self) -> None:
        """Пишет реестр обратно в файл БЕЗ BOM, с отступом для читаемости диффов."""
        # Сохраняем В ФОРМАТЕ BPMkit (ключ "projects") и переносим прочие top-level
        # ключи исходного файла — чтобы не разрушить общий с китом реестр.
        payload: dict = dict(self._extra_top)
        payload["default"] = self.default
        payload["projects"] = {name: stand.to_dict() for name, stand in self._stands.items()}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        self.path.write_text(text, encoding="utf-8")

    # --- доступ к записям ---

    def list(self) -> list[Stand]:
        """Список всех стендов реестра (без гарантии порядка вставки не даётся, но сохраняется)."""
        return list(self._stands.values())

    def names(self) -> list[str]:
        return list(self._stands.keys())

    def get(self, name: str) -> Stand:
        """Возвращает Stand по имени. Бросает RegistryError, если такого нет."""
        try:
            return self._stands[name]
        except KeyError as exc:
            raise RegistryError(f"Стенд '{name}' не найден в реестре {self.path}") from exc

    def get_default(self) -> Stand:
        """Возвращает стенд по умолчанию (поле ``default`` реестра)."""
        if not self.default:
            raise RegistryError("В реестре не задан стенд по умолчанию (default)")
        return self.get(self.default)

    def __contains__(self, name: str) -> bool:
        return name in self._stands

    def __len__(self) -> int:
        return len(self._stands)

    # --- изменение реестра ---

    def add_existing(self, stand: Stand, *, make_default: bool = False) -> None:
        """
        Привязывает УЖЕ СУЩЕСТВУЮЩИЙ стенд к реестру (не провижининг!).

        Ожидается, что каталог стенда/БД/дистрибутив уже существуют — этот
        метод только регистрирует их в реестре standkit, чтобы ядро могло
        ими управлять (start/stop/health). Валидирует запись перед добавлением.
        """
        errors = stand.validate()
        if errors:
            raise RegistryError(
                f"Невозможно добавить стенд '{stand.name}': {'; '.join(errors)}"
            )
        if stand.name in self._stands:
            raise RegistryError(f"Стенд '{stand.name}' уже есть в реестре")
        self._stands[stand.name] = stand
        if make_default or not self.default:
            self.default = stand.name

    def remove(self, name: str) -> None:
        """Удаляет запись стенда из реестра (сам стенд физически не трогает)."""
        if name not in self._stands:
            raise RegistryError(f"Стенд '{name}' не найден в реестре")
        del self._stands[name]
        if self.default == name:
            self.default = next(iter(self._stands), "")

    def update(self, stand: Stand) -> None:
        """Обновляет существующую запись стенда (по имени)."""
        if stand.name not in self._stands:
            raise RegistryError(f"Стенд '{stand.name}' не найден в реестре, обновлять нечего")
        errors = stand.validate()
        if errors:
            raise RegistryError(f"Невозможно обновить стенд '{stand.name}': {'; '.join(errors)}")
        self._stands[stand.name] = stand

    def filter_by_transport(self, transport: str) -> Iterable[Stand]:
        """Вспомогательный фильтр — например, для GUI, чтобы отдельно собрать agent-стенды."""
        return (s for s in self._stands.values() if s.transport.value == transport)
