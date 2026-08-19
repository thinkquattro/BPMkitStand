"""
Состояние ЗАПУЩЕННОГО экземпляра диспетчера (pid/порт/права) и перехват порта
вторым экземпляром — «takeover».

ЗАЧЕМ. Single-instance проверка (``standkit_hub.server.probe_hub_instance``)
знает про уже работающий хаб ровно одно: он отвечает на порту. Этого хватало,
пока все экземпляры были равноправны. Но запуск «от имени администратора» —
не равноправный: он существует именно для того, чтобы ЗАМЕНИТЬ собой процесс
без прав (без elevation ``appcmd.exe`` не управляет IIS, см.
``standkit.hosting``). Раньше такой запуск молча открывал браузер на старом,
неэлевированном экземпляре, и пользователь видел ту же ошибку прав, будучи
уверенным, что запустил диспетчер от администратора.

Поэтому работающий экземпляр оставляет в ``run_dir`` файл состояния со своим
pid, портом и признаком elevated, а новый решает по нему (``should_takeover``):
  - явный ``--takeover`` (так перезапускает себя сам хаб по кнопке дашборда) —
    перехватываем;
  - мы elevated, а работающий — нет: перехватываем (это ровно тот ручной
    сценарий «правый клик → запуск от имени администратора»);
  - во всех остальных случаях — прежнее поведение: второй экземпляр не нужен.

Обратного перехвата (elevated → обычный) НЕТ намеренно: понижать права
работающего диспетчера молча, за спиной пользователя, — сюрприз, а не помощь.

Файл состояния — подсказка, а не источник правды: он мог остаться от процесса,
убитого по питанию. Поэтому ``read_state`` отдаёт запись только если процесс с
этим pid ЖИВ, а битый/чужой файл трактуется как «состояния нет».
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from standkit import __version__ as _standkit_version
from standkit.platform import ProcessError, is_alive, stop

STATE_FILE_NAME = "standkit-hub.json"

# Сколько ждать, пока перехваченный экземпляр отпустит порт.
DEFAULT_TAKEOVER_TIMEOUT = 20.0
_PORT_POLL_INTERVAL = 0.25


@dataclass
class HubInstanceState:
    """Слепок работающего экземпляра хаба (то, что нужно знать второму запуску)."""

    pid: int
    host: str
    port: int
    # None — «выяснить не удалось» (не Windows): именно None, а не False,
    # см. standkit.platform.is_elevated.
    elevated: Optional[bool] = None
    version: str = _standkit_version
    started_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HubInstanceState":
        elevated = data.get("elevated")
        return cls(
            pid=int(data["pid"]),
            host=str(data.get("host", "127.0.0.1")),
            port=int(data["port"]),
            elevated=bool(elevated) if elevated is not None else None,
            version=str(data.get("version", "")),
            started_at=float(data.get("started_at", 0.0)),
        )


def state_path(run_dir: Path) -> Path:
    """Путь к файлу состояния экземпляра внутри ``run_dir``."""
    return Path(run_dir) / STATE_FILE_NAME


def write_state(path: Path, state: HubInstanceState) -> Path:
    """Пишет файл состояния (создавая каталог). Секретов в нём нет — обычные права."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def read_state(path: Path, *, require_alive: bool = True) -> Optional[HubInstanceState]:
    """
    Читает файл состояния. ``None`` — файла нет, он битый, либо (при
    ``require_alive``) записанный в нём процесс уже мёртв.

    ``require_alive=False`` нужен тестам и диагностике: показать содержимое
    файла как есть, не проверяя процесс.
    """
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        state = HubInstanceState.from_dict(data)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    if require_alive and not is_alive(state.pid):
        return None
    return state


def clear_state(path: Path, *, pid: Optional[int] = None) -> None:
    """
    Удаляет файл состояния (никогда не бросает).

    ``pid`` — защита от гонки при перехвате: старый экземпляр, умирая, не
    должен снести файл, который УЖЕ переписал новый. Удаляем, только если в
    файле всё ещё наш pid.
    """
    path = Path(path)
    if pid is not None:
        current = read_state(path, require_alive=False)
        if current is not None and current.pid != pid:
            return
    try:
        path.unlink()
    except OSError:
        pass


def should_takeover(
    running: Optional[HubInstanceState], *, we_elevated: Optional[bool], explicit: bool = False
) -> bool:
    """
    Нужно ли новому процессу отобрать порт у работающего.

    ``explicit`` — пришёл флаг ``--takeover`` (перезапуск по кнопке дашборда):
    там решение уже принято пользователем, файл состояния лишь подсказывает,
    кого гасить; если состояния нет — гасить некого, но и отступать не надо
    (порт освободит уходящий сам).
    """
    if explicit:
        return True
    if running is None:
        return False
    # Повышение прав — единственный автоматический повод. Обратного (elevated
    # уступает обычному) не бывает.
    return bool(we_elevated) and running.elevated is False


def stop_running_instance(state: HubInstanceState, *, timeout: float = 10.0) -> bool:
    """
    Останавливает перехватываемый экземпляр (эскалация «мягко → жёстко» —
    ``standkit.platform.stop``). Никогда не бросает: не удалось погасить —
    вернём False, вызывающий честно скажет об этом пользователю.
    """
    try:
        return stop(state.pid, timeout=timeout)
    except (ProcessError, OSError):
        return False


def wait_port_released(host: str, port: int, *, timeout: float = DEFAULT_TAKEOVER_TIMEOUT) -> bool:
    """
    Ждёт, пока на ``host:port`` перестанет отвечать хаб (порт освободился).

    Импорт ``probe_hub_instance`` — локальный: ``standkit_hub.server`` тянет за
    собой весь веб-слой, а этот модуль обязан оставаться лёгким и
    импортируемым из ``__main__`` до всякого bind'а.
    """
    from standkit_hub.server import probe_hub_instance

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe_hub_instance(host, port, timeout=0.5) is None:
            return True
        time.sleep(_PORT_POLL_INTERVAL)
    return probe_hub_instance(host, port, timeout=0.5) is None


def current_state(host: str, port: int, *, elevated: Optional[bool]) -> HubInstanceState:
    """Слепок ТЕКУЩЕГО процесса — то, что пишется в файл состояния сразу после bind'а."""
    return HubInstanceState(
        pid=os.getpid(),
        host=host,
        port=int(port),
        elevated=elevated,
        version=_standkit_version,
        started_at=time.time(),
    )
