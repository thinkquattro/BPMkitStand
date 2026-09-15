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
import os
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

from standkit.platform import current_user_name, current_user_sid, is_elevated
from standkit_hub import instance as _instance
from standkit_hub.config import HubConfig
from standkit_hub.mutex import acquire_hub_mutex
from standkit_hub.elevation import read_handoff, refusal_text, write_result_atomic
from standkit_hub.security import InsecureBindError, generate_session_token
from standkit_hub.server import DEFAULT_HUB_PORT, HubAlreadyRunning, bind_hub_server
from standkit_hub.shortcut import install_desktop_shortcut, uninstall_desktop_shortcut


def _describe_elevation(value) -> str:
    """Человеческий ответ на «с правами администратора ли процесс» (включая «неизвестно»)."""
    if value is None:
        return "неизвестно"
    return "да" if value else "нет"


def _takeover_running_instance(
    exc: HubAlreadyRunning, state_file: Path, *, explicit: bool, our_sid: Optional[str]
) -> bool:
    """
    Отобрать ли порт у уже работающего диспетчера — и, если да, погасить его и
    дождаться освобождения порта.

    Возвращает True, только если порт реально свободен и повторный bind имеет
    смысл. Правила решения — в ``standkit_hub.instance.should_takeover``
    (коротко: явный ``--takeover`` либо «мы elevated, а он нет», за
    исключением случая, когда работающий экземпляр принадлежит ДРУГОЙ
    учётной записи — тогда автоматический перехват не делаем, см. GAP-311 п.4).
    """
    state = _instance.read_state(state_file)
    if not _instance.should_takeover(state, we_elevated=is_elevated(), explicit=explicit, our_sid=our_sid):
        return False

    if state is None:
        # --takeover без файла состояния: кого гасить — неизвестно, но уходящий
        # экземпляр мог уже начать самозавершение (так делает кнопка на
        # дашборде), поэтому просто ждём порт.
        print(f"[standkit-hub] жду освобождения порта {exc.port} (файла состояния нет)")
    else:
        print(
            f"[standkit-hub] перехватываю порт {exc.port} у работающего диспетчера "
            f"(pid {state.pid}, права администратора: {_describe_elevation(state.elevated)})"
        )
        if not _instance.stop_running_instance(state):
            print(
                f"[standkit-hub] не удалось остановить процесс {state.pid} — перехват отменён",
                file=sys.stderr,
            )
            return False

    if not _instance.wait_port_released(exc.host, exc.port):
        print(f"[standkit-hub] порт {exc.port} так и не освободился — перехват отменён", file=sys.stderr)
        return False
    return True


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
    args = parser.parse_args(argv)

    if args.elevated_op:
        # ДО любых bind/mutex/state/handoff: это одноразовый процесс "выполнить
        # и выйти", HTTP-сервер ему не нужен вовсе (см. standkit_hub.elevated_op).
        from standkit_hub import elevated_op as _elevated_op

        if not args.stand or not args.result_file:
            print("[standkit-hub] --elevated-op требует --stand и --result-file", file=sys.stderr)
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
        print(f"[standkit-hub] {result.message}")
        return 0 if result.ok else 1

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
        print(f"[standkit-hub] не удалось подготовить папку реестра: {exc}", file=sys.stderr)

    state_file = _instance.state_path(config.resolve_run_dir())
    our_sid = current_user_sid()

    # Проверка учётки, подтвердившей UAC (GAP-311 п.4): --initiator-sid кладёт
    # туда SID пользователя, который ЗАПРОСИЛ повышение (кнопка "Перезапустить
    # с правами администратора" / одноразовая операция). Если запрос UAC
    # подтвердила ДРУГАЯ учётка — этот процесс не имеет права молча занять
    # порт/реестр стендов текущего пользователя: реестр, ключи шифрования
    # секретов и файлы диспетчера привязаны к профилю Windows, и "просто
    # продолжить" означало бы тихую потерю доступа к своим же данным для
    # исходного пользователя. Поэтому — отказ ДО bind/mutex/state/handoff.
    if args.initiator_sid:
        if our_sid and our_sid != args.initiator_sid:
            message = refusal_text(current_user_name())
            print(f"[standkit-hub] {message}", file=sys.stderr)
            if args.result_file:
                try:
                    write_result_atomic(
                        Path(args.result_file),
                        {
                            "status": "refused",
                            "message": message,
                            "user": current_user_name(),
                            "at": time.time(),
                        },
                    )
                except OSError:
                    pass
            return 3
        if args.result_file:
            try:
                write_result_atomic(
                    Path(args.result_file),
                    {"status": "accepted", "at": time.time()},
                )
            except OSError:
                pass

    # Сессия от предыдущего экземпляра (перезапуск с правами администратора):
    # файл одноразовый и протухающий, поэтому «не прочитали» — штатный исход,
    # а не отказ запускаться. Цена — вкладку придётся открыть заново.
    session_token = ""
    if args.session_token_file:
        session_token = read_handoff(Path(args.session_token_file)) or ""
        if not session_token:
            print(
                "[standkit-hub] сессию предыдущего экземпляра перенести не удалось "
                "(файл передачи отсутствует или протух) — открывайте дашборд заново по ярлыку",
                file=sys.stderr,
            )
    if not session_token:
        session_token = generate_session_token()

    def _report_port_busy(requested: int, exc: OSError) -> None:
        # Печатаем ДО повторного bind'а: пользователь должен понимать, почему
        # адрес в консоли/закладке вдруг отличается от привычного.
        print(f"[standkit-hub] порт {requested} занят ({exc.strerror or exc}) — беру свободный", file=sys.stderr)

    def _bind():
        return bind_hub_server(
            args.host,
            args.port,
            config_path=config_path,
            session_token=session_token,
            insecure=args.insecure,
            on_fallback=_report_port_busy,
            desktop_mode=args.desktop,
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
        if _takeover_running_instance(exc, state_file, explicit=args.takeover, our_sid=our_sid):
            try:
                httpd = _bind()
            except (HubAlreadyRunning, InsecureBindError, OSError) as exc2:
                print(f"[standkit-hub] перехват не удался: {exc2}", file=sys.stderr)
                return 1
        else:
            return _open_running_instance(exc, no_browser=args.no_browser)
    except InsecureBindError as exc:
        print(f"[standkit-hub] {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # Порт не занят, но bind всё равно не удался (нет прав, недоступный
        # адрес) — честный отказ с понятным текстом вместо трейсбека.
        print(f"[standkit-hub] не удалось занять {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1

    # GAP-229/GAP-284: этот процесс подтверждённо поднимает СВОЙ сервер (не открывает
    # браузер на уже работающем экземпляре -- тот путь завершился раньше, через
    # HubAlreadyRunning выше) -- сигналим установщику/деинсталлятору BPMkit до входа в
    # цикл обслуживания, тем же приёмом, что core.acquire_server_mutex() у сервера
    # (BPMkit/server/bpmkit/core.py репозитория bpmsoft-mcp, вызывается перед mcp.run()).
    acquire_hub_mutex()

    actual_port = httpd.server_address[1]
    if args.port and actual_port != args.port:
        print(f"[standkit-hub] порт {args.port} занят, слушаю {actual_port}")
    # Файл состояния — чтобы СЛЕДУЮЩИЙ запуск знал, кого он видит на порту
    # (в т.ч. с правами администратора тот процесс или нет).
    elevated = is_elevated()
    try:
        _instance.write_state(
            state_file,
            _instance.current_state(args.host, actual_port, elevated=elevated, user_sid=our_sid),
        )
    except OSError as exc:
        print(f"[standkit-hub] не удалось записать файл состояния: {exc}", file=sys.stderr)
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


def _serve_inner(httpd, *, args, url: str) -> int:
    if args.desktop:
        try:
            import webview  # type: ignore
        except ImportError:
            print(
                "[standkit-hub] pywebview не установлен (pip install standkit[desktop]) — открываю в системном браузере",
                file=sys.stderr,
            )
            if not args.no_browser:
                webbrowser.open(url)
        else:
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
