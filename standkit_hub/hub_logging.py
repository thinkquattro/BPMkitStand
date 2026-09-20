# -*- coding: utf-8 -*-
"""
Лог диспетчера в ФАЙЛ (GAP-384) — STDLIB-ONLY, как и весь standkit_hub.

ЗАЧЕМ ЭТОТ МОДУЛЬ ВООБЩЕ ПОЯВИЛСЯ. Инцидент 18.09.2026 20:56: диспетчер вышел
сам (автовыход по простою при живых стендах, GAP-383), три стенда осиротели,
владелец увидел на экране только ошибку связи. Разбирать было нечего.

Причина слепоты структурная, а не случайная: ярлык запускает диспетчер через
``pythonw.exe`` — процесс БЕЗ консоли, у которого ``sys.stderr`` не подключён
никуда (а на Windows он может быть и ``None``). Все объяснения диспетчера о
том, почему он завершается — «простой N минут», «получен стоп-запрос»,
«перехват порта» — были написаны через ``print(..., file=sys.stderr)``, то
есть существовали ровно на время своего вывода и никуда не попадали.

Отсюда правило, которое этот модуль обслуживает: **всё, что объясняет
завершение процесса или необработанный отказ, пишется сюда, а не в stderr.**
Лог — единственный свидетель для процесса, у которого нет ни окна, ни консоли,
ни того, кто смотрел бы на них в 20:56.

Ротация обязательна и не обсуждается: забытый диспетчер живёт неделями, и
неограниченный файл однажды становится второй аварией поверх первой.

Настройка логирования НЕ ИМЕЕТ ПРАВА уронить старт. Диспетчер без лога хуже
диспетчера с логом, но несравнимо лучше не запустившегося: любая беда при
создании каталога/файла — тихий отказ с ``None``, логгер при этом остаётся
рабочим объектом, и все вызовы выше по коду продолжают быть безопасными.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Optional

# Имя логгера — общее для всего диспетчера: и сервер, и поллер, и __main__
# пишут в один файл, потому что разбирают их вместе («что было в 20:56»), а не
# по отдельности.
LOGGER_NAME = "standkit_hub"

LOG_FILE_NAME = "hub.log"

#: Шаблон ЗАПАСНОГО файла лога — на случай, когда общий повернуть не удалось
#: (GAP-414, см. `GuardedRotatingFileHandler`). pid в имени делает файл
#: собственностью ОДНОГО процесса: повернуть его больше некому помешать.
LOG_FILE_PID_TEMPLATE = "hub.{pid}.log"

# 2 МБ × 5 — заведомо больше любого разумного разбора и заведомо меньше
# «файл съел диск у человека, который про диспетчер забыл».
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"

#: Переменная окружения, которой уровень лога поднимают ДО DEBUG (GAP-411).
#:
#: ЗАЧЕМ. Сторож простоя пишет факты каждого своего тика на уровне DEBUG — это и есть
#: ответ на вопрос «почему диспетчер не вышел», ради которого запись заводилась. Но в
#: exe-поставке уровень был прибит к INFO в коде: тики существовали только в тестах, а
#: у человека, который наблюдает проблему на своей машине, не было НИКАКОГО способа их
#: увидеть — то есть диагностика была написана для всех, кроме того, кому она нужна.
#:
#: Переменная, а не настройка в ``standkit-hub.json``: уровень лога нужен ровно на время
#: разбора одного случая, и просить человека править конфиг (а потом не забыть вернуть)
#: — это лишний способ оставить боевой диспетчер в DEBUG навсегда. Переменная исчезает
#: вместе с сеансом, в котором её задали.
LOG_LEVEL_ENV = "STANDKIT_HUB_LOG_LEVEL"

DEFAULT_LEVEL = logging.INFO


def level_from_env(default: int = DEFAULT_LEVEL, environ=None) -> int:
    """Уровень лога из ``STANDKIT_HUB_LOG_LEVEL``: имя (``DEBUG``) либо число (``10``).

    Мусор в переменной — ``default`` и НИКАКОГО отказа: диспетчер, не запустившийся из-за
    опечатки в имени уровня лога, — худший из возможных исходов настройки логирования."""
    raw = (environ if environ is not None else os.environ).get(LOG_LEVEL_ENV)
    if raw is None:
        return default
    value = str(raw).strip()
    if not value:
        return default
    if value.isdigit():
        return int(value)
    named = logging.getLevelName(value.upper())
    return named if isinstance(named, int) else default


#: Что случилось с ротацией за жизнь процесса (GAP-414). Читается `rotation_state()`;
#: тесты и разбор инцидента смотрят сюда, а не в stderr, которого у диспетчера нет.
_ROTATION_STATE = {"failures": 0, "detail": "", "fallback_path": None}


def rotation_state() -> dict:
    """Копия состояния ротации: {"failures": int, "detail": str, "fallback_path": Path|None}.

    Нужна ровно потому, что `logging` по построению ГЛУШИТ отказ обработчика
    (`Handler.handleError` пишет в stderr, а у процесса под `pythonw.exe`
    stderr нет). Без этого счётчика «ротация не сработала» невозможно ни
    увидеть, ни проверить тестом — а именно так дефект GAP-414 и прожил.
    """
    return dict(_ROTATION_STATE)


def reset_rotation_state() -> None:
    """Обнуляет счётчик (тесты; повторная настройка в одном процессе)."""
    _ROTATION_STATE.update({"failures": 0, "detail": "", "fallback_path": None})


class GuardedRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Ротация, которая НЕ МОЖЕТ провалиться молча (GAP-414).

    ЗАЧЕМ. Диспетчер перезапускается с повышением прав (`elevated`-путь), и в
    окне перезапуска файл `hub.log` держат ДВА процесса. На Windows
    переименование занятого файла невозможно, поэтому `doRollover()` падает
    `OSError`; `logging` ловит это в `handleError` и пишет в stderr — которого
    у процесса под `pythonw.exe` нет. Наружу: ротация отказала, никто не узнал,
    файл растёт без предела — то есть отказала ровно та гарантия, ради которой
    модуль и написан («неограниченный файл однажды становится второй аварией
    поверх первой»).

    ЧТО ДЕЛАЕМ ВМЕСТО МОЛЧАНИЯ. Отказ считается (`rotation_state()`), И процесс
    переходит на СВОЙ файл `hub.<pid>.log`: pid в имени снимает саму причину
    конфликта — этот файл держит один владелец, и повернуть его больше некому
    помешать. Причина переключения пишется ПЕРВОЙ СТРОКОЙ в новый файл, иначе
    разбирающий инцидент не поймёт, почему записи оборвались в одном файле и
    продолжились в другом.

    Второй отказ (не удалось даже открыть запасной файл) не роняет процесс:
    логирование остаётся без файла, но диспетчер живёт — приоритет тот же, что
    у `setup_logging`.
    """

    def doRollover(self) -> None:  # noqa: N802 - имя из stdlib
        try:
            super().doRollover()
        except OSError as exc:
            _ROTATION_STATE["failures"] += 1
            _ROTATION_STATE["detail"] = str(exc)
            self._switch_to_pid_file(exc)

    def _switch_to_pid_file(self, exc: BaseException) -> None:
        fallback = Path(self.baseFilename).parent / LOG_FILE_PID_TEMPLATE.format(pid=os.getpid())
        if Path(self.baseFilename) == fallback:
            # Уже на своём файле, и он тоже не повернулся — дальше идти некуда;
            # факт посчитан выше, продолжаем писать в него же.
            return
        try:
            if self.stream:
                self.stream.close()
                self.stream = None  # type: ignore[assignment]
            self.baseFilename = str(fallback)
            self.stream = self._open()
        except OSError:
            # Запасной файл не открылся — остаёмся без файла, но процесс живёт.
            self.stream = None  # type: ignore[assignment]
            return
        _ROTATION_STATE["fallback_path"] = fallback
        log = logger()
        setattr(log, "_standkit_log_path", fallback)
        record = logging.LogRecord(
            LOGGER_NAME, logging.WARNING, __file__, 0,
            "ротация общего файла лога не удалась (%s) — этот процесс (pid %s) "
            "продолжает писать в собственный файл %s; общий файл держит другой "
            "процесс диспетчера (перезапуск с повышением прав)",
            (exc, os.getpid(), fallback), None)
        try:
            self.stream.write(self.format(record) + self.terminator)
            self.flush()
        except Exception:  # noqa: BLE001 - объяснение не имеет права уронить запись
            pass


def pid_log_path(explicit_dir: "Optional[Path | str]" = None) -> Path:
    """Путь запасного (pid-именованного) файла лога — см. `GuardedRotatingFileHandler`."""
    return resolve_log_dir(explicit_dir) / LOG_FILE_PID_TEMPLATE.format(pid=os.getpid())


def logger() -> logging.Logger:
    """Логгер диспетчера. Всегда возвращает рабочий объект — см. докстринг модуля."""
    return logging.getLogger(LOGGER_NAME)


def resolve_log_dir(explicit: "Optional[Path | str]" = None) -> Path:
    """
    Каталог лога. Явный аргумент > ``%LOCALAPPDATA%`` > платформенный дефолт.

    Дефолт ОБЯЗАН быть при любом состоянии окружения: «не смогли определить
    каталог» здесь означает «следующий инцидент снова разбирать нечем».
    Поэтому последний фолбэк — домашний каталог, который есть всегда.
    """
    if explicit:
        return Path(explicit).expanduser()

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "BPMkit" / "logs"

    if sys.platform == "win32":
        # Windows без LOCALAPPDATA — экзотика (служба, урезанный профиль), но
        # молчать в ней нельзя ровно так же, как и в обычном сеансе.
        return Path.home() / "AppData" / "Local" / "BPMkit" / "logs"

    # POSIX: тот же смысл, что у %LOCALAPPDATA% — состояние приложения.
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / "BPMkit" / "logs"
    return Path.home() / ".local" / "state" / "BPMkit" / "logs"


def resolve_log_path(explicit_dir: "Optional[Path | str]" = None) -> Path:
    """Полный путь файла лога (``<каталог>/hub.log``)."""
    return resolve_log_dir(explicit_dir) / LOG_FILE_NAME


def setup_logging(*, log_dir: "Optional[Path | str]" = None,
                  level: "Optional[int]" = None,
                  force: bool = False) -> Optional[Path]:
    """
    Подключает ротируемый файловый обработчик. Возвращает путь лога или
    ``None``, если настроить не удалось (старт при этом НЕ прерывается).

    ``level=None`` (по умолчанию) — уровень берётся из ``STANDKIT_HUB_LOG_LEVEL``
    (см. ``level_from_env``), иначе INFO. Явный аргумент сильнее переменной: тесты
    задают уровень сами и не должны зависеть от окружения машины.

    ``force`` — пересобрать обработчики (нужен тестам и повторному запуску в
    одном процессе). Без него повторный вызов идемпотентен: второй файловый
    обработчик означал бы дублирование каждой строки.
    """
    if level is None:
        level = level_from_env()
    log = logger()
    if force:
        reset_logging()
    elif any(isinstance(h, logging.handlers.RotatingFileHandler) for h in log.handlers):
        return getattr(log, "_standkit_log_path", None)

    log.setLevel(level)
    # Диспетчер — не библиотека: свои записи он не отдаёт наверх, где их
    # подхватил бы чей-нибудь basicConfig и снова увёл в мёртвый stderr.
    log.propagate = False

    path = resolve_log_path(log_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = GuardedRotatingFileHandler(
            path, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8")
    except OSError:
        # Каталог занят файлом, нет прав, диск полон — молча остаёмся без
        # файла. Ронять из-за лога процесс, ради живучести которого лог и
        # заводился, было бы прямым противоречием.
        return None

    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    log.addHandler(handler)

    # Консоль — ТОЛЬКО если она есть (обычный python.exe при отладке). Под
    # pythonw.exe ``sys.stderr`` бывает ``None``, и StreamHandler(None) сам
    # стал бы источником отказов в каждой записи.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(logging.Formatter(_LOG_FORMAT))
        log.addHandler(stream)

    setattr(log, "_standkit_log_path", path)
    return path


def current_log_path() -> Optional[Path]:
    """Путь активного лога или ``None`` — то, что показывает ``self_check``."""
    return getattr(logger(), "_standkit_log_path", None)


def reset_logging() -> None:
    """Снимает обработчики (тесты; повторная настройка в одном процессе)."""
    log = logger()
    for handler in list(log.handlers):
        log.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 - уборка не имеет права падать
            pass
    if hasattr(log, "_standkit_log_path"):
        delattr(log, "_standkit_log_path")
