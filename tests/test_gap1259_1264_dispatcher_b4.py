# -*- coding: utf-8 -*-
"""Диспетчер: колонка «Транспорт», медленный CLI лицензии, установщик старше установленного,
дубль причины в баннере (волна B4).

* колонка «Транспорт» — вид первой строкой, хост второй с многоточием и подсказкой,
  без разрыва посреди слова;
* отказ/таймаут CLI лицензии — «не удалось проверить», а не «не активирована»: последняя
  удачная сводка остаётся, отказ «не дождались» помнится короткое время, одна сводка за раз;
* `stage_installer("latest")` не готовит установщик версии не новее установленной;
* баннер неподтверждённой активации называет причину один раз.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from standkit_companion import releases
from standkit_companion.state import CompanionState
from standkit_hub import license_api
from standkit_hub.config import CompanionSettings
import standkit_hub.server as server_module
from tests.test_companion_installer_channel import (
    _installer_meta, _installer_sidecar,
)
from tests.test_companion_releases import FakeClient, FakeCtx, _blob
from tests.test_gap604_updates_badge import _extract_function, _read_js
from tests.test_hub_license_api import INFO_OK, _Runs, _settings

WEB_DIR = Path(server_module.__file__).parent / "web"
NODE = shutil.which("node")


@pytest.fixture()
def cli_path(tmp_path) -> Path:
    path = tmp_path / "bpmkit.exe"
    path.write_text("", encoding="utf-8")
    return path


# ======================================================================================
# «Транспорт»
# ======================================================================================
class TestTransportColumn:
    def test_css_kind_and_host_are_lines_host_ellipsis_no_midword_break(self):
        css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
        cell = re.search(r"\.stands-table td\.cell-transport \{(.*?)\}", css, re.S)
        assert cell and "overflow-wrap: normal" in cell.group(1)
        assert "anywhere" not in cell.group(1)
        host = re.search(r"\.stands-table td\.cell-transport \.transport-host \{(.*?)\}", css, re.S)
        assert host
        for needle in ("text-overflow: ellipsis", "white-space: nowrap", "overflow: hidden"):
            assert needle in host.group(1)
        block = re.search(r"td\.cell-transport \.transport-kind,\s*"
                          r"\.stands-table td\.cell-transport \.transport-host \{(.*?)\}", css, re.S)
        assert block and "display: block" in block.group(1)

    def test_column_is_wider_and_widths_sum_to_100(self):
        css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
        widths = [int(m) for m in re.findall(r"^\.col-[a-z]+ \{ width: (\d+)%; \}", css, re.M)]
        assert sum(widths) == 100
        assert ".col-transport { width: 17%; }" in css

    def test_full_address_stays_in_title(self):
        js = _read_js()
        assert 'class="transport-value" title="${escapeAttr(title)}"' in js
        assert '<span class="transport-host">${escapeHtml(host)}</span>' in js


# ======================================================================================
# Лицензия: отказ CLI ≠ «не активирована»
# ======================================================================================
class TestLicenseCheckFailed:
    def test_timeout_without_prior_success_is_check_failed_not_free_offer(self, cli_path):
        runs = _Runs((-1, "", "timed out after 60.0 seconds"))
        snap = license_api.license_info(_settings(cli_path), run=runs)
        assert snap["check_failed"] is True
        assert snap["status"] == "unavailable"
        assert "timed out" in snap["check_error"]
        assert snap["cli"] == str(cli_path)

    def test_timeout_keeps_last_good_snapshot(self, cli_path):
        good = license_api.license_info(_settings(cli_path), run=_Runs(), cache_ttl=0)
        assert good["status"] == "valid" and "check_failed" not in good
        snap = license_api.license_info(
            _settings(cli_path), run=_Runs((-1, "", "timed out after 60.0 seconds")), cache_ttl=0)
        assert snap["status"] == "valid"
        assert snap["edition"] == "companion"
        assert snap["licensee"] == INFO_OK["licensee"]
        assert snap["check_failed"] is True and snap["stale"] is True
        assert snap["checked_at"]

    def test_last_good_survives_restart_via_file(self, cli_path):
        license_api.license_info(_settings(cli_path), run=_Runs(), cache_ttl=0)
        with license_api._cache_lock:
            license_api._last_good.clear()  # «перезапуск»: память пуста, файл остался
        license_api.invalidate_cache()
        snap = license_api.license_info(
            _settings(cli_path), run=_Runs((-1, "", "timed out")), cache_ttl=0)
        assert snap["status"] == "valid" and snap["stale"] is True

    def test_not_waiting_again_right_after_timeout(self, cli_path):
        runs = _Runs((-1, "", "timed out after 60.0 seconds"))
        license_api.license_info(_settings(cli_path), run=runs)
        license_api.license_info(_settings(cli_path), run=runs)
        assert len(runs.calls) == 1, "отказ «не дождались» помнится, повторный опрос не ждёт заново"

    def test_cli_answer_with_error_is_not_remembered(self, cli_path):
        runs = _Runs((1, "", "нет ответа"))
        license_api.license_info(_settings(cli_path), run=runs)
        license_api.license_info(_settings(cli_path), run=runs)
        assert len(runs.calls) == 2

    def test_mutation_drops_failure_memory_and_deactivate_forgets_last_good(self, cli_path):
        license_api.license_info(_settings(cli_path), run=_Runs(), cache_ttl=0)
        license_api.invalidate_cache()
        runs = _Runs((-1, "", "timed out"))
        license_api.license_info(_settings(cli_path), run=runs)
        license_api.invalidate_cache()
        license_api.license_info(_settings(cli_path), run=runs)
        assert len(runs.calls) == 2
        license_api.license_deactivate(
            _settings(cli_path), run=_Runs((0, json.dumps({"ok": True}), "")))
        snap = license_api.license_info(
            _settings(cli_path), run=_Runs((-1, "", "timed out")), cache_ttl=0)
        assert snap["status"] == "unavailable" and snap["edition"] == "free"

    def test_concurrent_requests_share_one_cli_run(self, cli_path):
        calls = []

        def slow_run(argv):
            calls.append(argv)
            time.sleep(0.3)
            return 0, json.dumps(INFO_OK), ""

        results = []
        threads = [threading.Thread(
            target=lambda: results.append(license_api.license_info(_settings(cli_path), run=slow_run)))
            for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(calls) == 1
        assert [r["status"] for r in results] == ["valid"] * 4

    def test_cli_timeout_is_longer_than_cold_start_of_slow_guest(self):
        assert license_api._CLI_TIMEOUT_S >= 60.0


class TestLicenseUiTexts:
    def test_free_title_has_id_and_check_failed_text(self):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        assert 'id="license-free-title"' in html
        js = _read_js()
        pane = _extract_function(js, "renderLicensePane")
        assert "snapshot.check_failed" in pane
        assert "Не удалось проверить лицензию" in pane

    def test_failed_check_keeps_channel_view_and_rechecks_itself(self):
        js = _read_js()
        apply = _extract_function(js, "applyLicense")
        assert "snapshot.check_failed" in apply and "lastLicense" in apply
        refresh = _extract_function(js, "refreshLicense")
        assert "LICENSE_RECHECK_MS" in refresh and "data.check_failed" in refresh


# ======================================================================================
# stage_installer: не старше установленной
# ======================================================================================
class TestStageInstallerNotOlder:
    def _env(self, tmp_path, installed: str):
        binary = tmp_path / "mcp" / "bpmkit.exe"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"binar")
        state = CompanionState(tmp_path / "companion-state.json")
        ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary),
                      mcp_version=installed)
        return state, ctx

    @pytest.mark.parametrize("published", ["1.4.46", "1.5.4"])
    def test_latest_not_newer_than_installed_stages_nothing(self, tmp_path, published):
        state, ctx = self._env(tmp_path, "1.5.4")
        blob = _blob(published)
        filename = f"bpmkit-setup-{published}.exe"
        client = FakeClient(_installer_meta(published, blob, filename=filename), blob,
                            sidecar=_installer_sidecar(filename, blob))
        result = releases.stage_installer(client, state, ctx, version="latest")
        assert result["reason"] == "up_to_date" and result["staged"] is False
        assert client.file_calls == [], "тело установщика не скачивается"
        assert releases.installer_status(state)["staged"] is None

    def test_latest_newer_than_installed_is_staged(self, tmp_path):
        state, ctx = self._env(tmp_path, "1.5.4")
        blob = _blob("1.5.5")
        filename = "bpmkit-setup-1.5.5.exe"
        client = FakeClient(_installer_meta("1.5.5", blob, filename=filename), blob,
                            sidecar=_installer_sidecar(filename, blob))
        result = releases.stage_installer(client, state, ctx, version="latest")
        assert result["version"] == "1.5.5"
        assert releases.installer_status(state)["staged"] is not None


# ======================================================================================
# Баннер активации: причина один раз
# ======================================================================================
@pytest.fixture(scope="module")
def notice(tmp_path_factory):
    if NODE is None:
        pytest.skip("node недоступен в этой среде")
    js = _read_js()
    snap = {"status": "valid", "licensee": "Л", "activation_notice_level": "warn",
            "activation_effective_deadline": "2026-11-05T12:00:00Z", "activation_hours_left": 72,
            "activation_last_error_message_ru": "Соединение с сервером активации отклонено."}
    same = {"ok": False, "message_ru": "Соединение с сервером активации отклонено."}
    other = {"ok": False, "message_ru": "сервер активации не отвечает"}
    script = "\n".join([
        _extract_function(js, "formatDate"),
        _extract_function(js, "pluralDays"),
        _extract_function(js, "activationLeftText"),
        _extract_function(js, "licenseActivationNoticeView"),
        f"const S = {json.dumps(snap, ensure_ascii=False)};",
        f"const SAME = {json.dumps(same, ensure_ascii=False)};",
        f"const OTHER = {json.dumps(other, ensure_ascii=False)};",
        "console.log(JSON.stringify({same: licenseActivationNoticeView(S, SAME),"
        " other: licenseActivationNoticeView(S, OTHER)}));",
    ])
    path = tmp_path_factory.mktemp("b4notice") / "n.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                          env=dict(os.environ, TZ="UTC"), encoding="utf-8",
                          errors="replace", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_banner_names_reason_once_when_probe_says_the_same(notice):
    text = notice["same"]["text"]
    assert text.count("отклонено") == 1
    assert "Проверка связи" not in text


def test_banner_keeps_probe_line_when_it_adds_information(notice):
    text = notice["other"]["text"]
    assert "Проверка связи: сервер активации не отвечает" in text
