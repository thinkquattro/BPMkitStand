"""Где лежит BPMkit-hub.exe, поставленный установщиком BPMkit.

Зачем. На машине клиента могут жить ДВА диспетчера: exe из установщика и pip-пакет
``standkit`` в каком-нибудь Python (остался со времён pip-установки, старый ярлык
«BPMkit Диспетчер.lnk» с ``pythonw -m standkit_hub``). Оба слушают порт 8770 и
отбирают его друг у друга версионным перехватом -- в окне оказывается то один, то
другой, а pip-версия ещё и не обновляется. Правило: если установщик поставил
exe, единственный диспетчер -- он; pip-запуск передаёт управление exe.

Ищем по записи деинсталляции Inno (AppId установщика, HKCU/HKLM, обе ветки
реестра) -> InstallLocation; запасные места -- каталоги по умолчанию
(``{autopf}\\BPMkit``: %LOCALAPPDATA%\\Programs и Program Files). Только stdlib,
никогда не бросает.
"""
from __future__ import annotations

import os
from typing import Optional

HUB_EXE_NAME = "BPMkit-hub.exe"
#: AppId установщика (packaging/installer/bpmkit_installer.iss) + суффикс Inno.
INSTALLER_UNINSTALL_KEY = "{9F5C2E14-7A63-4B8D-9C21-6E0D4A3B57F2}_is1"
#: Явный отказ от передачи управления exe (отладка pip-сборки издателем).
ALLOW_PIP_ENV = "BPMKIT_HUB_ALLOW_PIP"


def _registry_locations() -> list:
    try:
        import winreg  # type: ignore
    except ImportError:
        return []
    found = []
    sub = "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\" + INSTALLER_UNINSTALL_KEY
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (0, getattr(winreg, "KEY_WOW64_64KEY", 0), getattr(winreg, "KEY_WOW64_32KEY", 0)):
            try:
                with winreg.OpenKey(root, sub, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "InstallLocation")
            except OSError:
                continue
            if value and value not in found:
                found.append(str(value))
    return found


def _default_locations() -> list:
    out = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(os.path.join(local, "Programs", "BPMkit"))
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            out.append(os.path.join(base, "BPMkit"))
    return out


def find_installed_hub_exe() -> Optional[str]:
    """Путь к BPMkit-hub.exe установщика или None (не Windows / не установлен)."""
    if os.name != "nt":
        return None
    try:
        for base in _registry_locations() + _default_locations():
            path = os.path.join(base, HUB_EXE_NAME)
            if os.path.isfile(path):
                return path
    except Exception:  # noqa: BLE001 -- best-effort
        return None
    return None


def is_frozen_hub_executable(path: Optional[str]) -> bool:
    """Процесс запущен из exe-сборки диспетчера (по имени исполняемого файла)."""
    return bool(path) and os.path.basename(str(path)).lower() == HUB_EXE_NAME.lower()
