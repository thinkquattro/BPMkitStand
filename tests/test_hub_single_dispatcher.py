# -*- coding: utf-8 -*-
"""Один диспетчер на машине: переход pip -> установщик без швов.

1. pip-проверка обновления пишет `known_latest` в состояние канала -> карточка
   `hub_status()` честно показывает `update_available` (раньше всегда «последняя»).
2. pip-диспетчер не перехватывает порт у exe из установщика; exe у pip -- перехватывает.
3. Обычный pip-запуск при установленном exe передаёт управление exe; служебные режимы,
   frozen-сборка и явный argv (тесты/встраивание) -- не передают.
4. Ярлык из exe-сборки -- без аргументов интерпретатора.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

from standkit_companion import hub_channel
from standkit_companion.state import CompanionState
from standkit_hub import __main__ as hub_main
from standkit_hub import installed_exe, instance, self_version, shortcut


class _Resp:
    def __init__(self, version):
        self._v = version

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return ('{"info": {"version": "%s"}}' % self._v).encode()


def _state(tmp_path):
    return CompanionState.load(tmp_path / "state.json")


def test_pip_check_persists_known_latest(monkeypatch, tmp_path):
    monkeypatch.setattr(self_version, "urlopen", lambda req, timeout=None: _Resp("999.0.0"))
    self_version.reset_cache()
    st = _state(tmp_path)
    result = hub_channel.check_hub_pypi(st)
    assert result["available"] is True
    assert st.hub["known_latest"] == "999.0.0"
    assert st.hub["mode"] == "pip"
    card = hub_channel.hub_status(st)
    assert card["update_available"] is True
    assert card["known_latest"] == "999.0.0"


def test_pip_check_up_to_date_card_not_available(monkeypatch, tmp_path):
    from standkit import __version__ as cur
    monkeypatch.setattr(self_version, "urlopen", lambda req, timeout=None: _Resp(cur))
    self_version.reset_cache()
    st = _state(tmp_path)
    hub_channel.check_hub_pypi(st)
    assert hub_channel.hub_status(st)["update_available"] is False


def _running(exe):
    return instance.HubInstanceState(pid=1, host="127.0.0.1", port=8770, elevated=False,
                                     version="0.0.1", executable=exe)


def test_pip_never_takes_over_installer_exe():
    run = _running(r"C:\Users\u\AppData\Local\Programs\BPMkit\BPMkit-hub.exe")
    assert instance.should_takeover(run, we_elevated=True, our_version="9.9.9",
                                    our_frozen=False) is False
    # явный --takeover (кнопка «перезапустить») по-прежнему решает пользователь
    assert instance.should_takeover(run, we_elevated=False, explicit=True,
                                    our_frozen=False) is True


def test_exe_takes_over_pip_by_version():
    run = _running(r"C:\Python310\pythonw.exe")
    assert instance.should_takeover(run, we_elevated=False, our_version="9.9.9",
                                    our_frozen=True) is True


def test_pip_vs_pip_keeps_version_rule():
    run = _running(r"C:\Python310\pythonw.exe")
    assert instance.should_takeover(run, we_elevated=False, our_version="9.9.9",
                                    our_frozen=False) is True


def test_is_frozen_hub_executable():
    assert installed_exe.is_frozen_hub_executable(r"D:\x\bpmkit-HUB.exe")
    assert not installed_exe.is_frozen_hub_executable(r"C:\Python\pythonw.exe")
    assert not installed_exe.is_frozen_hub_executable(None)


def test_find_installed_exe_non_windows(monkeypatch):
    monkeypatch.setattr(installed_exe.os, "name", "posix")
    assert installed_exe.find_installed_hub_exe() is None


def test_find_installed_exe_by_default_location(monkeypatch, tmp_path):
    d = tmp_path / "Programs" / "BPMkit"
    d.mkdir(parents=True)
    (d / "BPMkit-hub.exe").write_bytes(b"MZ")
    monkeypatch.setattr(installed_exe.os, "name", "nt")
    monkeypatch.setattr(installed_exe, "_registry_locations", lambda: [])
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(var, raising=False)
    assert installed_exe.find_installed_hub_exe() == str(d / "BPMkit-hub.exe")


def _args(**kw):
    base = dict(takeover=False, result_file=None, initiator_sid=None, elevated_op=None,
                apply_self_update=False, hub_stop=False, session_token_file=None,
                no_browser=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def exe_found(monkeypatch):
    monkeypatch.setattr(installed_exe, "find_installed_hub_exe", lambda: r"C:\BPMkit\BPMkit-hub.exe")
    monkeypatch.delenv(installed_exe.ALLOW_PIP_ENV, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)


def test_hand_over_plain_pip_start(exe_found):
    assert hub_main._should_hand_over_to_installed_exe(_args()) is True


@pytest.mark.parametrize("flag", ["takeover", "result_file", "initiator_sid", "elevated_op",
                                  "apply_self_update", "hub_stop", "session_token_file"])
def test_service_modes_not_handed_over(exe_found, flag):
    assert hub_main._should_hand_over_to_installed_exe(_args(**{flag: "x"})) is False


def test_env_opt_out(exe_found, monkeypatch):
    monkeypatch.setenv(installed_exe.ALLOW_PIP_ENV, "1")
    assert hub_main._should_hand_over_to_installed_exe(_args()) is False


def test_frozen_not_handed_over(exe_found, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert hub_main._should_hand_over_to_installed_exe(_args()) is False


def test_no_exe_not_handed_over(monkeypatch):
    monkeypatch.setattr(installed_exe, "find_installed_hub_exe", lambda: None)
    monkeypatch.delenv(installed_exe.ALLOW_PIP_ENV, raising=False)
    assert hub_main._should_hand_over_to_installed_exe(_args()) is False


def test_hand_over_spawns_exe(exe_found, monkeypatch):
    calls = []
    import standkit.platform as plat
    monkeypatch.setattr(plat, "spawn_hidden", lambda cmd, *a, **kw: calls.append(cmd) or 1)
    assert hub_main._hand_over_to_installed_exe(_args(no_browser=True)) == 0
    assert calls == [[r"C:\BPMkit\BPMkit-hub.exe", "--no-browser"]]


def test_shortcut_script_arguments():
    from pathlib import Path
    s = shortcut.build_windows_shortcut_script(Path("a.lnk"), "X.exe", Path("i.ico"), Path("."), "")
    assert '$Shortcut.Arguments = ""' in s
    s2 = shortcut.build_windows_shortcut_script(Path("a.lnk"), "p.exe", Path("i.ico"), Path("."))
    assert '$Shortcut.Arguments = "-m standkit_hub"' in s2


def test_exe_takes_over_pip_even_same_version():
    run = instance.HubInstanceState(pid=1, host="127.0.0.1", port=8770, elevated=False,
                                    version="1.0.0", executable=r"C:\Py\pythonw.exe")
    assert instance.should_takeover(run, we_elevated=False, our_version="1.0.0",
                                    our_frozen=True) is True
    run.user_sid = "S-other"
    assert instance.should_takeover(run, we_elevated=False, our_version="1.0.0",
                                    our_sid="S-me", our_frozen=True) is False
