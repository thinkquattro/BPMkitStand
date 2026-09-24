"""
Точка входа веб-дашборда: ``python -m standkit_hub`` (или консольный скрипт
``standkit-gui``/``standkit-hub`` после установки пакета).

По умолчанию хаб слушает ``127.0.0.1`` на ФИКСИРОВАННОМ порту
``DEFAULT_HUB_PORT`` (8770), печатает URL с одноразовым сессионным токеном и
открывает системный браузер. Фиксированный порт — осознанное решение: origin
(схема+хост+ПОРТ) является ключом браузерного localStorage и HTTP-кэша, и на
прежнем эфемерном порту каждый запуск давал новый origin — тема не
запоминалась, кэш статики был вечно холодным, закладка протухала. Если порт
занят, хаб не падает: сначала проверяет, не занял ли его НАШ ЖЕ работающий
экземпляр (single-instance, см. ``server.probe_hub_instance``) — тогда просто
открывает браузер на нём и выходит, не плодя второй фоновый поллер; если порт
занял чужой сервис — откатывается на эфемерный и честно об этом пишет
(``--port 0`` — явная просьба эфемерного порта). Флаг
``--desktop`` — опциональная нативная оболочка через ``pywebview`` (extra
``standkit[desktop]``); при отсутствии пакета хаб печатает понятное
сообщение и падает обратно в браузер, а не роняется исключением импорта.

БЕЗОПАСНОСТЬ: см. standkit_hub/security.py и standkit_hub/server.py —
хаб управляет процессами стендов (RCE-поверхность), поэтому secure-defaults
идентичны headless-агенту: loopback-only, fail-closed на non-loopback без
``--insecure``.
"""

from __future__ import annotations

import argparse
import errno as _errno
import logging
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

from standkit import __version__ as _standkit_version
from standkit.platform import current_user_name, current_user_sid, is_elevated
from standkit_hub import instance as _instance
from standkit_hub.config import HubConfig
from standkit_hub import hub_logging as _hub_logging
from standkit_hub.mutex import acquire_hub_mutex
from standkit_hub import preload as _preload
from standkit_hub.elevation import ReparseGuardError, read_handoff, refusal_text, write_result_atomic
from standkit_hub.security import InsecureBindError, generate_session_token
from standkit_hub.server import DEFAULT_HUB_PORT, HubAlreadyRunning, bind_hub_server
from standkit_hub.shortcut import install_desktop_shortcut, uninstall_desktop_shortcut

# Лог диспетчера (GAP-384). Под pythonw.exe stderr не подключён никуда,
# поэтому всё, что объясняет отказ или завершение, идёт в файл.
_log = _hub_logging.logger()

# Сколько в сумме ждать строгий bind перехваченного порта (GAP-311 В4/В6):
# порт может освободиться не мгновенно даже после успешной остановки старого
# процесса — повторяем bind, а не пытаемся один раз.
_TAKEOVER_BIND_TIMEOUT_SEC = 20.0
_TAKEOVER_BIND_RETRY_SEC = 0.5


def _bind_with_retries(
    bind_fn,
    *,
    timeout: float = _TAKEOVER_BIND_TIMEOUT_SEC,
    poll_interval: float = _TAKEOVER_BIND_RETRY_SEC,
    sleep=time.sleep,
    clock=time.monotonic,
):
    """
    Повторяет ``bind_fn()`` до первого успеха либо истечения ``timeout``
    (GAP-311 В4/В6): после остановки перехваченного процесса ОС не всегда
    готова тут же отдать порт повторно (закрытие сокета — не гарантированно
    мгновенная операция), а одна неудачная попытка не должна ронять весь
    перехват. Пробрасывает последнее исключение, если время вышло.
    """
    deadline = clock() + timeout
    while True:
        try:
            return bind_fn()
        except (HubAlreadyRunning, OSError):
            if clock() >= deadline:
                raise
            sleep(poll_interval)


def _write_relaunch_result(result_file: str, *, status: str, message: str = "", **extra) -> None:
    """
    Пишет исход попытки перезапуска/перехвата в ``result_file`` (GAP-311
    В4/В5) — ``refused``/``serving``/``failed``. Единственная точка входа для
    этого из ``main()``: гарантирует одинаковый формат payload'а и не даёт
    процессу упасть на самой записи результата (reparse-отказ/диск только на
    чтение — best-effort, сообщение в stderr).

    ОСТАТОЧНЫЙ РИСК (задокументировано осознанно, а не забыто): между тем,
    как старый процесс получает файл-запрос остановки (см. ``standkit_hub.instance``,
    GAP-311 Б1) и реально останавливается, и тем, как этот (новый) процесс
    успевает выполнить bind, — есть окно, в котором "старый уже остановлен, а
    новый ещё не поднялся". Если в ЭТОМ окне новый процесс упадёт (краш,
    OOM-killer, потеря диска) ДО записи ``failed``, пользователь на секунды
    останется без диспетчера вовсе — приём "строгий bind с повторами до
    таймаута" уменьшает вероятность, но не устраняет её полностью; полное
    устранение потребовало бы транзакционной передачи порта между процессами,
    чего ОС не предоставляет.
    """
    payload: dict = {"status": status, "at": time.time()}
    if message:
        payload["message"] = message
    payload.update(extra)
    try:
        write_result_atomic(Path(result_file), payload)
    except (OSError, ReparseGuardError) as exc:
        _log.warning(f"не удалось записать результат перезапуска: {exc}")


def _describe_elevation(value) -> str:
    """Человеческий ответ на «с правами администратора ли процесс» (включая «неизвестно»)."""
    if value is None:
        return "неизвестно"
    return "да" if value else "нет"


def _takeover_running_instance(
    exc: HubAlreadyRunning, state_file: Path, run_dir: Path, *, explicit: bool, our_sid: Optional[str]
) -> "tuple[bool, str]":
    """
    Отобрать ли порт у уже работающего диспетчера — и, если да, попросить его
    остановиться штатно (файл-запрос остановки, GAP-311 Б1: НЕ убивает дерево
    процессов — живые kestrel-стенды и локальный агент остаются работать) и
    дождаться освобождения порта.

    Возвращает ``(ok, reason)`` (GAP-311 M4) — ``ok=True`` только если порт
    реально свободен и повторный bind имеет смысл; ``reason`` пуст в этом
    случае, иначе — человекочитаемая причина отказа (уходит в
    ``result_file`` вызывающего, а не заменяется обобщённым текстом). Правила
    решения — в ``standkit_hub.instance.should_takeover`` (коротко: явный
    ``--takeover`` либо «мы elevated, а он нет», за исключением случая, когда
    работающий экземпляр принадлежит ДРУГОЙ учётной записи — тогда
    автоматический перехват не делаем, см. GAP-311 п.4).
    """
    state = _instance.read_state(state_file)
    if not _instance.should_takeover(state, we_elevated=is_elevated(), explicit=explicit, our_sid=our_sid):
        return False, ""

    if state is None:
        # GAP-311 M8: стоп-запрос (Б1) адресуется ПО PID из файла состояния —
        # без него штатная остановка невозможна ВООБЩЕ (кнопка дашборда
        # больше НЕ завершает старый процесс сама, только через тот же
        # адресованный файл-запрос). Раньше здесь ждали освобождения порта до
        # 20с в надежде, что уходящий экземпляр как-то завершится сам —
        # надежда была обоснована ТОЛЬКО пока кнопка звала shutdown()
        # напрямую; сейчас это просто впустую потраченное время пользователя.
        # Отказ немедленно, без ожидания.
        reason = "файл состояния работающего диспетчера не найден — закройте его вручную и запустите снова"
        _log.warning(f"{reason}")
        return False, reason

    _log.warning(f"перехватываю порт {exc.port} у работающего диспетчера "
        f"(pid {state.pid}, права администратора: {_describe_elevation(state.elevated)})"
    )
    ok, reason = _instance.stop_running_instance(state, run_dir=run_dir, requester_pid=os.getpid())
    if not ok:
        message = reason or f"не удалось остановить процесс {state.pid}"
        print(f"[standkit-hub] {message} — перехват отменён")
        return False, message

    if not _instance.wait_port_released(exc.host, exc.port):
        reason = f"порт {exc.port} так и не освободился — перехват отменён"
        _log.warning(f"{reason}")
        return False, reason
    return True, ""


def _warn_if_run_dir_outside_profile(run_dir: Path) -> None:
    """
    WARN в лог диспетчера при старте, если ``run_dir`` (файл состояния,
    handoff, stop-request — см. ``standkit_hub.instance``/``elevation``)
    сконфигурирован ВНЕ домашнего каталога пользователя (GAP-311 M14).

    ЗАЧЕМ. ``write_handoff``/``instance._atomic_write_text`` ставят режим
    ``0o600`` при создании файла — на Windows это НЕ устанавливает ACL (см.
    docstring ``elevation.write_handoff``): реальная защита файла с токеном
    сессии целиком полагается на то, что ``run_dir`` лежит внутри профиля
    пользователя, куда по умолчанию (без явного расшаривания) нет доступа у
    других локальных учёток. Нестандартная конфигурация (``run_dir`` указан
    в общей/сетевой папке) тихо ломает это предположение — лучше явно
    предупредить в лог при каждом старте, чем оставить дыру незамеченной.

    Best-effort: ошибка определения домашнего каталога (напр. переменные
    окружения не заданы) — тихо пропускается, а не роняет запуск.
    """
    try:
        home = Path.home().resolve()
        resolved = Path(run_dir).resolve()
    except OSError:
        return
    try:
        resolved.relative_to(home)
    except ValueError:
        _log.warning(
            f"ВНИМАНИЕ: run_dir ({resolved}) находится вне домашнего "
            f"каталога пользователя ({home}) — файлы с правами 0o600 (handoff, состояние) "
            "на Windows не защищены ACL профиля; убедитесь, что каталог недоступен другим "
            "локальным учётным записям"
        )


def _cmd_hub_stop(state_file: Path, run_dir: Path) -> int:
    """CLI ``--hub-stop`` (GAP-445): останавливает диспетчер, работающий в ЭТОМ профиле,
    штатно (``_instance.stop_running_instance`` -- без убийства дерева процессов, живые
    стенды не трогает) и ПЕРЕД остановкой печатает ``HUB_SESSION=<id>`` -- номер сеанса
    служб терминалов (WinAPI ``ProcessIdToSessionId``, ``standkit.platform.current_session_id``)
    ОСТАНАВЛИВАЕМОГО процесса, снятый из файла состояния (``HubInstanceState.session_id``),
    а НЕ у текущего процесса (у него своего диспетчера нет -- этот вызов его и не поднимает).

    Установщик (``bpmkit_installer.iss``) и человек, читающий ``/LOG``/``setup_cli.log``
    после честного отказа «диспетчер запущен в другом сеансе» (GAP-445 (а)), используют эту
    строку, чтобы понять, В КАКОМ сеансе диспетчер работал -- остановить его там руками, если
    штатная остановка (``--hub-stop``, вызывается уже ИЗ ТОГО сеанса) недоступна.

    Диспетчера в этом профиле нет (файла состояния нет либо записанный в нём процесс уже
    мёртв) -- честный отказ, ``HUB_SESSION=`` не печатается вовсе (называть нечего)."""
    state = _instance.read_state(state_file)
    if state is None:
        print("[standkit-hub] диспетчер не запущен (файл состояния не найден или устарел)")
        return 1
    session_label = state.session_id if state.session_id is not None else "unknown"
    print(f"HUB_SESSION={session_label}")
    ok, reason = _instance.stop_running_instance(state, run_dir=run_dir)
    if not ok:
        print(f"[standkit-hub] не удалось остановить диспетчер: {reason}")
        return 1
    print(f"[standkit-hub] диспетчер (pid={state.pid}) остановлен")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="standkit-gui",
        description="Локальный веб-дашборд standkit — диспетчер стендов BPMSoft (вариант A: браузер/pywebview)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="адрес, на котором слушать (по умолчанию 127.0.0.1 — loopback-only, secure default)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_HUB_PORT,
        help=(
            f"порт (по умолчанию {DEFAULT_HUB_PORT}; если занят — автоматический откат "
            "на эфемерный. 0 — сразу эфемерный свободный порт, выбирается ОС)"
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help="путь к конфигу хаба (по умолчанию — %%APPDATA%%\\BPMkit\\standkit-hub.json / ~/.config/BPMkit/standkit-hub.json)",
    )
    parser.add_argument("--no-browser", action="store_true", help="не открывать системный браузер автоматически")
    parser.add_argument(
        "--desktop",
        action="store_true",
        help="открыть дашборд в нативном окне pywebview вместо браузера (требует extra standkit[desktop])",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="ОСОЗНАННЫЙ обход fail-closed проверки bind (non-loopback host без TLS) — только dev/тест",
    )
    parser.add_argument(
        "--install-shortcut",
        action="store_true",
        help="создать ярлык дашборда на рабочем столе и выйти (без запуска сервера)",
    )
    parser.add_argument(
        "--uninstall-shortcut",
        action="store_true",
        help="удалить ранее созданный ярлык дашборда и выйти (без запуска сервера)",
    )
    parser.add_argument(
        "--hub-stop",
        action="store_true",
        help=(
            "остановить работающий в ЭТОМ профиле диспетчер штатно и выйти, не поднимая "
            "свой HTTP-сервер -- печатает HUB_SESSION=<id> (номер сеанса служб терминалов "
            "остановленного процесса, ProcessIdToSessionId) перед остановкой, GAP-445"
        ),
    )
    parser.add_argument(
        "--takeover",
        action="store_true",
        help=(
            "отобрать порт у уже работающего диспетчера (остановив его) вместо того, чтобы "
            "просто открыть браузер на нём — так себя перезапускает кнопка «Перезапустить "
            "с правами администратора»"
        ),
    )
    parser.add_argument(
        "--session-token-file",
        default=None,
        help=(
            "файл передачи сессии от предыдущего экземпляра (одноразовый, удаляется при "
            "чтении) — чтобы уже открытая вкладка дашборда осталась авторизованной после "
            "перезапуска с правами администратора"
        ),
    )
    parser.add_argument(
        "--initiator-sid",
        default=None,
        help=(
            "SID пользователя Windows, ЗАПРОСИВШЕГО повышение прав (внутренний флаг "
            "перезапуска/одноразовой операции с правами, GAP-311 п.4) — если запрос UAC "
            "подтвердила ДРУГАЯ учётная запись, процесс отказывается работать и пишет "
            "причину в --result-file"
        ),
    )
    parser.add_argument(
        "--result-file",
        default=None,
        help=(
            "файл, куда пишется исход попытки повышения прав ({status: accepted|refused, ...}) "
            "— внутренний флаг, пользователем не задаётся вручную"
        ),
    )
    parser.add_argument(
        "--elevated-op",
        choices=("start", "stop", "restart"),
        default=None,
        help=(
            "выполнить ОДНУ операцию с правами администратора над стендом (--stand) и выйти, "
            "без запуска HTTP-сервера — внутренний режим, поднимаемый диспетчером через UAC "
            "(GAP-311 п.6)"
        ),
    )
    parser.add_argument(
        "--stand",
        default=None,
        help="имя стенда для --elevated-op",
    )
    parser.add_argument(
        "--apply-self-update",
        action="store_true",
        help=(
            "режим ПОМОЩНИКА самообновления диспетчера (GAP-523) — внутренний флаг, "
            "которым hub_channel.apply_self_update запускает ЗАСТЕЙДЖЕННЫЙ exe поверх "
            "работающего; ждёт выхода --wait-pid, подменяет --target собой, запускает "
            "его и выходит, без bind порта и без остального старта"
        ),
    )
    parser.add_argument(
        "--target",
        default=None,
        help="путь к exe, который нужно подменить (только с --apply-self-update)",
    )
    parser.add_argument(
        "--wait-pid",
        type=int,
        default=None,
        help="pid процесса, чьего выхода нужно дождаться перед подменой (только с "
             "--apply-self-update)",
    )
    args = parser.parse_args(argv)

    # Лог поднимаем ПЕРВЫМ делом после разбора аргументов — до mutex/bind/
    # state и до любой ветки, которая может завершиться отказом: инцидент
    # 18.09.2026 показал, что дороже всего обходятся именно те сообщения,
    # которые процесс успел бы написать ДО того, как что-то пошло не так.
    # Неудача настройки лога старт НЕ прерывает (см. hub_logging.setup_logging).
    _log_path = _hub_logging.setup_logging()
    _log.info(
        "старт standkit-hub: версия=%s python=%s pid=%s лог=%s уровень=%s (%s=%s)",
        _standkit_version, sys.version.split()[0], os.getpid(), _log_path or "(нет)",
        logging.getLevelName(_log.level),
        _hub_logging.LOG_LEVEL_ENV, os.environ.get(_hub_logging.LOG_LEVEL_ENV) or "не задана",
    )

    if args.apply_self_update:
        # GAP-523: ДО preload/mutex/bind/state — тот же принцип, что у
        # --elevated-op ниже: одноразовый процесс "выполнить и выйти", HTTP-
        # сервер ему не нужен вовсе, и открывать порт/мьютекс вторым
        # экземпляром, пока СТАРЫЙ (--wait-pid) ещё жив, было бы конфликтом на
        # пустом месте. Логирование уже поднято строкой выше — помощник
        # обязан оставить след в том же файле, что и обычный старт.
        from standkit_hub import self_update as _self_update

        if not args.target or not args.wait_pid:
            print("[standkit-hub] --apply-self-update требует --target и --wait-pid")
            return 1
        return _self_update.run_self_update_helper(
            target=args.target, wait_pid=args.wait_pid)

    # Необработанное исключение обязано остаться в логе, а не исчезнуть вместе
    # с невидимым stderr: без этого «диспетчер просто пропал» — всё, что
    # человек может сообщить о падении.
    def _log_unhandled(exc_type, exc_value, exc_tb):
        _log.critical("необработанное исключение — процесс завершается",
                      exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = _log_unhandled

    # GAP-412: все модули пакетов — в память ДО того, как начнётся работа.
    # `pip install -U` под живым процессом заменяет файлы на диске, и ленивый
    # import внутри функции после этого притащил бы НОВЫЙ модуль в СТАРЫЙ
    # процесс (см. standkit_hub.preload). Один проход при старте делает такой
    # import безвредным поиском в sys.modules.
    _loaded, _failed = _preload.preload()
    _log.info("модули пакетов загружены при старте: %d", len(_loaded))
    for _name, _reason in _failed:
        _log.warning("модуль %s не загрузился при старте: %s", _name, _reason)

    if args.elevated_op:
        # ДО любых bind/mutex/state/handoff: это одноразовый процесс "выполнить
        # и выйти", HTTP-сервер ему не нужен вовсе (см. standkit_hub.elevated_op).
        from standkit_hub import elevated_op as _elevated_op

        if not args.stand or not args.result_file:
            print("[standkit-hub] --elevated-op требует --stand и --result-file")
            return 1
        config_path = Path(args.config) if args.config else None
        return _elevated_op.run(
            stand=args.stand,
            action=args.elevated_op,
            result_file=Path(args.result_file),
            config_path=config_path,
            initiator_sid=args.initiator_sid,
        )

    if args.install_shortcut or args.uninstall_shortcut:
        result = install_desktop_shortcut() if args.install_shortcut else uninstall_desktop_shortcut()
        _log.warning(f"{result.message}")
        return 0 if result.ok else 1

    our_sid = current_user_sid()

    # Проверка учётки, подтвердившей UAC (GAP-311 п.4/В4) — ПЕРВЫМ ДЕЛОМ, до
    # HubConfig.load()/ensure_registry_dir(): --initiator-sid кладёт туда SID
    # пользователя, который ЗАПРОСИЛ повышение (кнопка "Перезапустить с
    # правами администратора" / одноразовая операция). Если запрос UAC
    # подтвердила ДРУГАЯ учётка — этот процесс не имеет права даже ПРОЧИТАТЬ
    # конфиг/реестр текущего пользователя, не то что занять его порт: реестр
    # стендов, ключи шифрования секретов и файлы диспетчера привязаны к
    # профилю Windows, и "просто продолжить" означало бы тихую потерю доступа
    # к своим же данным для исходного пользователя.
    if args.initiator_sid:
        if our_sid and our_sid != args.initiator_sid:
            message = refusal_text(current_user_name())
            print(f"[standkit-hub] {message}")
            if args.result_file:
                _write_relaunch_result(
                    args.result_file, status="refused", message=message, user=current_user_name()
                )
            return 3
        # SID совпал (или не определить ни с одной стороны) — молча
        # продолжаем. "Accepted" здесь больше НЕ пишется (В4/В5): протокол
        # заменён на явные "serving"/"failed" ПОСЛЕ реального перехвата и
        # bind'а (см. ниже) — раньше "accepted" сообщало лишь о совпадении
        # SID, а не о том, что новый процесс действительно поднял сервер.

    config_path = Path(args.config) if args.config else HubConfig.config_path()
    config = HubConfig.load(config_path)

    # Первый запуск: заранее создаём папку реестра проектов (напр.
    # %APPDATA%\BPMkit), чтобы показываемый путь к projects.json указывал на
    # реальную папку, а не «в никуда» (иначе открытие пути в проводнике даёт
    # «Windows не удаётся найти …»). Best-effort — сбой mkdir не должен ронять
    # запуск диспетчера.
    try:
        config.ensure_registry_dir()
    except OSError as exc:
        _log.warning(f"не удалось подготовить папку реестра: {exc}")

    run_dir = config.resolve_run_dir()
    _warn_if_run_dir_outside_profile(run_dir)
    state_file = _instance.state_path(run_dir)

    if args.hub_stop:
        return _cmd_hub_stop(state_file, run_dir)

    # Сессия от предыдущего экземпляра (перезапуск с правами администратора):
    # файл одноразовый и протухающий, поэтому «не прочитали» — штатный исход,
    # а не отказ запускаться. Цена — вкладку придётся открыть заново.
    session_token = ""
    if args.session_token_file:
        session_token = read_handoff(Path(args.session_token_file)) or ""
        if not session_token:
            _log.warning(
                "сессию предыдущего экземпляра перенести не удалось "
                "(файл передачи отсутствует или протух) — открывайте дашборд заново по ярлыку"
            )
    if not session_token:
        session_token = generate_session_token()

    # GAP-311 Н2: намерение перехвата — явный --takeover ЛИБО факт, что нас
    # подняли специально ЗАМЕНИТЬ работающий экземпляр (--result-file,
    # кнопка дашборда/перезапуск с правами). В обоих случаях ПЕРВЫЙ bind
    # тоже обязан быть строгим (без отката на эфемерный порт): если порт
    # занят НЕ нашим хабом (иначе был бы HubAlreadyRunning, а не OSError),
    # тихий откат на случайный порт означал бы, что перехвата не будет
    # вовсе, а пользователь всё равно увидит "успех" — на чужом порту.
    takeover_intent = bool(args.takeover or args.result_file)

    def _report_port_busy(requested: int, exc: OSError) -> None:
        # Печатаем ДО повторного bind'а: пользователь должен понимать, почему
        # адрес в консоли/закладке вдруг отличается от привычного.
        _log.warning(f"порт {requested} занят ({exc.strerror or exc}) — беру свободный")

    def _bind():
        return bind_hub_server(
            args.host,
            args.port,
            config_path=config_path,
            session_token=session_token,
            insecure=args.insecure,
            on_fallback=None if takeover_intent else _report_port_busy,
            desktop_mode=args.desktop,
            strict_port=takeover_intent,
        )

    def _bind_strict():
        # Перехват (GAP-311 В6): БЕЗ отката на эфемерный порт. Новый процесс
        # обязан занять ИМЕННО тот порт, на котором висела уже открытая
        # вкладка (origin — часть контракта сессии/localStorage) — откат на
        # случайный порт сделал бы перехват бессмысленным.
        return bind_hub_server(
            args.host,
            args.port,
            config_path=config_path,
            session_token=session_token,
            insecure=args.insecure,
            desktop_mode=args.desktop,
            strict_port=True,
        )

    try:
        httpd = _bind()
    except HubAlreadyRunning as exc:
        # Развилка первая — ПЕРЕХВАТ. Запуск «от имени администратора» (вручную
        # или кнопкой дашборда) существует ровно затем, чтобы заменить собой
        # процесс без прав: без elevation appcmd.exe не управляет IIS. Раньше
        # такой запуск молча открывал браузер на СТАРОМ, неэлевированном
        # экземпляре — пользователь видел ту же ошибку прав, будучи уверен, что
        # всё сделал правильно.
        takeover_ok, takeover_reason = _takeover_running_instance(
            exc, state_file, run_dir, explicit=args.takeover, our_sid=our_sid
        )
        if takeover_ok:
            # Порт мог освободиться формально (сокет закрыт), но ОС не всегда
            # готова тут же отдать его повторно — поэтому bind СТРОГО на тот
            # же порт делается С ПОВТОРАМИ до таймаута (GAP-311 В4/В6), а не
            # одной попыткой.
            try:
                httpd = _bind_with_retries(_bind_strict)
            except (HubAlreadyRunning, InsecureBindError, OSError) as exc2:
                message = f"перехват не удался: {exc2}"
                _log.warning(f"{message}")
                if args.result_file:
                    _write_relaunch_result(args.result_file, status="failed", message=message)
                return 1
        elif args.result_file:
            # Нас подняли специально ЗАМЕНИТЬ работающий экземпляр (кнопка
            # дашборда/ярлык «от администратора» с --result-file), но условия
            # перехвата (standkit_hub.instance.should_takeover) не выполнены
            # — молча открыть браузер на старом здесь НЕЛЬЗЯ: старый процесс
            # ждёт понятного исхода в result-файле, а не тишины (В4/В5).
            # GAP-311 M4: реальная причина отказа (SID не подтверждён,
            # не удалось остановить и т.п.), если она есть, — а не
            # обобщённый текст.
            message = takeover_reason or "перехват порта не выполнен: работающий экземпляр перехвату не подлежит"
            _log.warning(f"{message}")
            _write_relaunch_result(args.result_file, status="failed", message=message)
            return 1
        else:
            return _open_running_instance(exc, no_browser=args.no_browser)
    except InsecureBindError as exc:
        _log.warning(f"{exc}")
        if args.result_file:
            _write_relaunch_result(args.result_file, status="failed", message=str(exc))
        return 1
    except OSError as exc:
        # Порт не занят НАШИМ хабом (иначе был бы HubAlreadyRunning), но bind
        # всё равно не удался: либо порт занят ЧУЖИМ приложением (GAP-311 Н2
        # — при takeover_intent сюда попадаем именно в этом случае, отклик
        # строгий, без отката), либо иная причина (нет прав, недоступный
        # адрес) — честный отказ с понятным текстом вместо трейсбека.
        if takeover_intent and exc.errno in (_errno.EADDRINUSE, _errno.EACCES):
            # Проба не узнала хаб на порту, но это ещё не значит «чужое
            # приложение»: зависший диспетчер не отвечает за таймаут пробы.
            # Живой pid из файла состояния на том же порту — наш хаб, и текст
            # должен направить к нему, а не искать постороннюю программу
            # (повторное ревью GAP-311, М-Б).
            hung = _instance.read_state(state_file)
            if hung is not None and hung.port == args.port:
                message = (
                    f"работающий диспетчер (pid {hung.pid}) не ответил на проверку — "
                    "возможно, он завис; закройте его вручную и повторите"
                )
            else:
                message = f"порт {args.port} занят другим приложением"
        else:
            message = f"не удалось занять {args.host}:{args.port} — {exc}"
        _log.warning(f"{message}")
        if args.result_file:
            _write_relaunch_result(args.result_file, status="failed", message=message)
        return 1

    # GAP-229/GAP-284: этот процесс подтверждённо поднимает СВОЙ сервер (не открывает
    # браузер на уже работающем экземпляре -- тот путь завершился раньше, через
    # HubAlreadyRunning выше) -- сигналим установщику/деинсталлятору BPMkit до входа в
    # цикл обслуживания, тем же приёмом, что core.acquire_server_mutex() у сервера
    # (BPMkit/server/bpmkit/core.py репозитория bpmsoft-mcp, вызывается перед mcp.run()).
    acquire_hub_mutex()

    actual_port = httpd.server_address[1]
    if args.port and actual_port != args.port:
        _log.warning(f"порт {args.port} занят, слушаю {actual_port}")
    # Файл состояния — чтобы СЛЕДУЮЩИЙ запуск знал, кого он видит на порту
    # (в т.ч. с правами администратора тот процесс или нет).
    elevated = is_elevated()
    try:
        _instance.write_state(
            state_file,
            _instance.current_state(args.host, actual_port, elevated=elevated, user_sid=our_sid),
        )
    except (OSError, ReparseGuardError) as exc:
        print(f"[standkit-hub] не удалось записать файл состояния: {exc}")

    # Д-3: штатный выход (кнопка «Выход» и автовыход по простою) обязан убрать
    # файл состояния ДО завершения процесса — иначе следующий запуск увидит
    # «диспетчер уже работает» с pid'ом мёртвого. Отдаём серверу путь, а не
    # делаем это в его `finally`: уборка в `_serve` сработает позже, когда
    # `serve_forever` уже вышел, а знать об исчезновении диспетчера полезно
    # раньше. Повторный `clear_state` в `_serve.finally` безвреден.
    httpd.instance_state_file = state_file

    if args.result_file:
        # Успех: сервер реально поднялся на порту (GAP-311 В4/В5) — старый
        # процесс (если он ещё жив — обычно уже нет, см. docstring
        # _write_relaunch_result) узнает об этом только для отражения в UI;
        # своё завершение он планирует НЕ по этому файлу, а по факту
        # получения файла-запроса остановки (см. standkit_hub.instance,
        # GAP-311 Б1) — тот уходит РАНЬШЕ, на шаге перехвата порта.
        _write_relaunch_result(args.result_file, status="serving", pid=os.getpid(), port=actual_port)

    url = f"http://{args.host}:{actual_port}/?t={session_token}"
    print(f"[standkit-hub] дашборд слушает {args.host}:{actual_port}")
    print(f"[standkit-hub] права администратора: {_describe_elevation(elevated)}")
    print(f"[standkit-hub] откройте: {url}")
    return _serve(httpd, args=args, url=url, state_file=state_file)


def _open_running_instance(exc: HubAlreadyRunning, *, no_browser: bool) -> int:
    """
    Второй запуск по ярлыку (обычный сценарий: окно браузера закрыли, а
    процесс под pythonw остался жить — idle-shutdown у хаба нет). Второй
    сервер здесь не нужен и вреден: два фоновых поллера над одним реестром
    плюс разъехавшийся localStorage на другом origin. Просто открываем
    браузер на уже работающем экземпляре.

    Токен в URL НЕ подставляем — у нас его нет (он сгенерирован в чужом
    процессе). Работающий экземпляр узнает браузер по сессионной cookie,
    выданной при первом открытии; если cookie не пережила полное закрытие
    браузера, дашборд честно скажет об этом (см. app.js, разбор 401), и
    достаточно перезапустить диспетчер.
    """
    print(f"[standkit-hub] диспетчер уже работает на {exc.host}:{exc.port} — открываю его, второй не запускаю")
    if not no_browser:
        webbrowser.open(exc.url)
    return 0


def _serve(httpd, *, args, url: str, state_file: Path) -> int:
    """
    Блокирующая часть запуска: окно pywebview либо ``serve_forever``.

    Вынесена из ``main`` вместе с уборкой файла состояния — выходов здесь
    несколько (импорт pywebview не удался, закрыли окно, Ctrl+C), и забыть
    убрать за собой в одном из них было бы легко.
    """
    try:
        return _serve_inner(httpd, args=args, url=url)
    finally:
        # pid — защита от гонки: если наш порт уже перехватил новый экземпляр
        # (перезапуск с правами администратора), файл состояния уже ЕГО.
        _instance.clear_state(state_file, pid=os.getpid())


def _make_desktop_stop_callback(httpd, webview_module):
    """
    Колбэк для ``httpd.on_stop_request`` в desktop-режиме (GAP-311 Н3).

    Вызывается из ФОНОВОГО потока наблюдателя (``standkit_hub.server.
    _StopRequestWatcher``), НЕ из главного потока, в котором блокирует
    ``webview.start()`` — закрытие окон (``w.destroy()``) из чужого потока —
    штатный способ управления pywebview именно для такого случая (окно само
    вызывает закрытие обработчиком событий из любого потока). После закрытия
    ВСЕХ окон ``webview.start()`` в главном потоке возвращает управление, и
    ``_serve_inner`` доходит до своего ``finally`` (``httpd.shutdown()`` +
    ``server_close()``) как при обычном закрытии окна руками.

    Список окон копируется (``list(...)``) перед обходом: `destroy()` меняет
    сам ``webview.windows`` изнутри цикла — итерация по оригиналу словила бы
    ``RuntimeError: list changed size during iteration``.
    """

    def _stop() -> None:
        for window in list(getattr(webview_module, "windows", [])):
            try:
                window.destroy()
            except Exception:
                pass  # окно уже закрывалось/недоступно — не мешаем остальным
        httpd.shutdown()

    return _stop


def _serve_inner(httpd, *, args, url: str) -> int:
    if args.desktop:
        try:
            import webview  # type: ignore
        except ImportError:
            _log.warning(
                "pywebview не установлен (pip install standkit[desktop]) — открываю в системном браузере"
            )
            if not args.no_browser:
                webbrowser.open(url)
        else:
            # GAP-311 Н3: файл-запрос остановки (Б1) по умолчанию делает
            # только httpd.shutdown() — этого хватает браузерному режиму
            # (выход из serve_forever), но НЕ desktop-режиму: webview.start()
            # в ЭТОМ (главном) потоке продолжал бы блокировать процесс сколь
            # угодно долго, ожидая, что пользователь САМ закроет окно. Колбэк
            # обязан закрыть окна ЯВНО, чтобы webview.start() вернул
            # управление и функция могла дойти до своего finally/return.
            httpd.on_stop_request = _make_desktop_stop_callback(httpd, webview)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                webview.create_window("Диспетчер стендов BPMkit", url)
                webview.start()
            finally:
                httpd.shutdown()
                httpd.server_close()
            return 0
    elif not args.no_browser:
        webbrowser.open(url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        # Ctrl+C — штатная остановка дашборда: печатаем понятное сообщение, а не
        # трейсбек KeyboardInterrupt из глубины serve_forever/selectors.
        print("\n[standkit-hub] остановлено (Ctrl+C)")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
