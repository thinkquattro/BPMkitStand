"""
Просмотр лог-файлов стенда со стороны хаба: список файлов источника логов,
выбор основного (самого свежего) файла, tail содержимого, открытие папки
логов в файловом менеджере ОС хоста.

ДВА ИСТОЧНИКА логов на стенд (``source``, см. ``resolve_logs_dir``):

- ``"stand"`` — логи самого стенда (платформа BPMSoft), пишет их движок,
  каталог логов в корне стенда: явный ``Stand.logs_dir``, если задан, иначе
  подкаталог ``logs``/``Logs``/… внутри ``stand.stand_dir``, найденный БЕЗ
  УЧЁТА РЕГИСТРА (общий резолв ``standkit.logs.stand_logs_dir``). Это
  ближе всего к тому, что видно в PS-окне/консоли самого стенда — дефолт
  для панели "Текущее состояние".
- ``"bpmkit"`` — логи BPMkit-ПРОЕКТА (scaffold, ``project_scaffold``): не
  логи стенда и НЕ ``Stand.extra["logs_path"]`` (та запись указывает на тот
  же каталог, что и ``"stand"``, — путать источники не нужно), а подпапка
  ``logs`` внутри папки проекта ``Stand.extra["docs_folder"]``
  (``<docs_folder>/logs``, имя подкаталога — тоже без учёта регистра). Туда
  пишутся логи разработки поверх стенда; если ``docs_folder`` в записи
  стенда не задан (провижининг без project_scaffold), источник недоступен
  целиком.

Имя каталога логов НИГДЕ здесь не зашито строкой — оно резолвится по факту
через ``standkit.logs`` (единая логика с ``standkit.hosting.IisBackend``,
см. GAP-006): BPMSoft пишет в ``Logs``, и на регистрозависимой ФС жёсткое
``"logs"`` давало «каталог не найден» при живых логах.

Важно отличать оба источника от ``standkit.lifecycle.log_path``/
``standkit.logs`` (см. ``standkit_hub.server._api_stand_logs``) — тот лог
существует только для стендов, ЗАПУЩЕННЫХ САМИМ standkit
(``transport=local`` через ``lifecycle.start``), это третий, отдельный канал.

STDLIB-ONLY: ``pathlib``, ``subprocess``, ``sys``, ``os`` (только ``os.startfile``
на Windows), ``ctypes``/``threading``/``time`` (только Windows, вывод окна
проводника на передний план — см. ``_bring_explorer_to_front``).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

from standkit.logs import LOGS_DIR_NAME, find_logs_subdir, scan_denied, stand_logs_dir
from standkit.models import Stand, Transport

# Допустимые значения ``source`` — единственный источник истины для валидации
# как здесь, так и в standkit_hub.server (query-параметр ``source``).
LOG_SOURCES = ("stand", "bpmkit")

DEFAULT_LOG_SOURCE = "stand"


def _looks_windows_path(base: str) -> bool:
    """Признак Windows-пути: есть обратный слэш (в т.ч. UNC ``\\\\server\\share``)
    либо путь начинается с буквы диска (``C:``)."""
    if "\\" in base:
        return True
    return len(base) >= 2 and base[1] == ":" and base[0].isalpha()


def _join_for_display(base: str, name: str) -> str:
    """
    Склеивает ``<base>/<name>`` ДЛЯ ПОКАЗА пользователю, сохраняя стиль
    разделителей исходного пути — В ОБЕ СТОРОНЫ.

    Зачем не просто ``Path(base) / name``: путь стенда с ``transport=agent``
    описывает файловую систему УДАЛЁННОГО хоста, а собирается он на машине
    хаба, и стили разделителей у них могут не совпадать. ``pathlib.Path`` —
    это всегда путь ТЕКУЩЕЙ ОС, поэтому оба направления давали кашу:

    - POSIX-путь стенда на Windows-хабе → ``\\mnt\\composers\\…`` (GAP-006);
    - Windows-путь стенда на Linux-хабе → ``C:\\BPMSoft\\stand/logs`` —
      смесь разделителей, которую оператор не может скопировать никуда.

    Поэтому стиль выбирается ПО САМОМУ ПУТИ, а не по ОС хаба: похоже на
    Windows (буква диска или обратные слэши) — ``PureWindowsPath``; начинается
    со слэша — ``PurePosixPath``; всё прочее (относительные пути) — как раньше,
    через ``Path``, в нативном для текущей ОС стиле.
    """
    if _looks_windows_path(base):
        return str(PureWindowsPath(base) / name)
    if base.startswith("/"):
        return str(PurePosixPath(base) / name)
    return str(Path(base) / name)


def raw_logs_path(stand: Stand, source: str = DEFAULT_LOG_SOURCE) -> Optional[str]:
    """
    "Сырой" (не проверенный на существование) путь к каталогу логов для
    выбранного источника — используется только для человекочитаемых сообщений
    ("каталог не найден — <путь>"), когда ``resolve_logs_dir`` вернул ``None``.

    Для источника "stand" отдаёт явный ``stand.logs_dir``, если он задан в
    реестре (иначе сообщение показывало бы не тот путь, который на самом деле
    проверялся), иначе — ``<stand_dir>/logs`` с ИМЕНЕМ ПО УМОЛЧАНИЮ: реального
    каталога нет ни в каком регистре, показывать нечего кроме ожидаемого имени.

    Разделители — в стиле исходного пути (см. ``_join_for_display``), чтобы
    POSIX-путь удалённого стенда не превращался в ``\\mnt\\…`` на Windows-хабе.

    Бросает ``ValueError`` на неизвестный ``source`` — та же дисциплина, что
    и у ``resolve_logs_dir``.
    """
    if source == "stand":
        if stand.logs_dir:
            return stand.logs_dir
        return _join_for_display(stand.stand_dir, LOGS_DIR_NAME) if stand.stand_dir else None
    if source == "bpmkit":
        docs_folder = stand.extra.get("docs_folder")
        return _join_for_display(docs_folder, LOGS_DIR_NAME) if docs_folder else None
    raise ValueError(f"неизвестный источник логов: {source!r}")


def resolve_logs_dir(stand: Stand, source: str = DEFAULT_LOG_SOURCE) -> Optional[Path]:
    """
    Резолвит каталог логов стенда для выбранного источника:

    - ``source="stand"`` (по умолчанию) — явный ``stand.logs_dir``, иначе
      подкаталог ``logs`` внутри ``stand.stand_dir``, найденный без учёта
      регистра (``standkit.logs.stand_logs_dir``);
    - ``source="bpmkit"`` — подкаталог ``logs`` внутри
      ``stand.extra["docs_folder"]``, тоже без учёта регистра (логи
      BPMkit-проекта, папка внутри project-scaffold, НЕ
      ``stand.extra["logs_path"]``).

    Возвращает ``None``, если путь не задан (для "stand" — пусты и
    ``logs_dir``, и ``stand_dir``; для "bpmkit" — не задан ``docs_folder``),
    либо каталога нет, либо путь указывает не на каталог — вызывающая сторона
    (хаб) обязана отдать понятное сообщение "лог недоступен" (см.
    ``logs_unavailable_reason``), а не падать с исключением.

    Бросает ``ValueError`` на неизвестный ``source`` — это ошибка вызывающего
    кода (например, невалидированный query-параметр), а не штатная ситуация
    "лога нет"; HTTP-слой (``standkit_hub.server``) обязан провалидировать
    ``source`` ДО вызова и вернуть 400, не давая исключению дойти сюда.
    """
    if source == "stand":
        return stand_logs_dir(stand)
    if source == "bpmkit":
        docs_folder = stand.extra.get("docs_folder")
        return find_logs_subdir(docs_folder) if docs_folder else None
    raise ValueError(f"неизвестный источник логов: {source!r}")


def _scan_base(stand: Stand, source: str) -> Optional[str]:
    """
    Каталог, СОДЕРЖИМОЕ которого перебирает резолв логов для этого источника
    ("stand" — корень стенда, "bpmkit" — папка проекта). ``None``, если
    перебора не будет: у "stand" задан явный ``logs_dir`` — он используется
    как есть, без поиска подкаталога.
    """
    if source == "stand":
        return None if stand.logs_dir else (stand.stand_dir or None)
    if source == "bpmkit":
        return stand.extra.get("docs_folder") or None
    raise ValueError(f"неизвестный источник логов: {source!r}")


def logs_unavailable_reason(stand: Stand, source: str = DEFAULT_LOG_SOURCE) -> str:
    """
    Человекочитаемая причина, почему каталог логов недоступен — текст для
    сообщения хаба "лог недоступен (источник «…»: <причина>)". Вызывать
    имеет смысл только после ``resolve_logs_dir(...) is None``.

    Четыре ветки:

    1. путь вообще не задан в записи стенда → "путь не задан";
    2. ``transport=agent`` → каталог логов принадлежит файловой системе
       УДАЛЁННОГО хоста, а хаб проверяет существование локально: на своей
       машине он такого пути не увидит никогда. Писать в этом случае "каталог
       не найден" — врать про чужую ФС, поэтому причина формулируется честно;
    3. каталог стенда есть, но его НЕЛЬЗЯ ПЕРЕЧИСЛИТЬ (нет права чтения —
       типичный ``chmod 0711`` на ``/opt/<app>``) → так и говорим. Подкаталог
       с точным именем ``logs``/``Logs`` в такой ситуации всё равно находится
       (прямая проверка пути, см. ``standkit.logs.find_logs_subdir``), значит
       сюда мы попадаем, только когда каталог логов назван иначе: "не найден"
       было бы неверным диагнозом — мы просто не смогли посмотреть;
    4. иначе (локальный стенд) → "каталог не найден — <путь>".

    Путь берётся из ``raw_logs_path`` (стиль разделителей — как в записи
    стенда). Как и она, бросает ``ValueError`` на неизвестный ``source``.
    """
    raw = raw_logs_path(stand, source)
    if not raw:
        return "путь не задан"
    if stand.transport == Transport.AGENT:
        return f"каталог логов живёт на хосте стенда; хаб проверяет его локально и потому не видит — {raw}"
    base = _scan_base(stand, source)
    if base and scan_denied(base):
        return (
            f"нет прав на чтение каталога {base} — перечислить его содержимое "
            f"и найти подкаталог логов не удалось (ожидался {raw})"
        )
    return f"каталог не найден — {raw}"


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


# Классы окон проводника Windows: обычное окно папки и окно с деревом
# ("Проводник"). Ищем окно именно по классу, а не по заголовку целиком —
# заголовок зависит от настройки "выводить полный путь в строке заголовка".
_EXPLORER_WINDOW_CLASSES = ("CabinetWClass", "ExploreWClass")


def _bring_explorer_to_front(path: Path, timeout_s: float = 3.0) -> None:
    """
    Дожидается появления окна проводника для ``path`` и вытаскивает его
    на передний план. Вызывается в daemon-потоке: окно рождается не мгновенно,
    а HTTP-ответ хаба ждать этого не должен.

    Почему просто ``SetForegroundWindow`` не работает: Windows отдаёт фокус
    только процессу, который сам сейчас на переднем плане (foreground lock).
    В момент клика активен браузер, а не хаб. Классический легальный обход —
    ``AttachThreadInput`` к потоку текущего foreground-окна: на время
    привязки наш поток разделяет с ним очередь ввода и право менять фокус.
    Синтетические нажатия клавиш (трюк с ALT) сознательно НЕ используем —
    они прилетают в чужое активное окно.

    Любая ошибка WinAPI гасится: папка уже открыта, недополученный фокус —
    не повод ронять запрос.
    """
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # Без явных argtypes/restype 64-битные HWND режутся до int — окно
        # «находится», а все операции с ним молча ничего не делают.
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]

        target_full = str(path).rstrip("\\/").lower()
        target_name = (path.name or str(path)).lower()
        matches: list[int] = []

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _enum(hwnd, _lparam):
            cls_buf = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, cls_buf, 64)
            if cls_buf.value not in _EXPLORER_WINDOW_CLASSES:
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            title_buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buf, length + 1)
            title = title_buf.value.strip().lower()
            if title in (target_full, target_name):
                matches.append(hwnd)
                return False  # точное совпадение — дальше не ищем
            return True

        callback = WNDENUMPROC(_enum)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not matches:
            user32.EnumWindows(callback, 0)
            if matches:
                break
            time.sleep(0.15)
        if not matches:
            return

        hwnd = matches[0]
        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)

        fg = user32.GetForegroundWindow()
        fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
        my_tid = kernel32.GetCurrentThreadId()
        attached = bool(fg_tid) and fg_tid != my_tid and bool(
            user32.AttachThreadInput(my_tid, fg_tid, True)
        )
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(my_tid, fg_tid, False)
    except Exception:  # noqa: BLE001 — фокус best-effort, ошибки не эскалируем
        return


def _open_folder_windows(path: Path) -> None:
    """
    Windows: открыть каталог в проводнике И вывести его окно на передний план.

    Голого ``os.startfile`` мало. Хаб — фоновый процесс (активен браузер или
    вебвью, не он), а Windows запрещает неактивному процессу и его потомкам
    звать ``SetForegroundWindow``. Итог — проводник открывается ВТИХУЮ: окно
    под браузером либо просто мигающая кнопка в таскбаре. Пользователь жмёт
    «Открыть папку логов» и не видит реакции — ровно тот негативный UX,
    из-за которого это и написано.

    Два шага лечения:

    1. ``AllowSetForegroundWindow(ASFW_ANY)`` перед запуском — передаёт право
       на захват фокуса запускаемому процессу (срабатывает не всегда, зависит
       от того, кто владеет вводом);
    2. ``_bring_explorer_to_front`` в фоне — дожидается окна и поднимает его
       через ``AttachThreadInput`` (страховка на случай, когда шага 1 мало).

    Сам запуск — ``ShellExecuteW`` с ``SW_SHOWNORMAL``: та же операция "open",
    что и у ``os.startfile``, но с явным параметром показа окна, поэтому уже
    открытое свёрнутое окно той же папки разворачивается, а не остаётся
    в таскбаре.

    ``ctypes`` — stdlib, инвариант «без сторонних зависимостей» не нарушен.
    Если WinAPI недоступен — честный фолбэк на ``os.startfile`` (папка
    откроется, пусть и без гарантии фокуса).
    """
    try:
        import ctypes

        ASFW_ANY = -1
        SW_SHOWNORMAL = 1
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)  # type: ignore[attr-defined]
        rc = int(
            ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
                None, "open", str(path), None, None, SW_SHOWNORMAL
            )
        )
        # ShellExecuteW: значение > 32 — успех, всё остальное — код ошибки.
        if rc <= 32:
            raise OSError(f"ShellExecuteW вернул {rc}")
    except (ImportError, AttributeError, OSError):
        os.startfile(str(path))  # type: ignore[attr-defined]
    threading.Thread(
        target=_bring_explorer_to_front, args=(path,), name="standkit-focus-explorer", daemon=True
    ).start()


def open_folder(path: Path) -> OpenFolderResult:
    """
    Открывает каталог в файловом менеджере ОС хоста: Windows —
    ``_open_folder_windows`` (ShellExecuteW + передача фокуса, см. там);
    macOS — ``open -a Finder`` (``-a`` активирует Finder, а не только
    открывает окно фоном); остальное — ``xdg-open`` (через
    ``subprocess.Popen``, не блокируясь на ожидании закрытия окна).

    Никогда не бросает исключение наружу — при отсутствии DISPLAY, нужной
    утилиты в PATH и т.п. возвращает ``ok=False`` с текстом причины, чтобы
    хаб мог показать это пользователю, а не упасть 500-й.
    """
    if not path.exists():
        return OpenFolderResult(False, f"каталог не существует: {path}")
    try:
        if sys.platform == "win32":
            _open_folder_windows(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Finder", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return OpenFolderResult(True, f"открыто: {path}")
    except OSError as exc:
        return OpenFolderResult(False, f"не удалось открыть проводник: {exc}")
