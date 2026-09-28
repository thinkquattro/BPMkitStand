"""
GAP-604: бейдж «1» на кнопке «Обновления» горел, когда все четыре канала в окне
«актуально/последняя» (скриншот владельца 28.09.2026).

Причина — `companionHasNews` в app.js сравнивал `releases.known_latest` и
`releases.current_version` СТРОКОЙ на неравенство: "1.1.224" !== "1.2.0" истинно,
хотя известная каналу версия СТАРШЕ установленной. Сервер уже отдаёт
`releases.update_available`, посчитанный посегментно (`compare_versions > 0`).

Два слоя, как в test_hub_elevation_ui.py:
  - статический — в app.js не осталось строкового неравенства версий;
  - поведенческий (если есть ``node``) — РЕАЛЬНЫЕ функции, вырезанные из app.js,
    на снимках статуса: бейдж не горит при актуальных каналах и горит при реально
    более новой версии / перезапуске / подготовленном установщике.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

import standkit_hub.server as server_module

WEB_DIR = server_module.Path(server_module.__file__).parent / "web"
NODE = shutil.which("node")

FUNCS = (
    "releasesBlock",
    "installerBlock",
    "parseVersion",
    "compareVersions",
    "isNewerVersion",
    "pendingInstaller",
    "companionHasNews",
    "updateNotificationEvents",
    "actionUnavailableReason",
)


def _read_js() -> str:
    return (WEB_DIR / "app.js").read_text(encoding="utf-8")


def _extract_function(js: str, name: str) -> str:
    for prefix in (f"async function {name}(", f"function {name}("):
        idx = js.find(prefix)
        if idx != -1:
            break
    else:
        raise AssertionError(f"функция {name} не найдена в app.js")
    brace_start = js.index("{", idx)
    depth = 0
    for i in range(brace_start, len(js)):
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[idx : i + 1]
    raise AssertionError(f"не нашли конец функции {name}")


def test_no_string_inequality_between_versions_left_in_app_js():
    js = _read_js()
    assert not re.search(r"String\(\w+\)\s*!==\s*String\(\w+\)", js), (
        "версии сравниваются строкой на неравенство — GAP-604"
    )


def test_companion_has_news_relies_on_server_update_available():
    body = _extract_function(_read_js(), "companionHasNews")
    assert "rel.update_available" in body
    assert "rel.known_latest" not in body


# --------------------------------------------------------------------------
# Поведение в node
# --------------------------------------------------------------------------

CASES = {
    # скриншот владельца: known_latest старше running_version, сервер update_available=false
    "owner_screenshot": {
        "state": {"releases": {"known_latest": "1.1.224", "current_version": "1.2.0",
                               "update_available": False}},
    },
    "equal": {
        "state": {"releases": {"known_latest": "1.2.0", "current_version": "1.2.0",
                               "update_available": False}},
    },
    "newer_known": {
        "state": {"releases": {"known_latest": "1.2.1", "current_version": "1.2.0",
                               "update_available": True}},
    },
    "restart": {
        "state": {"releases": {"current_version": "1.2.0", "restart_required": True}},
    },
    "installer_newer": {
        "state": {"releases": {"current_version": "1.2.0"}},
        "installer": {"staged": {"version": "1.10.0"}},
    },
    "installer_older": {
        "state": {"releases": {"current_version": "1.2.0"}},
        "installer": {"staged": {"version": "1.1.224"}},
    },
    "installer_same": {
        "state": {"releases": {"current_version": "1.2.0"}},
        "installer": {"staged": {"version": "1.2.0"}},
    },
    "requires_installer_older_latest": {
        "state": {"releases": {"known_latest": "1.1.224", "current_version": "1.2.0",
                               "requires_installer": True, "update_available": False}},
    },
    "requires_installer_newer_latest": {
        "state": {"releases": {"known_latest": "1.3.0", "current_version": "1.2.0",
                               "requires_installer": True, "update_available": True}},
    },
}


def _run_node(tmp_path) -> dict:
    js = _read_js()
    parts = [_extract_function(js, name) for name in FUNCS]
    script = "\n".join(
        [
            'const COMPANION_ACTION_REASONS = {apply_update: "generic"};',
            *parts,
            f"const CASES = {json.dumps(CASES)};",
            "const out = {};",
            "for (const [k, st] of Object.entries(CASES)) {",
            "  out[k] = {",
            "    news: companionHasNews(st),",
            "    events: updateNotificationEvents(st).map((e) => e.key),",
            "    reason: actionUnavailableReason('apply_update', st),",
            "  };",
            "}",
            "out.cmp = [compareVersions('1.10.0', '1.9.9'), compareVersions('1.2', '1.2.0'),",
            "  compareVersions('v1.1.224', '1.2.0'), isNewerVersion('', '1.0.0')];",
            "console.log(JSON.stringify(out));",
        ]
    )
    path = tmp_path / "gap604_badge.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(path)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_badge_behaviour_via_node(tmp_path):
    r = _run_node(tmp_path)
    # не горит, когда всё актуально — в т.ч. ровно ситуация со скриншота
    assert r["owner_screenshot"]["news"] is False
    assert r["equal"]["news"] is False
    assert r["installer_older"]["news"] is False
    assert r["installer_same"]["news"] is False
    # горит при реально более новой версии / перезапуске / новом установщике
    assert r["newer_known"]["news"] is True
    assert r["restart"]["news"] is True
    assert r["installer_newer"]["news"] is True


@pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
def test_notifications_and_reasons_use_segment_compare_via_node(tmp_path):
    r = _run_node(tmp_path)
    assert r["owner_screenshot"]["events"] == []
    assert r["installer_older"]["events"] == []
    assert r["newer_known"]["events"] == ["found:1.2.1"]
    assert r["installer_newer"]["events"] == ["ready:1.10.0"]
    # «нужен установщик» — только когда известная версия действительно новее
    assert r["requires_installer_older_latest"]["reason"] == "generic"
    assert "установщиком" in r["requires_installer_newer_latest"]["reason"]
    assert r["cmp"] == [1, 0, -1, False]
