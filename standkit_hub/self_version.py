# -*- coding: utf-8 -*-
"""GAP-528: проверка версии диспетчера БЕЗ лицензии, stdlib-only.

Зачем отдельный модуль. `standkit_companion/hub_channel.py::check_hub_pypi` уже
делает то же самое — best-effort сверку `standkit` с PyPI для pip-режима, — но
живёт в ПЛАТНОЙ редакции и недоступен без неё. Бесплатная редакция окна
«Обновления» умеет показать только сам диспетчер (без каналов паттернов/
релизов/скиллов, которым нужна лицензия), и эта карточка обязана работать
БЕЗ пакета `standkit_companion` вовсе — точно так же, как остальное ядро хаба.

Поэтому сравнение версий и сетевой поход к PyPI переехали сюда,
в `standkit_hub` (тот же пакет, что и сам сервер, зависимость только от
stdlib и от `standkit` — НИКОГДА не от `standkit_companion`), а
`hub_channel.check_hub_pypi` стал тонкой обёрткой поверх этого модуля, чтобы
не разъезжаться в двух местах: см. докстринг там за подробностями про
разницу режимов "frozen"/"pip".

Кэш — в памяти ЭТОГО процесса, TTL 6 часов (см. `_CACHE_TTL_S`): проверка
версии на PyPI — попутная информация для карточки «О диспетчере», а не то,
ради чего стоит дёргать сеть на каждый `GET`. `GET /api/hub/self-version`
отдаёт кэш и при его отсутствии делает проверку лениво (best-effort, без
исключений наружу — сетевая беда превращается в поле `error`);
`POST /api/hub/self-version/check` обходит кэш принудительно.
"""
from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

__all__ = [
    "PYPI_PACKAGE_URL",
    "PIP_INSTALL_COMMAND",
    "is_frozen",
    "parse_version",
    "compare_versions",
    "fetch_pypi_latest",
    "cached_pypi_latest",
    "reset_cache",
]

#: Публичный JSON-эндпоинт PyPI — тот же контракт, что у обычного `GET
#: /pypi/<name>/json` любого пакета: `info.version` = последняя версия.
PYPI_PACKAGE_URL = "https://pypi.org/pypi/standkit/json"

#: Таймаут одного запроса к PyPI. Best-effort проверка не имеет права
#: подвесить ответ хаба на системный сетевой таймаут (десятки секунд).
_PYPI_TIMEOUT_S = 5.0

#: TTL кэша в памяти процесса — 6 часов.
_CACHE_TTL_S = 6 * 60 * 60

#: Команда, которую печатает UI/ответ API для pip-режима — диспетчер сам
#: `pip install` не запускает (см. докстринг `hub_channel` про запрет
#: незапрошенной установки стороннего пакета из процесса без терминала).
PIP_INSTALL_COMMAND = "python -m pip install -U standkit"

_SEGMENT_RE = re.compile(r"\d+")


def is_frozen() -> bool:
    """Запущен ли ЭТОТ процесс из самообновляемой сборки (PyInstaller), а не
    из pip-установки/исходников. Отдельная функция — чтобы тесты подменяли
    её одной точкой (`monkeypatch.setattr(self_version, "is_frozen", ...)`),
    не трогая `sys.frozen` глобально."""
    return bool(getattr(sys, "frozen", False))


def parse_version(value: object) -> tuple:
    """Строка версии → кортеж целых чисел посегментно (`"1.2.0-rc1"` →
    `(1, 2, 0)`). Нечисловой сегмент даёт `0`: падать на неожиданной строке
    сверка версий не имеет права. Ведущее `v`/`V` срезается."""
    text = str(value or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    if not text:
        return ()
    out = []
    for chunk in text.split("."):
        found = _SEGMENT_RE.match(chunk.strip())
        out.append(int(found.group()) if found else 0)
    return tuple(out)


def compare_versions(a: object, b: object) -> int:
    """`-1` / `0` / `+1` — посегментная сверка версий как кортежей int.

    Короткий кортеж дополняется нулями справа (`1.2` == `1.2.0`) — та же
    логика, что и у `standkit_companion.releases.compare_versions`/
    `standkit_companion.patterns.compare_versions` (продублирована здесь
    намеренно: этот модуль обязан работать БЕЗ `standkit_companion`)."""
    va, vb = parse_version(a), parse_version(b)
    width = max(len(va), len(vb))
    va = va + (0,) * (width - len(va))
    vb = vb + (0,) * (width - len(vb))
    return (va > vb) - (va < vb)


def fetch_pypi_latest(*, timeout: float = _PYPI_TIMEOUT_S) -> dict:
    """Один сетевой поход к PyPI. Возвращает `{"latest": str|None, "error":
    str|None}` — НИКОГДА не бросает исключение: недоступность PyPI, обрыв
    сети, кривой ответ — всё это best-effort ложится в поле `error`."""
    try:
        req = Request(PYPI_PACKAGE_URL, headers={"Accept": "application/json"})
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - фиксированный https-хост
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, HTTPError, ValueError, OSError, UnicodeDecodeError) as exc:
        return {"latest": None, "error": f"{type(exc).__name__}: {exc}"}
    latest = str((payload.get("info") or {}).get("version") or "").strip()
    return {"latest": latest or None, "error": None}


_cache_lock = threading.Lock()
#: `{"latest": str|None, "error": str|None, "checked_at": iso-строка, "_ts": monotonic}`
#: либо `None`, пока проверки ещё не было.
_cache: Optional[dict] = None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reset_cache() -> None:
    """Тестовый хук — сбрасывает кэш процесса (изоляция между тестами)."""
    global _cache
    with _cache_lock:
        _cache = None


def cached_pypi_latest(
    *,
    force: bool = False,
    fetch: Callable[[], dict] = fetch_pypi_latest,
) -> dict:
    """Кэш в памяти процесса, TTL 6 часов.

    `force=False` (обычный `GET`) — отдаёт кэш, если он ещё не протух, иначе
    делает проверку лениво (по месту, без фонового потока) и кладёт её в кэш.
    `force=True` (`POST .../check`) — ВСЕГДА обходит кэш и делает свежий
    запрос, но результат тоже кэширует — следующий обычный `GET` увидит
    именно его, а не сходит в сеть повторно.

    Возвращает `{"latest": str|None, "error": str|None, "checked_at": iso}`.
    """
    global _cache
    with _cache_lock:
        now = time.monotonic()
        if not force and _cache is not None and (now - _cache["_ts"]) < _CACHE_TTL_S:
            return {"latest": _cache["latest"], "error": _cache["error"],
                     "checked_at": _cache["checked_at"]}
        result = fetch()
        entry = {
            "latest": result.get("latest"),
            "error": result.get("error"),
            "checked_at": _utc_now_iso(),
            "_ts": now,
        }
        _cache = entry
        return {"latest": entry["latest"], "error": entry["error"],
                 "checked_at": entry["checked_at"]}
