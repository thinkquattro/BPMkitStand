# -*- coding: utf-8 -*-
"""Планировщик канала: три цикла (паттерны, релизы, отзыв) в ОДНОМ фоновом потоке.

Устройство повторяет `standkit_hub.poller.StatusPoller` — ожидание на `threading.Condition`
шагами не длиннее `MAX_WAIT_STEP_SEC`, перечитывание настроек перед каждым сном, фоновый
поток без права умереть, `daemon=True`. Отличия от поллера продиктованы предметом, и каждое
ниже объяснено.

**1. Один поток на три цикла, а не три потока.** Тики редкие (30 минут у паттернов, сутки у
релизов), и цена трёх потоков — не память, а поведение: каждый из них независимо дёргал бы
`context.resolve`, то есть ЗАПУСКАЛ CLI клиентского MCP за лицензионным конвертом. Три
запуска процесса подряд на Windows — это ещё и три шанса мигнуть консольным окном. Один
поток резолвит контекст ОДИН раз на пробуждение и отдаёт его всем циклам.

**2. Джиттер ±10% — обязателен, в отличие от поллера.** Поллер опрашивает локальные стенды,
и совпадение тиков у разных машин никого не касается. Здесь на том конце — единственный
синхронный воркер бэкенда издателя: после массового обновления парк клиентов стартует в одну
и ту же секунду и без разброса приходит к издателю ровно одновременно, навсегда (интервал
фиксированный — расхождение не накапливается). Источник разброса — `random.Random`,
засеянный СТАБИЛЬНО ДЛЯ МАШИНЫ (sha256 от пути конфига): клиент должен получать свой сдвиг,
а не новый после каждого перезапуска, иначе фаза сбрасывается вместе с обновлением. Встроенный
`hash()` для этого не годится — он рандомизирован по процессу (PYTHONHASHSEED).

**3. Не-retriable ошибка гасит ТОЛЬКО свой цикл.** `CompanionError.retriable is False`
(битый конверт, отозванная лицензия, недействительная подпись) означает «без действий
человека повтор ничего не изменит». Повторять такой запрос каждые 30 минут не только
бессмысленно: со стороны издателя поток одинаковых 401 с одним ключом выглядит как перебор.
Блокировка живёт В ПАМЯТИ раннера, а не в состоянии на диске — она про «эту сессию канала»,
а не про факт, который надо помнить между запусками; снимается `poke()` (человек нажал
«Проверить сейчас») и любым изменением настроек (человек починил то, на что жаловались).

**4. `enabled` перечитывается перед каждым тиком.** Пользователь включает цикл в UI и ждёт
эффекта без перезапуска хаба. Выключенный цикл — не ошибка, а `status: "disabled"`: красный
значок на том, что человек выключил сам, — худший вид ложной тревоги.

**5. Порядок внутри пробуждения — `revocations` → `patterns` → `releases`.** Отзыв первым
намеренно: если лицензия отозвана, остальные циклы всё равно получат `401 revoked`, и честнее
узнать причину сразу, чем показать пользователю два невнятных отказа и лишь потом настоящую
причину.

**6. `apply_staged` из планировщика не вызывается НИКОГДА** (`SECURITY.md` §4.1 «никакого
тихого действия»). Автоматика доходит ровно до `check` и — по явной настройке
`auto_stage_release` — до `stage`: скачать и проверить подпись можно молча, подменить
исполняемый файл MCP — только по команде человека. См. `_run_releases`.
"""
from __future__ import annotations

import hashlib
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import __version__, context, patterns, releases, revocations
from .backend import BackendClient
from .errors import ChannelError, CompanionError, ContextUnavailable, NotModified
from .state import STATE_FILE_NAME, CompanionState

__all__ = [
    "CYCLES",
    "RUN_ORDER",
    "ACTIONS",
    "JITTER_FRACTION",
    "MAX_WAIT_STEP_SEC",
    "CompanionRunner",
    "build_runner",
    "status_snapshot",
    "available_actions",
]

#: Имена циклов. Совпадают с именами секций состояния (`state.py`) и полей настроек
#: (`CompanionSettings`) — это не совпадение, а контракт: `getattr(settings, cycle)` и
#: `state.mark(cycle, ...)` работают по одному и тому же имени, без таблиц перевода.
CYCLES = ("patterns", "releases", "revocations")

#: Порядок ОДНОГО пробуждения (см. п.5 докстринга модуля) — намеренно не совпадает с
#: порядком объявления.
RUN_ORDER = ("revocations", "patterns", "releases")

#: Явные действия человека. Имена — те же, что у ключей `available_actions`, чтобы UI не
#: переводил «что разрешено» в «что вызвать» через свою таблицу соответствия.
ACTIONS = ("sync_patterns", "check_update", "stage_update", "apply_update",
           "rollback", "refresh_revocations")

#: Доля интервала, на которую срок «гуляет» в обе стороны (см. п.2 докстринга модуля).
JITTER_FRACTION = 0.10

#: Верхняя граница одного шага ожидания. Ровно как `MAX_POLL_STEP_SEC` в поллере хаба и
#: ровно по той же причине: при суточном интервале релизов `stop()` и правка настроек
#: обязаны подхватываться за секунды, а не за сутки.
MAX_WAIT_STEP_SEC = 5.0

#: На сколько секунд разбрасывается ПЕРВЫЙ тик после запуска хаба. Джиттер интервала эту
#: задачу не решает: после массового обновления весь парк стартует одновременно, и первый
#: тик у всех пришёлся бы на одну секунду. Минуты хватает, чтобы не выглядеть наплывом, и
#: она незаметна человеку, только что запустившему хаб.
STARTUP_SPREAD_SEC = 60.0

#: Нижняя граница интервала на случай нуля/мусора в конфиге. Настройки свою границу уже
#: держат (`CompanionCycle.from_dict`), но раннер обязан пережить и конфиг, собранный
#: конструктором в обход неё: без пола фоновый поток превратился бы в busy-loop, долбящий
#: бэкенд издателя.
MIN_INTERVAL_SEC = 60.0

#: Статусы, которые пишут в состояние сами модули циклов. Всё, чего здесь нет (например
#: `never` у ни разу не запускавшегося цикла), раннер трактует как успех: до сюда
#: выполнение доходит только тогда, когда функция цикла вернулась без исключения.
_KNOWN_STATUSES = frozenset({"ok", "skipped", "error"})


# ======================================================================================
# Служебные структуры
# ======================================================================================
@dataclass
class _CycleRuntime:
    """Память раннера об одном цикле. На диск не попадает НИЧЕГО из этого.

    `deadline` — момент следующего запуска по `monotonic`-часам (не по календарным:
    перевод часов и синхронизация времени не должны ни задерживать тик на часы, ни
    устраивать шквал догоняющих запусков).
    """

    deadline: float = 0.0
    delay: float = 0.0
    interval: float = 0.0
    halted: bool = False
    halt_reason: str = ""

    def clear_halt(self) -> None:
        self.halted = False
        self.halt_reason = ""


@dataclass
class _Session:
    """Лицензионный контекст и транспорт, общие на одно пробуждение (см. п.1 модуля)."""

    ctx: object
    client: object


def _outcome(cycle: str, status: str, detail: str, *, result: Optional[dict] = None,
             error: Optional[dict] = None, halted: bool = False) -> dict:
    """Единая форма исхода одного цикла — и для планировщика, и для ручного вызова.

    `error` — это `CompanionError.to_dict()` (машинный `kind`, `retriable`, `user_visible`),
    а не строка: решение «показывать ли это пользователю» принимает UI, и отнимать у него
    поля, по которым оно принимается, нельзя.
    """
    return {
        "cycle": cycle,
        "status": status,
        "detail": detail or "",
        "result": result,
        "error": error,
        "halted": bool(halted),
    }


def _stable_seed(config_path) -> int:
    """Зерно генератора джиттера, стабильное для машины и разное между машинами.

    Берётся от пути конфига: он у каждой установки свой (имя пользователя в `%APPDATA%`),
    но не меняется от перезапуска к перезапуску. Именно постоянство здесь и нужно — иначе
    после каждого рестарта клиент получал бы новую фазу и разброс парка сбрасывался бы
    ровно в тот момент, когда он важнее всего (массовое обновление = массовый рестарт).
    """
    raw = str(config_path or "").encode("utf-8", "replace")
    return int.from_bytes(hashlib.sha256(raw).digest()[:8], "big")


def available_actions(settings, state: Optional[CompanionState] = None) -> dict:
    """Что сейчас разрешено дёрнуть руками — для кнопок UI и для `status()`.

    `state` необязателен намеренно: вызвать `available_actions(settings)` должно быть можно
    там, где состояния под рукой нет (форма настроек). Без состояния `apply_update` и
    `rollback` считаются НЕдоступными — «не знаю» здесь обязано означать «не предлагать»:
    кнопка, которая гарантированно упадёт, хуже отсутствующей.

    Всё гасится главным рубильником `settings.enabled`: выключенный канал не предлагает
    ничего, включая локальные операции, — иначе «канал выключен» переставало бы быть
    правдой.

    Наличие подготовленного обновления проверяется `releases.staged_info` (то есть ФАЙЛОМ
    на диске, а не записью в состоянии): антивирус мог унести `.exe` в карантин, и запись
    без файла — это «нечего применять».
    """
    enabled = bool(getattr(settings, "enabled", False))
    staged = False
    history = False
    if state is not None:
        try:
            staged = releases.staged_info(state) is not None
            history = bool(state.releases.get("history"))
        except (OSError, AttributeError, TypeError):
            # Недоступный/непрочитанный диск не имеет права уронить ответ статуса: в этом
            # случае честно «действие недоступно».
            staged = False
            history = False
    return {
        "sync_patterns": enabled,
        "check_update": enabled,
        "stage_update": enabled,
        "apply_update": enabled and staged,
        "rollback": enabled and history,
        "refresh_revocations": enabled,
    }


# ======================================================================================
# Планировщик
# ======================================================================================
# Переменная окружения-рубильник для автотестов и CI (см. `CompanionRunner.start`).
DISABLE_ENV = "STANDKIT_COMPANION_DISABLED"


def environment_disabled() -> bool:
    """Запрещён ли старт фонового планировщика переменной окружения.

    Пустая строка и `0`/`false`/`no` считаются «не запрещён» — иначе случайно
    выставленная пустая переменная тихо выключила бы канал у пользователя.
    """
    raw = (os.environ.get(DISABLE_ENV) or "").strip().lower()
    return raw not in ("", "0", "false", "no", "off")


class CompanionRunner:
    """Фоновый планировщик трёх циклов канала.

    Все внешние зависимости передаются callable'ами, как в `StatusPoller`: раннер не знает
    ни про HTTP-слой хаба, ни про реальную сеть, и целиком тестируется подставными
    объектами.

    :param config_path: путь к конфигу хаба (`standkit-hub.json`) — источник настроек
        канала. Читается ПЕРЕД каждым тиком, а не запоминается при старте.
    :param state_path: файл состояния; по умолчанию `companion-state.json` рядом с
        конфигом (см. докстринг `state.py`).
    :param settings_loader: `() -> CompanionSettings`. По умолчанию — секция `companion`
        из конфига хаба.
    :param client_factory: `(ctx, settings) -> BackendClient`.
    :param context_resolver: `(settings) -> LicenseContext`.
    :param monotonic: источник монотонного времени (в тестах — управляемые часы).
    :param rng: генератор джиттера; по умолчанию — `random.Random` со стабильным для
        машины зерном. Подменяется в тестах, чтобы срок был воспроизводим.
    """

    def __init__(self, config_path, *, state_path=None, settings_loader=None,
                 client_factory=None, context_resolver=None, monotonic=None,
                 rng=None) -> None:
        self.config_path = Path(config_path)
        self.state_path = (Path(state_path) if state_path is not None
                           else self.config_path.parent / STATE_FILE_NAME)
        self._settings_loader = settings_loader or self._load_settings_from_config
        self._client_factory = client_factory or self._default_client
        self._context_resolver = context_resolver or context.resolve
        self._monotonic = monotonic or time.monotonic
        self._rng = rng if rng is not None else random.Random(_stable_seed(self.config_path))

        self._state = CompanionState.load(self.state_path)

        self._cond = threading.Condition()
        self._stopping = False
        self._poked = False
        self._thread: Optional[threading.Thread] = None

        # Прогоны циклов сериализуются: состояние на диске одно, а звать раннер могут
        # одновременно фоновый поток и HTTP-обработчик хаба (кнопка «Синхронизировать»).
        # `status()` этот замок НЕ берёт намеренно — см. его докстринг.
        self._run_lock = threading.Lock()

        now = self._monotonic()
        # Стартовые сроки — «прямо сейчас»: раннер, поднятый ради одного прогона
        # (`python -m standkit_companion run --once`), обязан выполнить работу, а не
        # ждать. Разброс первого тика добавляет `start()` — он про фоновый режим.
        self._runtime = {cycle: _CycleRuntime(deadline=now) for cycle in CYCLES}

        self._settings = None
        self._settings_key = ""
        # Последний известный исход резолва лицензионного контекста. Хранится, чтобы
        # `status()` мог отвечать про контекст, НЕ запуская CLI на каждый опрос UI.
        self._context_ok = False
        self._context_detail = ""
        self._context_cli: list = []
        # Отказ, случившийся вне отдельного цикла (нечитаемый конфиг, сбой самого
        # планировщика). Наружу уходит только через `status()`.
        self._last_error = ""

    # -- настройки ---------------------------------------------------------------------
    def _load_settings_from_config(self):
        """Секция `companion` конфига хаба.

        Импорт `standkit_hub` отложен внутрь функции сознательно: пакет хаба импортирует
        канал МЯГКО (`try/except ImportError`), и встречный импорт на уровне модуля замкнул
        бы кольцо при старте хаба.
        """
        from standkit_hub.config import HubConfig

        return HubConfig.load(self.config_path).companion

    def settings(self):
        """Свежие настройки канала. Битый конфиг не роняет тик — берутся дефолты.

        Побочный эффект: смена настроек СНИМАЕТ блокировки циклов. Человек, который правит
        настройки, тем самым сообщает, что чинил ровно то, на что канал жаловался, — и
        новую попытку он ждёт немедленно, а не после перезапуска хаба.
        """
        try:
            fresh = self._settings_loader()
        except Exception as exc:  # noqa: BLE001 - фоновый поток не имеет права умереть
            self._last_error = f"настройки канала не прочитаны: {exc}"
            if self._settings is not None:
                return self._settings
            from standkit_hub.config import CompanionSettings

            fresh = CompanionSettings()
        key = repr(fresh.to_dict()) if hasattr(fresh, "to_dict") else repr(fresh)
        if self._settings is not None and key != self._settings_key:
            for runtime in self._runtime.values():
                runtime.clear_halt()
        self._settings = fresh
        self._settings_key = key
        return fresh

    # -- расписание --------------------------------------------------------------------
    def _interval_of(self, settings, cycle: str) -> float:
        cycle_settings = getattr(settings, cycle, None)
        try:
            value = float(getattr(cycle_settings, "interval_sec", 0) or 0)
        except (TypeError, ValueError):
            value = 0.0
        return max(MIN_INTERVAL_SEC, value)

    def _enabled(self, settings, cycle: str) -> bool:
        """Включён ли цикл: главный рубильник И флаг самого цикла."""
        if not bool(getattr(settings, "enabled", False)):
            return False
        return bool(getattr(getattr(settings, cycle, None), "enabled", False))

    def _jittered(self, interval: float) -> float:
        """Интервал со сдвигом ±`JITTER_FRACTION`, посчитанным ЗАНОВО на каждый срок.

        Один сдвиг, вычисленный при старте, задачу не решает: он бы просто переставил
        одинаковые для всего парка тики на одинаковое же новое место.
        """
        factor = 1.0 + self._rng.uniform(-JITTER_FRACTION, JITTER_FRACTION)
        return max(1.0, interval * factor)

    def _schedule(self, cycle: str, settings, now: float) -> None:
        runtime = self._runtime[cycle]
        interval = self._interval_of(settings, cycle)
        delay = self._jittered(interval)
        runtime.interval = interval
        runtime.delay = delay
        runtime.deadline = now + delay

    def _next_wait(self, now: float) -> float:
        """Сколько осталось до ближайшего срока среди всех циклов."""
        return min(runtime.deadline - now for runtime in self._runtime.values())

    # -- сессия ------------------------------------------------------------------------
    def _default_client(self, ctx, settings) -> BackendClient:
        base_url = str(getattr(ctx, "backend_url", "") or "").strip()
        if not base_url:
            raise ContextUnavailable(
                "CLI BPMkit не сообщил адрес бэкенда издателя — канал не знает, куда "
                "обращаться; задайте его в настройках хаба (companion.backend_url)",
                kind="context_unavailable",
                detail="пустой backend_url в лицензионном контексте")
        return BackendClient(base_url, getattr(ctx, "envelope", "") or "")

    def _session(self, settings) -> _Session:
        """Контекст + транспорт на одно пробуждение. Отказ поднимается наверх типизированным.

        Резолв контекста запускает CLI клиентского MCP, поэтому делается ОДИН раз и
        переиспользуется всеми циклами. На кэш внутри `context.resolve` при этом не
        полагаемся: он про экономию запусков процесса, а нам нужно, чтобы все три цикла
        одного пробуждения видели ОДНУ И ТУ ЖЕ картину лицензии, а не две по разные
        стороны истечения TTL.
        """
        ctx = self._context_resolver(settings)
        client = self._client_factory(ctx, settings)
        self._context_ok = True
        self._context_detail = "лицензионный контекст получен от CLI BPMkit"
        self._context_cli = [str(item) for item in (getattr(ctx, "cli", None) or [])]
        return _Session(ctx=ctx, client=client)

    # -- выполнение --------------------------------------------------------------------
    def _run_patterns(self, session, settings) -> dict:
        return patterns.sync(session.client, self._state, session.ctx, settings)

    def _run_releases(self, session, settings) -> dict:
        """Проверка релиза и — по явной настройке — только подготовка.

        ЗАПРЕТ: `releases.apply_staged` отсюда НЕ вызывается и вызываться не должен НИКОГДА:
        подмена исполняемого файла MCP — всегда явное решение человека (`SECURITY.md` §4.1
        «никакого тихого действия»). Автоматический `stage` безопасен ровно потому, что
        останавливается перед этой чертой: скачанный файл лежит в стейджинге, ничего в
        поставке не тронуто, и до нажатия кнопки продолжает работать прежняя версия.
        Отдельный регресс-тест проверяет, что планировщик `apply_staged` не трогает.
        """
        check = releases.check(session.client, self._state, session.ctx)
        staged = None
        if check.get("available") and bool(getattr(settings, "auto_stage_release", False)):
            staged = releases.stage(session.client, self._state, session.ctx,
                                    check.get("target") or "latest")
        return {"check": check, "staged": staged}

    def _run_revocations(self, session, settings) -> dict:
        return revocations.refresh(session.client, self._state, session.ctx)

    def _dispatch(self, cycle: str, session: _Session, settings) -> dict:
        if cycle == "patterns":
            return self._run_patterns(session, settings)
        if cycle == "releases":
            return self._run_releases(session, settings)
        return self._run_revocations(session, settings)

    def _status_from_state(self, cycle: str) -> tuple:
        block = self._state.data.get(cycle) or {}
        status = str(block.get("last_status") or "")
        detail = str(block.get("last_detail") or "")
        # `never` (и любое неизвестное значение) до сюда доходит только если функция цикла
        # вернулась успешно, но состояние не пометила — считаем это успехом, а не сбоем.
        return (status if status in _KNOWN_STATUSES else "ok"), detail

    def _state_stamp(self, cycle: str) -> tuple:
        """Отпечаток последнего исхода цикла в состоянии.

        Нужен, чтобы отличить «цикл пометил состояние сам» от «не пометил». Метка времени
        в состоянии секундной точности, поэтому одного её сравнения мало — в отпечаток
        входят ещё статус и текст.
        """
        block = self._state.data.get(cycle) or {}
        key = "last_run_at" if cycle == "patterns" else "last_check_at"
        return (block.get(key), block.get("last_status"), block.get("last_detail"))

    def _save_state(self) -> None:
        """Сохранение состояния, которое не может стать второй ошибкой поверх первой."""
        try:
            self._state.save()
        except OSError as exc:
            self._last_error = f"состояние канала не сохранено: {exc}"

    def _fail(self, cycle: str, exc: CompanionError) -> dict:
        """Отказ цикла → запись в состояние (+ блокировка, если повтор бессмыслен).

        Исключение наружу не поднимается: тик, который умеет упасть, однажды уронит поток,
        и канал молча перестанет работать целиком. Наружу — только `status()`.
        """
        detail = exc.detail or str(exc)
        self._state.mark(cycle, "error", f"{exc.title()}: {detail}" if detail else exc.title())
        self._save_state()
        runtime = self._runtime[cycle]
        if not exc.retriable:
            runtime.halted = True
            runtime.halt_reason = exc.title()
        _, state_detail = self._status_from_state(cycle)
        return _outcome(cycle, "error", state_detail, error=exc.to_dict(),
                        halted=runtime.halted)

    def _execute(self, cycle: str, session: _Session, settings) -> dict:
        """Один прогон цикла с полным разбором исходов. Не бросает наружу ничего."""
        before = self._state_stamp(cycle)
        try:
            result = self._dispatch(cycle, session, settings)
        except NotModified as exc:
            # 304 — штатное «у вас уже актуальная версия». Модули циклов ловят его сами,
            # но `NotModified` — не-retriable по таблице `KIND_TITLES`, и просочись он
            # сюда как обычная ошибка, цикл встал бы намертво из-за УСПЕШНОГО исхода.
            self._state.mark(cycle, "ok", exc.title())
            self._save_state()
            return _outcome(cycle, "ok", exc.title())
        except CompanionError as exc:
            return self._fail(cycle, exc)
        except Exception as exc:  # noqa: BLE001 - см. докстринг `_fail`
            # Незнакомое исключение (битый диск, чужая библиотека) приводится к тому же
            # типизированному виду: у UI один разбор исходов, а не два.
            return self._fail(cycle, ChannelError(
                f"Непредвиденный сбой цикла «{cycle}»", kind="unknown",
                detail=f"{type(exc).__name__}: {exc}"))
        if self._state_stamp(cycle) == before and before[1] != "ok":
            # Цикл вернулся успешно, но состояние не пометил (штатные модули помечают
            # всегда — это страховка от будущего цикла, который забудет). Снять прошлый
            # отказ обязаны мы: иначе UI показывал бы ошибку ПОСЛЕ успешного тика, и
            # пользователь чинил бы то, что уже починилось.
            self._state.mark(cycle, "ok", "")
            self._save_state()
        status, detail = self._status_from_state(cycle)
        return _outcome(cycle, status, detail, result=result)

    # -- публичные прогоны --------------------------------------------------------------
    def run_cycle(self, cycle: str, *, force: bool = False) -> dict:
        """Прогнать один цикл немедленно.

        `force=True` — ручное действие человека из UI: оно сильнее флага `enabled` самого
        цикла и снимает блокировку. Обоснование: `enabled` — это про РАСПИСАНИЕ («ходить ли
        по таймеру»), а не про запрет операции; безопасность релизов держится не на нём, а
        на fail-closed проверке подписи внутри `releases.stage`, которую force не отключает
        и отключить не может. А вот главный рубильник `settings.enabled` force НЕ
        перебивает: «канал выключен» обязано означать ровно это, иначе выключить его
        по-настоящему нельзя.
        """
        cycle = self._require_cycle(cycle)
        settings = self.settings()
        with self._run_lock:
            now = self._monotonic()
            # Срок сдвигается в любом случае, включая отказ: иначе ручной прогон оставил бы
            # цикл «просроченным», и фоновый поток немедленно повторил бы ту же работу.
            self._schedule(cycle, settings, now)
            return self._run_one(cycle, settings, session=None, force=force)

    def run_due(self) -> dict:
        """Прогнать все циклы, у которых наступил срок, в порядке `RUN_ORDER`.

        Возвращает `{"order": [...], "results": {cycle: <исход>}, "context": {...}}`.
        Контекст резолвится ОДИН раз и только если есть кому его использовать: при всех
        выключенных циклах пробуждение не должно запускать CLI впустую.
        """
        settings = self.settings()
        with self._run_lock:
            now = self._monotonic()
            due = [cycle for cycle in RUN_ORDER if self._runtime[cycle].deadline <= now]
            for cycle in due:
                self._schedule(cycle, settings, now)

            runnable = [cycle for cycle in due
                        if self._enabled(settings, cycle) and not self._runtime[cycle].halted]
            session = None
            session_error: Optional[CompanionError] = None
            if runnable:
                try:
                    session = self._session(settings)
                except CompanionError as exc:
                    # Контекста нет — это НЕ смерть пробуждения: каждый цикл честно
                    # помечается своей причиной (`context_unavailable`/`no_license`), а
                    # поток продолжает жить.
                    session_error = exc
                    self._context_ok = False
                    self._context_detail = exc.title()
                except Exception as exc:  # noqa: BLE001 - поток не имеет права умереть
                    session_error = ChannelError(
                        "Лицензионный контекст не получен", kind="context_unavailable",
                        detail=f"{type(exc).__name__}: {exc}")
                    self._context_ok = False
                    self._context_detail = session_error.title()

            results = {}
            for cycle in due:
                results[cycle] = self._run_one(cycle, settings, session=session,
                                               session_error=session_error)
            return {
                "order": list(due),
                "results": results,
                "context": {"ok": self._context_ok, "detail": self._context_detail},
            }

    def _run_one(self, cycle: str, settings, *, session: Optional[_Session],
                 session_error: Optional[CompanionError] = None,
                 force: bool = False) -> dict:
        """Общая часть `run_cycle` и `run_due`: проверки, затем выполнение."""
        runtime = self._runtime[cycle]
        if not bool(getattr(settings, "enabled", False)):
            return _outcome(cycle, "disabled", "Канал обновлений выключен в настройках")
        if not self._enabled(settings, cycle) and not force:
            return _outcome(cycle, "disabled", "Цикл выключен в настройках")
        if force:
            runtime.clear_halt()
        if runtime.halted:
            return _outcome(cycle, "halted", runtime.halt_reason, halted=True)
        if session_error is not None:
            return self._fail(cycle, session_error)
        if session is None:
            try:
                session = self._session(settings)
            except CompanionError as exc:
                self._context_ok = False
                self._context_detail = exc.title()
                return self._fail(cycle, exc)
            except Exception as exc:  # noqa: BLE001 - см. докстринг `_fail`
                self._context_ok = False
                self._context_detail = "лицензионный контекст не получен"
                return self._fail(cycle, ChannelError(
                    "Лицензионный контекст не получен", kind="context_unavailable",
                    detail=f"{type(exc).__name__}: {exc}"))
        return self._execute(cycle, session, settings)

    def run_action(self, action: str, *, version: Optional[str] = None) -> dict:
        """Явное действие человека (кнопка UI, команда CLI).

        Отдельный вход, а не `run_cycle`, потому что два действия — `apply_update` и
        `rollback` — планировщику недоступны В ПРИНЦИПЕ (`SECURITY.md` §4.1), а
        `stage_update` умеет адресоваться к конкретной версии. Ошибки отсюда, в отличие от
        тика, поднимаются наверх типизированным `CompanionError`: их читает человек,
        который прямо сейчас смотрит на результат своей команды.

        Порядок проверок у локальных действий обратный привычному: сперва «есть ли что
        применять/куда откатываться» и только потом резолв контекста. Иначе человек без
        подготовленного обновления получал бы жалобу на отсутствие CLI BPMkit — правдивую,
        но не имеющую отношения к его вопросу.

        Главный рубильник `settings.enabled` здесь НЕ проверяется, в отличие от
        `run_cycle`. Он гасит расписание и кнопки UI (`available_actions`), но не должен
        запрещать ручной запуск из CLI: выключенный канал обязан оставаться диагностируемым
        — иначе на вопрос «почему не работает» нельзя ответить, не включив его сначала.
        """
        if action not in ACTIONS:
            raise ValueError(f"неизвестное действие канала: {action!r}")
        settings = self.settings()
        with self._run_lock:
            if action == "apply_update":
                if releases.staged_info(self._state) is None:
                    raise ChannelError(
                        "Подготовленного обновления нет — сначала выполните подготовку",
                        kind="nothing_staged")
                return releases.apply_staged(self._state, self._session(settings).ctx)
            if action == "rollback":
                if not (self._state.releases.get("history") or []):
                    raise ChannelError(
                        "Откатываться не на что: канал ещё не применял обновлений",
                        kind="nothing_to_rollback")
                return releases.rollback(self._state, self._session(settings).ctx,
                                         version=version)
            session = self._session(settings)
            if action == "sync_patterns":
                return patterns.sync(session.client, self._state, session.ctx, settings)
            if action == "check_update":
                return releases.check(session.client, self._state, session.ctx)
            if action == "stage_update":
                return releases.stage(session.client, self._state, session.ctx,
                                      version or "latest")
            return revocations.refresh(session.client, self._state, session.ctx)

    @staticmethod
    def _require_cycle(cycle: str) -> str:
        if cycle not in CYCLES:
            raise ValueError(f"неизвестный цикл канала: {cycle!r} (ожидалось {CYCLES})")
        return cycle

    # -- статус ---------------------------------------------------------------------------
    def status(self) -> dict:
        """Карточка канала для `/api/companion/status`, UI и CLI.

        ЗАПРЕТ: Лицензионный конверт сюда не попадает НИКОГДА — ни целиком, ни фрагментом.
        Ответ статуса ходит по HTTP в браузер и оседает в логах; конверт живёт в
        secretstore MCP и наружу не выходит вовсе (регресс-тест проверяет это буквально,
        поиском подстроки в сериализованном ответе).

        Контекст здесь НЕ резолвится: `status()` дёргает поллер UI раз в несколько секунд, а
        резолв — это ЗАПУСК процесса CLI с таймаутом до 30 секунд. Отдаётся последний
        известный исход; пока тиков не было — честное «ещё не запрашивался» плюс дешёвая
        файловая проверка, есть ли вообще рядом CLI.

        Замок прогонов тоже не берётся: статус обязан отвечать мгновенно и во время
        длинного сетевого тика, а чтение состояния под GIL безопасно (только `.get`, без
        итерирования по изменяемым словарям).
        """
        # Настройки читаются СВЕЖИМИ, а не берутся с последнего тика: иначе UI после
        # правки формы до получаса показывал бы старые «включён/интервал», то есть врал бы
        # человеку про его же только что сохранённое решение.
        settings = self.settings()
        running = self.is_running()
        now = self._monotonic()

        cycles = {}
        for cycle in CYCLES:
            runtime = self._runtime[cycle]
            last_status, last_detail = self._status_from_state(cycle)
            block = self._state.data.get(cycle) or {}
            cycles[cycle] = {
                "enabled": self._enabled(settings, cycle),
                "interval_sec": int(self._interval_of(settings, cycle)),
                # Пока планировщик не поднят, следующего запуска не существует — `None`
                # честнее нуля, который читался бы как «прямо сейчас».
                "next_run_in_sec": (max(0.0, round(runtime.deadline - now, 1))
                                    if running else None),
                "last_status": str(block.get("last_status") or "never"),
                "last_detail": last_detail,
                "halted": bool(runtime.halted),
                "halt_reason": runtime.halt_reason,
            }

        return {
            "running": running,
            # Явный признак «выключено средой, а не пользователем»: иначе `running: false`
            # в CI читается как поломка, хотя это осознанный рубильник conftest.py.
            "environment_disabled": environment_disabled(),
            "edition": "companion",
            "companion_version": __version__,
            "settings": settings.to_dict(),
            "state": self._state.summary(),
            "cycles": cycles,
            "context": self._context_status(settings),
            "actions": available_actions(settings, self._state),
            # Сбой вне отдельного цикла (нечитаемый конфиг, отказ диска). Ключ есть всегда
            # и пуст в норме: единственный способ узнать о таком отказе снаружи — статус,
            # потому что фоновый поток не имеет права упасть с ним наружу.
            "last_error": self._last_error,
        }

    def _context_status(self, settings) -> dict:
        cli = list(self._context_cli)
        if not cli:
            try:
                cli = [str(item) for item in (context.find_cli(settings) or [])]
            except Exception:  # noqa: BLE001 - статус не имеет права упасть из-за путей
                cli = []
        detail = self._context_detail
        if not detail:
            detail = ("лицензионный контекст ещё не запрашивался" if cli else
                      "рядом не найден CLI BPMkit — укажите путь в настройках канала")
        return {"ok": bool(self._context_ok), "detail": detail, "cli": cli}

    # -- жизненный цикл ------------------------------------------------------------------
    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def start(self) -> None:
        """Поднять фоновый поток (daemon). Повторный вызов — no-op, как у поллера хаба.

        Переменная окружения `STANDKIT_COMPANION_DISABLED` полностью запрещает старт. Она
        нужна не пользователю, а автотестам и CI: конфиг по умолчанию включает цикл
        паттернов, поэтому КАЖДЫЙ хаб, поднятый тестом на машине с установленной платной
        редакцией и живой лицензией, иначе завёл бы настоящий планировщик — и при неудачном
        жребии разброса реально сходил бы к бэкенду издателя посреди прогона тестов.
        Гасить это правкой конфига в каждом тестовом помощнике — договорённость, которую
        забудут; переменная окружения выключает канал одним местом (корневой `conftest.py`).
        """
        if environment_disabled():
            return
        if self.is_running():
            return
        settings = self.settings()
        now = self._monotonic()
        for cycle in CYCLES:
            runtime = self._runtime[cycle]
            interval = self._interval_of(settings, cycle)
            # Разброс ПЕРВОГО тика (см. `STARTUP_SPREAD_SEC`): парк, обновившийся разом,
            # стартует разом, и без этого пришёл бы к издателю одной секундой.
            spread = self._rng.uniform(0.0, min(interval, STARTUP_SPREAD_SEC))
            runtime.interval = interval
            runtime.delay = spread
            runtime.deadline = now + spread
        with self._cond:
            self._stopping = False
            self._poked = False
        self._thread = threading.Thread(target=self._loop, name="bpmkit-companion",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Попросить поток остановиться и подождать его не дольше `timeout`.

        Зовётся из `server_close()` хаба. Поток daemon, поэтому даже застрявший на сетевом
        таймауте тик не удержит процесс — но ждём мы его честно, чтобы не оборвать запись
        состояния на середине.
        """
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None

    def poke(self, cycle: Optional[str] = None) -> None:
        """Не досыпать интервал: прогнать цикл (или все) на ближайшем обороте.

        Заодно СНИМАЕТ блокировку не-retriable отказа — это и есть штатный выход из неё:
        человек нажал «Проверить сейчас», значит он вмешался в то, на что канал жаловался
        (ввёл ключ, поправил путь), и ждёт новой попытки.
        """
        targets = (self._require_cycle(cycle),) if cycle else CYCLES
        now = self._monotonic()
        for name in targets:
            runtime = self._runtime[name]
            runtime.clear_halt()
            runtime.deadline = now
        with self._cond:
            self._poked = True
            self._cond.notify_all()

    def _safe_run_due(self) -> None:
        """`run_due` под страховкой: фоновый поток не имеет права умереть ни от чего.

        Внутренние отказы циклов `run_due` разбирает сам; сюда доходит только то, что
        сломалось вокруг них (чтение конфига, отказ диска при сохранении состояния). Такой
        отказ становится строкой `last_error` в статусе — молчать о нём нельзя, падать
        из-за него тоже.
        """
        try:
            self.run_due()
        except Exception as exc:  # noqa: BLE001 - см. докстринг
            self._last_error = f"{type(exc).__name__}: {exc}"

    def _loop(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
            self._safe_run_due()
            with self._cond:
                while True:
                    if self._stopping:
                        return
                    if self._poked:
                        self._poked = False
                        break
                    left = self._next_wait(self._monotonic())
                    if left <= 0:
                        break
                    # Шаг ожидания ограничен сверху, чтобы поток замечал остановку и
                    # правку интервала даже при суточном периоде цикла релизов.
                    self._cond.wait(min(MAX_WAIT_STEP_SEC, left))


# ======================================================================================
# Фасады
# ======================================================================================
def build_runner(config_path, **kwargs) -> CompanionRunner:
    """Собрать (но НЕ запускать) планировщик канала.

    Отдельная функция нужна вызывающим, которые импортируют пакет мягко: хабу и CLI
    достаточно знать одно имя, а не класс с семью точками расширения. Запуск оставлен
    вызывающему намеренно — хаб поднимает поток только после успешного bind, чтобы при
    неудачном старте не оставить висящий фоновый поток.
    """
    return CompanionRunner(config_path, **kwargs)


def status_snapshot(config_path, *, state_path=None) -> dict:
    """Карточка канала БЕЗ поднятого планировщика — та же форма, что у `status()`.

    Нужна двум вызывающим: хабу, когда канал выключен настройкой (UI обязан показать
    почему, а не пустоту), и CLI `status`, который живёт ровно один вызов. Состояние
    читается с диска, `running` — всегда `False`, сроков следующего запуска нет (их некому
    соблюдать). Отсутствие конфига и состояния — штатная ситуация первого запуска, а не
    ошибка: обе загрузки best-effort.
    """
    return CompanionRunner(config_path, state_path=state_path).status()
