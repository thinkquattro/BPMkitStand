"""
Одноразовая операция с правами администратора над ОДНИМ стендом IIS
(``--elevated-op {start,stop,restart} --stand NAME --result-file PATH``,
GAP-311 п.6) — младший брат перезапуска всего диспетчера
(``standkit_hub.elevation``).

ЗАЧЕМ ОТДЕЛЬНЫЙ РЕЖИМ, А НЕ ПОЛНЫЙ ПЕРЕЗАПУСК. Частая причина запроса
elevation — операция над ОДНИМ стендом IIS (``appcmd.exe`` без прав даже
собственный ``redirection.config`` не читает, см. ``standkit.hosting``), а
не «весь диспетчер должен работать elevated». Полный перезапуск ощутим для
пользователя: переезжают ВСЕ вкладки и весь SSE-поток (``standkit_hub.elevation``),
хотя нужно было ровно одно нажатие «Старт». Поэтому эта операция поднимает
ТОТ ЖЕ исполняемый файл в режиме «выполнить и выйти»: без HTTP-сервера,
мьютекса, файла состояния экземпляра и файла передачи сессии — только
конфиг, реестр и ``FederatedClient``.

Запускается тем же способом, что и весь перезапуск (``ShellExecuteW runas``,
см. ``standkit_hub.elevation.relaunch_elevated``/``relaunch_command``,
frozen-aware) — параметрами из ``standkit_hub.elevation.build_elevated_op_params``.
Токен сессии НЕ передаётся: у одноразового процесса нет своего HTTP-сервера,
который его использовал бы, а самому read-only процессу токен ни для чего
не нужен.

Обрабатывается в ``standkit_hub.__main__`` ДО любых bind/mutex/state/handoff
— этот режим не должен занимать порт диспетчера, даже эфемерный.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

from standkit.hosting import HostingError
from standkit.lifecycle import LifecycleError
from standkit.models import HostKind
from standkit.platform import current_user_name, current_user_sid
from standkit.registry import Registry, RegistryError, default_registry_path
from standkit.secrets import SecretError
from standkit_hub import security as _security
from standkit_hub.client import FederatedClient, RemoteCallError
from standkit_hub.config import HubConfig
from standkit_hub.elevation import REFUSAL_TEXT_TEMPLATE, ReparseGuardError, write_result_atomic

# Действия, которые допускает одноразовая операция — то же множество, что и
# у ``POST /api/stand/<name>/<action>`` без «status»/«logs»/«adopt» (те не
# требуют elevation и не имеют смысла в этом режиме).
ACTIONS = ("start", "stop", "restart")

# Код возврата, когда путь result-файла оказался reparse point/symlink (В7):
# отдельно от обычных 0/1/3 — результат в этом случае НЕ записан вовсе (ни
# частично, ни ошибкой), и наблюдатель со стороны сервера увидит истёкший
# таймаут ("expired"), а не молчаливый провал.
RC_REPARSE_REFUSED = 4


def _registry_path_of(config: HubConfig) -> Path:
    return Path(config.registry_path) if config.registry_path else default_registry_path()


def _write(result_file: Path, payload: dict, *, fallback_rc: int) -> int:
    """
    Записывает результат и возвращает итоговый код возврата ПРОЦЕССА.

    ``ReparseGuardError`` (GAP-311 В7) — путь result-файла (или его каталог)
    оказался reparse point/symlink, подложенным ДО повышения прав: запись
    ОТКАЗАНА целиком (не пишем даже часть), явное сообщение в stderr,
    ``RC_REPARSE_REFUSED`` вместо обычного кода. Прочие ``OSError``
    (диск read-only и т.п.) — best-effort, процесс всё равно должен
    завершиться с ``fallback_rc``, а не повиснуть.
    """
    try:
        write_result_atomic(result_file, payload)
    except ReparseGuardError as exc:
        print(f"[standkit-hub-elevated-op] отказ записи результата: {exc}", file=sys.stderr)
        return RC_REPARSE_REFUSED
    except OSError:
        pass
    return fallback_rc


def run(
    *,
    stand: str,
    action: str,
    result_file: Path,
    config_path: Optional[Path] = None,
    initiator_sid: Optional[str] = None,
) -> int:
    """
    Выполняет ОДНУ операцию (start/stop/restart) над стендом IIS с текущими
    (elevated) правами и пишет результат в ``result_file``.

    Возвращает код выхода процесса: ``0`` — успех, ``1`` — ошибка/отказ
    валидации, ``3`` — отказ по несовпадению учётной записи (тот же код, что
    у отказа полного перезапуска в ``standkit_hub.__main__`` — единый
    контракт кодов возврата elevated-режимов). Процесс никогда не должен
    остаться «висеть» без результата: любой непредвиденный сбой тоже пишется
    в файл как ``error``, а не пробрасывается наружу трейсбеком (родитель —
    ``ShellExecuteW``, у него нет stderr, который кто-то читает).
    """
    result_file = Path(result_file)

    # Проверка учётки, подтвердившей UAC (GAP-311 п.4) — та же логика, что у
    # полного перезапуска в __main__.main(), но здесь решение принимается
    # ДО чтения конфига/реестра: чужой процесс не должен даже прочитать файлы
    # текущего пользователя.
    if initiator_sid:
        our_sid = current_user_sid()
        if our_sid and our_sid != initiator_sid:
            return _write(
                result_file,
                {
                    "status": "refused",
                    "message": REFUSAL_TEXT_TEMPLATE.format(user=current_user_name() or "неизвестно"),
                    "user": current_user_name(),
                    "at": time.time(),
                },
                fallback_rc=3,
            )

    if action not in ACTIONS:
        return _write(
            result_file,
            {"status": "error", "message": f"неизвестное действие: {action!r}", "at": time.time()},
            fallback_rc=1,
        )

    # Повторная валидация имени стенда (В7, defense in depth): этот процесс
    # получает ``stand`` через argv УЖЕ elevated — сервер валидировал его ДО
    # запуска ``runas``, но argv в принципе виден/подменяем другим процессом
    # той же машины, и elevated-режим не должен доверять ему слепо.
    if not _security.validate_stand_name(stand):
        return _write(
            result_file,
            {"status": "error", "message": "недопустимое имя стенда", "at": time.time()},
            fallback_rc=1,
        )

    cfg_path = Path(config_path) if config_path else HubConfig.config_path()
    try:
        config = HubConfig.load(cfg_path)
        registry = Registry.load(_registry_path_of(config))
    except (OSError, RegistryError, ValueError) as exc:
        return _write(
            result_file,
            {"status": "error", "message": f"конфигурация/реестр не прочитаны: {exc}", "at": time.time()},
            fallback_rc=1,
        )

    if stand not in registry:
        return _write(
            result_file,
            {"status": "error", "message": f"стенд '{stand}' не найден", "at": time.time()},
            fallback_rc=1,
        )

    # Белый список: только IIS. Прочие host_kind не нуждаются в elevation
    # (kestrel/docker/k8s управляются без прав администратора), и пускать их
    # через этот режим означало бы напрасный запрос UAC.
    record = registry.get(stand)
    if record.host_kind != HostKind.IIS:
        return _write(
            result_file,
            {
                "status": "error",
                "message": "однократная операция с правами доступна только для стендов IIS",
                "at": time.time(),
            },
            fallback_rc=1,
        )

    client = FederatedClient(registry)
    try:
        if action in ("stop", "restart"):
            result = getattr(client, action)(stand, force=False)
        else:
            result = getattr(client, action)(stand)
    except (RemoteCallError, SecretError, HostingError, LifecycleError) as exc:
        return _write(result_file, {"status": "error", "message": str(exc), "at": time.time()}, fallback_rc=1)
    except Exception as exc:  # noqa: BLE001 - последняя линия защиты: процесс обязан оставить результат
        return _write(result_file, {"status": "error", "message": str(exc), "at": time.time()}, fallback_rc=1)

    payload: dict = {"status": "ok", "message": "", "at": time.time()}
    if isinstance(result, int):
        payload["pid"] = result
    return _write(result_file, payload, fallback_rc=0)
