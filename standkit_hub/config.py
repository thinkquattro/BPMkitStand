"""
Конфиг веб-дашборда standkit (``standkit-hub.json``) — ВСЕ параметры,
которые иначе пришлось бы задавать флагами ``--host``/``--port``/... в
терминале при запуске headless-агента, плюс параметры самого хаба (реестр,
каталоги, интервал автообновления, список удалённых агентов федерации).

Намеренно НЕ импортирует ничего из ``http.server``/веб-слоя — модуль должен
быть тестируемым в изоляции (см. tests/test_hub_config.py). Отдаётся/
принимается фронтендом хаба через ``GET/POST /api/settings`` (см.
standkit_hub/server.py).

Путь конфига — та же папка BPMkit, что и реестр стендов (см.
standkit.registry.bpmkit_config_dir):
    Windows: %APPDATA%\\BPMkit\\standkit-hub.json
    POSIX:   ~/.config/BPMkit/standkit-hub.json  (или $XDG_CONFIG_HOME/BPMkit/...)

Секреты (control/readonly-токены агентов) в конфиге хранятся ТОЛЬКО как ссылки
(``*_ref``) на standkit.secrets — значения самих секретов сюда никогда не
попадают.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Optional

from standkit.registry import bpmkit_config_dir, default_registry_path

_CONFIG_FILE_NAME = "standkit-hub.json"

# Значения по умолчанию для параметров запуска локального агента (совпадают
# с default'ами standkit_agent/__main__.py — см. DEFAULT_LOCKOUT_* там же).
_DEFAULT_AGENT_HOST = "127.0.0.1"
_DEFAULT_AGENT_PORT = 8765
_DEFAULT_LOCKOUT_MAX_FAILURES = 5
_DEFAULT_LOCKOUT_WINDOW_SEC = 300.0
_DEFAULT_REFRESH_INTERVAL_SEC = 10

# Удалённый режим BPMkit (``BPMkit.exe serve-http``): значения по умолчанию.
# Порт -- 8766, а не 8765 как у самого serve-http: 8765 по умолчанию занимает локальный
# агент диспетчера, и две кнопки «Запустить» с одним портом дрались бы между собой.
# Токен по умолчанию -- тот же ref, что у serve-http (DEFAULT_TOKEN_REF сервера BPMkit).
DEFAULT_REMOTE_HOST = "127.0.0.1"
DEFAULT_REMOTE_PORT = 8766
DEFAULT_REMOTE_PROFILE = "default"
DEFAULT_REMOTE_PATH = "/mcp"
DEFAULT_REMOTE_TOKEN_REF = "bpmsoft-mcp:remote:token"

# Через сколько минут простоя диспетчер выходит сам. 0 — выключено.
# 30 минут — компромисс: дольше типичной паузы в работе (не закрываем диспетчер
# под руками у человека, отошедшего за кофе), но заметно короче рабочего дня, за
# который забытый фоновый процесс успевает стать «почему занят порт 8770».
_DEFAULT_IDLE_SHUTDOWN_MIN = 30

# Тема оформления дашборда. Источник правды — ИМЕННО конфиг, а не
# localStorage браузера: localStorage привязан к origin (включая порт), а хаб
# исторически стартовал на эфемерном порту — каждый запуск давал новый origin
# и, как следствие, пустое хранилище («тема не запоминается»). localStorage
# остаётся лишь клиентским кэшем, чтобы тема применилась до ответа /api/settings.
HUB_THEMES = ("light", "dark", "auto")
_DEFAULT_THEME = "auto"


def normalize_theme(value: object) -> str:
    """
    Приводит значение темы к одному из ``HUB_THEMES``.

    Неизвестное/битое значение (в т.ч. из руками правленого конфига) молча
    откатывается на ``auto`` — тема не тот параметр, ради которого стоит
    ронять запуск дашборда.
    """
    if isinstance(value, str) and value.strip().lower() in HUB_THEMES:
        return value.strip().lower()
    return _DEFAULT_THEME


def normalize_remote_port(value: object) -> int:
    """Порт удалённого режима: целое 1..65535, иначе дефолт (мусор из ручной правки
    конфига не должен ронять запуск диспетчера -- ошибку покажет кнопка «Запустить»)."""
    if isinstance(value, bool):
        return DEFAULT_REMOTE_PORT
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_REMOTE_PORT
    return port if 1 <= port <= 65535 else DEFAULT_REMOTE_PORT


def normalize_remote_text(value: object, default: str, *, lower: bool = False) -> str:
    """Строковая настройка удалённого режима: обрезанная, пустое -- дефолт."""
    text = str(value if value is not None else "").strip()
    if lower:
        text = text.lower()
    return text or default


def normalize_idle_shutdown_min(value: object) -> int:
    """
    Приводит «автовыход по простою» к целому числу минут ≥ 0.

    Битое/отрицательное значение (руками правленый конфиг, присланная формой
    строка) откатывается на дефолт, а НЕ на 0: молчаливое «выключено» из-за
    опечатки человек заметит только через сутки забытого процесса, тогда как
    вернувшийся дефолт виден в форме настроек сразу.

    ``0`` — единственное значение, отключающее таймер, и оно проходит как есть:
    это осознанное решение пользователя, а не сбой разбора.
    """
    if isinstance(value, bool):  # bool — подкласс int, но «True минут» бессмысленно
        return _DEFAULT_IDLE_SHUTDOWN_MIN
    try:
        minutes = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_IDLE_SHUTDOWN_MIN
    if minutes < 0:
        return _DEFAULT_IDLE_SHUTDOWN_MIN
    return minutes


@dataclass
class RemoteAgent:
    """Одна запись федерации удалённых агентов (мульти-агентная панель хаба)."""

    name: str = ""
    url: str = ""
    # Ссылка на секрет токена (standkit.secrets), НЕ сам токен.
    token_ref: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "RemoteAgent":
        return cls(
            name=str(data.get("name", "")),
            url=str(data.get("url", "")),
            token_ref=str(data.get("token_ref", "")),
        )

    def to_dict(self) -> dict:
        return {"name": self.name, "url": self.url, "token_ref": self.token_ref}


@dataclass
class CompanionCycle:
    """Один цикл канала обновлений: включён ли и как часто опрашивать.

    Интервал хранится в СЕКУНДАХ, а в форме настроек показывается в минутах/часах:
    единица измерения — вопрос представления, а конфиг обязан быть однозначным.
    """

    enabled: bool = False
    interval_sec: int = 1800

    @classmethod
    def from_dict(cls, data: object, *, default_enabled: bool, default_interval: int,
                  min_interval: int) -> "CompanionCycle":
        if not isinstance(data, dict):
            return cls(enabled=default_enabled, interval_sec=default_interval)
        enabled = data.get("enabled", default_enabled)
        raw = data.get("interval_sec", default_interval)
        try:
            interval = int(raw)
        except (TypeError, ValueError):
            interval = default_interval
        # Нижняя граница — не вкусовщина: слишком частый опрос лицензированного
        # эндпоинта выглядит на стороне издателя как перебор ключа и бессмысленно
        # нагружает единственный синхронный воркер бэкенда.
        return cls(enabled=bool(enabled), interval_sec=max(min_interval, interval))

    def to_dict(self) -> dict:
        return {"enabled": bool(self.enabled), "interval_sec": int(self.interval_sec)}


# Дефолты и нижние границы циклов канала. Паттерны включены (markdown, не исполняемый
# код — sha256 достаточно). Релизы включены по умолчанию: ключ подписи
# артефактов и HTTPS бэкенда уже в строю. Докуда доходит автоматика, решает режим
# обновлений `update_mode` (ниже): `notify` — проверка и подготовка, установка кнопкой;
# `auto` — ещё и установка подписанного артефакта без человека (runner._run_releases,
# standkit_companion.autoupdate, SECURITY.md §4.2); `off` — проверки нет.
_COMPANION_PATTERNS_INTERVAL = 21600 # 6 часов
_COMPANION_PATTERNS_MIN_INTERVAL = 300     # 5 минут
_COMPANION_RELEASES_INTERVAL = 86400       # раз в сутки
_COMPANION_RELEASES_MIN_INTERVAL = 3600    # час
# Отзыв (revocations) больше не самостоятельная настройка: цикл ВСЕГДА
# включён вместе с главным рубильником канала, а его интервал равен интервалу
# паттернов — см. CompanionRunner._enabled/_interval_of. Константы ниже остаются
# только для обратной совместимости чтения СТАРОГО конфига (см. CompanionCycle.from_dict
# у поля revocations) — новых мест, где они читались бы как независимая настройка, нет.
_COMPANION_REVOCATIONS_INTERVAL = 1800     # вместе с паттернами (легаси-дефолт)
_COMPANION_REVOCATIONS_MIN_INTERVAL = 300

# --- Режим обновлений MCP и кольцо раскатки --------------------------------------------
#
# Режим — ОДНО поле с тремя значениями, а не набор флажков: прежние `releases.enabled`
# и `auto_stage_release` описывали две точки одной шкалы «ничего → проверить → скачать»,
# и третья точка («поставить») флажком выражалась бы как ещё одна комбинация, половина
# которых бессмысленна («ставить, но не проверять»).
#
# * `auto`   — проверка, подготовка и установка подписанного артефакта без человека
#              (подпись и применимость — fail-closed, как у ручной установки);
# * `notify` — проверка и подготовка в фоне, установка — кнопкой человека;
# * `off`    — проверки новых версий MCP нет (цикл `releases` не тикает).
UPDATE_MODE_AUTO = "auto"
UPDATE_MODE_NOTIFY = "notify"
UPDATE_MODE_OFF = "off"
UPDATE_MODES = (UPDATE_MODE_AUTO, UPDATE_MODE_NOTIFY, UPDATE_MODE_OFF)

# Кольцо раскатки: `early` получает релиз сразу после публикации, `stable` — после срока
# `stable_after`, который издатель объявляет в `GET /v1/version/latest` (поле `rollout`).
# Кольцо — не пользовательская настройка окна «Обновления» (там оно только показывается):
# его задаёт установка (параметр мастера) или CLI BPMkit.
UPDATE_RING_EARLY = "early"
UPDATE_RING_STABLE = "stable"
UPDATE_RINGS = (UPDATE_RING_EARLY, UPDATE_RING_STABLE)
DEFAULT_UPDATE_RING = UPDATE_RING_STABLE


def normalize_update_mode(value: object) -> str:
    """Значение режима из ``UPDATE_MODES`` или пустая строка, если значение не распознано.

    Пустая строка, а не дефолт, — намеренно: «поле битое» и «поля нет» разрешаются ОДНИМ
    правилом (``derive_update_mode``), а не вторым умолчанием, которое разошлось бы с ним.
    """
    if isinstance(value, str) and value.strip().lower() in UPDATE_MODES:
        return value.strip().lower()
    return ""


def normalize_update_ring(value: object) -> str:
    """Кольцо раскатки из ``UPDATE_RINGS``; мусор — ``stable`` (позднее кольцо безопаснее)."""
    if isinstance(value, str) and value.strip().lower() in UPDATE_RINGS:
        return value.strip().lower()
    return DEFAULT_UPDATE_RING


def derive_update_mode(section: object) -> str:
    """Режим для секции ``companion`` без (распознаваемого) поля ``update_mode``.

    Правило миграции существующих установок: ``off``, если проверка релизов выключена в
    файле ЯВНО (``releases.enabled`` есть и ложно), иначе ``auto``. Явное «не проверять»
    — решение человека, и новый умолчательный режим его не перебивает; молчание
    конфига — не решение, и оно получает умолчание поставки.
    """
    releases = section.get("releases") if isinstance(section, dict) else None
    if isinstance(releases, dict) and "enabled" in releases and not bool(releases.get("enabled")):
        return UPDATE_MODE_OFF
    return UPDATE_MODE_AUTO


#: Признак «настройки канала прочитать не удалось» (`CompanionSettings.load_problem`):
#: файл конфига есть, но не читается или не разбирается, секция `companion` — не объект,
#: `update_mode` — незнакомое значение. Дефолты в этом случае — не выбор человека, и
#: режим в этом случае — `notify` (проверяет и готовит, но не ставит), а автоматика
#: называет причину. Нет файла вовсе — не проблема (первый запуск, умолчание поставки).
SETTINGS_UNREADABLE = "settings_unreadable"


def effective_update_mode(settings: object) -> str:
    """Действующий режим для объекта настроек (в том числе собранного конструктором).

    ``from_dict`` держит ``releases.enabled`` зеркалом режима, но настройки, собранные
    напрямую (тесты, чужой вызывающий), могут противоречить друг другу. Тогда выключенный
    цикл релизов сильнее режима: «не проверять» не может означать «ставить».
    """
    mode = normalize_update_mode(getattr(settings, "update_mode", ""))
    if not mode:
        releases = getattr(settings, "releases", None)
        enabled = getattr(releases, "enabled", True)
        mode = UPDATE_MODE_AUTO if enabled else UPDATE_MODE_OFF
    releases = getattr(settings, "releases", None)
    if releases is not None and not bool(getattr(releases, "enabled", True)):
        return UPDATE_MODE_OFF
    return mode


def sync_legacy_releases_toggle(merged: dict, incoming: object) -> dict:
    """Согласовать режим с прежним флажком ``releases.enabled``, присланным БЕЗ режима.

    Старые формы и вызывающие знают только флажок «проверять версии MCP по расписанию».
    ``from_dict`` выводит из режима флажок (режим главный), поэтому без этого шага
    присланное ``releases.enabled = false`` молча возвращалось бы к ``true``. Перевод:
    ``false`` → ``off``; ``true`` при текущем ``off`` → ``notify`` (флажок означал
    «проверять», а не «устанавливать без меня»). Присланный режим всегда главнее флажка.
    """
    out = dict(merged or {})
    if not isinstance(incoming, dict) or "update_mode" in incoming:
        return out
    releases = incoming.get("releases")
    if not isinstance(releases, dict) or "enabled" not in releases:
        return out
    current = normalize_update_mode(out.get("update_mode")) or derive_update_mode(out)
    if not bool(releases.get("enabled")):
        out["update_mode"] = UPDATE_MODE_OFF
    elif current == UPDATE_MODE_OFF:
        out["update_mode"] = UPDATE_MODE_NOTIFY
    return out


@dataclass
class CompanionSettings:
    """Настройки канала доставки обновлений издателя (пакет ``standkit_companion``).

    Секция живёт в конфиге ХАБА, а не в отдельном файле: канал поднимает процесс хаба,
    и раздвоение источников настроек — гарантированный источник расхождений «в UI одно,
    в службе другое».

    Чего здесь НЕТ и не будет:

    * лицензионного конверта и любых токенов — конверт резолвится у CLI самого MCP на
      каждый тик, в конфиге ему не место (инвариант SECURITY.md: в конфиге только ``*_ref``);
    * флага «не проверять подпись бинаря». Политика бинаря — строго fail-closed БЕЗ
      возможности отключения: автоапдейтер тянет и ИСПОЛНЯЕТ код. Отключаемым сделан
      только строгий режим для ПАТТЕРНОВ (``require_pattern_signature``), потому что там
      подписи ещё нет на стороне издателя и дефолт «не требовать» — единственный, при
      котором канал вообще работает.
    """

    # Главный рубильник: False — канал не поднимается вовсе, тиков нет.
    enabled: bool = True
    # Пусто — берётся дефолт клиентского MCP (env BPMKIT_BACKEND_URL поверх адреса
    # издателя). Своей второй константы адреса канал не заводит. Схема здесь
    # НЕ валидируется: единственное место политики транспорта — конструктор
    # `standkit_companion.backend.BackendClient` (только `https`, исключение — loopback),
    # и вторая её копия в конфиге разъехалась бы с первой молча.
    backend_url: str = ""
    # Путь к CLI BPMkit (bpmkit.exe / python -m bpmkit). Пусто — автодетект рядом с
    # поставкой. Нужен ТОЛЬКО чтобы спросить лицензионный контекст, не для логики канала.
    mcp_cli: str = ""
    patterns: CompanionCycle = field(default_factory=lambda: CompanionCycle(
        enabled=True, interval_sec=_COMPANION_PATTERNS_INTERVAL))
    # включены по умолчанию (было False) — см. комментарий у констант выше.
    releases: CompanionCycle = field(default_factory=lambda: CompanionCycle(
        enabled=True, interval_sec=_COMPANION_RELEASES_INTERVAL))
    # поле сохранено для обратной совместимости чтения старого конфига
    # (`from_dict`), но раннер больше не смотрит на `revocations.enabled` — цикл
    # принудительно включён вместе с главным рубильником `enabled`. Не показывается
    # в /api/settings (см. server.py::_UI_HIDDEN_COMPANION_FIELDS).
    revocations: CompanionCycle = field(default_factory=lambda: CompanionCycle(
        enabled=True, interval_sec=_COMPANION_REVOCATIONS_INTERVAL))
    # Прежний флажок «скачивать новую версию заранее». Остаётся только для совместимости
    # чтения старого конфига: подготовку теперь решает режим (`stage_releases` ниже —
    # `update_mode` равен `auto` или `notify`), значение этого поля канал не читает.
    auto_stage_release: bool = False
    # Требовать подпись у КАЖДОГО паттерна. Дефолт False: механизма подписи markdown-тела
    # у издателя ещё нет, включение сегодня просто выключит канал целиком.
    require_pattern_signature: bool = False
    # Режим обновлений MCP (`UPDATE_MODES`). В файле без поля выводится правилом
    # `derive_update_mode`; `releases.enabled` — его зеркало (`off` ⇔ цикл не тикает).
    update_mode: str = UPDATE_MODE_AUTO
    # Кольцо раскатки (`UPDATE_RINGS`): `stable` ждёт срока `stable_after` издателя.
    update_ring: str = DEFAULT_UPDATE_RING
    # Ключи секции, которых эта версия не знает (их мог записать CLI BPMkit или более
    # новый диспетчер). Хранятся и пишутся обратно как есть: запись конфига — это
    # read-modify-write, а не «оставить только своё».
    extra: dict = field(default_factory=dict)
    # `SETTINGS_UNREADABLE`, если секцию прочитать не удалось (см. константу), иначе
    # пусто. В файл не пишется (`to_dict`): это факт чтения, а не настройка.
    load_problem: str = ""

    @property
    def stage_releases(self) -> bool:
        """Готовить (скачивать и проверять) найденный релиз в фоне — режимы auto и notify."""
        return effective_update_mode(self) in (UPDATE_MODE_AUTO, UPDATE_MODE_NOTIFY)

    @classmethod
    def from_dict(cls, data: object) -> "CompanionSettings":
        if data is None:
            return cls()
        if not isinstance(data, dict):
            # Секция есть, но не объект — выбор человека не прочитан (см. SETTINGS_UNREADABLE).
            return cls(update_mode=UPDATE_MODE_NOTIFY, load_problem=SETTINGS_UNREADABLE)
        raw_mode = data.get("update_mode")
        mode = normalize_update_mode(raw_mode) or derive_update_mode(data)
        problem = ""
        if raw_mode is not None and not normalize_update_mode(raw_mode):
            # Незнакомый режим — не «поля нет»: человек (или более новая версия) что-то
            # выбрал, а что — не понять. Ставить без него нельзя: `notify` (или `off`,
            # если проверка релизов выключена явно), и признак для автоматики и окна.
            problem = SETTINGS_UNREADABLE
            if mode == UPDATE_MODE_AUTO:
                mode = UPDATE_MODE_NOTIFY
        releases = CompanionCycle.from_dict(
            data.get("releases"), default_enabled=True,
            default_interval=_COMPANION_RELEASES_INTERVAL,
            min_interval=_COMPANION_RELEASES_MIN_INTERVAL)
        # Зеркало режима: `off` — цикл релизов не тикает, иначе — тикает. Режим главнее
        # флажка (см. sync_legacy_releases_toggle — как прежний флажок переводится в режим).
        releases.enabled = mode != UPDATE_MODE_OFF
        known = {f.name for f in fields(cls)} - {"extra", "load_problem"}
        extra = {key: value for key, value in data.items()
                 if key not in known and key != "load_problem"}
        if problem:
            # Исходное значение режима не теряется: пересохранение конфига (любая правка
            # формы) пишет его обратно как есть (`to_dict`), а не `notify`.
            extra["update_mode"] = raw_mode
        return cls(
            enabled=bool(data.get("enabled", True)),
            backend_url=str(data.get("backend_url", "") or ""),
            mcp_cli=str(data.get("mcp_cli", "") or ""),
            patterns=CompanionCycle.from_dict(
                data.get("patterns"), default_enabled=True,
                default_interval=_COMPANION_PATTERNS_INTERVAL,
                min_interval=_COMPANION_PATTERNS_MIN_INTERVAL),
            releases=releases,
            revocations=CompanionCycle.from_dict(
                data.get("revocations"), default_enabled=True,
                default_interval=_COMPANION_REVOCATIONS_INTERVAL,
                min_interval=_COMPANION_REVOCATIONS_MIN_INTERVAL),
            auto_stage_release=bool(data.get("auto_stage_release", False)),
            require_pattern_signature=bool(data.get("require_pattern_signature", False)),
            update_mode=mode,
            update_ring=normalize_update_ring(data.get("update_ring")),
            extra=extra,
            load_problem=problem,
        )

    def to_dict(self) -> dict:
        """Секция для записи в файл (read-modify-write: незнакомые ключи — как были).

        Настройки, прочитанные с проблемой (`load_problem`), режим НЕ пишут: временное
        «не прочитали» не должно становиться постоянным `notify`. Незнакомое значение
        режима уходит обратно как было (оно лежит в `extra`); битый файл — без поля, и
        следующее чтение выведет режим по правилу `derive_update_mode`.
        """
        out = dict(self.extra or {})
        managed = {
            "enabled": bool(self.enabled),
            "backend_url": self.backend_url,
            "mcp_cli": self.mcp_cli,
            "patterns": self.patterns.to_dict(),
            "releases": self.releases.to_dict(),
            "revocations": self.revocations.to_dict(),
            "auto_stage_release": bool(self.auto_stage_release),
            "require_pattern_signature": bool(self.require_pattern_signature),
            # Действующий режим, а не сырое поле: настройки, собранные конструктором с
            # выключенным циклом релизов, обязаны пережить запись и чтение как `off`.
            "update_mode": effective_update_mode(self),
            "update_ring": normalize_update_ring(self.update_ring),
        }
        if self.load_problem:
            managed.pop("update_mode")
        out.update(managed)
        return out


@dataclass
class HubConfig:
    """
    Все настраиваемые пользователем параметры веб-дашборда, чтобы не лазить
    в PowerShell/``--help``.

    Поля сгруппированы по смыслу:
    - реестр/каталоги/автообновление — сам хаб;
    - agents — федерация удалённых standkit-агентов, которых показывает хаб;
    - agent_* / tls_* / lockout_* / insecure / audit_log — дефолты для запуска
      ЛОКАЛЬНОГО агента из хаба (зеркалят флаги standkit_agent/__main__.py
      один в один, чтобы форма настроек их полностью покрывала).
    """

    # --- Хаб ---
    registry_path: str = field(default_factory=lambda: str(default_registry_path()))
    run_dir: str = ""
    log_dir: str = ""
    refresh_interval_sec: int = _DEFAULT_REFRESH_INTERVAL_SEC
    # Автовыход по простою, МИНУТЫ; 0 — выключено. Простой =
    # «нет ни одного открытого SSE-клиента И ни одного ЖИВОГО стенда»: первое
    # означает «дашборд никто не смотрит», второе — «диспетчер сейчас ничем не
    # управляет». Оба условия обязательны вместе: закрытая вкладка при живых
    # стендах — норма (стенды поднимали через диспетчер и ждут), а открытая
    # вкладка без стендов — человек смотрит на пустой список, но смотрит.
    #
    # ЖИВОЙ — по факту пробы (``ProbeState.OK``: порт/pid), включая стенды,
    # поднятые мимо диспетчера и усыновлённые. Любое незнание
    # (отказ опроса, непробованный слепок) трактуется как «стенды есть»
    #: цена лишнего завершения несимметрично выше.
    idle_shutdown_min: int = _DEFAULT_IDLE_SHUTDOWN_MIN
    # light | dark | auto (см. normalize_theme). Подставляется сервером прямо
    # в атрибут data-theme отдаваемого index.html — тема применяется ДО
    # выполнения JS, без «мигания» светлой темой у любителей тёмной.
    theme: str = _DEFAULT_THEME

    # --- Федерация удалённых агентов ---
    agents: list[RemoteAgent] = field(default_factory=list)

    # --- Дефолты запуска локального агента (standkit_agent) ---
    agent_host: str = _DEFAULT_AGENT_HOST
    agent_port: int = _DEFAULT_AGENT_PORT
    token_ref: str = ""
    readonly_token_ref: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    tls_client_ca: str = ""
    insecure: bool = False
    audit_log: str = ""
    lockout_max_failures: int = _DEFAULT_LOCKOUT_MAX_FAILURES
    lockout_window_sec: float = _DEFAULT_LOCKOUT_WINDOW_SEC

    # --- Удалённый режим BPMkit (``BPMkit.exe serve-http``) ---
    # Токен -- ТОЛЬКО ссылка (remote_token_ref) на секрет; само значение сюда не попадает
    # (инвариант SECURITY.md: в конфиге только ``*_ref``).
    remote_host: str = DEFAULT_REMOTE_HOST
    remote_port: int = DEFAULT_REMOTE_PORT
    remote_profile: str = DEFAULT_REMOTE_PROFILE
    remote_token_ref: str = DEFAULT_REMOTE_TOKEN_REF
    # Принимать токен и в адресе (?token=...) -- для клиентов без заголовков
    # (McpConnection платформы ИИ-агентов BPMSoft 2.0). Токен в адресе оседает в журналах.
    remote_allow_query_token: bool = False
    # Поднимать удалённый режим вместе с диспетчером.
    remote_autostart: bool = False

    # --- Канал доставки обновлений издателя (пакет standkit_companion) ---
    # Секция присутствует в конфиге ВСЕГДА, даже в free-редакции без пакета: так форма
    # настроек и файл конфига не меняют форму при установке платной редакции, и
    # пользователь видит, что именно включится.
    companion: CompanionSettings = field(default_factory=CompanionSettings)

    # --- чтение/запись ---

    @classmethod
    def config_path(cls) -> Path:
        """Канонический путь к файлу конфига хаба (та же папка, что и реестр кита)."""
        return bpmkit_config_dir() / _CONFIG_FILE_NAME

    @classmethod
    def load(cls, path: Optional[str | Path] = None) -> "HubConfig":
        """
        Читает конфиг из ``path`` (по умолчанию — ``config_path()``).

        Если файла нет — возвращает конфиг с дефолтами (в т.ч.
        ``registry_path = default_registry_path()``); это нормальная ситуация
        при первом запуске хаба. Файл читается как ``utf-8-sig`` (терпим к BOM
        — тот же принцип, что и в standkit.registry.Registry.load).
        """
        p = Path(path) if path is not None else cls.config_path()
        if not p.exists():
            return cls()

        try:
            raw = p.read_text(encoding="utf-8-sig")
            data = json.loads(raw) if raw.strip() else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict):
            # Битый (пустой, нечитаемый, не объект) конфиг хаба не должен ронять запуск
            # диспетчера — откатываемся на дефолты (в отличие от реестра, это не
            # критичные для управления стендами данные). Но дефолты здесь — не выбор
            # человека: канал обновлений получает признак `SETTINGS_UNREADABLE`, и
            # автоматическая установка без него не идёт (ведёт себя как `notify`).
            return cls(companion=CompanionSettings(update_mode=UPDATE_MODE_NOTIFY,
                                                   load_problem=SETTINGS_UNREADABLE))

        return cls.from_dict(data)

    def save(self, path: Optional[str | Path] = None) -> None:
        """Пишет конфиг в ``path`` (по умолчанию — ``config_path()``), создавая папку при необходимости.

        Запись АТОМАРНАЯ (временный файл рядом → ``os.replace``). Поводом стал канал
        обновлений: его тик правит конфиг (например, включает override-корень паттернов) в
        тот же момент, когда пользователь жмёт «Сохранить» в форме настроек. Обычный
        ``write_text`` в этой гонке оставляет обрезанный JSON, а битый конфиг ``load()``
        молча заменяет дефолтами — то есть настройки исчезают без единого сообщения.
        Временный файл создаётся в ТОЙ ЖЕ папке: ``os.replace`` атомарен только в пределах
        одного тома.
        """
        p = Path(path) if path is not None else self.config_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        tmp = p.with_name(p.name + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)

    def resolve_run_dir(self) -> Path:
        """
        Каталог runtime-файлов диспетчера: ``run_dir`` из конфига, иначе
        ``~/.standkit/run``.

        Один ответ на всех, кто туда пишет: pid-файл локального агента
        (``standkit_hub.agent_control``), файл состояния экземпляра хаба и
        файл передачи сессии при перезапуске с правами администратора
        (``standkit_hub.instance`` / ``standkit_hub.elevation``). Раньше
        дефолт был захардкожен в agent_control — вторая копия неизбежно
        разъехалась бы с первой.

        Папку НЕ создаёт: решение «создавать ли» принимает тот, кто пишет.
        """
        return Path(self.run_dir) if self.run_dir else Path.home() / ".standkit" / "run"

    def ensure_registry_dir(self) -> Path:
        """Создаёт папку файла реестра проектов (``registry_path``), если её ещё
        нет, и возвращает её.

        Нужно при ПЕРВОМ запуске диспетчера: на свежей машине папки
        ``%APPDATA%\\BPMkit`` может не существовать, и показываемый в интерфейсе
        путь к ``projects.json`` указывает «в никуда» — попытка открыть его в
        проводнике даёт «Windows не удаётся найти …». Ранее папка появлялась
        только после первой записи (регистрация стенда / сохранение настроек)."""
        reg_parent = Path(self.registry_path).parent if self.registry_path else default_registry_path().parent
        reg_parent.mkdir(parents=True, exist_ok=True)
        return reg_parent

    # --- сериализация ---

    @classmethod
    def from_dict(cls, data: dict) -> "HubConfig":
        known = {f.name for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for key, value in data.items():
            if key not in known:
                continue
            if key == "agents":
                kwargs[key] = [RemoteAgent.from_dict(a) for a in value]
            elif key == "companion":
                kwargs[key] = CompanionSettings.from_dict(value)
            elif key == "theme":
                kwargs[key] = normalize_theme(value)
            elif key == "remote_port":
                kwargs[key] = normalize_remote_port(value)
            elif key == "remote_host":
                kwargs[key] = normalize_remote_text(value, DEFAULT_REMOTE_HOST)
            elif key == "remote_profile":
                kwargs[key] = normalize_remote_text(value, DEFAULT_REMOTE_PROFILE, lower=True)
            elif key == "remote_token_ref":
                kwargs[key] = normalize_remote_text(value, DEFAULT_REMOTE_TOKEN_REF)
            elif key in ("remote_allow_query_token", "remote_autostart"):
                kwargs[key] = bool(value)
            elif key == "idle_shutdown_min":
                kwargs[key] = normalize_idle_shutdown_min(value)
            else:
                kwargs[key] = value
        return cls(**kwargs)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["agents"] = [a.to_dict() for a in self.agents]
        result["companion"] = self.companion.to_dict()
        # Тему нормализуем и на выходе: конфиг мог быть собран напрямую
        # конструктором (в обход from_dict), а фронт обязан получать только
        # значение из HUB_THEMES.
        result["theme"] = normalize_theme(result.get("theme"))
        result["idle_shutdown_min"] = normalize_idle_shutdown_min(
            result.get("idle_shutdown_min"))
        return result
