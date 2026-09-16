"""
Перезапуск диспетчера «от имени администратора» (Windows/UAC) — кнопка
«Перезапустить с правами администратора» на дашборде.

ЗАЧЕМ. Управление IIS идёт через ``appcmd.exe``, который без elevation не
читает даже собственный ``redirection.config``: любая операция «Старт/Стоп»
над стендом-сайтом IIS падает, а диспетчер честно пишет «не хватает прав
администратора, запустите standkit-hub от имени администратора» (см.
``standkit.hosting.ELEVATION_HINT``). Выполнить этот совет вручную было
неочевидно: повторный запуск ярлыка «от имени администратора» НИЧЕГО не менял
— single-instance проверка видела уже работающий на 8770 экземпляр, новый
процесс просто открывал браузер на СТАРОМ (не elevated) и выходил (см.
``standkit_hub.server.HubAlreadyRunning`` и ``standkit_hub.instance``).

КАК ЭТО РАБОТАЕТ:
  1. хаб пишет в ``run_dir`` файл передачи сессии с ТЕКУЩИМ сессионным
     токеном (``write_handoff``);
  2. ``ShellExecuteW(..., "runas", ...)`` просит Windows поднять новый
     процесс хаба с elevated-токеном — это единственный штатный способ
     показать запрос UAC (повысить права УЖЕ запущенного процесса нельзя);
  3. новый процесс перехватывает порт у старого (флаг ``--takeover``) и
     читает сессионный токен из файла передачи (``--session-token-file``),
     поэтому уже открытая вкладка остаётся авторизованной: её HttpOnly-cookie
     совпадает с токеном нового процесса, и достаточно перезагрузить страницу.

БЕЗОПАСНОСТЬ. Файл передачи содержит сессионный токен, поэтому: создаётся
режимом ``0o600`` в каталоге пользователя, ЖИВЁТ СЕКУНДЫ (``HANDOFF_TTL_SEC``)
и удаляется при первом же чтении — даже если чтение неуспешно (протух, битый
JSON). Токен НИКОГДА не передаётся аргументами командной строки: argv виден
любому процессу в системе, а путь к файлу — нет (тот же контракт «секреты
только ссылкой», что у ``standkit_hub.agent_control``).

STDLIB-ONLY: ``ctypes`` (ShellExecuteW), ``json``, ``os``, ``sys``, ``time``.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

from standkit.platform import is_elevated
from standkit_hub.shortcut import windows_pythonw_executable

# Имя файла передачи сессии в ``run_dir`` (см. HubConfig.resolve_run_dir).
HANDOFF_FILE_NAME = "standkit-hub-handoff.json"

# Сколько секунд файл передачи считается годным. Окно должно покрывать
# показ запроса UAC и старт второго процесса, но не превращать файл в
# долгоживущий носитель токена.
HANDOFF_TTL_SEC = 180.0

# ShellExecuteW: значение > 32 — успех, всё остальное — код ошибки.
_SHELL_EXECUTE_SUCCESS_THRESHOLD = 32
# Пользователь нажал «Нет» в окне UAC — это не сбой, а осознанный отказ.
ERROR_CANCELLED = 1223
_SW_SHOWNORMAL = 1

# Текст отказа при повышении прав ПОД ДРУГОЙ учётной записью (GAP-311 п.4):
# новый (elevated) процесс обнаруживает, что запрос UAC подтвердил не тот
# пользователь, что запустил исходный процесс, и молча выходит, оставляя
# старый процесс работать как ни в чём не бывало. ``{user}`` — имя учётки,
# которая ФАКТИЧЕСКИ подтвердила права (см. standkit.platform.current_user_name).
REFUSAL_TEXT_TEMPLATE = (
    "Права подтверждены другой учётной записью ({user}). Диспетчер работает с "
    "реестром стендов и ключами текущего пользователя Windows, поэтому "
    "повышение под другой учётной записью не поддерживается. Войдите в Windows "
    "пользователем с правами администратора или попросите администратора "
    "добавить вашу учётную запись в группу «Администраторы»."
)


def refusal_text(user: Optional[str]) -> str:
    """Готовый текст отказа (см. ``REFUSAL_TEXT_TEMPLATE``) с подставленным именем."""
    return REFUSAL_TEXT_TEMPLATE.format(user=user or "неизвестно")


class ReparseGuardError(Exception):
    """
    Путь (или его родительский каталог) — reparse point/symlink, запись/чтение
    отказана (GAP-311 В7).
    """


def _is_reparse_or_symlink(path: Path) -> bool:
    """
    ``True`` — путь САМ является reparse point (NTFS junction/symlink на
    Windows, через ``st_file_attributes``) либо обычным symlink (POSIX).

    ЗАЧЕМ. Модель угроз: ``run_dir``, файл result-файла и файл передачи
    сессии — все под путями, которые НЕПОВЫШЕННЫЙ пользователь мог подготовить
    заранее (это его собственный профиль). Если на месте ожидаемого файла или
    каталога заранее подложен reparse point/symlink, ПОВЫШЕННЫЙ процесс,
    доверяя пути из argv, писал/читал бы туда, куда указывает подмена —
    классический вектор повышения привилегий (TOCTOU). Путь, которого пока
    не существует, не подозрителен: создание файла — это и есть штатная
    запись, а не подмена существующего.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if sys.platform == "win32":
        FILE_ATTRIBUTE_REPARSE_POINT = 0x400
        attrs = getattr(st, "st_file_attributes", 0)
        return bool(attrs & FILE_ATTRIBUTE_REPARSE_POINT)
    return stat.S_ISLNK(st.st_mode)


def ensure_not_reparse(path: Path) -> None:
    """
    Бросает ``ReparseGuardError``, если САМ путь ИЛИ его родительский каталог
    — reparse point/symlink (GAP-311 В7). Вызывается elevated-процессом ПЕРЕД
    записью result-файла и ПЕРЕД чтением файла передачи сессии — оба момента,
    где повышенный процесс доверяет пути, полученному из argv/конфига,
    подготовленному ДО повышения прав.
    """
    path = Path(path)
    for candidate in (path, path.parent):
        if _is_reparse_or_symlink(candidate):
            raise ReparseGuardError(f"путь {candidate} — reparse point/symlink, отказ")


class ElevationError(Exception):
    """Не удалось перезапустить диспетчер с правами администратора (текст пригоден для показа)."""


class ElevationCancelled(ElevationError):
    """Запрос UAC отклонён пользователем — старый процесс продолжает работать как ни в чём не бывало."""


def elevation_supported() -> bool:
    """Поддерживается ли перезапуск с повышением прав на этой ОС (пока только Windows)."""
    return sys.platform == "win32"


def can_restart_elevated() -> tuple[bool, str]:
    """
    Можно ли предлагать перезапуск прямо сейчас: ``(да/нет, причина отказа)``.

    Причина — готовый текст для интерфейса, а не код ошибки: кнопку на
    дашборде рисует фронт, и придумывать формулировку там было бы вторым
    источником правды.
    """
    if not elevation_supported():
        return False, "Перезапуск с правами администратора доступен только на Windows."
    if is_elevated():
        return False, "Диспетчер уже работает с правами администратора."
    return True, ""


# --------------------------------------------------------------------------
# Передача сессии новому процессу
# --------------------------------------------------------------------------


def handoff_path(run_dir: Path) -> Path:
    """Путь к файлу передачи сессии внутри ``run_dir``."""
    return Path(run_dir) / HANDOFF_FILE_NAME


def write_handoff(path: Path, session_token: str, *, now: Optional[float] = None) -> Path:
    """
    Пишет файл передачи сессии (перезаписывая прошлый, если он остался от
    отменённой попытки) с правами ``0o600``.

    Права выставляются В МОМЕНТ создания (``os.open`` с ``mode=0o600``), а не
    после записи: иначе между созданием и ``chmod`` существует окно, в котором
    файл с токеном доступен всем.

    ВАЖНО (GAP-311 M14): ``0o600`` -- это POSIX-режим. На Windows у него НЕТ
    эффекта на права доступа (NTFS/ACL режим ``os.open`` не устанавливает
    вовсе) — реальная защита файла на Windows целиком полагается на ACL
    родительского каталога профиля пользователя (``run_dir`` по умолчанию
    лежит внутри ``%LOCALAPPDATA%``/аналога, куда по умолчанию имеет доступ
    только сам пользователь и SYSTEM/Administrators). Если ``run_dir``
    сконфигурирован ВНЕ профиля пользователя (нестандартная установка), это
    неявное предположение нарушается — обнаружение и WARN в лог диспетчера
    при старте см. в ``standkit_hub.__main__`` (проверка "run_dir вне
    профиля пользователя").
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"session_token": session_token, "created_at": float(now if now is not None else time.time())},
        ensure_ascii=False,
    )
    # O_TRUNC, а не O_EXCL: остаток от прошлой (отменённой в UAC) попытки —
    # штатная ситуация, ронять из-за него перезапуск незачем.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload)
    return path


def discard_handoff(path: Path) -> None:
    """Удаляет файл передачи сессии, если он есть (никогда не бросает)."""
    try:
        Path(path).unlink()
    except OSError:
        pass


def write_result_atomic(path: Path, payload: dict, *, check_reparse: bool = True) -> None:
    """
    Атомарно пишет JSON-результат одноразовой операции с правами (отказ по
    SID/serving/failed при перезапуске диспетчера, статус одноразовой
    elevated-операции над стендом — см. ``standkit_hub.elevated_op``).

    ЗАЧЕМ АТОМАРНО. Наблюдатель на СТОРОНЕ СТАРОГО процесса (или сам хендлер
    ``GET /api/hub/elevated-op/<id>``) опрашивает этот файл параллельно с его
    записью новым процессом — обычная запись оставила бы окно, в котором
    читатель видит пустой либо обрубленный JSON. ``os.replace`` на одном томе
    — атомарная операция и на POSIX, и на NTFS: временный файл либо целиком
    появляется на месте финального, либо не появляется вовсе.

    ``check_reparse`` (GAP-311 В7) — перед записью проверяем, что путь/его
    каталог не reparse point/symlink (см. ``ensure_not_reparse``): пишет это
    ПОВЫШЕННЫЙ процесс по пути из argv, подготовленному ДО повышения прав.
    Имя временного файла — со случайным суффиксом (``secrets.token_hex``, а
    не предсказуемым ``.tmp<pid>``) и создаётся ``O_CREAT|O_EXCL`` — чтобы
    сам временный файл тоже нельзя было подстроить заранее.
    """
    path = Path(path)
    if check_reparse:
        ensure_not_reparse(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_result(path: Path) -> Optional[dict]:
    """
    Читает JSON-результат одноразовой операции, если файл уже появился.

    ``None`` — файла ещё нет (обычный исход при опросе "пока ждём") либо он
    оказался нечитаемым/битым (гонка с записью на файловых системах без
    строгой атомарности ``rename`` — трактуем консервативно, как "пока нет").
    Файл НЕ удаляется здесь: у файла результата, в отличие от файла передачи
    сессии, нет секрета внутри, а решение "когда удалить" разное у двух
    вызывающих (наблюдатель перезапуска — сразу; ``GET .../elevated-op/<id>``
    — тоже сразу, но по своему пути), поэтому удаление — забота вызывающего.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def read_handoff(
    path: Path, *, ttl: float = HANDOFF_TTL_SEC, now: Optional[float] = None, check_reparse: bool = True
) -> Optional[str]:
    """
    Читает сессионный токен из файла передачи и УДАЛЯЕТ файл — при любом
    исходе, включая протухший/битый (одноразовость важнее диагностики).

    ``None`` — файла нет, он старше ``ttl`` секунд, или содержимое не разбирается.
    Вызывающий (``standkit_hub.__main__``) в этом случае просто генерирует
    новый сессионный токен: перезапуск состоится, но вкладку придётся открыть
    заново по ярлыку.

    ``check_reparse`` (GAP-311 В7) — ПЕРЕД чтением (тем более удалением)
    проверяем, что путь/его каталог не reparse point/symlink: путь пришёл из
    argv, подготовленного ДО повышения прав. Reparse — файл НЕ трогаем вовсе
    (ни читаем, ни удаляем) и отдаём ``None``, как при любом другом отказе.
    """
    path = Path(path)
    if check_reparse and (_is_reparse_or_symlink(path) or _is_reparse_or_symlink(path.parent)):
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    finally:
        discard_handoff(path)

    try:
        data = json.loads(raw)
        token = data["session_token"]
        created_at = float(data["created_at"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None

    if not isinstance(token, str) or not token:
        return None
    moment = now if now is not None else time.time()
    # Отрицательная разница (часы сдвинули назад) — тоже «не доверяем».
    if not (0.0 <= moment - created_at <= ttl):
        return None
    return token


# --------------------------------------------------------------------------
# Собственно перезапуск
# --------------------------------------------------------------------------


def build_relaunch_params(
    *,
    port: int,
    host: Optional[str] = None,
    handoff: Optional[Path] = None,
    config_path: Optional[Path] = None,
    desktop: bool = False,
    initiator_sid: Optional[str] = None,
    result_file: Optional[Path] = None,
    insecure: bool = False,
) -> list[str]:
    """
    Аргументы (ХВОСТ, без имени модуля/исполняемого файла — тот добавляет
    ``relaunch_command``) нового (elevated) процесса хаба.

    Чистая функция без побочных эффектов — ровно тот же приём, что и в
    ``standkit_hub.agent_control.build_agent_argv``: маппинг «состояние → флаги»
    должен проверяться тестом без реального запуска процесса.

    ``--takeover`` обязателен: порт ещё занят старым процессом, и без него
    новый экземпляр упёрся бы в single-instance проверку и молча вышел —
    ровно тот сценарий, из-за которого ручной «запуск ярлыка от имени
    администратора» не давал никакого эффекта.

    ``desktop`` — в каком режиме работал СТАРЫЙ процесс (см. параметр
    ``desktop_mode`` у ``make_handler``/``bind_hub_server``): ``True`` даёт
    ``--desktop`` вместо ``--no-browser``. В режиме pywebview закрытие окна
    старого процесса не оставляет открытой вкладки браузера, которую можно
    было бы просто переиспользовать через файл передачи сессии — новому
    процессу нужно САМОМУ открыть окно, иначе пользователь остаётся без
    какого-либо интерфейса. ``--no-browser`` в браузерном режиме остаётся по
    тем же двум причинам, что раньше: вкладка у пользователя уже открыта
    (сессия переезжает через файл передачи), а браузер, запущенный ИЗ
    elevated-процесса, сам оказался бы elevated — этого не хочет никто.

    ``initiator_sid``/``result_file`` — проверка «ту же учётную запись ли
    подтвердили в UAC» (GAP-311 п.4): новый процесс сверяет SID и пишет исход
    в ``result_file`` ДО того, как сделать что-либо с портом/состоянием.

    ``host`` (M15) — передаётся ЯВНО, только если отличается от дефолтного
    ``127.0.0.1``: без этого новый процесс, запущенный с флагами по умолчанию,
    слушал бы не тот адрес, на котором на самом деле стоял старый (нестандартная
    настройка ``--host``). ``config_path``, если задан, приводится к
    АБСОЛЮТНОМУ (``resolve()``) — новый процесс стартует с ДРУГИМ рабочим
    каталогом (``cwd`` у ``ShellExecuteW`` — домашняя папка пользователя, см.
    ``relaunch_elevated``), и относительный путь там означал бы другой файл.

    ``insecure`` (M1) — в каком режиме bind'а работал СТАРЫЙ процесс (см.
    параметр ``insecure`` у ``bind_hub_server``/``create_hub_server``): если
    он был поднят с ``--insecure`` (non-loopback bind БЕЗ TLS, осознанный
    отказ от fail-closed защиты), новый процесс, унаследовавший ТОТ ЖЕ
    ``host``, обязан унаследовать и ЭТО решение — иначе строгий bind (В6) на
    non-loopback host упал бы в ``InsecureBindError`` вместо честного
    перехвата, хотя пользователь уже осознанно согласился на такой режим
    раньше.
    """
    params = ["--port", str(port), "--takeover"]
    params.append("--desktop" if desktop else "--no-browser")
    if host is not None and host != "127.0.0.1":
        params += ["--host", str(host)]
    if config_path is not None:
        params += ["--config", str(Path(config_path).resolve())]
    if handoff is not None:
        params += ["--session-token-file", str(Path(handoff).resolve())]
    if initiator_sid is not None:
        params += ["--initiator-sid", str(initiator_sid)]
    if result_file is not None:
        params += ["--result-file", str(Path(result_file).resolve())]
    if insecure:
        params.append("--insecure")
    return params


def build_elevated_op_params(
    *,
    stand: str,
    action: str,
    result_file: Path,
    config_path: Optional[Path] = None,
    initiator_sid: Optional[str] = None,
) -> list[str]:
    """
    Аргументы (хвост) одноразового elevated-процесса ``--elevated-op``
    (``standkit_hub.elevated_op``, GAP-311 п.6) — младший брат
    ``build_relaunch_params``: не про весь диспетчер, а про ОДНУ операцию над
    ОДНИМ стендом. Порт/``--takeover``/``--no-browser``/``--desktop`` тут не
    нужны — новый процесс не поднимает HTTP-сервер вовсе. Сессионный токен
    НЕ передаётся ни явно, ни через файл передачи: одноразовому процессу
    просто нечем его использовать.
    """
    params = ["--elevated-op", action, "--stand", stand, "--result-file", str(Path(result_file).resolve())]
    if config_path is not None:
        params += ["--config", str(Path(config_path).resolve())]
    if initiator_sid is not None:
        params += ["--initiator-sid", str(initiator_sid)]
    return params


def relaunch_command(params_tail: Sequence[str]) -> "tuple[str, list[str]]":
    """
    Исполняемый файл + ПОЛНЫЙ список аргументов нового процесса — с учётом
    того, что в поставке BPMkit диспетчер запускается не интерпретатором
    Python, а PyInstaller-сборкой (``BPMkit-hub.exe``, GAP-311 п.3).

    ``sys.frozen`` — стандартный признак PyInstaller-бутстрапа: внутри такого
    exe модуля ``standkit_hub`` как отдельно импортируемого пакета для
    ``-m`` нет (весь код упакован в сам exe), поэтому для него исполняемым
    файлом становится сам ``sys.executable`` (это и есть ``BPMkit-hub.exe``),
    а ``params_tail`` передаётся как есть, без ``-m standkit_hub``.

    Иначе (запуск из исходников/venv) — прежнее поведение: ``pythonw.exe``
    (без консольного окна) + ``-m standkit_hub`` перед хвостом аргументов.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, list(params_tail)
    return windows_pythonw_executable(), ["-m", "standkit_hub", *params_tail]


def quote_params(params: Sequence[str]) -> str:
    """
    Склеивает аргументы в строку для ``ShellExecuteW`` (он принимает ОДНУ
    строку, а не argv).

    Реализация — ``subprocess.list2cmdline`` (M15): та же логика экранирования,
    что использует сам CPython при запуске процессов на Windows (кавычки И
    экранирование ВНУТРЕННИХ кавычек/обратных слэшей по правилам
    ``CommandLineToArgvW``) — самодельное «пробел → в кавычки» не обрабатывало
    пути с кавычками внутри (казуистика, но именно на ней ломается наивная
    склейка). Пути с пробелами тут норма (``C:\\Program Files\\...``,
    ``C:\\Users\\Имя Фамилия\\AppData\\...``).
    """
    return subprocess.list2cmdline([str(p) for p in params])


def _default_shell_execute(executable: str, params: str, cwd: str) -> int:
    import ctypes

    return int(
        ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
            None, "runas", executable, params, cwd, _SW_SHOWNORMAL
        )
    )


def relaunch_elevated(
    params: Sequence[str],
    *,
    executable: Optional[str] = None,
    cwd: Optional[Path] = None,
    shell_execute: Optional[Callable[[str, str, str], int]] = None,
) -> None:
    """
    Просит Windows запустить новый процесс хаба с правами администратора
    (глагол ``runas`` — именно он показывает запрос UAC).

    Возврат означает лишь «запрос UAC подтверждён и процесс создан»: дождётся
    ли новый экземпляр освобождения порта — забота его собственной
    takeover-логики. Отказ пользователя в UAC — ``ElevationCancelled``, любая
    другая беда — ``ElevationError`` с человекочитаемым текстом. Наружу не
    выходит ни одно исключение ОС «как есть».

    ``shell_execute`` подменяется в тестах: настоящий вызов показал бы окно UAC.

    ``executable`` — явное указание исполняемого файла (в основном для
    тестов, которым не важна разница frozen/venv). Если не передан,
    вычисляется через ``relaunch_command`` (frozen-aware, GAP-311 п.3):
    у PyInstaller-сборки ``params`` уже готовый хвост, БЕЗ ``-m standkit_hub``
    — его туда, если нужно, добавит ``relaunch_command`` сам.
    """
    if not elevation_supported():
        raise ElevationError("Перезапуск с правами администратора доступен только на Windows.")

    if executable is None:
        executable, full_params = relaunch_command(params)
    else:
        full_params = list(params)
    call = shell_execute or _default_shell_execute
    try:
        rc = call(executable, quote_params(full_params), str(cwd or Path.home()))
    except Exception as exc:  # ctypes/WinAPI — что угодно, наружу отдаём понятный текст
        raise ElevationError(f"Не удалось запросить повышение прав: {exc}") from exc

    if rc == ERROR_CANCELLED:
        raise ElevationCancelled(
            "Запрос прав администратора отклонён — диспетчер продолжает работать без них."
        )
    if rc <= _SHELL_EXECUTE_SUCCESS_THRESHOLD:
        raise ElevationError(
            f"Windows отказалась запустить диспетчер с правами администратора (код {rc})."
        )
