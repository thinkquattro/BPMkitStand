# -*- coding: utf-8 -*-
"""
GAP-783: exe-диспетчер (установлен установщиком BPMkit, frozen) не показывает pip.

Решение владельца: «в версии, установленной из установщика, в меню обновления не
должно быть команды pip и кнопки копирования». Плюс дефект из аудита: в платном
виде окна «Обновления» подпись строки «Диспетчер стендов» всегда была «X · pip»,
даже у exe (свободный вид писал верно — «X · установщик»). И свободный вид exe
обещал «Проверяется по PyPI» и рисовал чип «последняя», хотя сервер для exe PyPI
не спрашивает вовсе.

Два слоя, как в test_gap604_updates_badge.py:
  - статический — разметка/сервер/стили;
  - поведенческий (если есть ``node``) — РЕАЛЬНЫЕ функции рендера, вырезанные из
    app.js, на подставном DOM.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

import standkit_hub.self_version as sv
import standkit_hub.server as server_module
from tests.test_gap604_updates_badge import _extract_function, _read_js
from tests.test_hub_server import _request, _start_hub

WEB_DIR = server_module.Path(server_module.__file__).parent / "web"
NODE = shutil.which("node")

FUNCS = (
    "hubBlock",
    "hubIsFrozen",
    "hubVersionLabel",
    "renderHubRow",
    "renderSelfVersionRow",
    "updatesFooterText",
)

ELEMENT_IDS = (
    "upd-hub-current", "upd-hub-apply-btn", "upd-hub-stage-btn", "upd-hub-pip",
    "upd-hub-pip-copy-btn", "upd-hub-pip-cmd", "upd-hub-installer-note",
    "upd-hub-wait-note", "upd-self-current", "upd-self-pip", "upd-self-pip-cmd",
    "upd-self-pip-copy-btn", "upd-self-installer-note",
)


# --------------------------------------------------------------------------
# Статика
# --------------------------------------------------------------------------

def test_markup_has_installer_notes_hidden_by_default():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for el_id in ("upd-hub-installer-note", "upd-self-installer-note"):
        marker = f'id="{el_id}" hidden>Обновление — установщиком BPMkit<'
        assert marker in html, el_id


def test_css_hides_inline_sub_with_hidden_attribute():
    css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    assert ".upd-sub.upd-sub-inline[hidden] { display: none; }" in css


def test_paid_row_label_no_longer_hardcodes_pip():
    body = _extract_function(_read_js(), "renderHubRow")
    assert "· pip`" not in body, "подпись платного вида снова зашита как «· pip» (GAP-783)"
    assert "hubVersionLabel(current, frozen)" in body


def test_self_version_frozen_payload_has_no_pip_command(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "is_frozen", lambda: True)
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/hub/self-version", token=token)
    assert status == 200
    assert body["mode"] == "frozen"
    assert body["pip_command"] is None
    assert body["source"] is None


# --------------------------------------------------------------------------
# Поведение в node
# --------------------------------------------------------------------------

HUB_CASES = {
    "exe_up_to_date": {"hub": {"frozen": True, "mode": "exe", "current": "0.12.22",
                               "update_available": False}},
    "exe_new_known": {"hub": {"frozen": True, "mode": "exe", "current": "0.12.22",
                              "known_latest": "0.12.23", "update_available": True}},
    "exe_staged": {"hub": {"frozen": True, "mode": "exe", "current": "0.12.22",
                           "staged": {"version": "0.12.23"}, "update_available": True}},
    # старый канал без поля frozen — признак по mode="exe"
    "exe_old_channel": {"hub": {"mode": "exe", "current": "0.12.22"}},
    "pip": {"hub": {"frozen": False, "mode": "pip", "current": "0.12.22",
                    "update_available": False}},
}

SELF_CASES = {
    "frozen": {"current": "0.12.22", "mode": "frozen", "latest": None,
               "update_available": False, "error": None, "pip_command": None},
    "pip": {"current": "0.12.22", "mode": "pip", "latest": "0.12.23",
            "update_available": True, "error": None,
            "pip_command": "python -m pip install -U standkit"},
}


def _run_node(tmp_path) -> dict:
    js = _read_js()
    parts = [_extract_function(js, name) for name in FUNCS]
    script = "\n".join(
        [
            "let ELS = {};",
            "let CHIPS = {};",
            "let DETAILS = {};",
            "let hubVersion = '';",
            "let companionBusy = false;",
            "let lastSelfVersion = null;",
            "function byId(id) { return ELS[id] || null; }",
            "function setChip(id, kind, text, title) { CHIPS[id] = {kind, text}; }",
            "function setDetail(id, text) { DETAILS[id] = text || ''; }",
            f"const IDS = {json.dumps(ELEMENT_IDS)};",
            "function freshDom() {",
            "  ELS = {}; CHIPS = {}; DETAILS = {};",
            "  for (const id of IDS) ELS[id] = {id, hidden: true, textContent: 'python -m pip install -U standkit', title: '', dataset: {}};",
            "}",
            "function snap() {",
            "  const out = {};",
            "  for (const id of IDS) out[id] = {hidden: ELS[id].hidden, text: ELS[id].textContent, title: ELS[id].title};",
            "  out.chips = CHIPS;",
            "  return out;",
            "}",
            *parts,
            f"const HUB = {json.dumps(HUB_CASES)};",
            f"const SELF = {json.dumps(SELF_CASES)};",
            "const out = {hub: {}, self: {}, footer: {}};",
            "for (const [k, st] of Object.entries(HUB)) { freshDom(); renderHubRow(st); out.hub[k] = snap(); }",
            "for (const [k, d] of Object.entries(SELF)) { freshDom(); renderSelfVersionRow(d); out.self[k] = snap(); }",
            "out.footer.paid = updatesFooterText(true, SELF.frozen);",
            "out.footer.free_frozen = updatesFooterText(false, SELF.frozen);",
            "out.footer.free_pip = updatesFooterText(false, SELF.pip);",
            "out.footer.free_unknown = updatesFooterText(false, null);",
            "console.log(JSON.stringify(out));",
        ]
    )
    path = tmp_path / "gap783_render.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(path)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    if NODE is None:
        pytest.skip("node недоступен в этой среде")
    return _run_node(tmp_path_factory.mktemp("gap783"))


@pytest.mark.parametrize("case", ["exe_up_to_date", "exe_new_known", "exe_staged", "exe_old_channel"])
def test_paid_exe_row_has_no_pip_and_says_installer(rendered, case):
    r = rendered["hub"][case]
    assert r["upd-hub-pip"]["hidden"] is True
    assert r["upd-hub-pip-copy-btn"]["hidden"] is True
    assert r["upd-hub-current"]["text"] == "0.12.22 · установщик"
    assert "pip" not in r["upd-hub-current"]["title"].lower()


def test_paid_exe_row_shows_channel_buttons_or_installer_hint(rendered):
    up = rendered["hub"]["exe_up_to_date"]
    assert up["upd-hub-installer-note"]["hidden"] is False
    assert up["upd-hub-stage-btn"]["hidden"] is True
    assert up["upd-hub-apply-btn"]["hidden"] is True

    new = rendered["hub"]["exe_new_known"]
    assert new["upd-hub-stage-btn"]["hidden"] is False
    assert new["upd-hub-installer-note"]["hidden"] is True

    staged = rendered["hub"]["exe_staged"]
    assert staged["upd-hub-apply-btn"]["hidden"] is False
    assert staged["upd-hub-apply-btn"]["text"] == "Установить 0.12.23"
    assert staged["upd-hub-installer-note"]["hidden"] is True


def test_paid_pip_row_keeps_pip_command_and_copy(rendered):
    r = rendered["hub"]["pip"]
    assert r["upd-hub-current"]["text"] == "0.12.22 · pip"
    assert r["upd-hub-pip"]["hidden"] is False
    assert r["upd-hub-pip-copy-btn"]["hidden"] is False
    assert r["upd-hub-installer-note"]["hidden"] is True


def test_free_frozen_row_has_no_pip_and_no_false_pypi_chip(rendered):
    r = rendered["self"]["frozen"]
    assert r["upd-self-current"]["text"] == "0.12.22 · установщик"
    assert r["upd-self-pip"]["hidden"] is True
    assert r["upd-self-pip-copy-btn"]["hidden"] is True
    assert r["upd-self-installer-note"]["hidden"] is False
    chip = r["chips"].get("upd-self-chip")
    assert chip == {"kind": None, "text": ""}, "чип «последняя» обещал бы сверку, которой для exe нет"


def test_free_pip_row_unchanged(rendered):
    r = rendered["self"]["pip"]
    assert r["upd-self-current"]["text"] == "0.12.22 · pip"
    assert r["upd-self-pip"]["hidden"] is False
    assert r["upd-self-pip-copy-btn"]["hidden"] is False
    assert r["upd-self-installer-note"]["hidden"] is True
    assert r["chips"]["upd-self-chip"]["kind"] == "new"


def test_footer_does_not_promise_pypi_for_exe(rendered):
    f = rendered["footer"]
    assert "PyPI" not in f["free_frozen"]
    assert "установщиком" in f["free_frozen"]
    assert "PyPI" in f["free_pip"]
    assert "PyPI" in f["free_unknown"]
    assert "PyPI" not in f["paid"]
