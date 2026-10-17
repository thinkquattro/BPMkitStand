# -*- coding: utf-8 -*-
"""Автоматическое обновление MCP: политика, исполнитель, автооткат.

Режим обновлений `auto` (`companion.update_mode`, по умолчанию) означает: найденный
релиз скачивается, проверяется и ставится БЕЗ человека. Граница автоматики проходит не
по «кто нажал кнопку», а по трём неизменным правилам:

1. **Подпись и применимость — fail-closed, как у ручной установки.** Модуль не проверяет
   ничего сам и не умеет ничего пропустить: он зовёт те же `releases.stage_installer` /
   `releases.apply_installer` / `releases.stage` / `releases.apply_staged`, что и кнопки
   окна «Обновления». У этих функций нет ни `force`, ни `skip_signature`; здесь их тоже нет.
2. **Установка — только в режиме `auto`.** Решение принимает чистая функция `decide`
   (тестируется без сети); любой другой режим доходит максимум до подготовки (`notify`)
   или не проверяет вовсе (`off`).
3. **Сеть и занятость — не отказ, а «подождём».** Обрыв связи, занятый файл, уже идущий
   установщик, второй процесс автообновления — исход `waiting`, цикл проверки не
   останавливается и повторит попытку сам.
4. **Работающий MCP-сервер — окно простоя.** Живой процесс onefile-сборки после подмены
   своего `bpmkit.exe` ломается: модули, не импортированные до подмены, PyInstaller читает
   уже из ЧУЖОГО файла («Error -3 while decompressing», ложное «лицензия invalid»).
   Поэтому при видимом мьютексе сервера автоматика только готовит файл и отвечает
   `waiting / mcp_running`; ставит — когда сервер не запущен (задача Планировщика, выход
   из хоста). Исключение — самоперенос: поставка объявляет `features: [self_relocate]` в
   `manifest.json`, а работающий процесс — `relocated: true` в `mcp_runtime.json` (он
   исполняется из копии в кэше версий, и `bpmkit.exe` никто не держит); тогда установщик
   получает `/SWAPBUSY` (путь «только exe» — `apply_staged(swap_busy=True)`). Без
   самопереноса `/SWAPBUSY` не передаётся никогда.

Исход любого вызова — один словарь `{ok, decision, reason, version, current, artifact}`:

* `applied` — установщик запущен (или бинарь подменён путём «только exe»);
* `staged` — артефакт скачан и проверен, установка ждёт перезапуска хоста после
  предыдущего автоматического обновления (ставить вторую версию поверх неподтверждённой
  нельзя: `bpmkit.exe.old` — единственная точка отката);
* `waiting` — кольцо раскатки ещё не наступило, раскатка не известна, файл/установщик
  заняты, сеть недоступна;
* `skipped` — режим, пауза, установка «для всех пользователей», плохая версия, нет
  обновления;
* `rolled_back` — новая версия дважды не дошла до рабочего старта и откачена.

**Кольца раскатки.** `early` ставит сразу после публикации, `stable` — не раньше
`stable_after` из поля `rollout` ответа `GET /v1/version/latest` (нет срока — сразу).
Пауза издателя (`rollout.paused`) останавливает автоматику для этой версии. Раскатка, о
которой в ЭТОЙ проверке узнать не удалось (запрос не прошёл, сведения о другой версии),
— не «паузы нет», а «не знаю»: `waiting`.

**Установка «для всех пользователей».** Каталог поставки, недоступный на запись без
повышения прав, означает, что тихий установщик попросит UAC, показать который фоновому
процессу нечем. Режим в этом случае ведёт себя как `notify` (причина `machine_scope`).

**Автооткат.** Сервер BPMkit пишет маркер старта `mcp_startup.json`
(`{version, attempts, ok, first_attempt_at, last_attempt_at, ok_at}`) рядом с
`mcp_runtime.json`. Если версия маркера — последняя поставленная автоматикой, попыток
старта не меньше двух, рабочего старта (`ok`) не было и с последней попытки прошло не
меньше двух минут, `bpmkit.exe` переименовывается в `bpmkit.exe.bad`, а
`bpmkit.exe.old` (или копия, снятая перед запуском установщика) возвращается на место;
версия попадает в `releases.bad_versions` и больше автоматически не ставится. Для
отката не нужен лицензионный контекст: сломанный бинарь — это и есть CLI, у которого
контекст спрашивается, поэтому всё нужное для отката запоминается в момент установки.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import fsutil, mcp_mutex, releases
from .errors import ChannelError, CompanionError
from .state import utc_now_iso

__all__ = [
    "DECISIONS",
    "TRIGGERS",
    "STARTUP_MARKER_FILENAME",
    "decide",
    "rollout_for",
    "install_dir_writable",
    "self_relocate_active",
    "SELF_RELOCATE_FEATURE",
    "startup_marker_path",
    "read_startup_marker",
    "pending_record",
    "reconcile_pending",
    "maybe_rollback",
    "precheck",
    "execute",
    "error_payload",
]

DECISION_APPLIED = "applied"
DECISION_STAGED = "staged"
DECISION_WAITING = "waiting"
DECISION_SKIPPED = "skipped"
DECISION_ROLLED_BACK = "rolled_back"
DECISIONS = (DECISION_APPLIED, DECISION_STAGED, DECISION_WAITING, DECISION_SKIPPED,
             DECISION_ROLLED_BACK)

#: Кто позвал: вход пользователя и повтор по расписанию (задача Планировщика),
#: ручной запуск CLI, цикл релизов работающего диспетчера, выход из хоста (сервер BPMkit,
#: получив конец stdin, запускает `setup auto-update --trigger exit`; ожидание
#: исчезновения мьютекса — забота самой подкоманды, здесь это обычный проход политики).
TRIGGERS = ("logon", "schedule", "manual", "hub", "exit")

#: Признак поставки «сервер умеет самоперенос» — элемент `features` в `manifest.json`.
SELF_RELOCATE_FEATURE = "self_relocate"

#: Маркер старта новой версии — контракт с сервером BPMkit (тот же каталог, что у
#: `mcp_runtime.json`, см. `releases._runtime_marker_path`).
STARTUP_MARKER_FILENAME = "mcp_startup.json"

#: Сколько неудачных стартов подряд считается «версия не работает». Одна неудача — это
#: ещё и «пользователь закрыл хост посреди старта»; две подряд — уже закономерность.
ROLLBACK_MIN_ATTEMPTS = 2

#: Сколько секунд после последней попытки старта ждать, прежде чем решить, что она
#: неудачна: вторая попытка, возможно, ещё идёт (холодный старт onefile-exe под
#: антивирусом — десятки секунд).
ROLLBACK_GRACE_SEC = 120

#: Сколько плохих версий помнить. Больше и не нужно: старые версии автоматика не ставит
#: всё равно (она ставит только новее установленной).
BAD_VERSIONS_KEEP = 20

#: После скольких неудачных автоматических запусков установщика одной и той же версии
#: автоматика перестаёт пробовать её снова (ручная кнопка по-прежнему доступна).
INSTALL_FAILURES_LIMIT = 3

#: Замок «одно автообновление на машину»: диспетчер и CLI, запущенный задачей
#: Планировщика, — разные процессы, и замок процесса (`runner._run_lock`) их не разводит.
LOCK_FILENAME = "autoupdate.lock"

#: Через сколько секунд замок считается брошенным (процесс-владелец умер, не убрав его).
LOCK_STALE_SEC = 900

#: Подкаталог копии бинаря, снятой перед автоматическим запуском установщика. Отдельный
#: подкаталог, а не `backups/`: уборка бэкапов (`releases.prune_backups`) удаляет там
#: всё, на что не ссылается история подмен «только exe».
AUTO_BACKUP_DIRNAME = "auto"

#: Причины, по которым режим ведёт себя как `notify`: подготовка в фоне продолжается.
_NOTIFY_LIKE = frozenset({"mode_notify", "machine_scope"})


# ======================================================================================
# Время
# ======================================================================================
def _aware(now) -> datetime:
    if isinstance(now, datetime):
        return now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if isinstance(now, str):
        parsed = parse_iso(now)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value) -> Optional[datetime]:
    """ISO-8601 (в том числе с `Z`) → aware datetime в UTC; мусор → `None`."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _same_version(a, b) -> bool:
    if not str(a or "").strip() or not str(b or "").strip():
        return False
    return releases.compare_versions(a, b) == 0


# ======================================================================================
# Политика (чистая функция)
# ======================================================================================
def _policy(decision: str, reason: str, *, stage: bool, apply: bool = False) -> dict:
    return {"decision": decision, "reason": reason, "stage": bool(stage), "apply": bool(apply)}


def decide(*, update_mode: str, available: bool, version: Optional[str],
           bad_versions=(), rollout: Optional[dict] = None, ring: str = "stable",
           now=None, install_writable: bool = True, pending_restart: bool = False,
           channel_enabled: bool = True, install_failures: int = 0,
           server_running: bool = False, self_relocate: bool = False) -> dict:
    """Что делать с найденным релизом: `{decision, reason, stage, apply}`.

    Без сети, без диска, без часов (кроме переданных) — весь выбор здесь, исполнитель
    только выполняет. Порядок проверок — от «нечего обсуждать» к «можно ставить»:

    1. канал выключен / режим `off` / нет обновления — `skipped`, ничего не качаем;
    2. версия помечена плохой или издатель поставил её раскатку на паузу — `skipped`, и
       скачивать её тоже незачем;
    3. режим `notify` или установка «для всех пользователей» — `skipped`, но подготовка
       в фоне идёт, как в `notify`;
    4. автоматический запуск установщика этой версии уже не удался `INSTALL_FAILURES_LIMIT`
       раз — `skipped` (не жжём установщик каждые два часа);
    5. раскатка не известна или кольцо `stable` ещё не наступило — `waiting` с подготовкой
       заранее (ко сроку файл уже проверен);
    6. предыдущая автоматическая установка ещё не подтверждена рабочим стартом — `staged`;
    7. MCP-сервер запущен, а самопереноса нет — `waiting / mcp_running` с подготовкой
       (поставим в окно простоя: выход из хоста, задача Планировщика);
    8. иначе — ставить.
    """
    mode = str(update_mode or "").strip().lower()
    if not channel_enabled:
        return _policy(DECISION_SKIPPED, "channel_disabled", stage=False)
    if mode == "off":
        return _policy(DECISION_SKIPPED, "mode_off", stage=False)
    if not available:
        return _policy(DECISION_SKIPPED, "no_update", stage=False)
    if any(_same_version(version, bad) for bad in (bad_versions or ())):
        return _policy(DECISION_SKIPPED, "bad_version", stage=False)
    if isinstance(rollout, dict) and rollout.get("paused") is True:
        return _policy(DECISION_SKIPPED, "paused", stage=False)
    if mode != "auto":
        return _policy(DECISION_SKIPPED, "mode_notify", stage=True)
    if not install_writable:
        return _policy(DECISION_SKIPPED, "machine_scope", stage=True)
    if install_failures >= INSTALL_FAILURES_LIMIT:
        return _policy(DECISION_SKIPPED, "install_failed", stage=False)
    if not isinstance(rollout, dict):
        return _policy(DECISION_WAITING, "rollout_unknown", stage=True)
    if str(ring or "").strip().lower() != "early":
        stable_after = parse_iso(rollout.get("stable_after"))
        if stable_after is not None and _aware(now) < stable_after:
            return _policy(DECISION_WAITING, "ring", stage=True)
    if pending_restart:
        return _policy(DECISION_STAGED, "pending_restart", stage=True)
    if server_running and not self_relocate:
        return _policy(DECISION_WAITING, "mcp_running", stage=True)
    return _policy(DECISION_APPLIED, "policy_ok", stage=True, apply=True)


def rollout_for(rel: dict, version: Optional[str]) -> Optional[dict]:
    """Раскатка ИМЕННО этой версии из состояния или `None`, если её не знаем.

    Сведения в состоянии — про `latest` эндпоинта версий (`rollout_version`). Если
    проверка релиза нашла другую версию (издатель выложил файл, но ещё не обновил
    сведения о версии), это сведения не о ней — «не знаю», а не «паузы нет».
    """
    rollout = rel.get("rollout")
    if not isinstance(rollout, dict):
        return None
    known = str(rel.get("rollout_version") or "").strip()
    if known and version and not _same_version(known, version):
        return None
    return dict(rollout)


def _manifest_features(binary: Path) -> set:
    """`features` из `manifest.json` поставки: рядом с бинарём или в корне установки.

    Список строк или словарь «признак → истина»; всё прочее — пусто."""
    for candidate in (binary.parent / "manifest.json",
                      releases._install_root_for(binary) / "manifest.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        features = data.get("features") if isinstance(data, dict) else None
        if isinstance(features, (list, tuple)):
            return {str(item).strip() for item in features if isinstance(item, str)}
        if isinstance(features, dict):
            return {str(key).strip() for key, value in features.items() if value}
        return set()
    return set()


def self_relocate_active(binary_path) -> bool:
    """Работающий сервер исполняется из копии (самоперенос) — `bpmkit.exe` никто не держит.

    Оба признака обязательны: поставка умеет самоперенос (`manifest.json`:
    `features` содержит `self_relocate`) И работающий процесс это сделал
    (`mcp_runtime.json`: `relocated: true`). Одного «умеет» мало — процесс, запущенный из
    исходников или до обновления, исполняется из самого `bpmkit.exe`.
    """
    text = str(binary_path or "").strip()
    if not text:
        return False
    if SELF_RELOCATE_FEATURE not in _manifest_features(Path(text)):
        return False
    runtime = releases.read_runtime_marker()
    return bool(runtime) and runtime.get("relocated") is True


def install_dir_writable(binary_path) -> bool:
    """Доступен ли каталог бинаря MCP на запись БЕЗ повышения прав.

    Проверяется созданием временного файла, а не `os.access`: на Windows права решают
    ACL каталога, которые `os.access` не читает. Каталога нет — «нет».
    """
    text = str(binary_path or "").strip()
    if not text:
        return False
    directory = Path(text).parent
    if not directory.is_dir():
        return False
    try:
        fd, name = tempfile.mkstemp(prefix=".bpmkit-write-probe-", dir=str(directory))
    except OSError:
        return False
    try:
        os.close(fd)
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass
    return True


# ======================================================================================
# Маркер старта и ожидающая подтверждения установка
# ======================================================================================
def startup_marker_path() -> Path:
    """`mcp_startup.json` — тот же каталог, что у `mcp_runtime.json`."""
    return releases._runtime_marker_path().parent / STARTUP_MARKER_FILENAME


def read_startup_marker() -> Optional[dict]:
    """Маркер старта новой версии, best-effort. `None` — нет, не читается, без версии."""
    try:
        data = json.loads(startup_marker_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = str(data.get("version") or "").strip()
    if not version:
        return None
    try:
        attempts = int(data.get("attempts") or 0)
    except (TypeError, ValueError):
        attempts = 0
    return {
        "version": version,
        "attempts": attempts,
        "ok": data.get("ok") is True,
        "first_attempt_at": data.get("first_attempt_at"),
        "last_attempt_at": data.get("last_attempt_at"),
        "ok_at": data.get("ok_at"),
    }


def pending_record(state) -> Optional[dict]:
    """Последняя автоматическая установка, ещё не подтверждённая рабочим стартом."""
    record = state.releases.get("auto")
    if not isinstance(record, dict) or not record.get("applied_version"):
        return None
    if record.get("confirmed_at"):
        return None
    return record


def _disk_version(binary: Path) -> Optional[str]:
    """Версия поставки на диске по `manifest.json` корня установки (его пишет установщик)."""
    manifest = releases._install_root_for(binary) / "manifest.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return str(data.get("version") or "").strip() or None


def _event(rel: dict, kind: str, *, src, dst, at: str, **extra) -> dict:
    event = {"kind": kind, "from": src or None, "to": dst or None, "at": at}
    event.update({key: value for key, value in extra.items() if value is not None})
    rel["last_auto_event"] = event
    return event


def _runtime_marker_confirms(record: dict, applied: str) -> bool:
    """Маркер работающего процесса (`mcp_runtime.json`) — поставленная версия, запущенная
    позже установки. Обе метки — `%Y-%m-%dT%H:%M:%SZ`, сравнение по времени."""
    runtime = releases.read_runtime_marker()
    if runtime is None or not _same_version(runtime.get("version"), applied):
        return False
    started = parse_iso(runtime.get("started_at"))
    applied_at = parse_iso(record.get("applied_at"))
    return started is not None and applied_at is not None and started > applied_at


def reconcile_pending(state, *, now=None) -> Optional[dict]:
    """Подтвердить или снять ожидающую автоматическую установку. Сети не трогает.

    * маркер старта показывает рабочий старт поставленной версии — установка
      подтверждена, автоматика снова может ставить следующие версии; маркера старта нет
      вовсе (сервер версии без него) — подтверждает маркер работающего процесса
      `mcp_runtime.json` с этой версией, запущенный позже установки;
    * установщик закончил работу, а поставка на диске осталась прежней — запуск не
      удался (журнал установщика — в событии), счётчик неудач версии растёт, ожидание
      снимается: при следующей проверке автоматика попробует снова (до
      `INSTALL_FAILURES_LIMIT` раз).

    Возвращает `{"kind": "confirmed"|"install_failed"}` или `None` (нечего делать).
    """
    record = pending_record(state)
    if record is None:
        return None
    rel = state.releases
    at = _iso(_aware(now))
    applied = str(record.get("applied_version") or "")
    marker = read_startup_marker()
    if marker is not None and marker["ok"] and _same_version(marker["version"], applied):
        record["confirmed_at"] = at
        state.save()
        return {"kind": "confirmed", "version": applied}
    if marker is None and _runtime_marker_confirms(record, applied):
        # Сервер без маркера старта (версия до него) — подтверждение по маркеру
        # работающего процесса: поставленная версия запустилась ПОСЛЕ установки. Иначе
        # ожидание не снялось бы никогда, и автоматика встала бы на первой же версии.
        record["confirmed_at"] = at
        record["confirmed_via"] = "runtime"
        state.save()
        return {"kind": "confirmed", "version": applied}
    if record.get("via") != "installer":
        return None
    launched = (releases.installer_status(state) or {}).get("launched") or {}
    if launched.get("running"):
        return None
    on_disk = _disk_version(Path(str(record.get("binary") or "")))
    if on_disk is None or _same_version(on_disk, applied):
        return None
    failures = dict(rel.get("auto_failures") or {})
    failures[applied] = int(failures.get(applied) or 0) + 1
    rel["auto_failures"] = failures
    rel["auto"] = None
    _event(rel, "install_failed", src=record.get("previous_version"), dst=applied, at=at,
           log=launched.get("log"))
    state.mark("releases", "error",
               f"Автоматическая установка версии {applied} не завершилась: на диске "
               f"осталась версия {on_disk}. Журнал установщика: {launched.get('log') or '—'}")
    state.save()
    return {"kind": "install_failed", "version": applied}


# ======================================================================================
# Автооткат
# ======================================================================================
def _payload(decision: str, reason: str, *, version=None, current=None, artifact=None,
             trigger: str = "", ok: bool = True, detail: str = "") -> dict:
    return {
        "ok": bool(ok),
        "decision": decision,
        "reason": reason,
        "version": version or None,
        "current": current or None,
        "artifact": artifact,
        "trigger": trigger or None,
        "detail": detail or "",
    }


def maybe_rollback(state, *, now=None, trigger: str = "") -> Optional[dict]:
    """Откатить автоматически поставленную версию, если она не дошла до рабочего старта.

    Лицензионный контекст не нужен (см. докстринг модуля). Возвращает исход
    `rolled_back`/`waiting`/`skipped` либо `None`, если откатывать нечего.
    """
    record = pending_record(state)
    if record is None:
        return None
    marker = read_startup_marker()
    applied = str(record.get("applied_version") or "")
    if marker is None or not _same_version(marker["version"], applied):
        return None
    if marker["ok"]:
        return None
    if marker["attempts"] < ROLLBACK_MIN_ATTEMPTS:
        return None
    moment = _aware(now)
    last = parse_iso(marker.get("last_attempt_at"))
    if last is not None and (moment - last).total_seconds() < ROLLBACK_GRACE_SEC:
        return None

    rel = state.releases
    at = _iso(moment)
    previous = str(record.get("previous_version") or "").strip()
    binary = Path(str(record.get("binary") or ""))
    old = binary.with_name(binary.name + releases.OLD_SUFFIX)
    bad = binary.with_name(binary.name + releases.BAD_SUFFIX)
    backup = Path(str(record.get("backup") or "")) if record.get("backup") else None
    source = old if old.is_file() else (backup if backup is not None and backup.is_file()
                                        else None)

    bad_versions = [str(v) for v in (rel.get("bad_versions") or [])
                    if not _same_version(v, applied)]
    bad_versions.append(applied)
    rel["bad_versions"] = bad_versions[-BAD_VERSIONS_KEEP:]

    if source is None or not str(binary):
        rel["auto"] = None
        _event(rel, "rollback_failed", src=applied, dst=previous, at=at,
               detail="нет ни bpmkit.exe.old, ни резервной копии", trigger=trigger or None)
        state.mark("releases", "error",
                   f"Версия {applied} не дошла до рабочего старта, откатить её нечем — "
                   f"переустановите BPMkit. Автоматически эта версия больше не ставится.")
        state.save()
        return _payload(DECISION_SKIPPED, "rollback_unavailable", version=applied,
                        current=previous, trigger=trigger, ok=False)

    try:
        if bad.exists():
            bad.unlink()
    except OSError:
        pass
    try:
        if binary.exists():
            os.replace(binary, bad)
    except OSError as exc:
        state.save()
        return _payload(DECISION_WAITING, "busy", version=applied, current=previous,
                        trigger=trigger, ok=False,
                        detail=f"не удалось отложить сломанный бинарь: {exc}")
    try:
        if source == old:
            os.replace(old, binary)
        else:
            tmp = binary.with_name(binary.name + ".rollback")
            fsutil.backup_copy(source, binary.parent, tmp.name)
            fsutil.replace_with_retry(tmp, binary)
    except OSError as exc:
        try:
            os.replace(bad, binary)
        except OSError:
            pass
        state.save()
        return _payload(DECISION_WAITING, "busy", version=applied, current=previous,
                        trigger=trigger, ok=False,
                        detail=f"не удалось вернуть прежний бинарь: {exc}")

    install_sync = releases._sync_install_companions(binary, previous)
    rel["current"] = {
        "version": previous or None,
        "path": str(binary),
        "applied_at": at,
        "rolled_back_from": applied,
        "install_sync": install_sync,
    }
    rel["restart_required"] = True
    rel["auto"] = None
    _event(rel, "rolled_back", src=applied, dst=previous, at=at, trigger=trigger or None)
    state.mark("releases", "ok",
               f"Версия {applied} не дошла до рабочего старта и откачена до "
               f"{previous or 'предыдущей'}. Перезапустите приложение, чтобы MCP-сервер "
               f"запустился из вернувшегося файла. Эта версия больше не ставится "
               f"автоматически.")
    state.save()
    return _payload(DECISION_ROLLED_BACK, "startup_failed", version=applied,
                    current=previous, trigger=trigger,
                    artifact={"binary": str(binary), "bad": str(bad),
                              "source": str(source), "install_sync": install_sync})


# ======================================================================================
# Исполнитель
# ======================================================================================
def _mode_of(settings) -> str:
    from standkit_hub.config import effective_update_mode

    return effective_update_mode(settings)


def precheck(settings, *, trigger: str = "") -> Optional[dict]:
    """Исход БЕЗ сети, если автоматике делать нечего по настройкам, иначе `None`."""
    if not bool(getattr(settings, "enabled", False)):
        return _payload(DECISION_SKIPPED, "channel_disabled", trigger=trigger)
    if _mode_of(settings) == "off":
        return _payload(DECISION_SKIPPED, "mode_off", trigger=trigger)
    return None


#: kind отказа канала → (решение, причина). Всё retriable, чего здесь нет, — `waiting`.
_ERROR_DECISIONS = {
    "elevation_required": (DECISION_SKIPPED, "machine_scope"),
    "requires_installer": (DECISION_SKIPPED, "requires_installer"),
    "mcp_running": (DECISION_WAITING, "busy"),
    "local_io": (DECISION_WAITING, "busy"),
}


def error_payload(exc: CompanionError, *, version=None, current=None, artifact=None,
                  trigger: str = "") -> dict:
    """Типизированный отказ канала → исход автообновления (наружу не бросаем)."""
    kind = str(getattr(exc, "kind", "") or "unknown")
    decision, reason = _ERROR_DECISIONS.get(kind, (
        DECISION_WAITING if getattr(exc, "retriable", False) else DECISION_SKIPPED, kind))
    detail = getattr(exc, "detail", "") or str(exc)
    return _payload(decision, reason, version=version, current=current, artifact=artifact,
                    trigger=trigger, ok=False, detail=detail)


def _as_companion_error(exc: BaseException) -> CompanionError:
    """Незнакомое исключение → типизированный retriable-отказ (`kind="unknown"`)."""
    if isinstance(exc, CompanionError):
        return exc
    return ChannelError("Непредвиденный сбой автоматического обновления", kind="unknown",
                        detail=f"{type(exc).__name__}: {exc}")


def _artifact(kind: str, record: dict, *, reused: bool) -> dict:
    return {
        "kind": kind,
        "version": record.get("version") or None,
        "filename": record.get("filename") or None,
        "path": record.get("path") or None,
        "sha256": record.get("sha256") or None,
        "reused": bool(reused),
    }


def _stage_artifact(client, state, ctx, check: dict, version: str) -> dict:
    """Подготовить артефакт целевой версии: подписанный установщик, иначе — бинарь.

    Уже подготовленный и проверенный файл той же версии не качается второй раз.
    """
    target = version if releases.is_numeric_version(version) else "latest"
    staged = releases.installer_status(state).get("staged")
    if staged and _same_version(staged.get("version"), version):
        return _artifact("installer", staged, reused=True)
    try:
        record = releases.stage_installer(client, state, ctx, target)
        return _artifact("installer", record, reused=False)
    except ChannelError as exc:
        if exc.kind != "installer_not_available":
            raise
    if check.get("requires_installer"):
        raise ChannelError(
            f"Версия {version or 'обновления'} ставится только установщиком, а установщик "
            f"для неё издателем не опубликован — автоматическая установка невозможна",
            kind="requires_installer")
    staged_exe = releases.staged_info(state)
    if staged_exe and _same_version(staged_exe.get("version"), version):
        return _artifact("exe", staged_exe, reused=True)
    exe_target = target if target != "latest" else (check.get("target") or "latest")
    record = releases.stage(client, state, ctx, exe_target)
    return _artifact("exe", record, reused=False)


def _backup_before_install(ctx, binary: Path, current) -> Optional[str]:
    """Копия работающего бинаря перед запуском установщика — запасная точка отката.

    `bpmkit.exe.old` установщик оставляет только когда файл был занят; копия нужна на
    случай, когда хост был закрыт и установщик перезаписал файл на месте. Лучшее
    старание: не удалась копия — установка всё равно идёт (откат тогда опирается на
    `.old`, а при его отсутствии честно сообщает, что откатить нечем).
    """
    if not binary.is_file():
        return None
    directory = releases.companion_workdir(ctx) / releases.BACKUP_DIRNAME / AUTO_BACKUP_DIRNAME
    name = (f"{binary.stem}-{releases._safe_component(str(current or ''))}"
            f"-{releases._safe_component(utc_now_iso())}{binary.suffix}")
    try:
        if directory.is_dir():
            for entry in directory.iterdir():
                if entry.is_file():
                    try:
                        entry.unlink()
                    except OSError:
                        pass
        copied = fsutil.backup_copy(binary, directory, name)
    except OSError:
        return None
    return str(copied) if copied else None


class _MachineLock:
    """Замок «одно автообновление на машину» — файл, созданный с `O_EXCL`."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def __enter__(self) -> "_MachineLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age < LOCK_STALE_SEC:
                    return self
                try:
                    self.path.unlink()
                except OSError:
                    return self
                continue
            except OSError:
                return self
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"pid": os.getpid(), "at": utc_now_iso()}))
            self.acquired = True
            return self
        return self

    def __exit__(self, *exc_info) -> None:
        if self.acquired:
            try:
                self.path.unlink()
            except OSError:
                pass


def _remember(state, payload: dict, at: str) -> dict:
    state.releases["last_auto_decision"] = {
        "decision": payload.get("decision"),
        "reason": payload.get("reason"),
        "version": payload.get("version"),
        "trigger": payload.get("trigger"),
        "at": at,
    }
    try:
        state.save()
    except OSError:
        pass
    return payload


def execute(state, session, settings, *, trigger: str = "manual", now=None,
            check: Optional[dict] = None) -> dict:
    """Проверка → политика → подготовка → установка. Исход — словарь, не исключение.

    Исключение поднимается только из самой ПРОВЕРКИ релиза (`releases.check`): это та
    же ошибка, что у цикла `releases` в режиме `notify`, и разбирает её тот же код
    (`runner._execute`: нет лицензии — цикл встаёт, сеть — повтор). Подготовка и
    установка ошибок наружу не бросают: проверка уже состоялась, а «не скачалось» или
    «файл занят» — повод подождать, а не остановить канал.
    """
    moment = _aware(now)
    at = _iso(moment)
    client, ctx = session.client, session.ctx
    if check is None:
        check = releases.check(client, state, ctx)
    rel = state.releases
    version = str(check.get("latest") or "").strip()
    current = check.get("current")
    binary = Path(str(getattr(ctx, "binary_path", "") or ""))
    failures = rel.get("auto_failures") or {}
    server_running = mcp_mutex.server_mutex_exists()
    relocated = server_running and self_relocate_active(binary)
    policy = decide(
        update_mode=_mode_of(settings),
        available=bool(check.get("available")),
        version=version,
        bad_versions=rel.get("bad_versions") or (),
        rollout=rollout_for(rel, version),
        ring=str(getattr(settings, "update_ring", "") or "stable"),
        now=moment,
        install_writable=install_dir_writable(binary),
        pending_restart=pending_record(state) is not None,
        channel_enabled=bool(getattr(settings, "enabled", True)),
        install_failures=int(failures.get(version) or 0) if isinstance(failures, dict) else 0,
        server_running=server_running,
        self_relocate=relocated,
    )
    payload = _payload(policy["decision"], policy["reason"], version=version,
                       current=current, trigger=trigger)
    if not policy["stage"]:
        return _remember(state, payload, at)

    lock_path = releases.companion_workdir(ctx) / LOCK_FILENAME
    with _MachineLock(lock_path) as lock:
        if not lock.acquired:
            return _remember(state, _payload(DECISION_WAITING, "busy_other_process",
                                             version=version, current=current,
                                             trigger=trigger), at)
        try:
            artifact = _stage_artifact(client, state, ctx, check, version)
        except Exception as exc:  # noqa: BLE001 - исход, а не исключение (см. докстринг)
            failed = error_payload(_as_companion_error(exc), version=version,
                                   current=current, trigger=trigger)
            if policy["reason"] in _NOTIFY_LIKE and failed["decision"] == DECISION_WAITING:
                # notify-подобный режим: установки и так не будет, причина — режим, а
                # неудавшаяся подготовка — подробность.
                failed["decision"], failed["reason"] = DECISION_SKIPPED, policy["reason"]
            return _remember(state, failed, at)
        payload["artifact"] = artifact
        if not policy["apply"]:
            return _remember(state, payload, at)

        launched = releases.installer_status(state).get("launched") or {}
        if launched.get("running"):
            return _remember(state, _payload(DECISION_WAITING, "installer_running",
                                             version=version, current=current,
                                             artifact=artifact, trigger=trigger), at)
        return _remember(state, _apply(state, ctx, artifact, binary=binary,
                                       version=version, current=current,
                                       trigger=trigger, at=at, payload=payload,
                                       swap_busy=relocated), at)


def _apply(state, ctx, artifact: dict, *, binary: Path, version: str, current,
           trigger: str, at: str, payload: dict, swap_busy: bool = False) -> dict:
    """Установка подготовленного артефакта (под замком машины).

    `swap_busy` — только при самопереносе работающего сервера (`self_relocate_active`):
    тогда установщику передаётся `/SWAPBUSY`, пути «только exe» — переименование занятого
    файла. Сервер не запущен — подменять нечего, ключ не нужен.

    Запись «поставлено автоматически» делается ДО запуска установщика: установщик
    первым делом останавливает диспетчер, и запись, отложенная «на после запуска»,
    могла бы не успеть на диск — а без неё некому ни подтвердить новую версию, ни
    откатить её. Не удалось запустить — запись возвращается к прежней.
    """
    rel = state.releases
    before_auto = rel.get("auto")
    before_event = rel.get("last_auto_event")
    via = "installer" if artifact["kind"] == "installer" else "exe"
    backup = _backup_before_install(ctx, binary, current) if via == "installer" else None
    rel["auto"] = {
        "applied_version": version,
        "previous_version": str(current or "") or None,
        "binary": str(binary),
        "backup": backup,
        "via": via,
        "applied_at": at,
        "trigger": trigger or None,
        "confirmed_at": None,
    }
    _event(rel, "applied", src=current, dst=version, at=at, via=via, trigger=trigger or None,
           swap_busy=bool(swap_busy))
    try:
        state.save()
    except OSError:
        pass
    try:
        if via == "installer":
            result = releases.apply_installer(state, ctx, target=artifact["version"],
                                              swap_busy=bool(swap_busy))
        else:
            result = releases.apply_staged(state, ctx, swap_busy=bool(swap_busy))
            rel["auto"]["backup"] = result.get("backup")
    except Exception as exc:  # noqa: BLE001 - исход, а не исключение
        rel["auto"] = before_auto
        rel["last_auto_event"] = before_event
        try:
            state.save()
        except OSError:
            pass
        return error_payload(_as_companion_error(exc), version=version, current=current,
                             artifact=artifact, trigger=trigger)
    try:
        state.save()
    except OSError:
        pass
    out = dict(payload)
    out["artifact"] = dict(artifact, result={
        key: value for key, value in (result or {}).items()
        if key in ("pid", "log", "launched", "applied", "old_path", "restart_required")})
    return out
