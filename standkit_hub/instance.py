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
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from standkit import __version__ as _standkit_version
from standkit.platform import ProcessError, is_alive, process_create_time, stop, wait_for_exit
from standkit_hub.elevation import ReparseGuardError, ensure_not_reparse

STATE_FILE_NAME = "standkit-hub.json"
# Файл-запрос штатной остановки (GAP-311 Б1) — см. докстринг stop_running_instance.
STOP_REQUEST_FILE_NAME = "standkit-hub-stop-request.json"

# Сколько ждать, пока перехваченный экземпляр отпустит порт.
DEFAULT_TAKEOVER_TIMEOUT = 20.0
_PORT_POLL_INTERVAL = 0.25

# Сколько ждать реакции на файл-запрос остановки, ПРЕЖДЕ чем эскалировать до
# platform.stop (жёсткого убийства самого pid, без дерева). Должно с запасом
# покрывать период опроса наблюдателя на стороне работающего хаба
# (см. standkit_hub.server._StopRequestWatcher, опрашивает раз в ~0.5с) плюс
# _SHUTDOWN_DELAY_SEC на закрытие сокета.
DEFAULT_STOP_REQUEST_TIMEOUT = 5.0


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
    # SID пользователя Windows, под которым работает ЭТОТ процесс (см.
    # standkit.platform.current_user_sid). None — не Windows либо не удалось
    # определить; отсутствие поля в старом файле состояния (запись до
    # GAP-311 п.4) читается тем же None — обратная совместимость.
    user_sid: Optional[str] = None
    # Время СОЗДАНИЯ процесса, прочитанное из ОС (см.
    # standkit.platform.process_create_time), а НЕ время записи этого файла
    # (``started_at`` выше — тот пишется уже ПОСЛЕ импортов/bind'а, то есть
    # заведомо позже реального старта процесса). GAP-311 Н1: единственное
    # надёжное подтверждение «это тот же самый процесс» при эскалации до
    # жёсткого убийства — сверка чужого ``started_at`` с ПЕРЕЗАПРОШЕННЫМ у ОС
    # временем создания текущего pid, а не сверка файла с самим собой.
    # None — платформа не поддерживается либо не удалось определить;
    # отсутствие поля в старом файле состояния — та же обратная совместимость,
    # что у ``user_sid``.
    process_create_time: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "HubInstanceState":
        elevated = data.get("elevated")
        user_sid = data.get("user_sid")
        create_time = data.get("process_create_time")
        return cls(
            pid=int(data["pid"]),
            host=str(data.get("host", "127.0.0.1")),
            port=int(data["port"]),
            elevated=bool(elevated) if elevated is not None else None,
            version=str(data.get("version", "")),
            started_at=float(data.get("started_at", 0.0)),
            user_sid=str(user_sid) if user_sid else None,
            process_create_time=float(create_time) if create_time is not None else None,
        )


def state_path(run_dir: Path) -> Path:
    """Путь к файлу состояния экземпляра внутри ``run_dir``."""
    return Path(run_dir) / STATE_FILE_NAME


def _atomic_write_text(path: Path, text: str) -> None:
    """
    Пишет файл АТОМАРНО: временный файл в ТОМ ЖЕ каталоге (той же файловой
    системе — иначе ``os.replace`` не был бы атомарным) + ``os.replace``.

    ЗАЧЕМ. Файл состояния/запроса остановки читает второй процесс, который
    может подоспеть в СЕРЕДИНЕ обычной ``write_text`` — увидит пустой либо
    обрубленный JSON (M13). Имя временного файла — со случайным суффиксом
    (``secrets.token_hex``), а не предсказуемым ``.tmp<pid>``: предсказуемое
    имя в общедоступном ``run_dir`` — цель для подмены (см. GAP-311 В7,
    ``standkit_hub.elevation.write_result_atomic`` — тот же приём).

    М5: та же reparse point/symlink-проверка родительского каталога и самого
    пути, что у ``elevation.write_result_atomic`` — файл состояния и
    файл-запрос остановки лежат в том же ``run_dir``, подложенным ДО того,
    как процесс успел его записать, что даёт ту же TOCTOU-угрозу (GAP-311 В7).
    """
    path = Path(path)
    ensure_not_reparse(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    ensure_not_reparse(path)  # каталог мог быть создан ТОЛЬКО что — проверяем повторно
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_state(path: Path, state: HubInstanceState) -> Path:
    """Пишет файл состояния АТОМАРНО (M13). Секретов в нём нет — обычные права."""
    path = Path(path)
    _atomic_write_text(path, json.dumps(state.to_dict(), ensure_ascii=False, indent=2))
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
    должен снести файл, который УЖЕ переписал новый (M13: удаляем только
    если файл СЕЙЧАС читается как валидный JSON с ровно нашим pid — битый,
    нечитаемый или чужой файл не трогаем вовсе, а не только «чужой pid»).
    """
    path = Path(path)
    if pid is not None:
        current = read_state(path, require_alive=False)
        if current is None or current.pid != pid:
            return
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------
# Файл-запрос штатной остановки (GAP-311 Б1)
# --------------------------------------------------------------------------


def stop_request_path(run_dir: Path) -> Path:
    """Путь к файлу-запросу остановки внутри ``run_dir``."""
    return Path(run_dir) / STOP_REQUEST_FILE_NAME


def write_stop_request(
    path: Path, *, target_pid: int, requester_pid: Optional[int] = None, now: Optional[float] = None
) -> Path:
    """
    Пишет файл-запрос «останови себя штатно» (атомарно, см. ``_atomic_write_text``).

    ``target_pid`` — кого просят остановиться (``os.getpid()`` работающего
    экземпляра из его же файла состояния); ``requester_pid`` — кто просит
    (диагностика, в решении не участвует).
    """
    path = Path(path)
    payload = {
        "target_pid": int(target_pid),
        "requester_pid": int(requester_pid) if requester_pid is not None else None,
        "at": float(now if now is not None else time.time()),
    }
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False))
    return path


def read_stop_request(path: Path) -> Optional[dict]:
    """Читает файл-запрос остановки. ``None`` — файла нет либо он битый (НЕ удаляет файл)."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "target_pid" not in data:
        return None
    return data


def discard_stop_request(path: Path) -> None:
    """Удаляет файл-запрос остановки, если он есть (никогда не бросает)."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def should_takeover(
    running: Optional[HubInstanceState],
    *,
    we_elevated: Optional[bool],
    explicit: bool = False,
    our_sid: Optional[str] = None,
) -> bool:
    """
    Нужно ли новому процессу отобрать порт у работающего.

    ``explicit`` — пришёл флаг ``--takeover`` (перезапуск по кнопке дашборда):
    там решение уже принято пользователем, файл состояния лишь подсказывает,
    кого гасить; если состояния нет — гасить некого, но и отступать не надо
    (порт освободит уходящий сам). При ``explicit=True`` сверку SID НЕ делаем
    — за неё уже отвечает более ранняя проверка инициатора в
    ``standkit_hub.__main__`` (см. ``--initiator-sid``, GAP-311 п.4): если она
    пропустила запрос дальше, значит SID совпал (или его нельзя определить
    ни с одной стороны), и здесь решение принято.

    ``our_sid`` — SID ТЕКУЩЕГО (нового) процесса. Если мы elevated, у
    работающего экземпляра известен ``user_sid``, у нас известен ``our_sid``,
    и они РАЗЛИЧАЮТСЯ — не перехватываем даже автоматически: значит, кто-то
    из другой учётной записи запустил (или сам поднял через UAC) свой
    процесс диспетчера рядом с процессом первого пользователя, и молча
    останавливать чужой рабочий экземпляр нельзя.
    """
    if explicit:
        return True
    if running is None:
        return False
    if we_elevated and our_sid and running.user_sid and our_sid != running.user_sid:
        return False
    # Повышение прав — единственный автоматический повод. Обратного (elevated
    # уступает обычному) не бывает.
    return bool(we_elevated) and running.elevated is False


# Допуск (секунды) при сверке времени создания процесса с ``started_at`` из
# файла состояния (GAP-311 Н1) — файл пишется ПОСЛЕ реального старта
# (импорты, bind, acquire_hub_mutex), поэтому небольшое расхождение —
# норма, а не признак подмены.
_PROCESS_IDENTITY_TOLERANCE_SEC = 5.0

# Подстроки образа/командной строки/пути, по которым процесс распознаётся
# как диспетчер BPMkit (GAP-311 Н1) — источник (venv/исходники) и
# PyInstaller-сборка (см. ``standkit_hub.elevation.relaunch_command``) имена
# исполняемого файла дают разные, поэтому проверяем ОБЕ подстроки.
_HUB_PROCESS_IDENTITY_MARKERS = ("standkit_hub", "standkit-hub", "standkit-gui", "bpmkit-hub")


def _process_looks_like_hub(pid: int) -> bool:
    """
    ``True`` — образ/командная строка/путь процесса ``pid`` содержит один из
    ``_HUB_PROCESS_IDENTITY_MARKERS`` (без учёта регистра). Любая ошибка
    сбора улик (нет прав на чужой ``/proc``, WinAPI недоступен) — честное
    ``False``, а НЕ повод считать личность подтверждённой.
    """
    try:
        from standkit import adopt as _adopt

        info = _adopt.process_identity_info(pid)
    except Exception:
        return False
    haystack = " ".join(str(info.get(key, "")) for key in ("image", "cmdline", "exe_path")).lower()
    return any(marker in haystack for marker in _HUB_PROCESS_IDENTITY_MARKERS)


def _same_process_as_recorded(expected: HubInstanceState) -> "tuple[bool, str]":
    """
    Подтверждает, что живой процесс ``expected.pid`` — ДЕЙСТВИТЕЛЬНО тот же
    диспетчер, что описан в снимке ``expected``, ПЕРЕД эскалацией до
    жёсткого убийства (GAP-311 Н1).

    ЗАЧЕМ ПЕРЕДЕЛАНО. Прежняя версия сверяла файл состояния САМ С СОБОЙ:
    читала ``run_dir/standkit-hub.json`` и сравнивала его с тем же самым
    снимком, который был передан аргументом ``expected`` (в норме читаемым
    ИЗ ТОГО ЖЕ файла чуть раньше) — сверка тривиально совпадала ВСЕГДА, пока
    файл просто не был переписан кем-то другим. Живой баг: старый хаб
    завершается, ОС отдаёт его pid посторонней команде (воспроизведено —
    ``sleep 600``), файл состояния остаётся НЕТРОНУТЫМ (никто его не
    переписал и не удалил) — старая сверка подтверждала подмену и жёсткое
    убийство настигало посторонний процесс.

    Подтверждение теперь ищется НЕЗАВИСИМО от файла состояния, заново у ОС:
      (а) время создания процесса (``platform.process_create_time``)
          совпадает с ``expected.process_create_time``
          (или, для старых записей без этого поля, с ``expected.started_at``)
          в пределах ``_PROCESS_IDENTITY_TOLERANCE_SEC``;
      (б) образ/командная строка/путь процесса содержит "standkit_hub" или
          "bpmkit-hub" (``_process_looks_like_hub``).

    Подтверждено — если сработала ХОТЯ БЫ ОДНА проверка (не обе одновременно):
    время создания недоступно на части систем (macOS, Linux без доступа к
    чужому ``/proc``), командная строка недоступна для сервисных/чужих
    учёток на Windows — требование ОБЕИХ проверок одновременно давало бы
    постоянные ложные отказы там, где сегодня всё было бы честно убито.

    Возвращает ``(confirmed, reason)`` — ``reason`` пуст при подтверждении,
    иначе — человекочитаемый текст для показа пользователю (GAP-311 M4).
    """
    if not is_alive(expected.pid):
        return False, f"процесс {expected.pid} уже не выполняется"

    expected_create_time = expected.process_create_time or expected.started_at or None
    actual_create_time = process_create_time(expected.pid)
    if actual_create_time is not None and expected_create_time:
        if abs(actual_create_time - expected_create_time) <= _PROCESS_IDENTITY_TOLERANCE_SEC:
            return True, ""
        # Время создания ИЗВЕСТНО и НЕ совпало — это другой процесс с тем же
        # pid, и совпадение маркера в командной строке это не перекрывает
        # (посторонний `vim standkit_hub/server.py` тоже содержит маркер).
        # Маркер — только запасная проверка, когда время создания получить
        # не удалось вовсе (повторное ревью GAP-311, М-А).
        return False, (
            f"не удалось подтвердить, что pid {expected.pid} — диспетчер (время создания "
            "процесса не совпадает с записанным); остановите его вручную"
        )

    if _process_looks_like_hub(expected.pid):
        return True, ""

    return False, (
        f"не удалось подтвердить, что pid {expected.pid} — диспетчер; остановите его вручную"
    )


def stop_running_instance(
    state: HubInstanceState,
    *,
    run_dir: Path,
    requester_pid: Optional[int] = None,
    stop_request_timeout: float = DEFAULT_STOP_REQUEST_TIMEOUT,
    hard_timeout: float = 10.0,
) -> "tuple[bool, str]":
    """
    Останавливает перехватываемый экземпляр диспетчера БЕЗ убийства его
    дерева процессов (GAP-311 Б1).

    ЗАЧЕМ ВООБЩЕ ПЕРЕДЕЛАНО. Раньше здесь напрямую звался
    ``standkit.platform.stop`` (`` taskkill /T``/эквивалент) — тот убивает
    ВЕСЬ процесс-дерево, а kestrel-стенды и локальный агент — ПРЯМЫЕ дети
    процесса хаба (``standkit.platform.spawn_hidden``). Перехват порта
    (перезапуск с правами администратора, ручной запуск ярлыка «от имени
    администратора») убивал заодно и все живые стенды пользователя — грубый
    побочный эффект, никак не связанный с целью «поднять НОВЫЙ хаб на этом
    порту».

    Порядок:
      1. штатный запрос: пишем файл-запрос остановки (``write_stop_request``)
         и ждём ``stop_request_timeout`` секунд, что работающий хаб (его
         фоновый наблюдатель, см. ``standkit_hub.server._StopRequestWatcher``)
         увидит запрос и остановит СВОЙ HTTP-сервер штатно (``shutdown()``) —
         дети живут, их же родителем становится init/system.
      2. файл состояния УЖЕ отсутствует (GAP-311 M2) — ``clear_state``
         зовётся ТОЛЬКО из штатного завершения ТОГО ЖЕ pid, значит процесс
         увидел стоп-запрос и начал закрываться, просто не успел выйти за
         ``stop_request_timeout``. Даём ему дожить до ``hard_timeout`` вместо
         немедленного отказа или (что хуже) эскалации до убийства процесса,
         который и так штатно завершается сам.
      3. если процесс всё равно жив, а файл состояния НЕ тронут — старая
         версия хаба без наблюдателя, процесс подвешен, файловая система
         недоступна и т.п. — эскалируем до ``platform.stop(..., tree=False)``:
         убиваем ТОЛЬКО указанный pid, без ``/T``. ПЕРЕД этим подтверждаем
         (``_same_process_as_recorded``, GAP-311 Н1), что живой pid всё ещё
         представляет ТОТ ЖЕ процесс, по данным заново опрошенным у ОС, а не
         по факту существования файла — иначе рискуем убить чужой процесс,
         которому ОС успела переотдать освободившийся pid.

    Никогда не бросает: возвращает ``(ok, reason)`` — ``reason`` пуст при
    успехе, иначе человекочитаемый текст для показа пользователю (M4).
    """
    path = stop_request_path(run_dir)
    try:
        write_stop_request(path, target_pid=state.pid, requester_pid=requester_pid)
    except (OSError, ReparseGuardError):
        pass
    else:
        if wait_for_exit(state.pid, stop_request_timeout):
            discard_stop_request(path)
            return True, ""
    discard_stop_request(path)

    if not is_alive(state.pid):
        return True, ""

    if not state_path(run_dir).exists():
        # M2: файл уже очищен самим процессом — он увидел стоп-запрос и
        # начал закрываться, просто не успел выйти вовремя. Ждём дольше,
        # не эскалируя до убийства процесса, который и так завершается.
        if wait_for_exit(state.pid, hard_timeout):
            return True, ""
        return False, f"процесс {state.pid} не завершился за отведённое время после стоп-запроса"

    confirmed, reason = _same_process_as_recorded(state)
    if not confirmed:
        if not is_alive(state.pid):
            return True, ""
        return False, reason

    try:
        ok = stop(state.pid, timeout=hard_timeout, tree=False)
    except (ProcessError, OSError) as exc:
        return False, str(exc)
    return ok, ("" if ok else f"не удалось остановить процесс {state.pid}")


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


def current_state(
    host: str, port: int, *, elevated: Optional[bool], user_sid: Optional[str] = None
) -> HubInstanceState:
    """Слепок ТЕКУЩЕГО процесса — то, что пишется в файл состояния сразу после bind'а."""
    return HubInstanceState(
        pid=os.getpid(),
        host=host,
        port=int(port),
        elevated=elevated,
        version=_standkit_version,
        started_at=time.time(),
        user_sid=user_sid,
        process_create_time=process_create_time(os.getpid()),
    )
