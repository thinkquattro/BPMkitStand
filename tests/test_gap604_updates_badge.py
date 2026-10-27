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
    # GAP-1045: actionUnavailableReason берёт причину из mcpUpdateView.
    "formatMegabytes",
    "mcpUpdateView",
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


# --------------------------------------------------------------------------
# GAP-1045: строка MCP окна «Обновления» — «Скачать»/ход/ошибка вместо
# выключенной «Установить», «Откатить» не рядом с доступным обновлением.
# --------------------------------------------------------------------------

MCP_ROW_CASES = {
    # версия известна, но не скачана (случай клиента: 1.3.16 → 1.4.32)
    "known_not_staged": {
        "state": {"releases": {"known_latest": "1.4.32", "current_version": "1.3.16",
                     "update_available": True, "rollback_available": True}},
        "actions": {"apply_update": False, "stage_update": True, "rollback": True},
    },
    # загрузка оборвалась на середине — кусок на диске
    "partial": {
        "state": {"releases": {"known_latest": "1.4.32", "current_version": "1.3.16",
                     "update_available": True, "resume_bytes": 52428800,
                     "resume_size_bytes": 96468992}},
        "actions": {"apply_update": False, "stage_update": True},
    },
    # прошлая загрузка упала — причина из состояния канала
    "stage_error": {
        "state": {"releases": {"known_latest": "1.4.32", "current_version": "1.3.16",
                     "update_available": True, "status": "error",
                     "detail": "read operation timed out"}},
        "actions": {"apply_update": False, "stage_update": True},
    },
    # скачана и проверена — «Установить» включена, «Откатить» не показывается
    "staged": {
        "state": {"releases": {"known_latest": "1.4.32", "staged_version": "1.4.32",
                     "current_version": "1.3.16", "update_available": True,
                     "rollback_available": True}},
        "actions": {"apply_update": True, "stage_update": True, "rollback": True},
    },
    # версию ставит установщик — ни «Скачать», ни «Установить» в этой строке
    "requires_installer": {
        "state": {"releases": {"known_latest": "1.4.32", "current_version": "1.3.16",
                     "update_available": True, "requires_installer": True}},
        "actions": {"apply_update": False, "stage_update": True},
    },
    # актуально, есть история — «Откатить» видна
    "up_to_date_with_history": {
        "state": {"releases": {"known_latest": "1.4.32", "current_version": "1.4.32",
                     "update_available": False, "rollback_available": True}},
        "actions": {"apply_update": False, "rollback": True},
    },
}


def _run_mcp_row_node(tmp_path) -> dict:
    js = _read_js()
    parts = [_extract_function(js, name) for name in FUNCS]
    script = "\n".join(
        [
            'const COMPANION_ACTION_REASONS = {apply_update: "generic"};',
            *parts,
            f"const CASES = {json.dumps(MCP_ROW_CASES)};",
            "const out = {};",
            "for (const [k, st] of Object.entries(CASES)) {",
            "  out[k] = {view: mcpUpdateView(st), reason: actionUnavailableReason('apply_update', st)};",
            "}",
            "console.log(JSON.stringify(out));",
        ]
    )
    path = tmp_path / "gap1045_mcp_row.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(
        [NODE, str(path)], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


class TestGap1045McpRow:
    """GAP-1045: видимость кнопок строки MCP совпадает с их доступностью."""

    @pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
    def test_known_but_not_staged_offers_download_not_disabled_install(self, tmp_path):
        r = _run_mcp_row_node(tmp_path)["known_not_staged"]
        assert r["view"]["install"] is False
        assert r["view"]["stage"] is True
        assert r["view"]["stageLabel"] == "Скачать"
        assert "1.4.32" in r["view"]["detail"] and "не скачана" in r["view"]["detail"]
        # откат рядом с доступным обновлением не показывается
        assert r["view"]["rollback"] is False
        # подсказка выключенной «Установить» — та же причина, а не общая фраза
        assert r["reason"] == r["view"]["detail"]

    @pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
    def test_partial_download_shows_progress(self, tmp_path):
        r = _run_mcp_row_node(tmp_path)["partial"]["view"]
        assert r["stage"] is True and r["stageState"] == "partial"
        assert r["stageLabel"] == "Докачать"
        assert "50,0 МБ" in r["detail"] and "92,0 МБ" in r["detail"]

    @pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
    def test_stage_error_shows_reason_and_retry(self, tmp_path):
        r = _run_mcp_row_node(tmp_path)["stage_error"]["view"]
        assert r["stageState"] == "error" and r["stageLabel"] == "Повторить"
        assert "read operation timed out" in r["detail"]

    @pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
    def test_staged_shows_enabled_install_without_rollback(self, tmp_path):
        r = _run_mcp_row_node(tmp_path)["staged"]["view"]
        assert r["install"] is True and r["stage"] is False and r["rollback"] is False

    @pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
    def test_installer_required_offers_neither(self, tmp_path):
        r = _run_mcp_row_node(tmp_path)["requires_installer"]
        assert r["view"]["install"] is False and r["view"]["stage"] is False
        assert "установщиком" in r["reason"]

    @pytest.mark.skipif(NODE is None, reason="node недоступен в этой среде")
    def test_rollback_only_without_pending_update(self, tmp_path):
        r = _run_mcp_row_node(tmp_path)["up_to_date_with_history"]["view"]
        assert r["rollback"] is True and r["install"] is False and r["stage"] is False

    def test_markup_and_render_use_single_view(self):
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        js = _read_js()
        assert '<button type="button" id="upd-stage-btn" data-companion-action="stage_update" hidden>' in html
        body = _extract_function(js, "renderMcpRow")
        assert "mcpUpdateView(status)" in body
        assert "install.hidden = !view.install" in body
        assert "!view.rollback" in body
        assert 'stage_update: "Скачиваем…"' in js

    def test_status_summary_reports_partial_size(self, tmp_path):
        from standkit_companion.state import CompanionState
        state = CompanionState(tmp_path / "state.json")
        state.releases["partial"] = {"bytes": 1024, "size_bytes": 4096}
        rel = state.summary()["releases"]
        assert (rel["resume_bytes"], rel["resume_size_bytes"]) == (1024, 4096)
        state.releases["partial"] = None
        assert state.summary()["releases"]["resume_size_bytes"] is None
