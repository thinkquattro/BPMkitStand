# -*- coding: utf-8 -*-
"""Состояние канала на диске — единственный источник правды о том, что уже применено.

Файл: `<bpmkit_config_dir>/companion-state.json` (на Windows — `%APPDATA%\\BPMkit\\`),
рядом с `standkit-hub.json`. Пишется атомарно (`fsutil.atomic_write_text`): состояние
переживает падение процесса и обрыв питания в середине записи — иначе битый JSON молча
сбросил бы курсор синхронизации и клиент перекачал бы базу паттернов с нуля.

Ключевое проектное решение: **файлы паттернов — проекция состояния, а не наоборот.**
`patterns.applied` хранит ПОЛНЫЕ записи паттернов, приехавших из канала, а `dev/patterns_
*_updates.md` и управляемый блок индекса РЕНДЕРЯТСЯ из них целиком на каждое применение.
Причина: отзыв паттерна (tombstone) при хирургическом удалении раздела из markdown —
операция, которая ломается на любом нестандартном оформлении тела; полная перерисовка из
состояния корректна по построению и тривиально тестируется. Цена — хранение тел в JSON,
это десятки-сотни килобайт, приемлемо.

Курсор синхронизации — ПАРА `(since, since_id)`. Хранить только время нельзя: строки с
одинаковой меткой на границе страницы теряются молча, сервер защищён именно парой
(`BPMkit-backend/app/routers/content.py`), и клиент обязан эту защиту не сломать.

Состояние НИКОГДА не содержит лицензионный конверт, токены и пути к секретам — только
результаты и метки времени. Конверт живёт в secretstore MCP и запрашивается на каждый тик.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from standkit.registry import bpmkit_config_dir

from . import fsutil

__all__ = ["STATE_FILE_NAME", "CompanionState", "state_path", "utc_now_iso"]

STATE_FILE_NAME = "companion-state.json"

_SCHEMA_VERSION = 1

# Сколько применённых версий бинаря помним для отката. 2 — минимум, при котором откат
# вообще имеет смысл (текущая + предыдущая); держим 3, чтобы пережить один неудачный
# промежуточный релиз.
RELEASE_HISTORY_KEEP = 3


def utc_now_iso() -> str:
    """UTC-метка секундной точности с `Z` — та же форма, что у сайдкара подписи
    (`signed_at`) и у бэкенда. Локальные зоны в состоянии не появляются никогда."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def state_path(config_dir: Optional[Path] = None) -> Path:
    """Путь файла состояния. `config_dir` переопределяется только в тестах."""
    base = Path(config_dir) if config_dir else bpmkit_config_dir()
    return Path(base) / STATE_FILE_NAME


def _default_state() -> dict:
    return {
        "schema_version": _SCHEMA_VERSION,
        "patterns": {
            "since": None,
            "since_id": None,
            "seeded": False,
            "root": "",
            "last_run_at": None,
            "last_status": "never",
            "last_detail": "",
            "last_bundle_sha256": "",
            "applied": [],
        },
        "releases": {
            "last_check_at": None,
            "last_status": "never",
            "last_detail": "",
            "known_latest": None,
            "etag": None,
            "partial": None,
            "staged": None,
            "current": None,
            "restart_required": False,
            "history": [],
        },
        "revocations": {
            "last_check_at": None,
            "last_status": "never",
            "last_detail": "",
            "etag": None,
            "revoked_ids": [],
        },
    }


class CompanionState:
    """Обёртка над файлом состояния. Не потокобезопасна сама по себе — сериализуется
    планировщиком (`runner.py`), у которого ровно один рабочий поток."""

    def __init__(self, path: Path, data: Optional[dict] = None) -> None:
        self.path = Path(path)
        self.data = data if data is not None else _default_state()

    # -- загрузка/сохранение -----------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "CompanionState":
        """Битый/отсутствующий файл — НЕ исключение: канал обязан подняться и на пустом
        состоянии (максимум — перекачает паттерны заново). Ровно та же best-effort
        семантика, что у `HubConfig.load`."""
        p = Path(path) if path else state_path()
        try:
            raw = p.read_text(encoding="utf-8-sig")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("корень состояния — не объект")
        except (OSError, ValueError):
            return cls(p)
        return cls(p, cls._migrate(data))

    @staticmethod
    def _migrate(data: dict) -> dict:
        """Достройка отсутствующих секций дефолтами. Формального версионирования пока не
        нужно — схема одна; но недостающая секция не должна ронять чтение, иначе апгрейд
        Companion через установщик обнулял бы курсор."""
        base = _default_state()
        for section, defaults in base.items():
            if section == "schema_version":
                continue
            got = data.get(section)
            if not isinstance(got, dict):
                data[section] = defaults
                continue
            for key, value in defaults.items():
                got.setdefault(key, value)
        data["schema_version"] = _SCHEMA_VERSION
        return data

    def save(self) -> None:
        fsutil.atomic_write_text(
            self.path, json.dumps(self.data, ensure_ascii=False, indent=2) + "\n")

    # -- доступ к секциям ---------------------------------------------------------------
    @property
    def patterns(self) -> dict:
        return self.data["patterns"]

    @property
    def releases(self) -> dict:
        return self.data["releases"]

    @property
    def revocations(self) -> dict:
        return self.data["revocations"]

    def mark(self, section: str, status: str, detail: str = "") -> None:
        """Единая точка записи исхода тика. `status` — `ok`/`skipped`/`error`/`never`."""
        block = self.data[section]
        key = "last_run_at" if section == "patterns" else "last_check_at"
        block[key] = utc_now_iso()
        block["last_status"] = status
        block["last_detail"] = detail or ""

    # -- релизы -------------------------------------------------------------------------
    def push_history(self, entry: dict) -> None:
        """Запомнить применённую версию для отката, подрезав хвост.

        Подрезка сознательно НЕ удаляет файлы бэкапов с диска: состояние обязано
        оставаться дешёвой и безопасной операцией. Осиротевшие бэкапы вычищает
        `releases.prune_backups`, у которого это единственная задача.
        """
        history = self.releases.setdefault("history", [])
        history.insert(0, entry)
        del history[RELEASE_HISTORY_KEEP:]

    # -- сводка для UI -------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Плоская карточка состояния для `/api/companion/status` и `self_check` MCP.

        Тела паттернов сюда НЕ попадают — только счётчики: ответ статуса дёргается
        поллером UI и обязан оставаться маленьким.
        """
        pat = self.patterns
        rel = self.releases
        rev = self.revocations
        staged = rel.get("staged") or {}
        current = rel.get("current") or {}
        return {
            "patterns": {
                "applied_count": len(pat.get("applied") or []),
                "last_run_at": pat.get("last_run_at"),
                "status": pat.get("last_status"),
                "detail": pat.get("last_detail"),
                "root": pat.get("root"),
                "seeded": bool(pat.get("seeded")),
                "cursor": {"since": pat.get("since"), "since_id": pat.get("since_id")},
            },
            "releases": {
                "last_check_at": rel.get("last_check_at"),
                "status": rel.get("last_status"),
                "detail": rel.get("last_detail"),
                "known_latest": rel.get("known_latest"),
                "staged_version": staged.get("version"),
                "staged_signed": staged.get("signed"),
                "current_version": current.get("version"),
                "restart_required": bool(rel.get("restart_required")),
                "rollback_available": bool(rel.get("history")),
                "resume_bytes": (rel.get("partial") or {}).get("bytes"),
            },
            "revocations": {
                "last_check_at": rev.get("last_check_at"),
                "status": rev.get("last_status"),
                "detail": rev.get("last_detail"),
                "revoked_count": len(rev.get("revoked_ids") or []),
            },
        }
