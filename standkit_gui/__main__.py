"""Точка входа GUI: ``python -m standkit_gui`` (или консольный скрипт ``standkit-gui``)."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="standkit-gui", description="Диспетчер стендов standkit (PySide6)")
    parser.add_argument(
        "--registry",
        default=None,
        help=(
            "путь к реестру стендов (по умолчанию — тот же реестр, что резолвит "
            "BPMkit MCP: env BPMSOFT_PROJECTS_FILE, иначе "
            "%%APPDATA%%\\BPMkit\\projects.json / ~/.config/BPMkit/projects.json, "
            "иначе ./projects.json)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        from standkit_gui.app import main as run_app
    except ImportError as exc:
        print(f"[standkit-gui] {exc}", file=sys.stderr)
        return 1

    return run_app(args.registry)


if __name__ == "__main__":
    raise SystemExit(main())
