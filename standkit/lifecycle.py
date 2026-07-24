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

TODO(следующая итерация):
  - polling готовности после start() (сейчас просто возвращает pid, готовность
    веб-хоста нужно проверять отдельно через standkit.health.http_ok в цикле);
  - блокировка pidfile от гонки двух одновременных start() одного стенда;
  - восстановление после "потерянного" pidfile (процесс с этим pid уже другой —
    нужна доп. проверка, например по имени командной строки).
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


# Host kind'ы, для которых нет отдельного standkit.hosting-бэкенда — заглушка
# k8s (следующий этап, отдельный ADR) обслуживается пока тем же путём, что и
# kestrel, поэтому включена в диспетчеризацию наравне с ним.
_KESTREL_LIKE = (HostKind.KESTREL, HostKind.K8S)


def start(
    stand: Stand,
    *,
    run_dir: Optional[Path] = None,
    log_dir: Optional[Path] = None,
    startup_check_delay: float = _DEFAULT_STARTUP_CHECK_DELAY,
) -> Optional[int]:
    """
    Запускает стенд, если он ещё не запущен. Диспетчер по ``stand.host_kind``
    (см. ADR-0001, docs/adr/0001-hosting-backends.md): kestrel/k8s-заглушка —
    ТЕКУЩИЙ код (см. ``_kestrel_start``) без изменений; iis/docker —
    соответствующий бэкенд ``standkit.hosting``. ``startup_check_delay``
    применяется только к kestrel-ветке.

    Возвращает pid процесса для kestrel, либо None/pid-эквивалент бэкенда для
    iis/docker (см. ``standkit.hosting.HostingBackend.start``).
    """
    _require_local(stand)
    if stand.host_kind in _KESTREL_LIKE:
        return _kestrel_start(
            stand, run_dir=run_dir, log_dir=log_dir, startup_check_delay=startup_check_delay
        )
    from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

    backend = _hosting.get_backend(stand)
    return backend.start(stand, run_dir=run_dir, log_dir=log_dir)


def stop(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """Останавливает стенд, если он запущен. Диспетчер по ``stand.host_kind`` (см. ``start``)."""
    _require_local(stand)
    if stand.host_kind in _KESTREL_LIKE:
        return _kestrel_stop(stand, run_dir=run_dir)
    from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

    backend = _hosting.get_backend(stand)
    return backend.stop(stand, run_dir=run_dir)


def restart(
    stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
) -> Optional[int]:
    """Останавливает (если жив) и заново запускает стенд. Диспетчер по ``stand.host_kind``."""
    _require_local(stand)
    if stand.host_kind in _KESTREL_LIKE:
        return _kestrel_restart(stand, run_dir=run_dir, log_dir=log_dir)
    from standkit import hosting as _hosting  # локальный импорт — избегаем цикла

    backend = _hosting.get_backend(stand)
    return backend.restart(stand, run_dir=run_dir, log_dir=log_dir)


def is_running(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """Быстрая проверка «жив ли стенд». Диспетчер по ``stand.host_kind`` (см. ``start``)."""
    _require_local(stand)
    if stand.host_kind in _KESTREL_LIKE:
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


def _kestrel_stop(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """Останавливает стенд, если он запущен. Возвращает True при успешной остановке.

    Приватная функция kestrel-пути — см. ``_kestrel_start``.
    """
    _require_local(stand)

    pf = pidfile_path(stand, run_dir)
    pid = _read_pid(pf)
    if pid is None:
        return True

    stopped = _platform.stop(pid)
    if stopped:
        try:
            pf.unlink(missing_ok=True)
        except OSError:
            pass
    return stopped


def _kestrel_restart(
    stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None
) -> int:
    """Останавливает (если жив) и заново запускает стенд. Возвращает новый pid.

    Приватная функция kestrel-пути — см. ``_kestrel_start``.
    """
    _kestrel_stop(stand, run_dir=run_dir)
    return _kestrel_start(stand, run_dir=run_dir, log_dir=log_dir)


def _kestrel_is_running(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """Быстрая проверка «жив ли стенд» по pidfile (обёртка над standkit.health.process_alive).

    Приватная функция kestrel-пути — см. ``_kestrel_start``.
    """
    _require_local(stand)
    pid = _read_pid(pidfile_path(stand, run_dir))
    return pid is not None and _platform.is_alive(pid)
