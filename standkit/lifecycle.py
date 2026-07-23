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

from pathlib import Path
from typing import Optional

from standkit import platform as _platform
from standkit.models import Stand, Transport

_DEFAULT_RUN_DIR = Path.home() / ".standkit" / "run"
_DEFAULT_LOG_DIR = Path.home() / ".standkit" / "logs"


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


def start(stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None) -> int:
    """
    Запускает стенд headless-процессом, если он ещё не запущен.

    Возвращает pid процесса (существующего, если стенд уже был жив, либо
    только что созданного).

    TODO: команда запуска сейчас собирается по минимальному шаблону
    ``dotnet <stand_dll>`` в ``stand_dir`` — в реальном стенде BPMSoft может
    требоваться доп. окружение/аргументы (ASPNETCORE_ENVIRONMENT, --urls и
    т.п.), это зона расширения следующей итерации.
    """
    _require_local(stand)

    pf = pidfile_path(stand, run_dir)
    existing = _read_pid(pf)
    if existing and _platform.is_alive(existing):
        return existing

    stand_dir = Path(stand.stand_dir)
    if not stand_dir.exists():
        raise LifecycleError(f"Каталог стенда не найден: {stand_dir}")

    cmd = [
        stand.dotnet,
        stand.stand_dll,
        "--urls",
        f"http://{stand.stand_host}:{stand.stand_port}",
    ]
    lp = log_path(stand, log_dir)
    pid = _platform.spawn_hidden(cmd, cwd=stand_dir, log_path=lp)
    _write_pid(pf, pid)
    return pid


def stop(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """Останавливает стенд, если он запущен. Возвращает True при успешной остановке."""
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


def restart(stand: Stand, *, run_dir: Optional[Path] = None, log_dir: Optional[Path] = None) -> int:
    """Останавливает (если жив) и заново запускает стенд. Возвращает новый pid."""
    stop(stand, run_dir=run_dir)
    return start(stand, run_dir=run_dir, log_dir=log_dir)


def is_running(stand: Stand, *, run_dir: Optional[Path] = None) -> bool:
    """Быстрая проверка «жив ли стенд» по pidfile (обёртка над standkit.health.process_alive)."""
    _require_local(stand)
    pid = _read_pid(pidfile_path(stand, run_dir))
    return pid is not None and _platform.is_alive(pid)
