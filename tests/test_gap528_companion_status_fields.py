# -*- coding: utf-8 -*-
"""GAP-528 п.2: три бага статуса `GET /api/companion/status` (хост владельца с
MCP 1.1.224; `companion-state.json`: `releases.running_version="1.1.224"`,
`releases.known_latest="1.1.224"`, `releases.current=null`):

  (а) `releases.current_version` подставляет `running_version`, когда маркер
      канала пуст, и помечает источник `current_version_source` —
      `state.CompanionState.summary()`;
  (б) `skills_channel.skills_status()` подставляет версию запущенного MCP
      (`mcp_runtime.json`) вместо `None`, когда `installed.json` ещё нет,
      и помечает источник `installed_source`;
  (в) явное булево `update_available` в секциях `releases` (state.summary()),
      `hub` (`hub_channel.hub_status`) и `skills` (`skills_channel.skills_status`).

Ничего из существующей формы ответа не удаляется — только добавленные поля.
"""
from __future__ import annotations

import json

import pytest

from standkit_companion import hub_channel, skills_channel
from standkit_companion.state import CompanionState


@pytest.fixture(autouse=True)
def _no_real_runtime_marker(monkeypatch):
    """Гермет по образцу `tests/test_skills_channel.py`: `read_runtime_marker`
    не имеет права подсмотреть настоящий `mcp_runtime.json` этой машины."""
    monkeypatch.setattr(skills_channel, "read_runtime_marker", lambda: None)


# ======================================================================================
# (а) releases.current_version — фолбэк на running_version
# ======================================================================================
def test_releases_current_version_falls_back_to_running_version(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    state.releases["current"] = None
    state.releases["running_version"] = "1.1.224"
    state.releases["known_latest"] = "1.1.224"

    releases = state.summary()["releases"]

    assert releases["current_version"] == "1.1.224"
    assert releases["current_version_source"] == "running"
    # Живой сценарий (хост владельца): известная версия совпадает с running —
    # обновление НЕ доступно, поле обязано это честно отразить.
    assert releases["update_available"] is False


def test_releases_current_version_prefers_real_marker_over_running(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    state.releases["current"] = {"version": "1.1.220"}
    state.releases["running_version"] = "1.1.224"

    releases = state.summary()["releases"]

    # Маркер канала (`current`) — обычный, наиболее точный источник; фолбэк
    # включается ТОЛЬКО когда его нет вовсе, а не всегда предпочитается.
    assert releases["current_version"] == "1.1.220"
    assert releases["current_version_source"] == "installed"


def test_releases_update_available_true_when_known_latest_is_newer(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    state.releases["current"] = None
    state.releases["running_version"] = "1.1.224"
    state.releases["known_latest"] = "1.1.230"

    releases = state.summary()["releases"]
    assert releases["update_available"] is True


def test_releases_update_available_true_when_staged_regardless_of_known_latest(tmp_path):
    state = CompanionState(tmp_path / "companion-state.json")
    state.releases["current"] = {"version": "1.1.224"}
    state.releases["staged"] = {"version": "1.1.224"}
    state.releases["known_latest"] = None

    releases = state.summary()["releases"]
    assert releases["update_available"] is True


def test_releases_current_version_none_when_nothing_known(tmp_path):
    """Ни маркера канала, ни запущенного MCP — форма поля прежняя (`None`),
    регресс на старое поведение НЕ появляется."""
    state = CompanionState(tmp_path / "companion-state.json")
    releases = state.summary()["releases"]
    assert releases["current_version"] is None
    assert releases["current_version_source"] is None
    assert releases["update_available"] is False


# ======================================================================================
# (б) skills_channel.skills_status — фолбэк на mcp_runtime.json
# ======================================================================================
def test_skills_status_installed_none_falls_back_to_runtime_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_channel, "read_runtime_marker",
                        lambda: {"version": "1.1.224", "started_at": "2026-09-24T10:00:00Z"})
    state = CompanionState(tmp_path / "companion-state.json")
    state.skills["known_latest"] = "1.1.224"

    status = skills_channel.skills_status(state)

    assert status["installed"]["version"] == "1.1.224"
    assert status["installed_source"] == "running"
    assert status["update_available"] is False


def test_skills_status_prefers_installed_marker_over_runtime(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    monkeypatch.setattr(skills_channel, "bpmkit_config_dir", lambda: appdata)
    monkeypatch.setattr(skills_channel, "read_runtime_marker",
                        lambda: {"version": "1.1.224"})
    marker_path = skills_channel.installed_marker_path()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text(
        json.dumps({"version": "1.1.220", "sha256": "ab" * 32,
                    "applied_at": "2026-09-20T10:00:00Z", "hosts": ["claude-code"]}),
        encoding="utf-8")
    state = CompanionState(tmp_path / "companion-state.json")

    status = skills_channel.skills_status(state)

    assert status["installed"]["version"] == "1.1.220"
    assert status["installed_source"] == "marker"


def test_skills_status_no_marker_no_runtime_stays_none(tmp_path):
    """Ни `installed.json`, ни `mcp_runtime.json` — форма поля прежняя."""
    state = CompanionState(tmp_path / "companion-state.json")
    status = skills_channel.skills_status(state)
    assert status["installed"] is None
    assert status["installed_source"] is None
    assert status["update_available"] is False


def test_skills_status_update_available_true_when_known_latest_is_newer(tmp_path, monkeypatch):
    monkeypatch.setattr(skills_channel, "read_runtime_marker",
                        lambda: {"version": "1.1.224"})
    state = CompanionState(tmp_path / "companion-state.json")
    state.skills["known_latest"] = "1.1.230"

    status = skills_channel.skills_status(state)
    assert status["update_available"] is True


# ======================================================================================
# (в) hub_channel.hub_status — update_available
# ======================================================================================
def test_hub_status_update_available_true_when_known_latest_is_newer(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_channel, "_standkit_version", "0.12.10")
    state = CompanionState(tmp_path / "companion-state.json")
    state.hub["known_latest"] = "0.12.11"

    status = hub_channel.hub_status(state)
    assert status["update_available"] is True
    assert status["current"] == "0.12.10"


def test_hub_status_update_available_false_when_up_to_date(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_channel, "_standkit_version", "0.12.11")
    state = CompanionState(tmp_path / "companion-state.json")
    state.hub["known_latest"] = "0.12.11"

    status = hub_channel.hub_status(state)
    assert status["update_available"] is False


def test_hub_status_update_available_true_when_staged(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_channel, "_standkit_version", "0.12.11")
    state = CompanionState(tmp_path / "companion-state.json")
    state.hub["known_latest"] = None
    state.hub["staged"] = {"version": "0.12.12", "path": str(tmp_path / "missing.exe")}

    status = hub_channel.hub_status(state)
    # Файл стейджинга физически отсутствует -> staged_hub_info вернёт None,
    # а вместе с ним и update_available обязан честно стать False.
    assert status["staged"] is None
    assert status["update_available"] is False


# ======================================================================================
# GAP-928: после установки установщиком окно «Обновления» показывает версию из manifest.json
# ======================================================================================
class TestGap928ManifestVersion:
    def test_fresh_install_shows_manifest_version_not_unknown(self, tmp_path):
        state = CompanionState(tmp_path / "companion-state.json")
        state.releases["current"] = None
        state.releases["running_version"] = None      # MCP после установки ещё не запускали

        releases = state.summary(disk_version="1.4.16")["releases"]

        assert releases["current_version"] == "1.4.16"
        assert releases["current_version_source"] == "manifest"

    def test_summary_without_disk_version_is_unchanged(self, tmp_path):
        state = CompanionState(tmp_path / "companion-state.json")
        state.releases["current"] = None
        assert state.summary()["releases"]["current_version"] is None

    def test_installer_newer_than_channel_record_wins(self, tmp_path):
        state = CompanionState(tmp_path / "companion-state.json")
        state.releases["current"] = {"version": "1.4.10"}

        releases = state.summary(disk_version="1.4.16")["releases"]

        assert releases["current_version"] == "1.4.16"
        assert releases["current_version_source"] == "manifest"

    def test_channel_record_not_older_than_disk_stays(self, tmp_path):
        state = CompanionState(tmp_path / "companion-state.json")
        state.releases["current"] = {"version": "1.4.16"}

        releases = state.summary(disk_version="1.4.16")["releases"]

        assert releases["current_version_source"] == "installed"

    def test_manifest_beats_stale_running_marker(self, tmp_path):
        state = CompanionState(tmp_path / "companion-state.json")
        state.releases["current"] = None
        state.releases["running_version"] = "1.4.10"

        releases = state.summary(disk_version="1.4.16")["releases"]

        assert releases["current_version"] == "1.4.16"

    @pytest.mark.parametrize("latest,expected", [("1.4.17", True), ("1.4.16", False)])
    def test_update_available_is_computed_against_manifest(self, tmp_path, latest, expected):
        state = CompanionState(tmp_path / "companion-state.json")
        state.releases["current"] = None
        state.releases["known_latest"] = latest

        assert state.summary(disk_version="1.4.16")["releases"]["update_available"] is expected

    def test_runner_status_reads_manifest_next_to_installed_cli(self, tmp_path):
        """Раскладка установщика: <app>\\server\\BPMkit.exe и <app>\\manifest.json."""
        from standkit_companion.runner import CompanionRunner

        app = tmp_path / "app"
        (app / "server").mkdir(parents=True)
        exe = app / "server" / "BPMkit.exe"
        exe.write_bytes(b"MZ")
        (app / "manifest.json").write_text(json.dumps({"name": "BPMkit", "version": "1.4.16"}),
                                           encoding="utf-8")
        runner = CompanionRunner(tmp_path / "standkit-hub.json",
                                 state_path=tmp_path / "companion-state.json")
        runner._context_cli = [str(exe)]

        releases = runner.status()["state"]["releases"]

        assert releases["current_version"] == "1.4.16"
        assert releases["current_version_source"] == "manifest"

    def test_runner_status_survives_missing_manifest(self, tmp_path):
        from standkit_companion.runner import CompanionRunner

        runner = CompanionRunner(tmp_path / "standkit-hub.json",
                                 state_path=tmp_path / "companion-state.json")
        runner._context_cli = [str(tmp_path / "nowhere" / "BPMkit.exe")]

        assert runner.status()["state"]["releases"]["current_version"] is None
