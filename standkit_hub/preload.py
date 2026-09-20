# -*- coding: utf-8 -*-
"""
Полная загрузка модулей пакета при старте диспетчера (GAP-412) — STDLIB-ONLY.

ЗАЧЕМ. ``pip install -U standkit`` ЗАМЕНЯЕТ файлы пакета на диске, ничего не
спрашивая у работающего процесса. Пока модуль уже импортирован, это безвредно:
в памяти живёт старый код, и он согласован сам с собой. Опасны ровно те модули,
которые к моменту обновления импортированы ЕЩЁ НЕ БЫЛИ, — ленивый ``import``
внутри функции подтянет их ПОСЛЕ замены, и в одном процессе окажется половина
старой версии и половина новой. Разбирать такой процесс невозможно: трейсбек
показывает строки файла, которого в памяти нет.

Ленивые импорты при этом никуда не денутся и не должны: половина из них
разрывает циклы (``lifecycle`` ↔ ``hosting``), половина — платформенная
(``ctypes`` на POSIX импортировать незачем). Поэтому лечится не место импорта,
а МОМЕНТ: при старте диспетчер один раз проходит по всем модулям своих пакетов
и импортирует их. Дальше любой ленивый ``import`` — это просто поиск в
``sys.modules``, то есть тот самый старый согласованный код.

Отказ импорта одного модуля НЕ роняет старт: редакция без ``standkit_companion``
— штатная поставка, а необязательная зависимость (``pywebview``) отсутствует у
большинства. Причина уходит в лог и в результат, диспетчер продолжает работу.

В frozen-сборке (PyInstaller) описанного класса проблем не существует вовсе:
пакет лежит внутри exe и подменить его отдельно нельзя. Перебор модулей там
делается тем же ``pkgutil`` и при неудаче просто ничего не находит — это
штатный, а не аварийный исход.
"""
from __future__ import annotations

import importlib
import pkgutil
from typing import Iterable, Optional

#: Пакеты, чьи модули загружаются целиком. ``standkit_agent`` сюда НЕ входит:
#: он поднимается отдельным процессом и в адресном пространстве хаба не живёт.
PACKAGES = ("standkit", "standkit_hub", "standkit_companion")

#: Модули, которые перебор ПРОПУСКАЕТ. ``__main__`` любого пакета — точка входа:
#: импортировать её из работающего процесса значит получить второй экземпляр
#: модуля (и, при неудачной гварде, второй разбор аргументов).
SKIP_SUFFIXES = (".__main__",)


def package_module_names(packages: "Optional[Iterable[str]]" = None) -> "list[str]":
    """Имена всех модулей перечисленных пакетов (без подпакетов-точек входа).

    Пакет, которого нет в этой редакции поставки, молча пропускается —
    ``standkit_companion`` отсутствует в свободной сборке по дизайну.
    """
    result: "list[str]" = []
    for name in (packages if packages is not None else PACKAGES):
        try:
            pkg = importlib.import_module(name)
        except ImportError:
            continue
        paths = list(getattr(pkg, "__path__", []) or [])
        if not paths:
            continue
        for info in pkgutil.walk_packages(paths, prefix=name + "."):
            if any(info.name.endswith(suffix) for suffix in SKIP_SUFFIXES):
                continue
            result.append(info.name)
    return sorted(set(result))


def preload(packages: "Optional[Iterable[str]]" = None) -> "tuple[list[str], list[tuple[str, str]]]":
    """Импортирует все модули пакетов. Возвращает ``(загружено, [(модуль, причина)])``."""
    loaded: "list[str]" = []
    failed: "list[tuple[str, str]]" = []
    for name in package_module_names(packages):
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - один модуль не роняет старт диспетчера
            failed.append((name, f"{type(exc).__name__}: {exc}"))
        else:
            loaded.append(name)
    return loaded, failed
