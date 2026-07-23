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
from pathlib import Path
from typing import Iterable, Optional

from standkit.models import Stand

_DEFAULT_SCHEMA_VERSION = 1


class RegistryError(Exception):
    """Ошибки чтения/записи/валидации реестра стендов."""


class Registry:
    """
    Реестр стендов поверх JSON-файла.

    Пример:
        reg = Registry.load("projects.json")
        stand = reg.get("my-stand")
        reg.add_existing(Stand(name="another", stand_dir="/opt/bpmsoft/another"))
        reg.save()
    """

    def __init__(self, path: Path, default: str = "", stands: Optional[dict[str, Stand]] = None):
        self.path = Path(path)
        self.default = default
        self._stands: dict[str, Stand] = stands or {}

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
        stands_raw = data.get("stands", {})
        stands = {name: Stand.from_dict(name, rec) for name, rec in stands_raw.items()}
        return cls(path=p, default=default, stands=stands)

    def save(self) -> None:
        """Пишет реестр обратно в файл БЕЗ BOM, с отступом для читаемости диффов."""
        payload = {
            "_schema_version": _DEFAULT_SCHEMA_VERSION,
            "default": self.default,
            "stands": {name: stand.to_dict() for name, stand in self._stands.items()},
        }
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
