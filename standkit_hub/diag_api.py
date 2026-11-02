# -*- coding: utf-8 -*-
"""Архив диагностики BPMkit глазами диспетчера: тонкий прокси к CLI MCP.

Своей логики сбора здесь НЕТ — тот же принцип, что у `license_api`: состав архива,
маскирование секретов и имени пользователя, manifest с хешами живут в самом MCP
(`bpmkit/diag_bundle.py`). Хаб умеет ровно две вещи:

    <mcp_cli> setup diag-bundle --json   # собрать архив (по умолчанию — на рабочий стол)
    открыть папку ПОСЛЕДНЕГО собранного архива в Проводнике

ЗАПРЕТЫ:

* **по сети ничего не отправляется** — ни хабом, ни CLI: результат — только файл,
  который человек сам пришлёт издателю (отправка на бэкенд — вопрос EULA, вне
  объёма);
* **путь из браузера не принимается.** «Открыть папку» открывает каталог архива,
  путь к которому вернул CLI этому же процессу хаба; тело запроса пути не несёт —
  тот же приём фиксированной цели, что у `POST /api/hub/open-folder`.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from standkit_hub import license_api, logs_browser

__all__ = [
    "DIAG_BUNDLE_TAIL",
    "diag_bundle",
    "last_bundle_path",
    "open_last_bundle_folder",
    "reset_last_bundle",
]

#: Хвост argv — контракт с пакетом MCP (`setup_cli.cmd_diag_bundle`).
DIAG_BUNDLE_TAIL = ("setup", "diag-bundle", "--json")

#: Сборка включает self_check и пробу сети — с запасом на медленную машину.
_CLI_DIAG_TIMEOUT_S = 300.0

_lock = threading.Lock()
_last_bundle: dict = {}


def diag_bundle(settings, *, run: Optional[Callable] = None) -> dict:
    """Собрать архив диагностики. Успех — `{"ok": True, "path", "folder", "size"}`.

    Отказ — `license_api.LicenseCliError` (503 — CLI не найден, 400 — архив не
    создан, текст причины из `detail` CLI). Старый MCP без подкоманды — понятный
    текст «обновите BPMkit», а не вывод argparse.
    """
    argv = license_api._cli_or_raise(settings) + list(DIAG_BUNDLE_TAIL)
    if run is not None:
        rc, stdout, stderr = run(argv)
    else:
        rc, stdout, stderr = license_api._default_run(argv, timeout=_CLI_DIAG_TIMEOUT_S)
    payload = license_api._parse_json(stdout)
    if payload is None:
        if license_api.is_unknown_command(rc, stderr):
            raise license_api.LicenseCliError(
                "Эта версия BPMkit не умеет собирать архив диагностики — обновите BPMkit",
                detail=license_api._clip(stderr))
        raise license_api.LicenseCliError(
            "CLI BPMkit не собрал архив диагностики",
            detail=license_api._clip(stderr) or f"код возврата {rc}, вывод не разобран как JSON")
    path = str(payload.get("path") or "").strip()
    if not payload.get("ok") or rc != 0 or not path:
        raise license_api.LicenseCliError(
            "Архив диагностики не создан",
            detail=str(payload.get("detail") or payload.get("error") or "")
            or license_api._clip(stderr) or f"код возврата {rc}")
    folder = str(Path(path).parent)
    with _lock:
        _last_bundle.clear()
        _last_bundle.update({"path": path, "folder": folder})
    result = {"ok": True, "path": path, "folder": folder, "size": payload.get("size")}
    if payload.get("fallback_reason"):
        # Рабочий стол недоступен — CLI положил архив в другое место; скажем почему.
        result["fallback_reason"] = license_api._clip(str(payload.get("fallback_reason")))
    return result


def last_bundle_path() -> Optional[str]:
    with _lock:
        return _last_bundle.get("path")


def reset_last_bundle() -> None:
    """Забыть последний архив (тесты)."""
    with _lock:
        _last_bundle.clear()


def open_last_bundle_folder(*, opener: Optional[Callable] = None) -> tuple:
    """Открыть папку последнего архива: `(ok, message)`.

    `opener(Path) -> OpenFolderResult` — точка инъекции для тестов; по умолчанию
    `logs_browser.open_folder` (тот же путь, что у «Открыть папку» логов стенда).
    """
    with _lock:
        folder = _last_bundle.get("folder")
    if not folder:
        return False, "архив диагностики в этом сеансе диспетчера ещё не собирался"
    result = (opener or logs_browser.open_folder)(Path(folder))
    return bool(result.ok), str(result.message)
