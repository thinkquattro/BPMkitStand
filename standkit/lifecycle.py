"""
Жизненный цикл стенда: start/stop/restart поверх standkit.platform.

Хранит pid запущенного стенда в per-stand pidfile в рабочей папке (по умолчанию
``<домашняя папка пользователя>/.standkit/run/<имя стенда>.pid``), чтобы между
разными вызовами (в т.ч. из другого процесса Python — например, из
standkit_agent) можно было понять, жив ли стенд и как его остановить.

Работает только для ``transport == "local"``. Для ``transport == "agent"``
вызывающая сторона (GUI/клиент) должна ходить по HTTP к соответствующему
standkit_agent — этот модуль намеренно не содержит сетевого кода (см.
standkit_gui.client).

Стенд, поднятый МИМО диспетчера (руками из консоли, скриптом, чужой сессией),
pidfile не имеет — раньше это означало «остановить нечем». Теперь есть
усыновление (см. ``standkit.adopt``): ``stop`` ищет владельца порта, валидирует
его и — ТОЛЬКО с явного согласия вызывающей стороны (``force=True``) — пишет
pidfile, после чего дальше работает прежний код без изменений. Без согласия
бросается ``AdoptionRequired`` с найденным кандидатом: молча убивать процесс,
найденный по порту, нельзя ни при каких условиях.

Остаточные пункты следующих итераций (polling готовности после start,
блокировка pidfile от гонки) — см. docs/ARCHITECTURE.md, раздел «Что уже
реализовано в каркасе vs TODO».
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Optional

from standkit import platform as _platform
from standkit.models import HostKind, Stand, Transport

_DEFAULT_RUN_DIR = Path.home() / ".standkit" / "run"
_DEFAULT_LOG_DIR = Path.home() / ".standkit" / "logs"

# Пауза после spawn_hidden() перед проверкой "жив ли процесс" — типичный
# симптом "тихого" провала старта: неверный dll/аргументы, .NET-хост пишет
# ошибку в лог и завершается за доли секунды, а spawn_hidden() успевает
# вернуть валидный pid до этого момента. Вынесено в параметр start(), чтобы
# тесты могли передать 0 и не ждать реальное время.
_DEFAULT_STARTUP_CHECK_DELAY = 0.6


class LifecycleError(Exception):
    """Ошибки управления жизненным циклом стенда."""


class AdoptionUnavailable(LifecycleError):
    """
    Усыновить стенд нечем: владелец порта не найден либо не прошёл валидацию
    (см. ``standkit.adopt.validate_candidate``).

    Отдельный тип нужен вызывающей стороне, чтобы отличить «кандидата нет»
    (хаб отвечает 404 на ``POST /api/stand/<name>/adopt``) от «кандидат есть,
    но нужно подтверждение» (``AdoptionRequired``).
    """


class AdoptionRequired(LifecycleError):
    """
    Найден валидный кандидат на усыновление — требуется ЯВНОЕ согласие
    пользователя, прежде чем диспетчер возьмёт процесс под управление и
    остановит его.

    ``candidate`` (``standkit.adopt.AdoptCandidate``) прокидывается наверх,
    чтобы UI показал, что именно предлагается убить: pid, образ, каталог.
    """

    def __init__(self, stand_name: str, candidate):
        self.stand_name = stand_name
        self.candidate = candidate
        super().__init__(
            f"стенд '{stand_name}' запущен вне диспетчера. Найден процесс "
            f"{candidate.describe()}. Требуется подтверждение, чтобы взять его "
            "под управление и остановить."
        )


def pidfile_path(stand: Stand, run_dir: Optional[Path] = None) -> Path:
    """Путь к pid-файлу стенда (по умолчанию — общая рабочая папка standkit пользователя)."""
    run_dir = Path(run_dir) if run_dir else _DEFAULT_RUN_DIR
    return run_dir / f"{stand.name}.pid"


def log_path(stand: Stand, log_dir: Optional[Path] = None) -> Path:
    """Путь к лог-файлу стенда (см. standkit.logs.tail)."""
    log_dir = Path(log_dir) if log_dir else _DEFAULT_LOG_DIR
    return log_dir / f"{stand.name}.log"


def _read_pid(pf: Path) -> Optional[int]:
    if not pf.exists():
        return None
    try:
        return int(pf.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


def _write_pid(pf: Path, pid: int) -> None:
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(str(pid), encoding="utf-8")


def _require_local(stand: Stand) -> None:
    if stand.transport != Transport.LOCAL:
        raise LifecycleError(
            f"Стенд '{stand.name}' имеет transport={stand.transport.value!r} — "
            "управление headless-процессом доступно только для transport='local' "
            "(для 'agent' используйте HTTP-клиент standkit_gui.client к соответствующему агенту)"
        )


def _resolve_dotnet(dotnet: str) -> str:
    """
    Резолвит команду ``dotnet`` записи стенда в конкретный исполняемый путь.

    - Если ``dotnet`` уже указывает на существующий файл (абсолютный или
      относительный путь) — используется как есть, PATH не требуется.
    - Иначе (голое имя вроде ``"dotnet"``, дефолт по схеме ``Stand``) ищется
      в PATH через ``shutil.which``.

    Раньше отсутствие ``dotnet`` в PATH приводило к "тихому" провалу —
    subprocess.Popen падал где-то внутри spawn_hidden() с малопонятной
    OSError, либо (на некоторых системах) вообще не поднимал процесс без
    видимой ошибки в UI хаба. Явная проверка здесь даёт понятный текст ДО
    попытки спавна.
    """
    candidate = Path(dotnet)
    if candidate.exists():
        return str(candidate)
    resolved = shutil.which(dotnet)
    if resolved is None:
        raise LifecycleError(
            f"dotnet не найден в PATH: {dotnet!r} — установите .NET SDK/Runtime "
            "или укажите полный путь в поле 'dotnet' записи реестра стенда"
        )
    return resolved


def start(
    stand: Stand,
    *,
    run_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    startup_check_delay: float = _DEFAULT_STARTUP_CHECK_DELAY,
) -> Optional[int]:
    """
    Запускает стенд, если он ещё не запущен. Диспетчер по ``stand.host_kind``
    (см. ADR-0001, docs/adr/0001-hosting-backends.md): kestrel — ТЕКУЩИЙ код
    (см. ``_kestrel_start``) без изменений; iis/docker/k8s — соответствующий
    бэкенд ``standkit.hosting``. ``startup_check_delay`` применяется только к
    kestrel-ветке.

    Возвращает pid процесса для kestrel, либо None/pid-эквивалент бэкенда для
    iis/docker/k8s (см. ``standkit.hosting.HostingBackend.start``).
    """
    _require_local(stand)
    if stand.host_kind == HostKind.KESTREL:
        return _kestrel_start(
            stand, run_dir=run_dir, log_dir=log_dir, startup_check_delay=startup_check_delay
        )
    from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

    backend = _hosting.get_backend(stand)
    return backend.start(stand, run_dir=run_dir, log_dir=log_dir)


def stop(stand: Stand, *, run_dir: Optional[Path] = None, force: bool = False) -> bool:
    """
    Останавливает стенд, если он запущен. Диспетчер по ``stand.host_kind`` (см. ``start``).

    ``force=True`` — согласие пользователя на усыновление стенда, поднятого вне
    диспетчера (см. ``_kestrel_stop``). Для iis/docker/k8s флаг не нужен и
    игнорируется: там объект управления глобальный (сайт IIS, контейнер,
    деплоймент), а не дочерний процесс, и остановка работает независимо от
    того, кто стенд поднял.
    """
    _require_local(stand)
    if stand.host_kind == HostKind.KESTREL:
        return _kestrel_stop(stand, run_dir=run_dir, force=force)
    from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

    backend = _hosting.get_backend(stand)
    return backend.stop(stand, run_dir=run_dir)


def restart(
    stand: Stand,
    *,
    run_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    force: bool = False,
) -> Optional[int]:
    """Останавливает (если жив) и заново запускает стенд. Диспетчер по ``stand.host_kind``."""
    _require_local(stand)
    if stand.host_kind == HostKind.KESTREL:
        return _kestrel_restart(stand, run_dir=run_dir, log_dir=log_dir, force=force)
    from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

    backend = _hosting.get_backend(stand)
    return backend.restart(stand, run_dir=run_dir, log_dir=log_dir)


def adopt(stand: Stand, *, run_dir: Optional[Path] = None):
    """
    Берёт под управление стенд, запущенный вне диспетчера: находит владельца
    порта ``stand.stand_port``, валидирует его (см.
    ``standkit.adopt.validate_candidate``) и записывает pidfile. Дальше стенд
    для диспетчера ничем не отличается от запущенного им самим.

    Возвращает усыновлённого ``AdoptCandidate``. Бросает:
      - ``AdoptionUnavailable`` — владельца порта определить не удалось;
      - ``LifecycleError`` — владелец найден, но это не процесс этого стенда
        (внятный текст с pid и путём — см. ``validate_candidate``);
      - ``LifecycleError`` — стенд не kestrel/не local (усыновлять нечего).

    Процесс при этом НЕ останавливается: усыновление — отдельный шаг,
    остановка — уже обычный ``stop``.
    """
    _require_local(stand)
    if stand.host_kind != HostKind.KESTREL:
        raise LifecycleError(
            f"стенд '{stand.name}': усыновление применимо только к host_kind=kestrel — "
            f"для {stand.host_kind.value!r} объект управления глобальный "
            "(сайт IIS/контейнер/деплоймент), pidfile ему не нужен"
        )
    candidate = _adoption_candidate(stand)
    _write_pid(pidfile_path(stand, run_dir), candidate.pid)
    return candidate


def is_managed(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """
    True, если стенд находится ПОД УПРАВЛЕНИЕМ диспетчера — есть живой pidfile.

    Отличается от ``is_running``: стенд может быть жив (порт отвечает), но
    поднят мимо диспетчера — тогда ``is_running`` (для kestrel по pidfile)
    и этот флаг оба дадут False, а health-проба покажет OK. Хаб по этой
    разнице рисует бейдж «вне диспетчера».
    """
    if stand.transport != Transport.LOCAL or stand.host_kind != HostKind.KESTREL:
        return True  # для agent/iis/docker/k8s понятия «pidfile диспетчера» нет
    pid = _read_pid(pidfile_path(stand, run_dir))
    return pid is not None and _platform.is_alive(pid)


def is_running(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """Быстрая проверка «жив ли стенд». Диспетчер по ``stand.host_kind`` (см. ``start``)."""
    _require_local(stand)
    if stand.host_kind == HostKind.KESTREL:
        return _kestrel_is_running(stand, run_dir=run_dir)
    from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

    backend = _hosting.get_backend(stand)
    return backend.is_running(stand, run_dir=run_dir)


def _kestrel_start(
    stand: Stand,
    *,
    run_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    startup_check_delay: float = _DEFAULT_STARTUP_CHECK_DELAY,
) -> int:
    """
    Запускает стенд headless-процессом, если он ещё не запущен.

    Возвращает pid процесса (существующего, если стенд уже был жив, либо
    только что созданного). Бросает ``LifecycleError`` с понятным текстом,
    если ``dotnet`` не резолвится (см. ``_resolve_dotnet``) либо процесс
    завершился сразу после старта (см. проверку ``is_alive`` ниже) — раньше
    в обоих случаях start() мог тихо "вернуть успех", хотя стенд не поднялся.

    Команда запуска — ``dotnet <stand_dll>`` в ``stand_dir``, БЕЗ аргументов
    командной строки: BPMSoft.WebHost их не принимает (свой CommandLineParser),
    адрес/порт берётся из конфига стенда. Доп. окружение (ASPNETCORE_ENVIRONMENT
    и т.п.) при необходимости — зона расширения следующей итерации.

    Приватная функция kestrel-пути — вызывается диспетчером ``start()`` и
    напрямую из ``standkit.hosting.KestrelBackend`` (во избежание рекурсии
    диспетчер↔бэкенд, см. ADR-0001).
    """
    _require_local(stand)

    pf = pidfile_path(stand, run_dir)
    existing = _read_pid(pf)
    if existing and _platform.is_alive(existing):
        return existing

    stand_dir = Path(stand.stand_dir)
    if not stand_dir.exists():
        raise LifecycleError(f"Каталог стенда не найден: {stand_dir}")

    dotnet_path = _resolve_dotnet(stand.dotnet)
    # BPMSoft.WebHost парсит аргументы СВОИМ CommandLineParser и НЕ понимает
    # ASP.NET-флаги вроде --urls (ошибка «Verb '--urls' is not recognized»).
    # Адрес/порт стенд берёт из собственного конфига (appsettings). Поэтому
    # запускаем без доп. аргументов — просто dotnet <stand_dll> в каталоге
    # стенда (так же, как это делает сам BPMkit/кит).
    cmd = [dotnet_path, stand.stand_dll]
    lp = log_path(stand, log_dir)
    pid = _platform.spawn_hidden(cmd, cwd=stand_dir, log_path=lp)

    if startup_check_delay > 0:
        time.sleep(startup_check_delay)
    if not _platform.is_alive(pid):
        raise LifecycleError(
            f"процесс стенда '{stand.name}' завершился сразу после старта — "
            f"смотрите логи ({lp})"
        )

    _write_pid(pf, pid)
    return pid


def _adoption_candidate(stand: Stand):
    """
    Находит и валидирует владельца порта стенда. Возвращает
    ``standkit.adopt.AdoptCandidate`` либо бросает ``LifecycleError`` с
    понятным текстом.

    Импорт ``standkit.adopt`` локальный: он тянет ``standkit.hosting`` за
    декодированием вывода внешних команд, а тот — обратно ``lifecycle``.
    """
    from standkit import adopt as _adopt  # локальный импорт — избегаем цикла

    candidate = _adopt.find_candidate(stand)
    if candidate is None:
        raise AdoptionUnavailable(
            f"стенд '{stand.name}' запущен вне диспетчера, но определить процесс, "
            f"который слушает порт {stand.stand_port}, не удалось (нет прав на чужой "
            "процесс либо в системе нет netstat/ss/lsof). Остановите процесс вручную."
        )
    ok, reason = _adopt.validate_candidate(stand, candidate)
    if not ok:
        raise LifecycleError(f"стенд '{stand.name}': {reason}")
    return candidate


def _kestrel_stop(stand: Stand, *, run_dir: Optional[Path] = None, force: bool = False) -> bool:
    """Останавливает стенд, если он запущен. Возвращает True при успешной остановке.

    Если pidfile нет (или он «протух» — процесс из него мёртв), а стенд при этом
    РЕАЛЬНО жив (отвечает TCP-порт), включается усыновление:

      - ищем и валидируем владельца порта (см. ``_adoption_candidate``);
      - ``force=False`` — бросаем ``AdoptionRequired`` с кандидатом наверх, чтобы
        пользователь подтвердил. Процесс НЕ трогаем;
      - ``force=True`` — записываем pidfile и дальше идём обычным путём.

    Приватная функция kestrel-пути — см. ``_kestrel_start``.
    """
    _require_local(stand)

    pf = pidfile_path(stand, run_dir)
    pid = _read_pid(pf)
    if pid is not None and not _platform.is_alive(pid):
        # Протухший pidfile (процесс из него давно мёртв) — не даём ему увести
        # нас в _platform.stop() по чужому/переиспользованному pid. Удаляем и
        # действуем так, будто pidfile не было: возможно, стенд перезапустили
        # руками, и его нужно усыновить заново.
        try:
            pf.unlink(missing_ok=True)
        except OSError:
            pass
        pid = None

    if pid is None:
        from standkit import health as _health  # локальный импорт — избегаем цикла

        if not (
            stand.stand_host
            and stand.stand_port
            and _health.tcp_open(stand.stand_host, stand.stand_port)
        ):
            return True  # действительно ничего не запущено — останавливать нечего

        candidate = _adoption_candidate(stand)
        if not force:
            raise AdoptionRequired(stand.name, candidate)
        _write_pid(pf, candidate.pid)
        pid = candidate.pid

    stopped = _platform.stop(pid)
    if stopped:
        try:
            pf.unlink(missing_ok=True)
        except OSError:
            pass
    return stopped


def _kestrel_restart(
    stand: Stand,
    *,
    run_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    force: bool = False,
) -> int:
    """Останавливает (если жив) и заново запускает стенд. Возвращает новый pid.

    ``force`` прокидывается в ``_kestrel_stop``: рестарт стенда, поднятого вне
    диспетчера, тоже требует явного согласия на усыновление (иначе рестарт
    падал бы вместе со stop — ровно тот симптом, ради которого всё затевалось).

    Приватная функция kestrel-пути — см. ``_kestrel_start``.
    """
    _kestrel_stop(stand, run_dir=run_dir, force=force)
    return _kestrel_start(stand, run_dir=run_dir, log_dir=log_dir)


def _kestrel_is_running(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """Быстрая проверка «жив ли стенд» по pidfile (обёртка над standkit.health.process_alive).

    Приватная функция kestrel-пути — см. ``_kestrel_start``.
    """
    _require_local(stand)
    pid = _read_pid(pidfile_path(stand, run_dir))
    return pid is not None and _platform.is_alive(pid)
