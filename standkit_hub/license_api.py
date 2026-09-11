# -*- coding: utf-8 -*-
"""Лицензия BPMkit глазами диспетчера: тонкий прокси к CLI самого MCP.

Своей лицензионной логики здесь НЕТ и быть не может (тот же принцип, что у
`standkit_companion/context.py`): проверка конверта живёт в клиентском MCP и на
бэкенде издателя, а третья копия расходится с ними не «если», а «когда». Хаб умеет
ровно три вещи — спросить сводку, отдать новый ключ и снять активацию, — и каждая
из них выполняется ОДНИМ вызовом CLI::

    <mcp_cli> setup license-info --json
    <mcp_cli> setup license-store <файл-с-ключом> --json
    <mcp_cli> setup license-deactivate --json

Почему модуль лежит в MIT-ядре хаба, а не в платном пакете канала. Экран лицензии
обязан работать И в свободной редакции — именно там он нужнее всего («ключ ввёл, а
что дальше?»). Поэтому `find_cli` здесь свой (тонкая обёртка), а не импортированный
из `standkit_companion.context`: пакета канала рядом может не быть вовсе. Сама логика
резолва — общий хелпер `standkit.cli_resolve` (GAP-273): явная настройка сильнее
переменной окружения `BPMKIT_CLI`, та сильнее автодетекта бинаря рядом с поставкой, а
если бинаря нигде нет — фолбэк на запуск из исходников (`server/main.py`) тем же
python, что у хаба. Общий хелпер, а не «дословное» повторение в двух модулях (так
было до GAP-273) — расхождение здесь означало бы «канал видит один MCP, экран
лицензии другой», а руками синхронизировать два места рано или поздно забудут.

ЗАПРЕТЫ, ради которых модуль существует отдельно:

* **ключ не передаётся в argv.** Командная строка процесса видна любому
  пользователю машины (`tasklist`, `ps`), поэтому текст ключа кладётся во
  временный файл с правами только владельца (0600) в пользовательском каталоге
  BPMkit и удаляется в `finally` — включая пути отказа и таймаута;
* **ключ не попадает ни в логи, ни в ответы, ни в текст ошибок.** Наружу уходят
  только stderr CLI (обрезанный) и коды возврата — тем же приёмом, что в
  `context._clip`;
* **кэш только для чтения.** Сводка живёт `CACHE_TTL_SEC` секунд (экран лицензии
  опрашивается вместе с прочими вкладками, а запуск процесса стоит десятки
  миллисекунд), и любая мутация — запись ключа, снятие активации — сбрасывает его
  немедленно: «ввёл ключ, а ничего не изменилось» здесь недопустимо.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from standkit.cli_resolve import (
    CLI_ENV_VAR,
    candidate_roots as _shared_candidate_roots,
    describe_search_targets,
    resolve_command_string,
    search_roots,
)
from standkit.platform import run_console
from standkit.registry import bpmkit_config_dir

__all__ = [
    "EDITION_COMPANION",
    "EDITION_FREE",
    "CACHE_TTL_SEC",
    "LICENSE_INFO_TAIL",
    "LICENSE_STORE_TAIL",
    "LICENSE_DEACTIVATE_TAIL",
    "LicenseCliError",
    "find_cli",
    "license_info",
    "license_store_token",
    "license_store_file",
    "license_deactivate",
    "invalidate_cache",
]

#: Редакции — те же значения, что у `standkit_hub.server.EDITION_*`. Дублируются,
#: чтобы модуль не импортировал веб-слой (симметрично config.py).
EDITION_COMPANION = "companion"
EDITION_FREE = "free"

#: Хвосты argv — контракт с чужим пакетом, поэтому константы, а не строки по месту.
LICENSE_INFO_TAIL = ("setup", "license-info", "--json")
LICENSE_STORE_TAIL = ("setup", "license-store")
LICENSE_DEACTIVATE_TAIL = ("setup", "license-deactivate", "--json")

#: Сколько живёт сводка. Минута — компромисс между «не запускать процесс на каждый
#: тик UI» и «истёкшая лицензия замечается в пределах минуты».
CACHE_TTL_SEC = 60.0

#: Ожидание ответа CLI. Как в context.py: с запасом на холодный старт `.exe` под
#: антивирусом. У записи ключа запас больше — она ещё и ходит в сеть к издателю
#: (best-effort активация).
_CLI_TIMEOUT_S = 30.0
_CLI_WRITE_TIMEOUT_S = 60.0

_DETAIL_LIMIT = 300

# Резолв CLI (имена бинаря, подпути, фолбэк на запуск из исходников, переменная
# окружения) — общий хелпер `standkit.cli_resolve` (GAP-273), тот же, что у
# standkit_companion/context.py (см. докстринг модуля выше).

_cache_lock = threading.Lock()
_cache: dict = {}


def _clip(text: str, limit: int = _DETAIL_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


class LicenseCliError(Exception):
    """Отказ CLI лицензии, уже приведённый к форме ответа хаба.

    `status` — HTTP-код, который обязан уйти наружу: 503, когда CLI рядом нет
    вовсе (чинится путём в настройках), и 400 на всё остальное (CLI ответил
    отказом — ключ не тот, файл не читается, бэкенд отказал).
    """

    def __init__(self, error: str, *, detail: str = "", status: int = 400) -> None:
        super().__init__(error)
        self.error = error
        self.detail = _clip(detail)
        self.status = int(status)

    def to_dict(self) -> dict:
        return {"ok": False, "error": self.error, "detail": self.detail}


# ------------------------------------------------------------------------------------
# Поиск CLI
# ------------------------------------------------------------------------------------
def _candidate_roots(extra_roots: Optional[Sequence] = None) -> list:
    """Тонкая обёртка над `standkit.cli_resolve.candidate_roots` — имя остаётся
    стабильным для тестов, которые подменяют именно его
    (`monkeypatch.setattr(license_api, "_candidate_roots", ...)`)."""
    return _shared_candidate_roots(__file__, extra_roots=extra_roots)


def find_cli(settings, *, extra_roots: Optional[Sequence] = None) -> Optional[list]:
    """argv-префикс запуска CLI BPMkit или `None`, если рядом его нет.

    `settings` — секция `companion` конфига хаба (нужно единственное поле
    `mcp_cli`): у экрана лицензии и у канала обновлений ОДИН путь к MCP, второго
    поля в настройках не заводим.

    Порядок резолва (GAP-273; сильнее — выше) — дословно тот же, что у
    `standkit_companion.context.find_cli` (общий хелпер `standkit.cli_resolve`):

    1. Явная настройка `companion.mcp_cli`.
    2. Переменная окружения `BPMKIT_CLI` (тот же формат).
    3. Автодетект бинаря (`bpmkit.exe`/`bpmkit`) рядом с поставкой.
    4. Автодетект запуска из исходников (`server/main.py` тем же python, что у хаба),
       если бинарь не нашёлся нигде.
    """
    configured = str(getattr(settings, "mcp_cli", "") or "").strip()
    if configured:
        return resolve_command_string(configured)

    from_env = str(os.environ.get(CLI_ENV_VAR, "") or "").strip()
    if from_env:
        return resolve_command_string(from_env)

    return search_roots(_candidate_roots(extra_roots))


# ------------------------------------------------------------------------------------
# Запуск CLI
# ------------------------------------------------------------------------------------
def _default_run(argv: list, *, timeout: float = _CLI_TIMEOUT_S) -> tuple:
    """Запуск через ЕДИНУЮ точку `standkit.platform.run_console` (GAP-138).

    Любое исключение сводится к `rc=-1`: вызывающий разбирает один вид отказа, а не
    зоопарк исключений `subprocess`.
    """
    try:
        proc = run_console(list(argv), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - см. докстринг
        return -1, "", str(exc)
    rc = proc.returncode if proc.returncode is not None else -1
    return int(rc), proc.stdout or "", proc.stderr or ""


def _parse_json(stdout: str) -> Optional[dict]:
    """JSON из stdout CLI; `None`, если разобрать нечего.

    Терпим к ведущему/хвостовому шуму (баннер рантайма) — вырезаем участок от первой
    `{` до последней `}`. Сырой stdout в ошибки не попадает.
    """
    text = (stdout or "").strip()
    if not text:
        return None
    for candidate in (text, text[text.find("{"): text.rfind("}") + 1] if "{" in text and "}" in text else ""):
        if not candidate:
            continue
        try:
            payload = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _cli_or_raise(settings) -> list:
    cli = find_cli(settings)
    if not cli:
        raise LicenseCliError(
            "Рядом не найден CLI BPMkit — укажите путь к нему в настройках "
            f"(раздел «Канал обновлений», поле «Путь к CLI BPMkit») либо переменной "
            f"окружения {CLI_ENV_VAR}",
            detail=describe_search_targets(_candidate_roots()),
            status=503,
        )
    return cli


def _run_json(settings, tail: Sequence[str], *, run: Optional[Callable] = None,
              timeout: float = _CLI_TIMEOUT_S, failure: str) -> dict:
    """Один вызов CLI, отдающий JSON. Отказ — типизированный `LicenseCliError`."""
    argv = _cli_or_raise(settings) + list(tail)
    # Точка инъекции для тестов: `run(argv) -> (rc, stdout, stderr)`. Таймаут —
    # забота реального запуска, подставному раннеру он не нужен и в его сигнатуру
    # не протекает.
    if run is not None:
        rc, stdout, stderr = run(argv)
    else:
        rc, stdout, stderr = _default_run(argv, timeout=timeout)
    payload = _parse_json(stdout)
    if payload is None:
        raise LicenseCliError(
            failure,
            detail=_clip(stderr) or f"код возврата {rc}, вывод не разобран как JSON",
        )
    if not payload.get("ok", True):
        raise LicenseCliError(
            str(payload.get("error") or failure),
            detail=_clip(str(payload.get("detail") or "")) or _clip(stderr),
        )
    if rc != 0:
        # CLI ответил валидным `ok: true`, но ненулевым кодом — доверяем коду:
        # молча считать такой исход успехом значило бы показать «ключ принят» там,
        # где он не принят.
        raise LicenseCliError(failure, detail=_clip(stderr) or f"код возврата {rc}")
    return payload


# ------------------------------------------------------------------------------------
# Сводка (GET /api/license)
# ------------------------------------------------------------------------------------
def _free_snapshot(detail: str) -> dict:
    """Ответ, когда CLI рядом нет: свободная редакция и честная причина.

    Это НЕ ошибка: диспетчер без MCP — штатная (и самая частая на новой машине)
    ситуация, экран лицензии обязан показать её текстом, а не пустотой.
    """
    return {"ok": True, "edition": EDITION_FREE, "status": "unavailable",
            "detail": _clip(detail)}


def license_info(settings, *, run: Optional[Callable] = None,
                 cache_ttl: float = CACHE_TTL_SEC) -> dict:
    """Сводка лицензии для `GET /api/license` (с кэшем на `cache_ttl` секунд).

    Конверт сюда не попадает: `setup license-info --json` отдаёт БЕЗОПАСНУЮ сводку
    (статус, лицензиат, тариф, хвост идентификатора, срок), а не сам ключ.
    """
    cli = find_cli(settings)
    if not cli:
        return _free_snapshot(describe_search_targets(_candidate_roots()))

    key = tuple(cli) + LICENSE_INFO_TAIL
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        payload = _run_json(settings, LICENSE_INFO_TAIL, run=run,
                            failure="CLI BPMkit не ответил на запрос сведений о лицензии")
    except LicenseCliError as exc:
        # Сводка обязана отвечать всегда: отказавший CLI — это «сведений нет», а не
        # 500 на экране лицензии. Такой ответ НЕ кэшируется — починка (правка пути,
        # установка MCP) должна быть видна сразу.
        return {"ok": True, "edition": EDITION_FREE, "status": "unavailable",
                "detail": exc.detail or exc.error}

    snapshot = dict(payload)
    snapshot["edition"] = EDITION_COMPANION
    _cache_put(key, snapshot, cache_ttl)
    return snapshot


# ------------------------------------------------------------------------------------
# Запись ключа (PUT /api/license, POST /api/license/file)
# ------------------------------------------------------------------------------------
def _write_key_file(token: str) -> Path:
    """Кладёт текст ключа во временный файл с правами ТОЛЬКО владельца.

    Каталог — пользовательский `bpmkit_config_dir()`, а не общий `%TEMP%`/`/tmp`:
    во втором соседний пользователь машины успел бы прочитать файл между созданием и
    выставлением прав. Файл открывается сразу с режимом 0600 (`os.open` + `O_EXCL`),
    а не `chmod` после записи.
    """
    directory = bpmkit_config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"license-input-{os.getpid()}-{int(time.time() * 1000)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(token)
    return path


def _store_from_path(settings, path: Path, *, run: Optional[Callable] = None) -> dict:
    argv_tail = list(LICENSE_STORE_TAIL) + [str(path), "--json"]
    return _run_json(settings, argv_tail, run=run, timeout=_CLI_WRITE_TIMEOUT_S,
                     failure="CLI BPMkit не принял лицензионный ключ")


def license_store_token(settings, token: str, *, run: Optional[Callable] = None) -> dict:
    """Записать ключ, полученный текстом (`PUT /api/license`).

    Ключ уходит в CLI ФАЙЛОМ, а не аргументом: командная строка чужого процесса
    видна всей машине. Файл удаляется в `finally` при любом исходе.
    """
    text = (token or "").strip()
    if not text:
        raise LicenseCliError("Пустой лицензионный ключ",
                              detail="поле 'token' обязано содержать текст ключа")
    path = _write_key_file(text)
    try:
        payload = _store_from_path(settings, path, run=run)
    finally:
        _unlink_quietly(path)
    invalidate_cache()
    return payload


def license_store_file(settings, path: str, *, run: Optional[Callable] = None) -> dict:
    """Записать ключ из файла, выбранного человеком (`POST /api/license/file`).

    Файл читается ХАБОМ и передаётся CLI своей временной копией: путь, пришедший из
    браузера, к CLI напрямую не уезжает, а ключ не гоняется через страницу вовсе.
    """
    raw = (path or "").strip()
    if not raw:
        raise LicenseCliError("Не указан путь к файлу ключа",
                              detail="поле 'path' обязано быть непустой строкой")
    source = Path(raw)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise LicenseCliError(f"Файл ключа не прочитан: {source.name}",
                              detail=f"{type(exc).__name__}: {exc}") from None
    return license_store_token(settings, text, run=run)


# ------------------------------------------------------------------------------------
# Снятие активации (DELETE /api/license)
# ------------------------------------------------------------------------------------
def license_deactivate(settings, *, run: Optional[Callable] = None) -> dict:
    """Снять активацию у издателя и удалить ключ локально (`DELETE /api/license`).

    Отсутствие сети — не отказ: CLI выполняет локальное удаление и честно сообщает
    `remote: "unreachable"`. Разбор этих полей — дело UI, хаб их не трактует.
    """
    payload = _run_json(settings, LICENSE_DEACTIVATE_TAIL, run=run,
                        timeout=_CLI_WRITE_TIMEOUT_S,
                        failure="CLI BPMkit не выполнил снятие активации")
    invalidate_cache()
    return payload


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        # Не смогли удалить (антивирус держит, файл уже унесли) — это не повод
        # уронить операцию, ради которой файл создавался.
        pass


# ------------------------------------------------------------------------------------
# Кэш
# ------------------------------------------------------------------------------------
def invalidate_cache() -> None:
    """Сбросить кэш сводки. Зовётся после КАЖДОЙ мутации лицензии."""
    with _cache_lock:
        _cache.clear()


def _cache_get(key) -> Optional[dict]:
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        expires_at, payload = entry
        if expires_at <= time.monotonic():
            _cache.pop(key, None)
            return None
        return dict(payload)


def _cache_put(key, payload: dict, ttl: float) -> None:
    try:
        ttl = float(ttl)
    except (TypeError, ValueError):
        ttl = 0.0
    if ttl <= 0:
        return
    with _cache_lock:
        _cache[key] = (time.monotonic() + ttl, dict(payload))
