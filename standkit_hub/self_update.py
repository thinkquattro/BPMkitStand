# -*- coding: utf-8 -*-
"""Помощник самообновления диспетчера (`--apply-self-update`) — GAP-523.

Этот модуль исполняется ВТОРЫМ процессом: тем самым exe, который
`hub_channel.apply_self_update` скачал, проверил (подпись, `kind="hub"`,
PE-заголовок) и запустил с флагами `--apply-self-update --target <старый
exe> --wait-pid <pid старого процесса>` ДО того, как старый процесс успел
завершиться (порядок из `hub_channel.apply_self_update`: сначала спавн
помощника, потом — ответ HTTP и `request_self_shutdown` у ВЫЗЫВАЮЩЕГО
слоя, `standkit_hub.server`). Поэтому первый шаг помощника — ПОДОЖДАТЬ,
не полагаться на то, что `target` уже свободен.

Порядок, каждый шаг — явно перед следующим (тот же принцип
«бэкап → подмена», что у `standkit_companion.fsutil.replace_with_retry`,
которым эта подмена и сделана):

1. дождаться выхода `wait_pid` (`standkit.platform.is_alive`, опрос с
   таймаутом) — старый процесс всё ещё может держать `target` открытым на
   Windows (не-shareable загрузка `.exe`);
2. бэкап `target` → `target.old` (перезаписывает предыдущий бэкап — этот
   помощник не копит историю, в отличие от `releases.py`: диспетчер не
   откатывается автоматически, только установщиком/ручной заменой файла);
3. подмена `target` СОБОЙ (`sys.executable`) через `replace_with_retry`
   (ретраи на sharing violation — тот же приём, что у `releases.apply_staged`);
4. запуск `target` обычным образом (`Popen`, НЕ `spawn_hidden`: новый
   диспетчер обязан вести себя как обычный старт, включая открытие браузера,
   а не прятаться);
5. выход помощника — рекурсии тут нет: у запущенного `target` уже нет флага
   `--apply-self-update`.

Нет прав на запись в `target` (установка «для всех пользователей» —
`Program Files`) — попытка подмены даёт `PermissionError`/`OSError`, которую
`run_self_update_helper` превращает в диагностическое сообщение в лог и
код возврата 2 (elevation), НЕ поднимает UAC молча (SECURITY.md — тихое
повышение прав запрещено; человек видит текст в карточке ошибки ДО запуска
помощника, см. `hub_channel.apply_self_update` про `ELEVATION_WINERROR`,
здесь — симметричный случай уже ПОСЛЕ запуска, когда сама Windows не
отказала процессу стартовать, но файл всё равно недоступен на запись).
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from standkit.platform import ProcessError, is_alive, spawn_hidden

_log = logging.getLogger("standkit_hub.self_update")

#: Сколько ждать выхода старого процесса (контракт серии: «таймаут 60 с»).
WAIT_EXIT_TIMEOUT_S = 60.0
#: Шаг опроса — как у остальных ожиданий пакета (poller/раннер): секунды, не
#: busy-loop, но и не минуты между проверками.
WAIT_EXIT_POLL_S = 0.5

#: Число попыток и пауза подмены файла — то же значение, что у
#: `standkit_companion.fsutil._REPLACE_ATTEMPTS`/`_REPLACE_PAUSE_S` (10 × 0.3с
#: ≈ 3с): загрузчик Windows отпускает файл почти сразу после выхода процесса,
#: длинных ожиданий здесь не нужно, а вот пары попыток может не хватить.
REPLACE_ATTEMPTS = 10
REPLACE_PAUSE_S = 0.3

#: Код возврата помощника, когда подмена не удалась из-за прав доступа —
#: отличается от прочих файловых ошибок (3), чтобы лог/диагностика не путали
#: «файл занят чужим процессом» и «нет прав записи вовсе».
RC_OK = 0
RC_WAIT_TIMEOUT = 1
RC_ELEVATION_REQUIRED = 2
RC_LOCAL_IO = 3


def _wait_for_exit(pid: int, *, timeout: float = WAIT_EXIT_TIMEOUT_S,
                   poll: float = WAIT_EXIT_POLL_S,
                   sleep=time.sleep, clock=time.monotonic) -> bool:
    """`True` — процесс `pid` завершился (или не был жив изначально) в пределах
    `timeout`; `False` — таймаут, процесс всё ещё жив.

    `pid <= 0` считается «уже вышел» — вызывающий (`main()`) передаёт валидный
    pid из `--wait-pid`, но функция обязана остаться безопасной и на мусоре."""
    if pid <= 0:
        return True
    deadline = clock() + max(0.0, timeout)
    while is_alive(pid):
        if clock() >= deadline:
            return False
        sleep(poll)
    return True


def _replace_with_retry(src: Path, dst: Path, *, attempts: int = REPLACE_ATTEMPTS,
                        pause: float = REPLACE_PAUSE_S, sleep=time.sleep) -> None:
    """Копия `src` поверх `dst` с ретраями на `OSError` (тот же приём, что
    `standkit_companion.fsutil.replace_with_retry`, но копия, а не `os.replace`:
    `src` — это САМ РАБОТАЮЩИЙ помощник (`sys.executable`), переименовать
    собственный исполняемый файл, пока он выполняется, на Windows можно
    (файл не блокирует переименование ЧУЖИМИ читателями, но не должен
    оставаться источником операции над самим собой длиннее одной попытки),
    поэтому используется запись через временный файл РЯДОМ с `dst` и
    `os.replace` — атомарно и на одном томе."""
    tmp = dst.with_name(dst.name + ".newexe")
    last: Optional[BaseException] = None
    for attempt in range(max(1, attempts)):
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
            return
        except OSError as exc:
            last = exc
            try:
                tmp.unlink()
            except OSError:
                pass
            if attempt + 1 < attempts:
                sleep(pause)
    assert last is not None
    raise last


def _relaunch_previous(target_path: Path) -> None:
    """Поднять прежнюю версию диспетчера после неудачной подмены (best-effort)."""
    if not target_path.is_file():
        return
    try:
        spawn_hidden([str(target_path)], cwd=target_path.parent,
                     log_path=target_path.parent / "self_update_launch.log")
        _log.info("самообновление диспетчера: подмена не удалась — запущена прежняя версия %s",
                  target_path)
    except ProcessError as exc:
        _log.error("самообновление диспетчера: прежнюю версию %s запустить не удалось: %s — "
                   "запустите диспетчер ярлыком", target_path, exc)


def run_self_update_helper(*, target: str, wait_pid: int,
                           self_path: Optional[str] = None) -> int:
    """Точка входа помощника (`--apply-self-update --target ... --wait-pid ...`).

    `self_path` — переопределение источника подмены, только для тестов
    (штатно — `sys.executable`, реальный путь к ЭТОМУ запущенному exe)."""
    target_path = Path(target)
    src_path = Path(self_path) if self_path else Path(sys.executable)

    _log.info("самообновление диспетчера: помощник pid=%s ждёт выхода pid=%s, target=%s",
             os.getpid(), wait_pid, target_path)

    if not _wait_for_exit(wait_pid):
        _log.error("самообновление диспетчера: старый процесс pid=%s не завершился за %.0f с "
                   "— подмена не выполнена", wait_pid, WAIT_EXIT_TIMEOUT_S)
        return RC_WAIT_TIMEOUT

    backup_path = target_path.with_suffix(target_path.suffix + ".old")
    try:
        if target_path.is_file():
            shutil.copy2(target_path, backup_path)
    except OSError as exc:
        # Бэкап — страховка, а не обязательное условие: если его не удалось
        # сделать (диск полон, права), подмена всё равно имеет смысл
        # попробовать — лог фиксирует потерю страховки, не останавливает шаг.
        _log.warning("самообновление диспетчера: бэкап %s не создан: %s", backup_path, exc)

    try:
        _replace_with_retry(src_path, target_path)
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror == 5:  # ERROR_ACCESS_DENIED — нет прав записи в target
            _log.error("самообновление диспетчера: нет прав на запись в %s (%s) — "
                       "нужны права администратора", target_path, exc)
            rc = RC_ELEVATION_REQUIRED
        else:
            _log.error("самообновление диспетчера: не удалось подменить %s: %s", target_path, exc)
            rc = RC_LOCAL_IO
        # Старый диспетчер уже вышел — без этого шага пользователь остался бы
        # вовсе без диспетчера. Поднимаем ПРЕЖНЮЮ версию (target не тронут:
        # подмена идёт через временный файл и os.replace).
        _relaunch_previous(target_path)
        return rc

    _log.info("самообновление диспетчера: %s подменён, запускаю новую версию", target_path)
    launch_log = target_path.parent / "self_update_launch.log"
    try:
        # `spawn_hidden` — ЕДИНАЯ точка запуска процессов пакета (GAP-138,
        # см. `standkit.platform.spawn_hidden`): голый `subprocess.Popen` вне
        # `standkit/platform.py` запрещён и стережётся `tests/test_no_window.py`.
        # Обычный старт диспетчера — обязан обойтись без своего консольного
        # окна ровно так же, как и любой другой процесс пакета.
        spawn_hidden([str(target_path)], cwd=target_path.parent, log_path=launch_log)
    except ProcessError as exc:
        _log.error("самообновление диспетчера: подмена выполнена, но запуск %s не удался: "
                   "%s — запустите диспетчер вручную", target_path, exc)
        return RC_LOCAL_IO

    _log.info("самообновление диспетчера: завершено успешно")
    return RC_OK
