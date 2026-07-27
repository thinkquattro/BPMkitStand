"""
Тесты standkit_hub.shortcut — без реального создания ярлыков.

POSIX-ветка (Linux): резолв путей + генерация содержимого .desktop
проверяются end-to-end во временных каталогах (monkeypatch Path.home()).

Windows-ветка: реального .lnk не создаём (нет реального Windows/WScript в
CI) — проверяем, что генерируется корректный PowerShell-скрипт/пути и что
subprocess.run вызывается с ожидаемыми аргументами (monkeypatch).

macOS: просто убеждаемся, что функции не падают и возвращают понятный статус.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import standkit_hub.shortcut as shortcut_module
from standkit_hub.shortcut import (
    build_desktop_entry,
    build_windows_shortcut_script,
    install_desktop_shortcut,
    uninstall_desktop_shortcut,
)


# --- macOS / неподдерживаемые платформы ---


def test_install_unsupported_platform_returns_ok_false(monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "darwin")

    result = install_desktop_shortcut()

    assert result.ok is False
    assert result.path is None
    assert "не поддерживается" in result.message


def test_uninstall_unsupported_platform_returns_ok_false(monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "darwin")

    result = uninstall_desktop_shortcut()

    assert result.ok is False


# --- Linux ---


# Эти два теста проверяют СОДЕРЖИМОЕ .desktop-файла и POSIX-права на него,
# то есть вещи, которых на Windows не существует: Path("/tmp/icon.png") там
# превращается в "\tmp\icon.png", а бит исполнения не выставляется в принципе.
# Раньше они просто падали при каждом прогоне на машине разработчика и
# приучали не смотреть на красноту.
posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason=".desktop-файлы и POSIX-права — только Linux"
)


@posix_only
def test_build_desktop_entry_contains_required_fields():
    content = build_desktop_entry("standkit-gui", Path("/tmp/icon.png"))

    assert "[Desktop Entry]" in content
    assert "Type=Application" in content
    assert "Name=BPMkit Диспетчер" in content
    assert "Exec=standkit-gui" in content
    assert "Icon=/tmp/icon.png" in content
    assert "Terminal=false" in content


@posix_only
def test_install_linux_writes_applications_entry_and_desktop_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Desktop").mkdir()

    result = install_desktop_shortcut()

    assert result.ok is True
    apps_entry = tmp_path / ".local" / "share" / "applications" / "bpmkit-standkit.desktop"
    desktop_copy = tmp_path / "Desktop" / "bpmkit-standkit.desktop"
    icon_file = tmp_path / ".local" / "share" / "icons" / "bpmkit-standkit.png"

    assert apps_entry.exists()
    assert desktop_copy.exists()
    assert icon_file.exists()
    assert apps_entry.read_text(encoding="utf-8") == desktop_copy.read_text(encoding="utf-8")
    # Исполняемый бит выставлен.
    assert apps_entry.stat().st_mode & 0o111


def test_install_linux_without_desktop_dir_still_creates_applications_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # Намеренно НЕ создаём tmp_path / "Desktop".

    result = install_desktop_shortcut()

    assert result.ok is True
    apps_entry = tmp_path / ".local" / "share" / "applications" / "bpmkit-standkit.desktop"
    assert apps_entry.exists()
    assert not (tmp_path / "Desktop").exists()


def test_uninstall_linux_removes_entries_created_by_install(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Desktop").mkdir()

    install_desktop_shortcut()
    result = uninstall_desktop_shortcut()

    assert result.ok is True
    assert not (tmp_path / ".local" / "share" / "applications" / "bpmkit-standkit.desktop").exists()
    assert not (tmp_path / "Desktop" / "bpmkit-standkit.desktop").exists()


def test_uninstall_linux_is_idempotent_when_nothing_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "linux")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = uninstall_desktop_shortcut()

    assert result.ok is True
    assert "отсутств" in result.message


# --- Windows (без реального Windows-окружения — только сборка команды) ---


def test_build_windows_shortcut_script_contains_expected_pieces(tmp_path):
    lnk_path = tmp_path / "BPMkit Диспетчер.lnk"
    icon_path = tmp_path / "bpmkit-icon.ico"

    script = build_windows_shortcut_script(lnk_path, "C:/Python/pythonw.exe", icon_path, tmp_path)

    assert "New-Object -ComObject WScript.Shell" in script
    assert str(lnk_path) in script
    assert "C:/Python/pythonw.exe" in script
    assert "-m standkit_hub" in script
    assert str(icon_path) in script
    assert "$Shortcut.Save()" in script


def test_install_windows_invokes_subprocess_with_powershell(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    captured = {}

    class _FakeCompletedProcess:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompletedProcess()

    monkeypatch.setattr(shortcut_module.subprocess, "run", _fake_run)

    result = install_desktop_shortcut()

    assert result.ok is True
    assert captured["cmd"][0] == "powershell"
    assert "-Command" in captured["cmd"]
    script = captured["cmd"][-1]
    assert "WScript.Shell" in script
    assert str(tmp_path / "Desktop" / "BPMkit Диспетчер.lnk") in script


def test_install_windows_reports_powershell_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    class _FakeFailedProcess:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(shortcut_module.subprocess, "run", lambda cmd, **kwargs: _FakeFailedProcess())

    result = install_desktop_shortcut()

    assert result.ok is False
    assert "boom" in result.message


def test_uninstall_windows_missing_shortcut_is_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = uninstall_desktop_shortcut()

    assert result.ok is True
    assert "отсутств" in result.message


def test_uninstall_windows_removes_existing_shortcut(tmp_path, monkeypatch):
    monkeypatch.setattr(shortcut_module.sys, "platform", "win32")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    lnk = desktop / "BPMkit Диспетчер.lnk"
    lnk.write_text("stub", encoding="utf-8")

    result = uninstall_desktop_shortcut()

    assert result.ok is True
    assert not lnk.exists()
