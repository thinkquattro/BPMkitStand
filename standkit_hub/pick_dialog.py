# -*- coding: utf-8 -*-
"""Нативный диалог выбора файла/каталога для `POST /api/pick`.

Зачем он есть. Браузерный `<input type="file">` отдаёт СОДЕРЖИМОЕ файла, но не его
путь — а диспетчеру нужен именно путь (каталог стенда, каталог логов, файл
лицензии рядом с MCP). Загонять лицензионный ключ через страницу ради того, чтобы
узнать, где он лежит, — лишний путь утечки: страница его не увидит вовсе, если
файл выберет сама ОС и вернёт хабу один путь.

Как это устроено. Хаб — локальный процесс на машине оператора, поэтому диалог
поднимается ШТАТНЫМ средством ОС и только stdlib'ом (`subprocess` через
`standkit.platform.run_console`, GAP-138):

* **Windows** — PowerShell с `System.Windows.Forms`: `OpenFileDialog` для файла,
  `FolderBrowserDialog` для каталога. Два обязательных условия, без которых диалог
  теряется ЗА окном браузера и выглядит как зависший интерфейс:
  `-STA` (COM-модель, без неё оба диалога просто не открываются) и
  владелец-окно — скрытая `Form` с `TopMost = $true`, которую диалог получает
  аргументом `ShowDialog($owner)`;
* **Linux** — `zenity`, иначе `kdialog`; **macOS** — `osascript`. Всё это
  best-effort: ни один из них не является зависимостью пакета;
* **диалога нет вовсе** — честный `{"path": null, "error": "no_dialog"}`, а не
  молчаливая отмена: UI обязан сказать «введите путь руками», а не делать вид, что
  человек передумал.

Отмена и выбор различаются намеренно: отмена — `{"path": null}` БЕЗ `error`.

Таймаут — `DIALOG_TIMEOUT_SEC` (5 минут): человек может уйти от машины с открытым
диалогом, а поток обработчика ждать вечно не имеет права.
"""

from __future__ import annotations

import shutil
import sys
from typing import Callable, Optional

from standkit.platform import run_console

__all__ = [
    "PICK_KINDS",
    "DIALOG_TIMEOUT_SEC",
    "NO_DIALOG",
    "build_command",
    "parse_output",
    "pick",
]

#: Что можно выбрать. Больше видов не предполагается: «сохранить как» диспетчеру
#: не нужен, а множественный выбор не нужен ни одному его полю.
PICK_KINDS = ("file", "dir")

#: Ожидание ответа диалога, секунды.
DIALOG_TIMEOUT_SEC = 300.0

#: Признак «нативного диалога на этой машине нет».
NO_DIALOG = "no_dialog"

#: Фильтр по умолчанию для выбора файла (формат WinForms — им же пользуется UI).
DEFAULT_FILTER = "Все файлы (*.*)|*.*"


def _one_line(value: object, limit: int = 300) -> str:
    """Однострочный безопасный текст для подстановки в команду диалога.

    Переводы строк вырезаются, а не экранируются: в заголовке диалога им делать
    нечего, а в аргументе `osascript`/PowerShell они ломают разбор команды.
    """
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _ps_quote(value: str) -> str:
    """Строковый литерал PowerShell в одинарных кавычках (внутри они удваиваются).

    Одинарные кавычки, а не двойные: в них PowerShell НЕ раскрывает `$переменные`
    и подстановки — заголовок с `$` от пользователя останется заголовком.
    """
    return "'" + str(value).replace("'", "''") + "'"


def _powershell_script(kind: str, title: str, initial: str, file_filter: str) -> str:
    """Скрипт диалога для Windows (см. докстринг модуля про `-STA`/`TopMost`)."""
    lines = [
        # Вывод пути — строго UTF-8: путь с кириллицей иначе приезжает в кодировке
        # консоли и превращается в «??????».
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8",
        "Add-Type -AssemblyName System.Windows.Forms",
        # Скрытая форма-владелец: единственный способ заставить модальный диалог
        # всплыть ПОВЕРХ браузера, из которого его позвали.
        "$owner = New-Object System.Windows.Forms.Form",
        "$owner.TopMost = $true",
        "$owner.ShowInTaskbar = $false",
        "$owner.StartPosition = 'CenterScreen'",
        # Форма невидима, но существует: `Opacity = 0` вместо задания размера —
        # размер потребовал бы ещё и сборки System.Drawing, а видимого окна тут нет.
        "$owner.Opacity = 0",
        "$owner.Show()",
        "$owner.Activate()",
    ]
    if kind == "dir":
        lines += [
            "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog",
            f"$dlg.Description = {_ps_quote(title)}",
            "$dlg.ShowNewFolderButton = $true",
        ]
        if initial:
            lines.append(f"$dlg.SelectedPath = {_ps_quote(initial)}")
        picked = "$dlg.SelectedPath"
    else:
        lines += [
            "$dlg = New-Object System.Windows.Forms.OpenFileDialog",
            f"$dlg.Title = {_ps_quote(title)}",
            f"$dlg.Filter = {_ps_quote(file_filter or DEFAULT_FILTER)}",
            "$dlg.Multiselect = $false",
            "$dlg.CheckFileExists = $true",
        ]
        if initial:
            lines.append(f"$dlg.InitialDirectory = {_ps_quote(initial)}")
        picked = "$dlg.FileName"
    lines += [
        "$result = $dlg.ShowDialog($owner)",
        "$owner.Close()",
        "$owner.Dispose()",
        f"if ($result -eq [System.Windows.Forms.DialogResult]::OK) {{ [Console]::Out.WriteLine({picked}) }}",
    ]
    return "; ".join(lines)


def build_command(kind: str, *, title: str = "", initial: str = "",
                  file_filter: str = "", platform: Optional[str] = None,
                  which: Optional[Callable] = None) -> Optional[list]:
    """argv нативного диалога или `None`, если на этой машине его нет.

    `platform`/`which` — точки инъекции для тестов: набор обязан проверять СБОРКУ
    команды под каждую ОС, не открывая ни одного настоящего окна.
    """
    if kind not in PICK_KINDS:
        raise ValueError(f"неизвестный вид выбора: {kind!r} (ожидалось {PICK_KINDS})")
    plat = platform if platform is not None else sys.platform
    lookup = which if which is not None else shutil.which
    title = _one_line(title)
    initial = _one_line(initial, limit=4096)
    file_filter = _one_line(file_filter)

    if plat == "win32":
        return ["powershell", "-NoProfile", "-STA", "-Command",
                _powershell_script(kind, title, initial, file_filter)]

    if plat == "darwin":
        if not lookup("osascript"):
            return None
        prompt = title or ("Выберите каталог" if kind == "dir" else "Выберите файл")
        what = "choose folder" if kind == "dir" else "choose file"
        script = f'POSIX path of ({what} with prompt "{prompt}")'
        return ["osascript", "-e", script]

    if lookup("zenity"):
        argv = ["zenity", "--file-selection"]
        if kind == "dir":
            argv.append("--directory")
        if title:
            argv.append(f"--title={title}")
        if initial:
            # zenity различает «начальный каталог» по завершающему разделителю.
            argv.append(f"--filename={initial}")
        return argv

    if lookup("kdialog"):
        start = initial or "."
        if kind == "dir":
            return ["kdialog", "--getexistingdirectory", start] + (["--title", title] if title else [])
        return ["kdialog", "--getopenfilename", start] + (["--title", title] if title else [])

    return None


def parse_output(rc: int, stdout: str) -> dict:
    """Исход диалога: путь, отмена или отказ.

    Отмена узнаётся по ДВУМ признакам сразу — ненулевой код (zenity/kdialog/
    osascript отвечают `1`) и пустой stdout (PowerShell закрывает диалог с кодом 0 и
    ничего не печатает). Разделять их незачем: и то и другое — «человек передумал».
    """
    path = (stdout or "").strip().splitlines()
    value = path[0].strip() if path else ""
    if rc != 0 or not value:
        return {"path": None}
    return {"path": value}


def pick(kind: str, *, title: str = "", initial: str = "", file_filter: str = "",
         run: Optional[Callable] = None, platform: Optional[str] = None,
         which: Optional[Callable] = None) -> dict:
    """Показать диалог и вернуть `{"path": ...}` (см. докстринг модуля).

    `run(argv) -> (rc, stdout, stderr)` — точка инъекции для тестов; реальный запуск
    идёт через `standkit.platform.run_console`, который на Windows гасит консольное
    окно самого PowerShell (сам диалог — окно графическое и остаётся видимым).
    """
    argv = build_command(kind, title=title, initial=initial, file_filter=file_filter,
                         platform=platform, which=which)
    if argv is None:
        return {"path": None, "error": NO_DIALOG}
    runner = run if run is not None else _default_run
    rc, stdout, _stderr = runner(argv)
    return parse_output(rc, stdout)


def _default_run(argv: list) -> tuple:
    try:
        proc = run_console(list(argv), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=DIALOG_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 - один вид отказа наружу, как в license_api
        return -1, "", str(exc)
    rc = proc.returncode if proc.returncode is not None else -1
    return int(rc), proc.stdout or "", proc.stderr or ""
