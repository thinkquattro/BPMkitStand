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
import sys
import threading
import webbrowser
from pathlib import Path

from standkit_hub.config import HubConfig
from standkit_hub.security import InsecureBindError, generate_session_token
from standkit_hub.server import DEFAULT_HUB_PORT, HubAlreadyRunning, bind_hub_server
from standkit_hub.shortcut import install_desktop_shortcut, uninstall_desktop_shortcut


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
    args = parser.parse_args(argv)

    if args.install_shortcut or args.uninstall_shortcut:
        result = install_desktop_shortcut() if args.install_shortcut else uninstall_desktop_shortcut()
        print(f"[standkit-hub] {result.message}")
        return 0 if result.ok else 1

    config_path = Path(args.config) if args.config else HubConfig.config_path()

    # Первый запуск: заранее создаём папку реестра проектов (напр.
    # %APPDATA%\BPMkit), чтобы показываемый путь к projects.json указывал на
    # реальную папку, а не «в никуда» (иначе открытие пути в проводнике даёт
    # «Windows не удаётся найти …»). Best-effort — сбой mkdir не должен ронять
    # запуск диспетчера.
    try:
        HubConfig.load(config_path).ensure_registry_dir()
    except OSError as exc:
        print(f"[standkit-hub] не удалось подготовить папку реестра: {exc}", file=sys.stderr)

    session_token = generate_session_token()

    def _report_port_busy(requested: int, exc: OSError) -> None:
        # Печатаем ДО повторного bind'а: пользователь должен понимать, почему
        # адрес в консоли/закладке вдруг отличается от привычного.
        print(f"[standkit-hub] порт {requested} занят ({exc.strerror or exc}) — беру свободный", file=sys.stderr)

    try:
        httpd = bind_hub_server(
            args.host,
            args.port,
            config_path=config_path,
            session_token=session_token,
            insecure=args.insecure,
            on_fallback=_report_port_busy,
        )
    except HubAlreadyRunning as exc:
        # Второй запуск по ярлыку (обычный сценарий: окно браузера закрыли, а
        # процесс под pythonw остался жить — idle-shutdown у хаба нет). Второй
        # сервер здесь не нужен и вреден: два фоновых поллера над одним
        # реестром плюс разъехавшийся localStorage на другом origin. Просто
        # открываем браузер на уже работающем экземпляре.
        #
        # Токен в URL НЕ подставляем — у нас его нет (он сгенерирован в чужом
        # процессе). Работающий экземпляр узнает браузер по сессионной cookie,
        # выданной при первом открытии; если cookie не пережила полное
        # закрытие браузера, дашборд честно скажет об этом (см. app.js,
        # разбор 401), и достаточно перезапустить диспетчер.
        print(f"[standkit-hub] диспетчер уже работает на {exc.host}:{exc.port} — открываю его, второй не запускаю")
        if not args.no_browser:
            webbrowser.open(exc.url)
        return 0
    except InsecureBindError as exc:
        print(f"[standkit-hub] {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # Порт не занят, но bind всё равно не удался (нет прав, недоступный
        # адрес) — честный отказ с понятным текстом вместо трейсбека.
        print(f"[standkit-hub] не удалось занять {args.host}:{args.port} — {exc}", file=sys.stderr)
        return 1

    actual_port = httpd.server_address[1]
    if args.port and actual_port != args.port:
        print(f"[standkit-hub] порт {args.port} занят, слушаю {actual_port}")
    url = f"http://{args.host}:{actual_port}/?t={session_token}"
    print(f"[standkit-hub] дашборд слушает {args.host}:{actual_port}")
    print(f"[standkit-hub] откройте: {url}")

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
                webview.create_window("BPMkit Дашборд", url)
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
