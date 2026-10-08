# -*- coding: utf-8 -*-
"""
GAP-782 (часть сабмодуля): строка «Активирована на» экрана лицензии.

Раньше при ``activated=false`` диспетчер писал «не активирована у издателя» —
у клиента это читалось как «издатель не активировал лицензию», а значило лишь
«онлайн-подтверждения пока нет». Сводка лицензии (``GET /api/license`` =
JSON ``<mcp_cli> setup license-info --json`` = ``licensing.license_summary`` MCP
плюс поля хаба) получает ``activation_state`` (active / pending / overdue /
cap_exceeded / revoked), ``activation_deadline``, ``last_online_error`` —
``licenseActivationView`` в app.js рисует их честно, а для старого MCP без
``activation_state`` держит фолбэк по ``activated``.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import standkit_hub.server as server_module
from tests.test_gap604_updates_badge import _extract_function, _read_js

WEB_DIR = server_module.Path(server_module.__file__).parent / "web"
NODE = shutil.which("node")

CASES = {
    "active": {"activation_state": "active", "activated": True,
               "activated_at": "2026-10-05T10:00:00", "fingerprint_label": "WS-01"},
    "pending": {"activation_state": "pending", "activated": False,
                "activation_deadline": "2026-10-10T12:00:00",
                "last_online_error": "нет связи с сервером"},
    "pending_no_err": {"activation_state": "pending", "activated": False,
                       "activation_deadline": "2026-10-10T12:00:00"},
    "overdue": {"activation_state": "overdue", "activated": False,
                "activation_deadline": "2026-10-01T12:00:00"},
    "cap": {"activation_state": "cap_exceeded", "activated": False},
    "revoked": {"activation_state": "revoked", "activated": False},
    "old_not_activated": {"activated": False},
    "old_activated": {"activated": True, "activated_at": "2026-10-05T10:00:00"},
    "unknown_state": {"activation_state": "something_new", "activated": False},
}


def test_page_has_no_old_publisher_wording():
    js = _read_js()
    assert "не активирована у издателя" not in js
    assert "licenseActivationView(snapshot)" in _extract_function(js, "renderLicensePane")


def test_markup_has_activation_key_id():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="lic-activated-k"' in html


@pytest.fixture(scope="module")
def views(tmp_path_factory):
    if NODE is None:
        pytest.skip("node недоступен в этой среде")
    js = _read_js()
    script = "\n".join([
        _extract_function(js, "formatDate"),
        _extract_function(js, "licenseActivationView"),
        f"const CASES = {json.dumps(CASES, ensure_ascii=False)};",
        "const out = {};",
        "for (const [k, snap] of Object.entries(CASES)) out[k] = licenseActivationView(snap);",
        "console.log(JSON.stringify(out));",
    ])
    path = tmp_path_factory.mktemp("gap782") / "view.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run([NODE, str(path)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_active(views):
    v = views["active"]
    assert v["key"] == "Активирована на"
    assert v["text"] == "этом компьютере (WS-01) · 05.10.2026"
    assert v["cls"] == ""


def test_pending_shows_deadline_and_last_error(views):
    v = views["pending"]
    assert v["cls"] == "lic-warn"
    assert "ожидает подтверждения сервером (до 10.10.2026)" in v["text"]
    assert "нет связи с сервером" in v["text"]
    assert views["pending_no_err"]["text"] == "ожидает подтверждения сервером (до 10.10.2026)"


@pytest.mark.parametrize("case,needle", [
    ("overdue", "подключитесь к интернету"),
    ("cap", "превышено число установок"),
    ("revoked", "отозвана издателем"),
])
def test_blocking_states_are_red_with_action(views, case, needle):
    v = views[case]
    assert v["cls"] == "lic-crit"
    assert needle in v["text"].lower()


def test_overdue_mentions_deadline(views):
    assert "01.10.2026" in views["overdue"]["text"]


def test_old_server_fallback(views):
    assert views["old_not_activated"]["text"] == "онлайн-активация не подтверждена"
    assert "издател" not in views["old_not_activated"]["text"]
    assert views["old_activated"]["text"] == "этом компьютере · 05.10.2026"
    assert views["unknown_state"]["text"] == "онлайн-активация не подтверждена"
