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
from .patterns import compare_versions

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
            # GAP-437: размер поставочной базы, посчитанный при seed (см.
            # `patterns.seed_override_root`/`patterns._count_shipped_patterns`). `None` —
            # размер ещё не считался ЧЕСТНО (не «база пуста»).
            "shipped_count": None,
            "last_run_at": None,
            "last_status": "never",
            "last_detail": "",
            "last_bundle_sha256": "",
            "applied": [],
            # GAP-528: признак «последний проход канала реально что-то применил или
            # отозвал» — единственный надёжный сигнал «есть новые паттерны», раз сам
            # `sync` дренирует очередь целиком за один тик (см. `patterns.sync`) и
            # отдельного «доступно, но не скачано» состояния у канала нет. Кнопка
            # «Загрузить новые» в UI смотрит на это поле (см. `summary`), не на
            # дельту total_available, которая после того же тика уже не отличима от
            # «всё было применено раньше».
            "had_new_last_run": False,
            # GAP-528: ожидающие у издателя после курсора (`patterns.peek`).
            "pending_count": 0,
            "pending_more": False,
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
            # GAP-447: что реально работает СЕЙЧАС (маркер `mcp_runtime.json`), отдельно
            # от того, что канал СЧИТАЕТ установленным (`current`) — до перезапуска это два
            # разных факта. `None` — маркера ни разу не было видно.
            "running_version": None,
            "running_started_at": None,
            # GAP-442: состав обновления и известные проблемы (`GET /v1/version/latest`),
            # попутный запрос при проверке релиза — см. `releases._update_release_notes`.
            "release_notes_version": None,
            "release_notes": [],
            "known_issues": [],
            # GAP-463: издатель объявил, что версия `release_notes_version` ставится
            # ТОЛЬКО установщиком (`requires_installer` в `GET /v1/version/latest`,
            # булево, СИММЕТРИЧНО остальным полям попутного запроса выше). `False` —
            # значение по умолчанию и единственно верная трактовка отсутствия поля,
            # отсутствия записи для `latest` и мусора в поле (обратная совместимость
            # со старым бэкендом — см. докстринг `releases._update_release_notes`).
            "requires_installer": False,
        },
        # GAP-361: узкий поток кукбука. Отдельная секция, а не поле внутри
        # `releases`, — по той же причине, по которой отдельный модуль
        # `cookbook.py`: у документа свой жизненный цикл, и он не должен ни
        # занимать слот `staged` бинаря, ни участвовать в откате релизов.
        "cookbook": {
            "last_check_at": None,
            "last_status": "never",
            "last_detail": "",
            "known_latest": None,
            "last_sync_at": None,
            "installed": None,
        },
        # Обратный проход канала (GAP-260/GAP-248 п.2): единственный поток,
        # который не ПРИНИМАЕТ, а ОТДАЁТ. Свой слот по той же причине, что у
        # кукбука: у него свой исход («офлайн» здесь — норма, а не ошибка) и
        # своя строка в UI; мешать его в `patterns` значило бы объявлять
        # неудачей синхронизацию паттернов из-за того, что очередь не уехала.
        "candidates": {
            "last_check_at": None,
            "last_status": "never",
            "last_detail": "",
            "last_sync_at": None,
            "last_sent": None,
            "last_remaining": None,
        },
        "revocations": {
            "last_check_at": None,
            "last_status": "never",
            "last_detail": "",
            "etag": None,
            "revoked_ids": [],
        },
        # GAP-523: узкий поток самообновления диспетчера (kind=hub). Своя
        # секция, а не поле `releases` — предмет другой (бинарь диспетчера,
        # не бинарь MCP), свой стейджинг, свой цикл "подготовлено/запущено".
        "hub": {
            "last_check_at": None,
            "last_status": "never",
            "last_detail": "",
            "known_latest": None,
            # "exe" — самообновляемая сборка (frozen), "pip" — pip-установка
            # (канал только показывает команду), None — режим ещё не
            # определялся (до первой проверки).
            "mode": None,
            "staged": None,
            "partial": None,
            "self_update_launched": None,
        },
        # GAP-288: узкий поток скиллов/плагина (kind=skills). Установленная
        # версия — не тут, а в маркере `%APPDATA%\\BPMkit\\skills\\installed.json`
        # (источник правды делят все процессы, включая CLI); здесь — только
        # то, что канал сам скачал/применил в ЭТОМ пробуждении.
        "skills": {
            "last_check_at": None,
            "last_status": "never",
            "last_detail": "",
            "known_latest": None,
            "staged": None,
            "partial": None,
            "installed": None,
        },
    }


def _latest_pattern_version(applied: list) -> Optional[str]:
    """Самая свежая версия среди применённых паттернов (`None`, если её не с чем сравнивать).

    Сравнение — той же функцией, что и у самого цикла паттернов (`patterns.compare_versions`,
    та же семантика "major.minor.patch", что и у версий MCP). Импорт отложен внутрь функции:
    `state.py` не тянет `patterns.py` на уровне модуля намеренно (симметрично тому, как
    `runner.py` откладывает импорт `standkit_hub.config` — держим границы модулей узкими).
    """
    from . import patterns as _patterns

    best: Optional[str] = None
    for rec in applied or []:
        if not isinstance(rec, dict):
            continue
        value = str(rec.get("version") or "").strip()
        if not value:
            continue
        if best is None or _patterns.compare_versions(value, best) > 0:
            best = value
    return best


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

    @property
    def cookbook(self) -> dict:
        return self.data["cookbook"]

    @property
    def hub(self) -> dict:
        return self.data["hub"]

    @property
    def skills(self) -> dict:
        return self.data["skills"]

    @property
    def candidates(self) -> dict:
        return self.data["candidates"]

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
        cb = self.cookbook
        hub = self.hub
        skl = self.skills
        staged = rel.get("staged") or {}
        current = rel.get("current") or {}
        # GAP-528 п.2а: `current_version` пуст (свежая установка/маркер не
        # писался) — подставляем версию РЕАЛЬНО работающего процесса
        # (`running_version`, маркер `mcp_runtime.json`) вместо честного, но
        # бесполезного для человека `None`. Источник помечается ОТДЕЛЬНЫМ
        # полем `current_version_source`: "installed" — обычный случай
        # (маркер канала есть), "running" — приближение, значение подменено.
        current_version = current.get("version") or None
        running_version = rel.get("running_version")
        current_version_source = "installed" if current_version else None
        if not current_version and running_version:
            current_version = running_version
            current_version_source = "running"
        rel_known_latest = rel.get("known_latest")
        rel_staged_version = staged.get("version")
        # GAP-528 п.2в: подготовленная версия ИЛИ известная более новая, чем
        # действующая (маркер/приближение выше) — сравнивать строкой нельзя
        # ("0.9.0" > "0.10.0" как строки), только посегментно.
        releases_update_available = bool(rel_staged_version) or (
            bool(rel_known_latest) and bool(current_version)
            and compare_versions(rel_known_latest, current_version) > 0
        )
        applied = pat.get("applied") or []
        shipped_count = pat.get("shipped_count")
        total_available = (int(shipped_count) + len(applied)) if shipped_count is not None else None
        return {
            "patterns": {
                "applied_count": len(applied),
                # GAP-241: самая свежая версия среди применённых паттернов — сводка для
                # верхнего уровня статуса канала (см. server.py::_patterns_summary).
                # `None`, если сравнивать не с чем (пусто/поле версии не заполнено).
                "latest_version": _latest_pattern_version(applied),
                "last_run_at": pat.get("last_run_at"),
                "status": pat.get("last_status"),
                "detail": pat.get("last_detail"),
                "root": pat.get("root"),
                "seeded": bool(pat.get("seeded")),
                "cursor": {"since": pat.get("since"), "since_id": pat.get("since_id")},
                # GAP-437: размер поставочной базы (см. `patterns.seed_override_root`) и
                # честная ОЦЕНКА фактически доступной базы (поставочная + применённая
                # дельта). `None` у обоих — размер поставочной базы ещё не посчитан, и
                # выдумывать его нельзя (см. докстринг `_default_state`).
                "shipped_count": shipped_count,
                "total_available": total_available,
                # GAP-528: True — последний завершённый проход канала применил или
                # отозвал хотя бы один паттерн (`had_new_last_run`, см. `patterns.sync`).
                # Кнопка «Загрузить новые» в окне «Обновления» видна только при этом
                # флаге; повторный тик без дельты сбрасывает его сам.
                "new_available": bool(pat.get("pending_count") or pat.get("pending_more")),
                "pending_count": int(pat.get("pending_count") or 0),
                "pending_more": bool(pat.get("pending_more")),
                "had_new_last_run": bool(pat.get("had_new_last_run")),
            },
            "releases": {
                "last_check_at": rel.get("last_check_at"),
                "status": rel.get("last_status"),
                "detail": rel.get("last_detail"),
                "known_latest": rel_known_latest,
                "staged_version": staged.get("version"),
                "staged_signed": staged.get("signed"),
                # GAP-528 п.2а: с фолбэком на `running_version`, когда маркер
                # канала пуст — см. вычисление `current_version` выше.
                "current_version": current_version,
                "current_version_source": current_version_source,
                "restart_required": bool(rel.get("restart_required")),
                "rollback_available": bool(rel.get("history")),
                "resume_bytes": (rel.get("partial") or {}).get("bytes"),
                # GAP-447: версия и момент старта РЕАЛЬНО работающего процесса (маркер
                # `mcp_runtime.json`), отдельно от того, что канал считает установленным
                # (`current_version` выше). `None` — маркера не видно.
                "running_version": running_version,
                "running_started_at": rel.get("running_started_at"),
                # GAP-442: состав обновления/известные проблемы, попутный запрос
                # `GET /v1/version/latest` при проверке релиза.
                "release_notes_version": rel.get("release_notes_version"),
                "release_notes": list(rel.get("release_notes") or []),
                "known_issues": list(rel.get("known_issues") or []),
                # GAP-463: `True` — обновление до `release_notes_version` ставится
                # установщиком, канал его не стейджит и не подменяет им бинарь (см.
                # `releases.stage`/`releases.apply_staged`); UI диспетчера не должен
                # предлагать тихую установку в этом состоянии.
                "requires_installer": bool(rel.get("requires_installer")),
                # GAP-528 п.2в: подготовленная ЛИБО известная более новая версия —
                # UI прячет кнопку «Установить», когда ставить нечего.
                "update_available": releases_update_available,
            },
            "cookbook": {
                "last_check_at": cb.get("last_check_at"),
                "status": cb.get("last_status"),
                "detail": cb.get("last_detail"),
                "known_latest": cb.get("known_latest"),
                "installed_version": (cb.get("installed") or {}).get("version"),
                "path": (cb.get("installed") or {}).get("path"),
            },
            "revocations": {
                "last_check_at": rev.get("last_check_at"),
                "status": rev.get("last_status"),
                "detail": rev.get("last_detail"),
                "revoked_count": len(rev.get("revoked_ids") or []),
            },
            "hub": {
                "last_check_at": hub.get("last_check_at"),
                "status": hub.get("last_status"),
                "detail": hub.get("last_detail"),
                "known_latest": hub.get("known_latest"),
                "mode": hub.get("mode"),
                "staged_version": (hub.get("staged") or {}).get("version"),
            },
            "skills": {
                "last_check_at": skl.get("last_check_at"),
                "status": skl.get("last_status"),
                "detail": skl.get("last_detail"),
                "known_latest": skl.get("known_latest"),
                "staged_version": (skl.get("staged") or {}).get("version"),
                "installed_version": (skl.get("installed") or {}).get("version"),
            },
        }
