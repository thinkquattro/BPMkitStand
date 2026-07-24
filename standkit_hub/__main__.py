"""
Точка входа веб-дашборда: ``python -m standkit_hub`` (или консольный скрипт
``standkit-gui``/``standkit-hub`` после установки пакета).

По умолчанию хаб слушает ``127.0.0.1`` на эфемерном порту, печатает URL с
одноразовым сессионным токеном и открывает системный браузер. Флаг
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
from standkit_hub.server import create_hub_server
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
        default=0,
        help="порт (по умолчанию 0 — эфемерный свободный порт, выбирается ОС)",
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

    try:
        httpd = create_hub_server(
            args.host,
            args.port,
            config_path=config_path,
            session_token=session_token,
            insecure=args.insecure,
        )
    except InsecureBindError as exc:
        print(f"[standkit-hub] {exc}", file=sys.stderr)
        return 1

    actual_port = httpd.server_address[1]
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
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
