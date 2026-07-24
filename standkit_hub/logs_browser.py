"""
Просмотр лог-файлов стенда со стороны хаба: список файлов в ``logs_path``,
выбор основного (самого свежего) файла, tail содержимого, открытие папки
логов в файловом менеджере ОС хоста.

Важно отличать от ``standkit.lifecycle.log_path``/``standkit.logs`` — тот
лог существует только для стендов, ЗАПУЩЕННЫХ САМИМ standkit
(``transport=local`` через ``lifecycle.start``). На практике стенды часто
подняты извне (вручную, IIS/Kestrel-службой, сторонним скриптом) — их
собственный лог лежит там, где решил сам стенд/деплой, а не в papке
standkit. Путь к этому "внешнему" логу приходит из реестра BPMkit как
``Stand.extra["logs_path"]``.

STDLIB-ONLY: ``pathlib``, ``subprocess``, ``sys``.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from standkit.models import Stand


def resolve_logs_dir(stand: Stand) -> Optional[Path]:
    """
    Резолвит каталог логов стенда из ``extra["logs_path"]``.

    Возвращает ``None``, если поле не задано, либо путь не существует, либо
    указывает не на каталог — вызывающая сторона (хаб) обязана отдать
    понятное сообщение "лог недоступен", а не падать с исключением.
    """
    raw = stand.extra.get("logs_path")
    if not raw:
        return None
    p = Path(str(raw))
    if not p.exists() or not p.is_dir():
        return None
    return p


def list_log_files(logs_dir: Path) -> list[dict]:
    """
    Список лог-файлов каталога (без рекурсии в подкаталоги): имя, размер в
    байтах, mtime (unix timestamp). Отсортирован по mtime по убыванию (самый
    свежий — первым), чтобы фронтенду не нужно было сортировать самому.
    """
    entries: list[dict] = []
    for child in logs_dir.iterdir():
        if not child.is_file():
            continue
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append({"name": child.name, "size": st.st_size, "mtime": st.st_mtime})
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def pick_primary_log(logs_dir: Path) -> Optional[Path]:
    """Выбирает "основной" лог каталога — самый свежий по mtime файл."""
    files = list_log_files(logs_dir)
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
    Открывает каталог в файловом менеджере ОС хоста (Windows — ``explorer``,
    macOS — ``open``, остальное — ``xdg-open``) через ``subprocess.Popen``
    (не блокируясь на ожидании закрытия окна).

    Никогда не бросает исключение наружу — при отсутствии DISPLAY, нужной
    утилиты в PATH и т.п. возвращает ``ok=False`` с текстом причины, чтобы
    хаб мог показать это пользователю, а не упасть 500-й.
    """
    if not path.exists():
        return OpenFolderResult(False, f"каталог не существует: {path}")
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return OpenFolderResult(True, f"открыто: {path}")
    except OSError as exc:
        return OpenFolderResult(False, f"не удалось открыть проводник: {exc}")
