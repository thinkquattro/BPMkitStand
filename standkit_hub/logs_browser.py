"""
Просмотр лог-файлов стенда со стороны хаба: список файлов источника логов,
выбор основного (самого свежего) файла, tail содержимого, открытие папки
логов в файловом менеджере ОС хоста.

ДВА ИСТОЧНИКА логов на стенд (``source``, см. ``resolve_logs_dir``):

- ``"stand"`` — логи самого стенда (платформа BPMSoft), пишет их движок,
  каталог ``<stand.stand_dir>/logs`` (папка ``logs`` в корне стенда). Это
  ближе всего к тому, что видно в PS-окне/консоли самого стенда — дефолт
  для панели "Текущее состояние".
- ``"bpmkit"`` — логи BPMkit-ПРОЕКТА (scaffold, ``project_scaffold``): не
  логи стенда и НЕ ``Stand.extra["logs_path"]`` (та запись указывает на тот
  же каталог, что и ``"stand"``, — путать источники не нужно), а подпапка
  ``logs`` внутри папки проекта ``Stand.extra["docs_folder"]``
  (``<docs_folder>/logs``). Туда пишутся логи разработки поверх стенда;
  если ``docs_folder`` в записи стенда не задан (провижининг без
  project_scaffold), источник недоступен целиком.

Важно отличать оба источника от ``standkit.lifecycle.log_path``/
``standkit.logs`` (см. ``standkit_hub.server._api_stand_logs``) — тот лог
существует только для стендов, ЗАПУЩЕННЫХ САМИМ standkit
(``transport=local`` через ``lifecycle.start``), это третий, отдельный канал.

STDLIB-ONLY: ``pathlib``, ``subprocess``, ``sys``, ``os`` (только ``os.startfile``
на Windows).
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from standkit.models import Stand

# Допустимые значения ``source`` — единственный источник истины для валидации
# как здесь, так и в standkit_hub.server (query-параметр ``source``).
LOG_SOURCES = ("stand", "bpmkit")

DEFAULT_LOG_SOURCE = "stand"


def raw_logs_path(stand: Stand, source: str = DEFAULT_LOG_SOURCE) -> Optional[str]:
    """
    "Сырой" (не проверенный на существование) путь к каталогу логов для
    выбранного источника — используется только для человекочитаемых сообщений
    ("каталог не найден — <путь>"), когда ``resolve_logs_dir`` вернул ``None``.

    Бросает ``ValueError`` на неизвестный ``source`` — та же дисциплина, что
    и у ``resolve_logs_dir``.
    """
    if source == "stand":
        return str(Path(stand.stand_dir) / "logs") if stand.stand_dir else None
    if source == "bpmkit":
        docs_folder = stand.extra.get("docs_folder")
        return str(Path(docs_folder) / "logs") if docs_folder else None
    raise ValueError(f"неизвестный источник логов: {source!r}")


def resolve_logs_dir(stand: Stand, source: str = DEFAULT_LOG_SOURCE) -> Optional[Path]:
    """
    Резолвит каталог логов стенда для выбранного источника:

    - ``source="stand"`` (по умолчанию) — ``<stand.stand_dir>/logs``;
    - ``source="bpmkit"`` — ``<stand.extra["docs_folder"]>/logs`` (логи
      BPMkit-проекта, папка ``logs`` внутри project-scaffold, НЕ
      ``stand.extra["logs_path"]``).

    Возвращает ``None``, если путь не задан (для "stand" — пуст сам
    ``stand_dir``; для "bpmkit" — не задан ``docs_folder``), либо не
    существует, либо указывает не на каталог —
    вызывающая сторона (хаб) обязана отдать понятное сообщение "лог
    недоступен", а не падать с исключением.

    Бросает ``ValueError`` на неизвестный ``source`` — это ошибка вызывающего
    кода (например, невалидированный query-параметр), а не штатная ситуация
    "лога нет"; HTTP-слой (``standkit_hub.server``) обязан провалидировать
    ``source`` ДО вызова и вернуть 400, не давая исключению дойти сюда.
    """
    raw = raw_logs_path(stand, source)
    if not raw:
        return None
    p = Path(raw)
    if not p.exists() or not p.is_dir():
        return None
    return p


def start_of_today_ts() -> float:
    """Unix-timestamp локальной полуночи сегодняшнего дня (для отсечения
    старых логов «старше сегодня» — логи IIS/.NET по дням бывают очень тяжёлыми)."""
    import datetime as _dt

    now = _dt.datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def list_log_files(logs_dir: Path, *, since_mtime: Optional[float] = None) -> list[dict]:
    """
    Список лог-файлов каталога, ВКЛЮЧАЯ вложенные подкаталоги: стенды BPMSoft
    (и .NET-хосты вообще) часто пишут логи не плоско, а в подпапки по датам,
    например ``logs/2026-07-24/*.log``. Без рекурсии верхний уровень содержал
    бы только папки, и хаб ошибочно сообщал «в каталоге нет файлов».

    ``since_mtime`` (опц.) — не включать файлы, изменённые РАНЬШЕ этого unix-ts
    (напр. ``start_of_today_ts()`` — «только за сегодня»): у IIS/.NET накапливаются
    сотни тяжёлых дневных файлов, перечислять/читать всё — дорого.

    ``name`` — путь файла ОТНОСИТЕЛЬНО ``logs_dir`` в POSIX-форме (с прямыми
    слэшами, напр. ``2026-07-24/app.log``), совместимый с
    ``sanitize_log_filename`` на любой ОС. Размер в байтах, mtime (unix
    timestamp). Отсортирован по mtime по убыванию (самый свежий — первым).
    """
    entries: list[dict] = []
    for child in logs_dir.rglob("*"):
        if not child.is_file():
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        if since_mtime is not None and st.st_mtime < since_mtime:
            continue
        rel = child.relative_to(logs_dir).as_posix()
        entries.append({"name": rel, "size": st.st_size, "mtime": st.st_mtime})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def pick_primary_log(logs_dir: Path, *, since_mtime: Optional[float] = None) -> Optional[Path]:
    """Выбирает "основной" лог каталога — самый свежий по mtime файл. С
    ``since_mtime`` учитывает только файлы не старше указанного момента."""
    files = list_log_files(logs_dir, since_mtime=since_mtime)
    if not files:
        return None
    return logs_dir / files[0]["name"]


def sanitize_log_filename(logs_dir: Path, name: str) -> Optional[Path]:
    """
    Резолвит имя файла лога СТРОГО внутри ``logs_dir`` (защита от path
    traversal) — тот же принцип, что ``standkit_hub.security.sanitize_static_path``,
    но для произвольного (не фиксированного) каталога логов конкретного стенда.

    Возвращает ``None``, если путь небезопасен, не существует или указывает
    не на файл.
    """
    if not name or name.startswith("/") or "\\" in name or ".." in Path(name).parts:
        return None
    logs_dir_resolved = logs_dir.resolve()
    candidate = (logs_dir / name).resolve()
    try:
        candidate.relative_to(logs_dir_resolved)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


@dataclass
class OpenFolderResult:
    """Результат попытки открыть папку логов в файловом менеджере ОС хоста."""

    ok: bool
    message: str


def open_folder(path: Path) -> OpenFolderResult:
    """
    Открывает каталог в файловом менеджере ОС хоста: Windows — ``os.startfile``
    (штатный способ ОС попросить проводник открыть путь — надёжнее выводит
    открытое окно на передний план, чем спавн ``explorer`` через subprocess,
    который часто просто сворачивает уже открытое окно той же папки в
    таскбар вместо фокуса); macOS — ``open``, остальное — ``xdg-open`` (оба
    через ``subprocess.Popen``, не блокируясь на ожидании закрытия окна).

    Никогда не бросает исключение наружу — при отсутствии DISPLAY, нужной
    утилиты в PATH и т.п. возвращает ``ok=False`` с текстом причины, чтобы
    хаб мог показать это пользователю, а не упасть 500-й.
    """
    if not path.exists():
        return OpenFolderResult(False, f"каталог не существует: {path}")
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return OpenFolderResult(True, f"открыто: {path}")
    except OSError as exc:
        return OpenFolderResult(False, f"не удалось открыть проводник: {exc}")
