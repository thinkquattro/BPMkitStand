"""
Ярлык веб-дашборда standkit на рабочем столе — кроссплатформенно:

  - Windows: ``.lnk`` через ``WScript.Shell`` из PowerShell (без pywin32);
  - Linux: freedesktop ``.desktop``-файл (меню приложений + копия на
    рабочий стол, если такая папка есть);
  - macOS: осознанно НЕ реализовано — возвращается понятный статус.

Ярлык запускает ``python -m standkit_hub`` — хаб сам поднимает локальный
веб-сервер и открывает системный браузер (см. standkit_hub/__main__.py).

Все публичные функции возвращают ``ShortcutResult`` и НИКОГДА не бросают
исключений наружу (в т.ч. на неподдерживаемой ОС или при сбое внешней
команды) — вызывающий код просто показывает ``message`` пользователю.

Иконка берётся из уже упакованных ресурсов ``standkit_hub/assets``
(``importlib.resources`` — работает и из установленного пакета, не только
из исходников репозитория) и извлекается на диск рядом с ярлыком/записью
меню, т.к. .lnk/.desktop не умеют встраивать иконку из архива напрямую.
"""

from __future__ import annotations

import importlib.resources
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_APP_NAME = "BPMkit Диспетчер"
_WINDOWS_SHORTCUT_NAME = "BPMkit Диспетчер.lnk"
_WINDOWS_ICON_ASSET = "bpmkit-icon.ico"
_LINUX_DESKTOP_FILE_NAME = "bpmkit-standkit.desktop"
_LINUX_ICON_ASSET = "icon.png"
_LINUX_ICON_FILE_NAME = "bpmkit-standkit.png"


@dataclass
class ShortcutResult:
    """Результат install/uninstall — без исключений, только статус + текст для пользователя."""

    ok: bool
    path: Optional[str]
    message: str


def _extract_asset(asset_name: str, dest_path: Path) -> Path:
    """
    Извлекает файл ресурса пакета (``standkit_hub/assets/<asset_name>``) на
    диск по ``dest_path`` (создавая родительские папки при необходимости).
    """
    resource = importlib.resources.files("standkit_hub") / "assets" / asset_name
    with importlib.resources.as_file(resource) as src:
        data = Path(src).read_bytes()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(data)
    return dest_path


# ============================== Windows ==============================


def _windows_desktop_dir() -> Path:
    return Path.home() / "Desktop"


def _windows_local_appdata_bpmkit_dir() -> Path:
    local_appdata = os.environ.get("LOCALAPPDATA")
    base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
    return base / "BPMkit"


def _windows_pythonw_executable() -> str:
    """``pythonw.exe`` рядом с текущим интерпретатором (без консольного окна); фолбэк — ``sys.executable``."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        return str(candidate)
    return sys.executable


def build_windows_shortcut_script(lnk_path: Path, target: str, icon_path: Path, working_dir: Path) -> str:
    """
    Собирает PowerShell-скрипт создания ``.lnk`` через ``WScript.Shell``
    (без pywin32). Вынесено в отдельную чистую функцию специально ради
    тестируемости без реального ``subprocess.run``.
    """
    return (
        "$WshShell = New-Object -ComObject WScript.Shell\n"
        f'$Shortcut = $WshShell.CreateShortcut("{lnk_path}")\n'
        f'$Shortcut.TargetPath = "{target}"\n'
        '$Shortcut.Arguments = "-m standkit_hub"\n'
        f'$Shortcut.IconLocation = "{icon_path}"\n'
        f'$Shortcut.WorkingDirectory = "{working_dir}"\n'
        "$Shortcut.Save()\n"
    )


def _install_windows() -> ShortcutResult:
    desktop_dir = _windows_desktop_dir()
    lnk_path = desktop_dir / _WINDOWS_SHORTCUT_NAME

    try:
        icon_path = _extract_asset(
            _WINDOWS_ICON_ASSET, _windows_local_appdata_bpmkit_dir() / _WINDOWS_ICON_ASSET
        )
    except (FileNotFoundError, OSError) as exc:
        return ShortcutResult(ok=False, path=None, message=f"Не удалось извлечь иконку: {exc}")

    target = _windows_pythonw_executable()
    script = build_windows_shortcut_script(lnk_path, target, icon_path, Path.home())

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ShortcutResult(ok=False, path=None, message=f"Не удалось запустить PowerShell: {exc}")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return ShortcutResult(ok=False, path=None, message=f"PowerShell вернул ошибку: {detail}")

    return ShortcutResult(ok=True, path=str(lnk_path), message=f"Ярлык создан: {lnk_path}")


def _uninstall_windows() -> ShortcutResult:
    lnk_path = _windows_desktop_dir() / _WINDOWS_SHORTCUT_NAME
    if not lnk_path.exists():
        return ShortcutResult(ok=True, path=str(lnk_path), message="Ярлык уже отсутствует")
    try:
        lnk_path.unlink()
    except OSError as exc:
        return ShortcutResult(ok=False, path=str(lnk_path), message=f"Не удалось удалить ярлык: {exc}")
    return ShortcutResult(ok=True, path=str(lnk_path), message=f"Ярлык удалён: {lnk_path}")


# ================================ Linux ================================


def _linux_applications_dir() -> Path:
    return Path.home() / ".local" / "share" / "applications"


def _linux_icons_dir() -> Path:
    return Path.home() / ".local" / "share" / "icons"


def _linux_desktop_dir() -> Optional[Path]:
    candidate = Path.home() / "Desktop"
    return candidate if candidate.is_dir() else None


def _linux_exec_command() -> str:
    if shutil.which("standkit-gui"):
        return "standkit-gui"
    return "python3 -m standkit_hub"


def build_desktop_entry(exec_cmd: str, icon_path: Path) -> str:
    """Содержимое freedesktop ``.desktop``-файла — чистая функция, тестируется без файловой системы."""
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={_APP_NAME}\n"
        f"Exec={exec_cmd}\n"
        f"Icon={icon_path}\n"
        "Terminal=false\n"
        "Categories=Utility;\n"
    )


def _install_linux() -> ShortcutResult:
    try:
        icon_path = _extract_asset(_LINUX_ICON_ASSET, _linux_icons_dir() / _LINUX_ICON_FILE_NAME)
    except (FileNotFoundError, OSError) as exc:
        return ShortcutResult(ok=False, path=None, message=f"Не удалось извлечь иконку: {exc}")

    content = build_desktop_entry(_linux_exec_command(), icon_path)

    apps_dir = _linux_applications_dir()
    entry_path = apps_dir / _LINUX_DESKTOP_FILE_NAME
    try:
        apps_dir.mkdir(parents=True, exist_ok=True)
        entry_path.write_text(content, encoding="utf-8")
        entry_path.chmod(0o755)
    except OSError as exc:
        return ShortcutResult(ok=False, path=None, message=f"Не удалось записать .desktop-файл: {exc}")

    desktop_dir = _linux_desktop_dir()
    if desktop_dir is not None:
        desktop_copy = desktop_dir / _LINUX_DESKTOP_FILE_NAME
        try:
            desktop_copy.write_text(content, encoding="utf-8")
            desktop_copy.chmod(0o755)
        except OSError:
            # Копия на рабочий стол — best-effort; запись в меню приложений
            # (apps_dir) уже создана и считается основным результатом.
            pass

    return ShortcutResult(ok=True, path=str(entry_path), message=f"Ярлык создан: {entry_path}")


def _uninstall_linux() -> ShortcutResult:
    candidates = [_linux_applications_dir() / _LINUX_DESKTOP_FILE_NAME]
    desktop_dir = _linux_desktop_dir()
    if desktop_dir is not None:
        candidates.append(desktop_dir / _LINUX_DESKTOP_FILE_NAME)

    removed_any = False
    for path in candidates:
        if path.exists():
            try:
                path.unlink()
                removed_any = True
            except OSError as exc:
                return ShortcutResult(ok=False, path=str(path), message=f"Не удалось удалить {path}: {exc}")

    icon_path = _linux_icons_dir() / _LINUX_ICON_FILE_NAME
    if icon_path.exists():
        try:
            icon_path.unlink()
        except OSError:
            pass

    message = "Ярлык удалён" if removed_any else "Ярлык уже отсутствовал"
    return ShortcutResult(ok=True, path=str(candidates[0]), message=message)


# ============================ macOS / прочее ============================


def _unsupported(action: str) -> ShortcutResult:
    return ShortcutResult(
        ok=False,
        path=None,
        message=f"{action} ярлыка не поддерживается на платформе {sys.platform!r} (macOS и т.п.)",
    )


# ================================ API ================================


def install_desktop_shortcut() -> ShortcutResult:
    """Создаёт ярлык веб-дашборда standkit на рабочем столе (Windows ``.lnk`` / Linux ``.desktop``)."""
    if sys.platform == "win32":
        return _install_windows()
    if sys.platform.startswith("linux"):
        return _install_linux()
    return _unsupported("Создание")


def uninstall_desktop_shortcut() -> ShortcutResult:
    """Удаляет ранее созданный ярлык веб-дашборда standkit."""
    if sys.platform == "win32":
        return _uninstall_windows()
    if sys.platform.startswith("linux"):
        return _uninstall_linux()
    return _unsupported("Удаление")
