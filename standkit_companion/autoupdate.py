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
   из хоста). Исключение — самоперенос: поставка объявляет `self_relocate` в
   `manifest.json` (`_meta["pro.bpmkit"]["features"]`), а работающий процесс —
   `relocated: true` в `mcp_runtime.json` (он исполняется из копии в кэше версий, и
   `bpmkit.exe` никто не держит); тогда установщик
   получает `/SWAPBUSY` (путь «только exe» — `apply_staged(swap_busy=True)`). Без
   самопереноса `/SWAPBUSY` не передаётся никогда.

Исход любого вызова — один словарь `{ok, decision, reason, version, current, artifact}`:

* `applied` — установщик запущен (или бинарь подменён путём «только exe»);
* `staged` — артефакт скачан и проверен, установка ждёт перезапуска хоста после
  предыдущего автоматического обновления (ставить вторую версию поверх неподтверждённой
  нельзя: копия прежнего бинаря — единственная точка отката);
* `waiting` — кольцо раскатки ещё не наступило, раскатка не известна, файл/установщик
  заняты, сервер запущен без самопереноса, сеть недоступна;
* `skipped` — режим, пауза, установка «для всех пользователей», настройки канала не
  прочитаны (`settings_unreadable`), номер версии не известен (`version_unknown`),
  плохая версия, нет обновления;
* `rolled_back` — новая версия дважды не дошла до рабочего старта и откачена.

**Два процесса.** Файл состояния пишут диспетчер (цикл релизов) и CLI BPMkit из задачи
Планировщика. Под замком машины (`autoupdate.lock`, владелец — pid) общие ключи
перечитываются с диска, политика и установленная версия считаются заново, а запись
сливается с диском (`state.SHARED_KEYS`): решение «ставить» принимается по тому, что
уже сделал другой процесс, и не затирает его записи. Установленная версия — из
состояния или из `manifest.json` поставки, если там новее (установщик пишет туда, а не в
состояние); подтверждение установки установщиком записывает её и в состояние.

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
`mcp_runtime.json`. Установку подтверждает только явный `ok` маркера этой версии, позже
автоустановки (`applied_at`); живость процесса — нет (он мог повиснуть до рабочего
старта). Если версия маркера — последняя поставленная автоматикой, попыток старта сверх
счётчика маркера в момент установки (маркер этой версии мог остаться от смоука того же
exe) не меньше двух, последняя — после установки, рабочего старта (`ok`) не было и с
последней попытки прошло не меньше двух минут (нет метки — отсчёт от последней записи
маркера), `bpmkit.exe` переименовывается в `bpmkit.exe.bad`, а на место возвращается
копия, снятая ЭТОЙ установкой; `bpmkit.exe.old` — только если копии нет, а установка шла с
подменой занятого файла (иначе `.old` остался от прежней подмены, и откат на него вернул
бы не ту версию). Пока жив процесс поставленной версии (маркер `mcp_runtime.json` этой
версии, запущенный после установки, и живой `pid`), откат откладывается: файл держит он,
а вердикт выносит его завершение. Откат — под замком машины. Версия попадает в
`releases.bad_versions` и больше автоматически не ставится (снять — `clear_bad_versions`:
кнопка окна и команда CLI). Для отката не нужен лицензионный
контекст: сломанный бинарь — это и есть CLI, у которого контекст спрашивается, поэтому
всё нужное для отката запоминается в момент установки.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from standkit.platform import is_alive

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
    "clear_bad_versions",
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

#: Сколько плохих версий помнить (общая константа канала — её же использует ручной откат).
BAD_VERSIONS_KEEP = releases.BAD_VERSIONS_KEEP

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
_NOTIFY_LIKE = frozenset({"mode_notify", "machine_scope", "settings_unreadable"})

#: Сколько секунд после автоматического запуска установщика ждать, прежде чем
#: признать установку «не подтверждаемой», если версию поставки на диске прочитать
#: нельзя (`manifest.json` нет или он битый): ожидание подтверждения не вечное.
PENDING_UNVERIFIED_SEC = 6 * 3600


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
           server_running: bool = False, self_relocate: bool = False,
           settings_readable: bool = True) -> dict:
    """Что делать с найденным релизом: `{decision, reason, stage, apply}`.

    Без сети, без диска, без часов (кроме переданных) — весь выбор здесь, исполнитель
    только выполняет. Порядок проверок — от «нечего обсуждать» к «можно ставить»:

    1. канал выключен / режим `off` / нет обновления — `skipped`, ничего не качаем;
    1а. номер новой версии не известен (не числовой) — `skipped / version_unknown`: без
       номера не проверить ни плохую версию, ни раскатку, ни что поставилось;
    2. версия помечена плохой или издатель поставил её раскатку на паузу — `skipped`, и
       скачивать её тоже незачем;
    3. настройки канала не прочитаны (`settings_unreadable`), режим `notify` или
       установка «для всех пользователей» — `skipped`, но подготовка в фоне идёт, как в
       `notify`;
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
    if not releases.is_numeric_version(version):
        return _policy(DECISION_SKIPPED, "version_unknown", stage=False)
    if any(_same_version(version, bad) for bad in (bad_versions or ())):
        return _policy(DECISION_SKIPPED, "bad_version", stage=False)
    if isinstance(rollout, dict) and rollout.get("paused") is True:
        return _policy(DECISION_SKIPPED, "paused", stage=False)
    if not settings_readable:
        return _policy(DECISION_SKIPPED, "settings_unreadable", stage=True)
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
    if not isinstance(rollout, dict) or not str(version or "").strip():
        return None
    known = str(rel.get("rollout_version") or "").strip()
    if known and not _same_version(known, version):
        return None
    return dict(rollout)


#: Пространство имён BPMkit в `_meta` манифеста поставки: верхнеуровневые ключи схема
#: манифеста MCPB не допускает (упаковка падает), поэтому признаки поставки — здесь.
MANIFEST_META_NAMESPACE = "pro.bpmkit"


def _feature_list(value) -> Optional[set]:
    """Список строк → множество признаков; всё, что не список, — `None` («признаков нет»)."""
    if not isinstance(value, list):
        return None
    return {item.strip() for item in value if isinstance(item, str)}


def _manifest_features(binary: Path) -> set:
    """Признаки поставки из `manifest.json`: рядом с бинарём или в корне установки.

    Основной путь — `_meta["pro.bpmkit"]["features"]` (список строк); запасной —
    верхнеуровневый `features` (прежние сборки). Не список, нет ключа, не читается —
    признаков нет: самоперенос — разрешение подменять работающий файл, и «не понял формат»
    обязано означать «не разрешено».
    """
    for candidate in (binary.parent / "manifest.json",
                      releases._install_root_for(binary) / "manifest.json"):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            return set()
        meta = data.get("_meta")
        ours = meta.get(MANIFEST_META_NAMESPACE) if isinstance(meta, dict) else None
        found = set()
        primary = _feature_list(ours.get("features")) if isinstance(ours, dict) else None
        if primary:
            found |= primary
        fallback = _feature_list(data.get("features"))
        if fallback:
            found |= fallback
        return found
    return set()


def _pid_alive(pid) -> bool:
    """Жив ли процесс `pid` (мусор и неположительные — «нет»)."""
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    try:
        return bool(is_alive(number))
    except Exception:  # noqa: BLE001 - «не знаю» = «не доказано»
        return False


def _same_file(a, b) -> bool:
    """Один и тот же путь (без учёта регистра на Windows, с разрешением `..`)."""
    def norm(value) -> str:
        text = os.path.normcase(os.path.abspath(str(value)))
        return text
    try:
        return norm(a) == norm(b)
    except (TypeError, ValueError, OSError):
        return False


def _is_within(path, root) -> bool:
    """`path` лежит внутри каталога `root` (без учёта регистра на Windows)."""
    try:
        target = os.path.normcase(os.path.abspath(str(path)))
        base = os.path.normcase(os.path.abspath(str(root)))
        return os.path.commonpath([target, base]) == base
    except (TypeError, ValueError, OSError):
        return False


def self_relocate_active(binary_path) -> bool:
    """Работающий сервер исполняется из копии (самоперенос) — `bpmkit.exe` никто не держит.

    Все признаки обязательны:

    * поставка умеет самоперенос (`manifest.json`: `_meta["pro.bpmkit"]["features"]`,
      запасной путь — верхний `features`, содержит `self_relocate`);
    * работающий процесс это сделал: `mcp_runtime.json` — `relocated: true`, а
      `relocated_binary` (путь копии; `binary` у копии — установленный exe) существует и
      лежит НЕ в каталоге установки;
    * процесс маркера жив (`pid`) — маркер умершего процесса ничего не говорит о том,
      кто держит файл сейчас;
    * не виден мьютекс `BPMkitMcpServerPinned` — его держит сервер, работающий НЕ из
      копии (самоперенос выключен, откатился, проиграл гонку): второй хост с таким
      сервером держит сам `bpmkit.exe` (строгий детект: «нет» — только
      `ERROR_FILE_NOT_FOUND`).

    Одного «умеет» мало — процесс, запущенный из исходников или до обновления, исполняется
    из самого `bpmkit.exe`.
    """
    text = str(binary_path or "").strip()
    if not text:
        return False
    if SELF_RELOCATE_FEATURE not in _manifest_features(Path(text)):
        return False
    runtime = releases.read_runtime_marker()
    if not runtime or runtime.get("relocated") is not True:
        return False
    copy = str(runtime.get("relocated_binary") or "").strip()
    if not copy or _same_file(copy, text):
        return False
    if _is_within(copy, releases._install_root_for(Path(text))):
        return False
    try:
        if not Path(copy).is_file():
            return False
    except OSError:
        return False
    if not _pid_alive(runtime.get("pid")):
        return False
    return not mcp_mutex.pinned_server_running()


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
    """Маркер старта новой версии, best-effort. `None` — нет, не читается, без версии.

    `mtime` — время последней записи файла (UTC ISO): запасная метка последней попытки
    старта, если `last_attempt_at` в маркере нет или она не разбирается.
    """
    path = startup_marker_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        mtime = _iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
    except (OSError, ValueError, OverflowError):
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
        "mtime": mtime,
    }


def _after_install(record: dict, moment) -> bool:
    """Метка маркера старта позже автоустановки (`applied_at` записи `releases.auto`).

    Маркер ЭТОЙ версии мог появиться раньше установки (смоук или VM на машине кольца
    `early` запускали тот же exe до неё) — такая метка не доказывает ни рабочий старт, ни
    провал установленной версии. Нет любой из меток — «не доказано».
    """
    applied_at = parse_iso(record.get("applied_at"))
    stamp = parse_iso(moment) if isinstance(moment, str) else None
    return applied_at is not None and stamp is not None and stamp > applied_at


def pending_record(state) -> Optional[dict]:
    """Последняя автоматическая установка, ещё не подтверждённая рабочим стартом."""
    record = state.releases.get("auto")
    if not isinstance(record, dict) or not record.get("applied_version"):
        return None
    if record.get("confirmed_at"):
        return None
    return record


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
    return _after_install(record, runtime.get("started_at"))


def _live_process_of(record: dict, applied: str) -> bool:
    """Процесс поставленной версии жив прямо сейчас: маркер `mcp_runtime.json` этой
    версии, запущенный позже установки, и его `pid` жив.

    Это НЕ подтверждение установки: процесс мог повиснуть, не дойдя до рабочего старта,
    а мьютекс держит любой сервер. Живость только ОТКЛАДЫВАЕТ автооткат (живой процесс
    держит файл, и вердикт выносит его завершение); подтверждает установку только явный
    `ok` маркера старта этой версии.
    """
    runtime = releases.read_runtime_marker()
    if runtime is None or not _same_version(runtime.get("version"), applied):
        return False
    if not _after_install(record, runtime.get("started_at")):
        return False
    return _pid_alive(runtime.get("pid"))


#: Строки журнала установщика (`/LOG`), которыми он сообщает об отказе ДО копирования
#: файлов из-за запущенного MCP-сервера (без `/SWAPBUSY`). Контракт с установщиком
#: BPMkit: тихий режим не выбирает код выхода, различитель причины — журнал.
INSTALLER_LOG_CANCELLED = "установка отменена ДО копирования файлов"
INSTALLER_LOG_SERVER = "MCP-сервер (мьютекс"

#: Сколько последних байт журнала установщика читать (причина отказа — в конце).
_INSTALLER_LOG_TAIL = 256 * 1024


def _installer_refused_by_server(log_path) -> bool:
    """Установщик отказал, потому что работал MCP-сервер (по его журналу `/LOG`)."""
    text = str(log_path or "").strip()
    if not text:
        return False
    try:
        with open(text, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _INSTALLER_LOG_TAIL))
            raw = fh.read()
    except OSError:
        return False
    for encoding in ("utf-8", "utf-16", "cp1251"):
        content = raw.decode(encoding, errors="ignore")
        if INSTALLER_LOG_CANCELLED in content and INSTALLER_LOG_SERVER in content:
            return True
    return False


def _confirm(state, record: dict, applied: str, at: str, *, via: str = "") -> dict:
    """Установка подтверждена рабочим стартом: запись и установленная версия.

    `releases.current` пишет здесь путь установщика — он сам состояния канала не
    трогает, и без этой записи проверка релиза считала бы установленной прежнюю версию
    (и ставила бы новую снова). Путь «только exe» `current` уже записал при подмене.
    """
    record["confirmed_at"] = at
    if via:
        record["confirmed_via"] = via
    rel = state.releases
    current = rel.get("current") if isinstance(rel.get("current"), dict) else {}
    if not _same_version(current.get("version"), applied):
        rel["current"] = {
            "version": applied,
            "path": record.get("binary") or None,
            "applied_at": record.get("applied_at"),
            "via": record.get("via") or None,
            "confirmed_at": at,
        }
    state.save()
    return {"kind": "confirmed", "version": applied}


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

    * живость процесса поставленной версии установку НЕ подтверждает (он мог
      повиснуть до рабочего старта) — только явный `ok` маркера старта этой версии;
    * версия поставки на диске не читается дольше `PENDING_UNVERIFIED_SEC` —
      `install_unverified` (считается неудачей версии, ожидание не вечное);
    * поставка осталась прежней, а установщик отказал из-за работающего сервера (его
      журнал, или сервер был виден уже при запуске без `/SWAPBUSY`) — `install_deferred`:
      неудачей версии не считается, поставим в окно простоя.

    Подтверждение установщиком записывает `releases.current` (сам установщик состояние
    канала не трогает). Возвращает `{"kind": "confirmed"|"install_failed"|
    "install_unverified"|"install_deferred"}` или `None` (нечего делать).
    """
    record = pending_record(state)
    if record is None:
        return None
    rel = state.releases
    moment = _aware(now)
    at = _iso(moment)
    applied = str(record.get("applied_version") or "")
    marker = read_startup_marker()
    if (marker is not None and marker["ok"] and _same_version(marker["version"], applied)
            and _after_install(record, marker.get("ok_at"))):
        return _confirm(state, record, applied, at)
    if marker is None and _runtime_marker_confirms(record, applied):
        # Сервер без маркера старта (версия до него) — подтверждение по маркеру
        # работающего процесса: поставленная версия запустилась ПОСЛЕ установки. Иначе
        # ожидание не снялось бы никогда, и автоматика встала бы на первой же версии.
        return _confirm(state, record, applied, at, via="runtime")
    if record.get("via") != "installer":
        return None
    launched = (releases.installer_status(state) or {}).get("launched") or {}
    if launched.get("running"):
        return None
    on_disk = releases.manifest_version(record.get("binary") or "")
    if on_disk is None:
        # Версию поставки прочитать нельзя: ни подтвердить, ни опровергнуть. Ждать
        # вечно нельзя (автоматика встала бы навсегда) — по истечении срока ожидание
        # снимается как «не подтверждено» и считается неудачей этой версии
        # (повторы ограничены `INSTALL_FAILURES_LIMIT`), а человек видит причину.
        applied_at = parse_iso(record.get("applied_at"))
        if applied_at is not None and (
                moment - applied_at).total_seconds() < PENDING_UNVERIFIED_SEC:
            return None
        return _install_not_confirmed(
            state, record, applied, at, kind="install_unverified", log=launched.get("log"),
            text=(f"Автоматическую установку версии {applied} не удалось подтвердить: "
                  f"manifest.json поставки не читается, рабочего старта новой версии не "
                  f"было. Проверьте установку BPMkit."))
    if _same_version(on_disk, applied):
        return None
    if _installer_refused_by_server(launched.get("log")) or (
            record.get("server_running") is True and not record.get("swap_busy")):
        # Установщик отказал из-за работающего MCP-сервера (его журнал говорит об этом
        # прямо; или сервер был виден уже при запуске, а `/SWAPBUSY` не передавался) —
        # это не неудача версии: ожидание снимается БЕЗ счёта, поставим в следующее окно
        # простоя. Мьютекс В МОМЕНТ СВЕРКИ ничего не доказывает: сервер мог подняться уже
        # после того, как установщик отработал (или не смог по другой причине).
        rel["auto"] = None
        _event(rel, "install_deferred", src=record.get("previous_version"), dst=applied,
               at=at, log=launched.get("log"))
        state.mark("releases", "ok",
                   f"Установка версии {applied} отложена: MCP-сервер был запущен и держал "
                   f"файл. Повторим, когда он будет остановлен.")
        state.save()
        return {"kind": "install_deferred", "version": applied}
    return _install_not_confirmed(
        state, record, applied, at, kind="install_failed", log=launched.get("log"),
        text=(f"Автоматическая установка версии {applied} не завершилась: на диске "
              f"осталась версия {on_disk}. Журнал установщика: "
              f"{launched.get('log') or '—'}"))


def _install_not_confirmed(state, record: dict, applied: str, at: str, *, kind: str,
                           log, text: str) -> dict:
    """Снять ожидание как неудачу версии: счётчик, событие, причина в статусе."""
    rel = state.releases
    failures = dict(rel.get("auto_failures") or {})
    failures[applied] = int(failures.get(applied) or 0) + 1
    rel["auto_failures"] = failures
    rel["auto"] = None
    _event(rel, kind, src=record.get("previous_version"), dst=applied, at=at, log=log)
    state.mark("releases", "error", text)
    state.save()
    return {"kind": kind, "version": applied}


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


def _rollback_source(record: dict, binary: Path, old: Path) -> Optional[Path]:
    """Чем откатывать: копия ЭТОЙ установки, иначе `.old`, только если он от неё.

    Копия (`record["backup"]`) снята прямо перед установкой — это ровно прежняя версия.
    `bpmkit.exe.old` появляется только при подмене занятого файла (`/SWAPBUSY` или
    `apply_staged(swap_busy=True)`); без неё он остался от какой-то прежней подмены, и
    откат на него вернул бы не ту версию, а `current` соврал бы. Поэтому `.old` берётся,
    только если копии нет, а запись говорит, что эта установка шла с `swap_busy`.
    """
    backup = Path(str(record.get("backup") or "")) if record.get("backup") else None
    if backup is not None and backup.is_file():
        return backup
    if record.get("swap_busy") is True and old.is_file():
        return old
    return None


def _lock_path(state, *, ctx=None, record: Optional[dict] = None) -> Path:
    """Файл замка машины: рабочий каталог канала (`releases.companion_workdir`).

    Без контекста (автооткат, снятие запрета) — каталог, записанный установкой, иначе
    `companion` рядом с файлом состояния (там же, где рабочий каталог по умолчанию).
    """
    if ctx is not None:
        return releases.companion_workdir(ctx) / LOCK_FILENAME
    workdir = str((record or {}).get("workdir") or "").strip()
    if workdir:
        return Path(workdir) / LOCK_FILENAME
    return Path(state.path).parent / releases.COMPANION_DIRNAME / LOCK_FILENAME


def _int(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _rollback_due(state, moment) -> Optional[tuple]:
    """`(record, applied)`, если поставленную версию пора откатывать, иначе `None`.

    * маркер старта — этой версии и без `ok`;
    * попыток старта ПОСЛЕ установки не меньше `ROLLBACK_MIN_ATTEMPTS`: счётчик маркера
      минус его значение в момент установки (`startup_attempts_at_install`; маркер этой
      версии мог остаться от смоука до неё); сервер начал счёт заново — сам счётчик;
    * последняя попытка (нет метки — время записи маркера) позже установки и с неё прошло
      не меньше `ROLLBACK_GRACE_SEC`;
    * процесс этой версии не жив (`_live_process_of`): живой процесс держит файл, откат
      откладывается до его завершения.
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
    baseline = _int(record.get("startup_attempts_at_install"))
    attempts = _int(marker["attempts"])
    new_attempts = attempts - baseline if attempts >= baseline else attempts
    if new_attempts < ROLLBACK_MIN_ATTEMPTS:
        return None
    last = parse_iso(marker.get("last_attempt_at")) or parse_iso(marker.get("mtime"))
    applied_at = parse_iso(record.get("applied_at"))
    if last is None or applied_at is None or last <= applied_at:
        return None
    if (moment - last).total_seconds() < ROLLBACK_GRACE_SEC:
        return None
    if _live_process_of(record, applied):
        return None
    return record, applied


def maybe_rollback(state, *, now=None, trigger: str = "") -> Optional[dict]:
    """Откатить автоматически поставленную версию, если она не дошла до рабочего старта.

    Лицензионный контекст не нужен (см. докстринг модуля). Откат — под замком машины
    (тот же, что у установки): условия проверяются заново по свежему состоянию с диска.
    Возвращает исход `rolled_back`/`waiting`/`skipped` либо `None`, если откатывать
    нечего.
    """
    moment = _aware(now)
    if _rollback_due(state, moment) is None:
        return None
    record = pending_record(state) or {}
    with _MachineLock(_lock_path(state, record=record)) as lock:
        if not lock.acquired:
            return _payload(DECISION_WAITING, "busy_other_process",
                            version=record.get("applied_version"),
                            current=record.get("previous_version"), trigger=trigger)
        _refresh_shared(state)
        due = _rollback_due(state, moment)
        if due is None:
            return None
        record, applied = due
        return _rollback(state, record, applied, moment, trigger=trigger)


def _rollback(state, record: dict, applied: str, moment, *, trigger: str) -> dict:
    """Сам откат (под замком машины): `.bad`, источник, синхронизация поставки."""
    rel = state.releases
    at = _iso(moment)
    previous = str(record.get("previous_version") or "").strip()
    binary = Path(str(record.get("binary") or ""))
    old = binary.with_name(binary.name + releases.OLD_SUFFIX)
    bad = binary.with_name(binary.name + releases.BAD_SUFFIX)
    source = _rollback_source(record, binary, old)

    bad_versions = [str(v) for v in (rel.get("bad_versions") or [])
                    if not _same_version(v, applied)]
    bad_versions.append(applied)
    rel["bad_versions"] = bad_versions[-BAD_VERSIONS_KEEP:]

    if source is None or not str(binary):
        rel["auto"] = None
        _event(rel, "rollback_failed", src=applied, dst=previous, at=at,
               detail="нет ни резервной копии этой установки, ни bpmkit.exe.old от неё",
               trigger=trigger or None)
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


def clear_bad_versions(state, *, version: Optional[str] = None) -> dict:
    """Снять запрет автоматики: `releases.bad_versions` и счётчик неудачных запусков.

    Явное действие человека (CLI `clear-bad-versions`, кнопка окна «Обновления»): версия,
    помеченная плохой (откат после неудачного старта, ручной откат) или исчерпавшая
    `INSTALL_FAILURES_LIMIT`, снова может ставиться автоматически. `version` — снять
    только её, иначе — все. Ничего не ставит само и сети не трогает. Под замком машины:
    иначе автоматика другого процесса могла бы записать свои ключи поверх.
    """
    with _MachineLock(_lock_path(state)) as lock:
        if not lock.acquired:
            raise ChannelError(
                "Автоматическое обновление сейчас выполняется другим процессом — "
                "повторите через минуту", kind="local_io")
        return _clear_bad_versions(state, version=version)


def _clear_bad_versions(state, *, version: Optional[str]) -> dict:
    _refresh_shared(state)
    rel = state.releases
    wanted = str(version or "").strip()
    bad = [str(item) for item in (rel.get("bad_versions") or [])]
    failures = dict(rel.get("auto_failures") or {})

    def _hit(item) -> bool:
        return not wanted or _same_version(item, wanted)

    cleared = sorted({item for item in bad if _hit(item)}
                     | {item for item in failures if _hit(item)},
                     key=releases.parse_version)
    rel["bad_versions"] = [item for item in bad if not _hit(item)]
    rel["auto_failures"] = {key: value for key, value in failures.items() if not _hit(key)}
    state.save()
    return {"cleared": cleared, "bad_versions": list(rel["bad_versions"]),
            "reason": "bad_versions_cleared" if cleared else "nothing_to_clear"}


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
    """Замок «одно автообновление на машину» — файл, созданный с `O_EXCL`.

    Брошенным замок считается, если процесс-владелец (`pid` в файле) уже не жив — сразу,
    не дожидаясь срока, — или если замку больше `LOCK_STALE_SEC` (владелец не известен
    или pid переиспользован). Живой владелец со свежим замком — «занято».
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.acquired = False

    def _abandoned(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            age = 0.0
        if age >= LOCK_STALE_SEC:
            return True
        try:
            owner = json.loads(self.path.read_text(encoding="utf-8")).get("pid")
        except (OSError, ValueError, AttributeError):
            return False
        if owner is None:
            return False
        return not _pid_alive(owner)

    def __enter__(self) -> "_MachineLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _attempt in range(2):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._abandoned():
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


def _refresh_shared(state) -> None:
    """Общие ключи состояния — с диска (их пишет и другой процесс, см.
    `state.SHARED_KEYS`). Best-effort: нечитаемый файл память не трогает."""
    refresh = getattr(state, "refresh_shared", None)
    if refresh is None:
        return
    try:
        refresh()
    except Exception:  # noqa: BLE001 - чтение состояния best-effort
        pass


def _evaluate(state, ctx, settings, check: dict, binary: Path, moment) -> tuple:
    """Политика по ТЕКУЩЕМУ состоянию: `(policy, version, current, relocated)`.

    Установленная версия пересчитывается (`releases._current_version`: состояние и
    `manifest.json` поставки), а не берётся из `check`: между проверкой и этим местом
    другой процесс мог поставить ту же версию.
    """
    rel = state.releases
    version = str(check.get("latest") or "").strip()
    current = releases._current_version(state, ctx) or check.get("current")
    available = bool(check.get("available"))
    if (available and releases.is_numeric_version(version)
            and releases.is_numeric_version(current)
            and releases.compare_versions(version, current) <= 0):
        available = False
    failures = rel.get("auto_failures") or {}
    server_running = mcp_mutex.server_mutex_exists(strict=True)
    relocated = server_running and self_relocate_active(binary)
    policy = decide(
        update_mode=_mode_of(settings),
        available=available,
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
        settings_readable=not str(getattr(settings, "load_problem", "") or ""),
    )
    return policy, version, current, relocated


def execute(state, session, settings, *, trigger: str = "manual", now=None,
            check: Optional[dict] = None) -> dict:
    """Проверка → политика → подготовка → установка. Исход — словарь, не исключение.

    Исключение поднимается только из самой ПРОВЕРКИ релиза (`releases.check`): это та
    же ошибка, что у цикла `releases` в режиме `notify`, и разбирает её тот же код
    (`runner._execute`: нет лицензии — цикл встаёт, сеть — повтор). Подготовка и
    установка ошибок наружу не бросают: проверка уже состоялась, а «не скачалось» или
    «файл занят» — повод подождать, а не остановить канал.

    Два процесса (диспетчер и CLI из задачи Планировщика) делят файл состояния: под
    замком машины общие ключи перечитываются с диска и политика считается ЗАНОВО —
    решение «ставить» принимается по тому, что уже сделал другой. Мьютекс сервера и
    самоперенос проверяются ещё раз прямо перед установкой: подготовка (скачивание)
    могла длиться минуты.
    """
    moment = _aware(now)
    at = _iso(moment)
    client, ctx = session.client, session.ctx
    _refresh_shared(state)
    if check is None:
        check = releases.check(client, state, ctx)
    binary = Path(str(getattr(ctx, "binary_path", "") or ""))
    policy, version, current, _relocated = _evaluate(state, ctx, settings, check, binary,
                                                     moment)
    payload = _payload(policy["decision"], policy["reason"], version=version,
                       current=current, trigger=trigger)
    if not policy["stage"]:
        return _remember(state, payload, at)

    with _MachineLock(_lock_path(state, ctx=ctx)) as lock:
        if not lock.acquired:
            return _remember(state, _payload(DECISION_WAITING, "busy_other_process",
                                             version=version, current=current,
                                             trigger=trigger), at)
        _refresh_shared(state)
        policy, version, current, _relocated = _evaluate(state, ctx, settings, check,
                                                         binary, moment)
        payload = _payload(policy["decision"], policy["reason"], version=version,
                           current=current, trigger=trigger)
        if not policy["stage"]:
            return _remember(state, payload, at)
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
        if not _same_version(artifact.get("version"), version):
            # Подготовлен файл не той версии, которую разрешила политика (плохую версию,
            # раскатку и сбои проверяли для `version`) — ставить его нельзя.
            return _remember(state, _payload(
                DECISION_SKIPPED, "artifact_version_mismatch", version=version,
                current=current, artifact=artifact, trigger=trigger, ok=False,
                detail=f"подготовлена версия {artifact.get('version') or '—'}, "
                       f"ожидалась {version}"), at)
        if not policy["apply"]:
            return _remember(state, payload, at)

        launched = releases.installer_status(state).get("launched") or {}
        if launched.get("running"):
            return _remember(state, _payload(DECISION_WAITING, "installer_running",
                                             version=version, current=current,
                                             artifact=artifact, trigger=trigger), at)
        server_running = mcp_mutex.server_mutex_exists(strict=True)
        relocated = server_running and self_relocate_active(binary)
        if server_running and not relocated:
            return _remember(state, _payload(DECISION_WAITING, "mcp_running",
                                             version=version, current=current,
                                             artifact=artifact, trigger=trigger), at)
        return _remember(state, _apply(state, ctx, artifact, binary=binary,
                                       version=version, current=current,
                                       trigger=trigger, at=at, payload=payload,
                                       swap_busy=relocated,
                                       server_running=server_running), at)


def _apply(state, ctx, artifact: dict, *, binary: Path, version: str, current,
           trigger: str, at: str, payload: dict, swap_busy: bool = False,
           server_running: bool = False) -> dict:
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
    # Счётчик маркера старта ЭТОЙ версии в момент установки: попытки сверх него — уже
    # запуски установленного файла (маркер мог остаться от смоука того же exe).
    marker = read_startup_marker()
    attempts_before = (_int(marker["attempts"])
                       if marker is not None and _same_version(marker["version"], version)
                       else 0)
    rel["auto"] = {
        "applied_version": version,
        "previous_version": str(current or "") or None,
        "binary": str(binary),
        "backup": backup,
        "via": via,
        "applied_at": at,
        "trigger": trigger or None,
        # `/SWAPBUSY` (или переименование пути «только exe») разрешён этой установке:
        # только тогда `bpmkit.exe.old` может быть прежней версией ОТ НЕЁ (откат).
        "swap_busy": bool(swap_busy),
        # мьютекс сервера при запуске (только с самопереносом он бывает виден) — для
        # разбора отказа установщика (`install_deferred`).
        "server_running": bool(server_running),
        "startup_attempts_at_install": attempts_before,
        # каталог замка машины для автоотката (у него нет лицензионного контекста).
        "workdir": str(releases.companion_workdir(ctx)),
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
            rel["auto"]["swap_busy"] = bool(result.get("old_path"))
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
