# -*- coding: utf-8 -*-
"""канал диспетчера проверяется попутно, и «последняя» -- только по факту проверки.

Инцидент 20.10.2026: у клиента диспетчер оставался версией первой установки (0.12.19),
хотя MCP самообновился до 1.3.16; окно «Обновления» писало «последняя». Причины:
`check_hub` звался только отдельным действием, а pip-проверка не писала результат
в состояние.
"""
from __future__ import annotations

import pytest

from standkit_companion import hub_channel
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from standkit_hub import self_version
from tests.test_companion_runner import make_runner, patch_cycles, settings_all_on, Recorder


class _Resp:
    def __init__(self, version):
        self._v = version

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return ('{"info": {"version": "%s"}}' % self._v).encode()


@pytest.fixture(autouse=True)
def _clean_cache():
    self_version.reset_cache()
    yield
    self_version.reset_cache()


def test_pip_check_persists_known_latest(monkeypatch, tmp_path):
    monkeypatch.setattr(self_version, "urlopen", lambda req, timeout=None: _Resp("999.0.0"))
    st = CompanionState(tmp_path / "s.json")
    result = hub_channel.check_hub_pypi(st)
    assert result["available"] is True
    card = hub_channel.hub_status(st)
    assert card["known_latest"] == "999.0.0"
    assert card["update_available"] is True
    assert card["status"] == "ok" and card["checked_at"]


def test_never_checked_card_has_no_check_fact(tmp_path):
    card = hub_channel.hub_status(CompanionState(tmp_path / "s.json"))
    assert not card["checked_at"]
    assert card["update_available"] is False


def test_check_update_also_checks_frozen_hub(tmp_path, monkeypatch):
    log: list = []
    stubs = patch_cycles(monkeypatch, log)
    monkeypatch.setattr(hub_channel, "is_frozen_hub", lambda: True)
    r = make_runner(tmp_path, settings_all_on())
    result = r.run_action("check_update")
    assert stubs["hub_check"].calls == 1
    assert stubs["hub_check_pypi"].calls == 0
    assert result["hub"]["mode"] == "exe"


def test_check_update_checks_pip_hub(tmp_path, monkeypatch):
    log: list = []
    stubs = patch_cycles(monkeypatch, log)
    monkeypatch.setattr(hub_channel, "is_frozen_hub", lambda: False)
    r = make_runner(tmp_path, settings_all_on())
    r.run_action("check_update")
    assert stubs["hub_check_pypi"].calls == 1
    assert stubs["hub_check"].calls == 0


def test_hub_failure_does_not_break_mcp_check(tmp_path, monkeypatch):
    log: list = []
    patch_cycles(monkeypatch, log)
    monkeypatch.setattr(hub_channel, "is_frozen_hub", lambda: True)
    monkeypatch.setattr(hub_channel, "check_hub", Recorder(
        "hub", [], raises=[ChannelError("бэкенд недоступен", kind="http_error")]))
    r = make_runner(tmp_path, settings_all_on())
    result = r.run_action("check_update")
    assert "error" in result["hub"]
    assert r._state.hub["last_status"] == "error"
    assert result["available"] is False  # проверка MCP отработала


def test_scheduled_release_tick_checks_hub(tmp_path, monkeypatch):
    log: list = []
    stubs = patch_cycles(monkeypatch, log)
    monkeypatch.setattr(hub_channel, "is_frozen_hub", lambda: True)
    r = make_runner(tmp_path, settings_all_on())
    r.run_cycle("releases")
    assert stubs["hub_check"].calls == 1


def test_ui_chip_requires_check_fact():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "standkit_hub" / "web" / "app.js").read_text(encoding="utf-8")
    assert 'hub.checked_at && hub.status === "ok") setChip("upd-hub-chip", "ok", "последняя"' in js
    assert '"не проверялась"' in js
