# -*- coding: utf-8 -*-
"""Резолв CLI BPMkit — общий хелпер для канала обновлений
(`standkit_companion.context`) и экрана лицензии хаба (`standkit_hub.license_api`).

Почему один хелпер, а не «дословно повторённая» логика в двух модулях (так было до
GAP-273: оба модуля прямо признавались в докстринге, что копия дословная, и держали
её синхронной руками). Ручная синхронизация — риск не «если», а «когда»: рано или
поздно одно место поправят, а другое забудут, и канал обновлений увидит один MCP, а
экран лицензии — другой. GAP-273 и стал первым таким расхождением: фолбэк на запуск
из исходников понадобился ОБОИМ потребителям одновременно.

Что чинит GAP-273. На машине издателя MCP запускается из исходников —
``python …\\BPMkit\\server\\main.py``, а не собранным ``bpmkit.exe``: локальная
разработка, CI, машина без установленной поставки. Прежний автодетект искал только
исполняемый файл и на такой машине молчал: «автодетект bpmkit.exe рядом с поставкой
не дал результата», хотя рядом был рабочий MCP — просто из исходников.

Порядок резолва (сильнее — выше, задаётся вызывающим модулем через `find_cli`-обвязку
в нём самом; здесь только строительные блоки):

1. Явная настройка (``companion.mcp_cli`` в конфиге хаба) — её задал человек, она
   сильнее любого автодетекта и не подменяется ничем.
2. Переменная окружения ``BPMKIT_CLI`` — тот же формат, что у настройки (путь к
   файлу ИЛИ командная строка вида ``python -m bpmkit``). Нужна, когда прописать путь
   в конфиге хаба неудобно или нельзя: запуск из исходников на машине издателя, CI,
   разовая подмена без правки файла настроек.
3. Автодетект бинаря (``bpmkit.exe``/``bpmkit``) рядом с поставкой — как было.
4. Автодетект запуска из исходников (GAP-273): ``<root>/server/main.py`` или
   ``<root>/BPMkit/server/main.py`` — БЕЗ исполняемого файла рядом ни в одном из
   корней-кандидатов. CLI собирается как ``[python, <путь main.py>]``, где ``python``
   — ТОТ ЖЕ интерпретатор, что у хаба (``sys.executable``): лицензия и версия обязаны
   читаться тем же MCP, что видит хаб, а не первым ``python`` из PATH. Поправка для
   Windows: если хаб запущен под ``pythonw.exe`` (без консоли — трей, служба), рядом с
   ним предпочитается ``python.exe`` — ``pythonw.exe`` не даёт дочернему процессу
   консольных дескрипторов, и CLI под ним не смог бы надёжно отдать JSON на stdout.

Шаг 3 приоритетнее шага 4 в целом (не «корень за корнем»): если бинарь нашёлся хоть в
одном из корней-кандидатов, исходники не проверяются вовсе — собранная поставка
авторитетнее дерева исходников, даже если оба случайно лежат рядом.
"""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from typing import Optional, Sequence

__all__ = [
    "CLI_ENV_VAR",
    "CLI_NAMES",
    "CLI_SUBPATHS",
    "CLI_MAX_UP",
    "SOURCE_MAIN_SUBPATHS",
    "resolve_command_string",
    "candidate_roots",
    "search_roots",
    "python_for_hub",
    "describe_search_targets",
]

#: Переменная окружения с готовой командой запуска CLI. Приоритет — ниже явной
#: настройки ``mcp_cli``, но выше автодетекта (см. докстринг модуля, шаг 2).
CLI_ENV_VAR = "BPMKIT_CLI"

#: Имена исполняемого файла CLI. ``.exe`` первым — поставка клиента всегда
#: Windows-овая; второе имя нужно, чтобы резолв был отлаживаем на Linux, где
#: расширения нет.
CLI_NAMES = ("bpmkit.exe", "bpmkit")

#: Куда смотреть от корня-кандидата в поисках БИНАРЯ. ``server/`` — штатное место в
#: поставке MCP, пустой кортеж — сборка «всё рядом» (бинарь прямо в корне).
CLI_SUBPATHS = (("server",), ())

#: Куда смотреть от корня-кандидата в поисках запуска ИЗ ИСХОДНИКОВ (GAP-273): то же
#: ``server/``, но с точкой входа вместо бинаря, и второй вариант — с дополнительным
#: уровнем ``BPMkit/``, потому что чекаут исходников на машине издателя кладёт корень
#: MCP в папку с этим именем на уровень выше, чем лежал бы поставленный бинарь.
SOURCE_MAIN_SUBPATHS = (("server", "main.py"), ("BPMkit", "server", "main.py"))

#: На сколько уровней вверх от пакета-потребителя (``standkit_companion`` или
#: ``standkit_hub``) подниматься в поисках корня MCP. Поставка кладёт BPMkitStand
#: ВНУТРЬ пакета MCP (``build/pack/BPMkitStand``), то есть корень пакета — на два
#: уровня выше каталога пакета-потребителя; берём с запасом на нестандартную
#: распаковку.
CLI_MAX_UP = 4


def resolve_command_string(value: str) -> Optional[list]:
    """Строка настройки/переменной окружения → argv-префикс запуска CLI.

    Путь к файлу берётся целиком (в нём бывают пробелы — дробить его нельзя);
    иначе строка — командная строка (``python -m bpmkit``), и её нужно разобрать на
    токены.

    ``posix=False`` на Windows обязателен: в posix-режиме ``shlex`` съедает обратные
    слэши как экранирование, и ``C:\\Program Files\\bpmkit.exe`` превращается в
    ``C:Program Filesbpmkit.exe``. Плата за это — сохранённые кавычки вокруг
    токенов, их снимаем сами.
    """
    text = (value or "").strip()
    if not text:
        return None
    as_path = Path(text)
    if as_path.is_file():
        return [str(as_path)]
    posix = os.name != "nt"
    parts = shlex.split(text, posix=posix)
    if not posix:
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    parts = [p for p in parts if p]
    return parts or None


def candidate_roots(package_file: str, *, extra_roots: Optional[Sequence] = None,
                     max_up: int = CLI_MAX_UP) -> list:
    """Корни, в которых имеет смысл искать CLI: сначала явно переданные (тесты,
    будущие настройки), затем каталоги вверх от пакета-потребителя.

    ``package_file`` — ``__file__`` модуля-потребителя (``context.py`` или
    ``license_api.py``): от него и считаются уровни вверх.
    """
    roots: list = [Path(r) for r in (extra_roots or [])]
    node = Path(package_file).resolve().parent
    for _ in range(max_up):
        node = node.parent
        roots.append(node)
    return roots


def python_for_hub() -> str:
    """Интерпретатор для фолбэка «запуск из исходников» (GAP-273, шаг 4) — тот же,
    что у самого хаба.

    На Windows, если хаб запущен под ``pythonw.exe`` (нет консоли — трей, служба),
    предпочитаем ``python.exe`` рядом с ним: у ``pythonw.exe`` не выставлены
    консольные дескрипторы, и дочерний CLI под ним не смог бы надёжно отдать
    JSON-ответ на stdout.

    Через ``os.path``, а не ``pathlib.Path``: строковые операции ``os.path``
    ведут себя одинаково независимо от хоста и потому тестируемы через подмену
    ``os.name`` на любой платформе. ``pathlib.Path`` так тестировать нельзя —
    конкретный класс `WindowsPath` явно отказывается создаваться на POSIX-хосте
    (``NotImplementedError``) даже если ``os.name`` подменён, и попытка сделать
    это на CI-машине под Linux ломает не только тест, но и служебные вызовы
    `pathlib.Path` самого pytest следом за ним.
    """
    executable = sys.executable or "python"
    if os.name == "nt" and os.path.basename(executable).lower() == "pythonw.exe":
        sibling = os.path.join(os.path.dirname(executable), "python.exe")
        if os.path.isfile(sibling):
            return sibling
    return executable


def _binary_in_root(root: Path) -> Optional[list]:
    for subpath in CLI_SUBPATHS:
        for name in CLI_NAMES:
            candidate = root.joinpath(*subpath, name)
            if candidate.is_file():
                return [str(candidate)]
    return None


def _source_in_root(root: Path) -> Optional[list]:
    for subpath in SOURCE_MAIN_SUBPATHS:
        main_py = root.joinpath(*subpath)
        if main_py.is_file():
            return [python_for_hub(), str(main_py)]
    return None


def search_roots(roots: Sequence[Path]) -> Optional[list]:
    """Поиск CLI в корнях-кандидатах: сначала бинарь во ВСЕХ корнях, и только если он
    не нашёлся нигде — запуск из исходников (GAP-273, шаг 4; см. докстринг модуля,
    почему в этом порядке)."""
    for root in roots:
        found = _binary_in_root(root)
        if found:
            return found
    for root in roots:
        found = _source_in_root(root)
        if found:
            return found
    return None


def describe_search_targets(roots: Sequence[Path]) -> str:
    """Компактный текст для сообщения об отказе: где именно искали и как задать путь
    вручную. Человек обязан видеть уже проверенные места, а не гадать — а полный
    перебор (корни × подпути × имена) нечитаем и всё равно обрезается лимитом длины
    сообщения об ошибке."""
    root_list = list(roots)
    shown = ", ".join(str(r) for r in root_list[:3])
    more = f" и ещё {len(root_list) - 3}" if len(root_list) > 3 else ""
    return (f"искали bpmkit(.exe) и server/main.py рядом с: {shown}{more}; "
            f"задайте companion.mcp_cli в настройках хаба или переменную окружения "
            f"{CLI_ENV_VAR}")
