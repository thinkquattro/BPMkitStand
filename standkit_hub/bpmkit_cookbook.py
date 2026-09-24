# -*- coding: utf-8 -*-
"""Поиск локальной копии кукбука BPMkit (инструкции пользователя MCP) для меню
«Справка» дашборда (GAP-522, 24.09.2026).

**Зачем.** Кнопка «?» в шапке открывала только кукбук самого диспетчера
(`/static/cookbook.html`, BPMkitStand). Инструкции пользователя BPMkit — что
умеет агент, кейсы, FAQ, харнессы — из диспетчера было не открыть, хотя она
лежит на той же машине. Хаб отдаёт её маршрутом ``GET /bpmkit-cookbook``.

**Где лежит документ.** Две копии, обе пишет не хаб:

* профиль — ``%APPDATA%\\BPMkit\\docs\\cookbook.html`` (``bpmkit_config_dir()/docs``):
  туда кладёт документ установщик и туда же доставляет свежие редакции канал
  обновлений (``standkit_companion/cookbook.py``, GAP-361);
* поставка — ``{app}\\docs\\cookbook.html`` рядом с установленным MCP. Каталог
  установки хаб узнаёт из маркера работающего MCP ``mcp_runtime.json``
  (поле ``binary`` = ``{app}\\server\\BPMkit.exe``, GAP-447) — тот же путь, что
  у ``standkit_companion.cookbook._shipped_path``.

Побеждает НОВЕЙШАЯ копия — те же правила, что у ``installed_version`` канала
(GAP-429): числовой префикс версии из ``<meta name="bpmkit-cookbook-version">``
посегментно целыми числами, при равенстве — время файла; копия без
разбираемой версии уступает любой с версией.

Модуль stdlib-only и НЕ зависит от ``standkit_companion``: меню справки есть и
у свободной редакции диспетчера, где пакета канала нет.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional

from standkit.registry import bpmkit_config_dir

__all__ = ["COOKBOOK_FILENAME", "candidates", "find_cookbook", "read_version"]

COOKBOOK_FILENAME = "cookbook.html"

_RUNTIME_MARKER_FILENAME = "mcp_runtime.json"

_VERSION_META_RES = (
    re.compile(
        r"""<meta\s+[^>]*name\s*=\s*["']bpmkit-cookbook-version["'][^>]*"""
        r"""content\s*=\s*["']([^"']+)["']""",
        re.IGNORECASE,
    ),
    re.compile(
        r"""<meta\s+[^>]*content\s*=\s*["']([^"']+)["'][^>]*"""
        r"""name\s*=\s*["']bpmkit-cookbook-version["']""",
        re.IGNORECASE,
    ),
)
_VERSION_VALUE_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VERSION_PREFIX_RE = re.compile(r"^(\d+(?:\.\d+)*)")
_SCAN_BYTES = 64 * 1024


def read_version(path: Path) -> Optional[str]:
    """Версия из ``<meta>`` в голове файла либо ``None``."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(_SCAN_BYTES)
    except OSError:
        return None
    text = head.decode("utf-8", errors="replace")
    for regex in _VERSION_META_RES:
        match = regex.search(text)
        if match:
            value = match.group(1).strip()
            if _VERSION_VALUE_RE.match(value):
                return value
    return None


def _order_key(version: Optional[str]):
    if not version:
        return None
    match = _VERSION_PREFIX_RE.match(version.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _runtime_marker_path() -> Path:
    """Тот же контракт, что у ``standkit_companion.releases._runtime_marker_path``."""
    if sys.platform == "win32":
        return bpmkit_config_dir() / _RUNTIME_MARKER_FILENAME
    return Path.home() / ".bpmkit" / _RUNTIME_MARKER_FILENAME


def _shipped_from_marker(marker_path: Path) -> Optional[Path]:
    try:
        data = json.loads(Path(marker_path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    binary = str(data.get("binary") or "").strip()
    if not binary:
        return None
    return Path(binary).parent.parent / "docs" / COOKBOOK_FILENAME


def candidates(config_dir: Optional[Path] = None,
               marker_path: Optional[Path] = None) -> list:
    """Пути, где может лежать кукбук BPMkit, в порядке поиска (профиль, поставка).
    Существование не проверяется — это делает ``find_cookbook``."""
    base = Path(config_dir) if config_dir else bpmkit_config_dir()
    out = [("профиль", base / "docs" / COOKBOOK_FILENAME)]
    shipped = _shipped_from_marker(marker_path or _runtime_marker_path())
    if shipped is not None:
        out.append(("поставка", shipped))
    return out


def find_cookbook(config_dir: Optional[Path] = None,
                  marker_path: Optional[Path] = None) -> Optional[dict]:
    """Самая свежая существующая копия: ``{origin, path, version}`` либо ``None``."""
    found = []
    for origin, path in candidates(config_dir, marker_path):
        try:
            st = Path(path).stat()
        except OSError:
            continue
        if not Path(path).is_file():
            continue
        version = read_version(path)
        found.append({"origin": origin, "path": Path(path), "version": version,
                      "mtime": st.st_mtime})
    if not found:
        return None

    def _key(entry):
        order = _order_key(entry["version"])
        return (0, (), 0.0) if order is None else (1, order, entry["mtime"])

    best = found[0]
    for candidate in found[1:]:
        if _key(candidate) > _key(best):
            best = candidate
    return {"origin": best["origin"], "path": best["path"], "version": best["version"]}
