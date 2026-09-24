"""
GAP-522 (24.09.2026): меню «Справка» в шапке дашборда — кукбук BPMkit +
кукбук BPMkitStand.

Держим три вещи:
  * поиск локальной копии кукбука BPMkit (`standkit_hub.bpmkit_cookbook`):
    профиль / папка установки по маркеру `mcp_runtime.json`, новейшая копия;
  * маршрут хаба `GET /bpmkit-cookbook`: отдаёт файл как HTML без кэша,
    а при отсутствии копии — человеческую 404-страницу, не JSON;
  * разметку меню: кнопка «?» раскрывает два пункта-ссылки, и пункты НЕ
    носят класс `.split-btn-menu-item` (его обработчики в app.js открывают
    папку логов — пункт справки с этим классом открыл бы Проводник).
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

import standkit_hub.server as server_module
from standkit_hub import bpmkit_cookbook
from tests.test_hub_server import _close_hub_servers, _start_hub  # noqa: F401 - autouse-фикстура

WEB_DIR = Path(server_module.__file__).parent / "web"


def _doc(version: str | None, marker: str = "") -> str:
    meta = (
        f'<meta name="bpmkit-cookbook-version" content="{version}">' if version else ""
    )
    return f"<!doctype html><html><head>{meta}</head><body>{marker}</body></html>"


def _put(path: Path, text: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _marker(tmp_path: Path, app_dir: Path) -> Path:
    marker = tmp_path / "marker" / "mcp_runtime.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"version": "1.1.200", "started_at": "2026-09-24T10:00:00Z",
                    "binary": str(app_dir / "server" / "BPMkit.exe")}),
        encoding="utf-8",
    )
    return marker


# --- поиск копии -------------------------------------------------------------


def test_find_cookbook_none_when_no_copies(tmp_path):
    assert bpmkit_cookbook.find_cookbook(tmp_path / "cfg", tmp_path / "no-marker.json") is None


def test_find_cookbook_profile_only(tmp_path):
    cfg = tmp_path / "cfg"
    _put(cfg / "docs" / "cookbook.html", _doc("1.1.150-aaaa0000"))
    found = bpmkit_cookbook.find_cookbook(cfg, tmp_path / "no-marker.json")
    assert found["origin"] == "профиль"
    assert found["path"] == cfg / "docs" / "cookbook.html"
    assert found["version"] == "1.1.150-aaaa0000"


def test_find_cookbook_shipped_via_runtime_marker(tmp_path):
    app = tmp_path / "Program Files" / "BPMkit"
    _put(app / "docs" / "cookbook.html", _doc("1.1.150-bbbb0000"))
    found = bpmkit_cookbook.find_cookbook(tmp_path / "cfg", _marker(tmp_path, app))
    assert found["origin"] == "поставка"
    assert found["path"] == app / "docs" / "cookbook.html"


def test_find_cookbook_newer_version_wins_numerically(tmp_path):
    """1.1.149 новее 1.1.9 — сравнение посегментно целыми, не строкой (GAP-429)."""
    cfg = tmp_path / "cfg"
    app = tmp_path / "app"
    _put(cfg / "docs" / "cookbook.html", _doc("1.1.9-old00000"))
    _put(app / "docs" / "cookbook.html", _doc("1.1.149-new11111"))
    found = bpmkit_cookbook.find_cookbook(cfg, _marker(tmp_path, app))
    assert found["origin"] == "поставка"
    assert found["version"] == "1.1.149-new11111"


def test_find_cookbook_equal_prefix_fresher_file_wins(tmp_path):
    cfg = tmp_path / "cfg"
    app = tmp_path / "app"
    _put(cfg / "docs" / "cookbook.html", _doc("1.1.150-aaaa0000"), mtime=1_700_000_000)
    _put(app / "docs" / "cookbook.html", _doc("1.1.150-bbbb1111"), mtime=1_600_000_000)
    found = bpmkit_cookbook.find_cookbook(cfg, _marker(tmp_path, app))
    assert found["origin"] == "профиль"


def test_find_cookbook_without_versions_profile_first(tmp_path):
    cfg = tmp_path / "cfg"
    app = tmp_path / "app"
    _put(cfg / "docs" / "cookbook.html", _doc(None), mtime=1_600_000_000)
    _put(app / "docs" / "cookbook.html", _doc(None), mtime=1_700_000_000)
    found = bpmkit_cookbook.find_cookbook(cfg, _marker(tmp_path, app))
    assert found["origin"] == "профиль"


def test_find_cookbook_versioned_copy_beats_unversioned(tmp_path):
    cfg = tmp_path / "cfg"
    app = tmp_path / "app"
    _put(cfg / "docs" / "cookbook.html", _doc(None))
    _put(app / "docs" / "cookbook.html", _doc("1.0.0-cccc2222"))
    found = bpmkit_cookbook.find_cookbook(cfg, _marker(tmp_path, app))
    assert found["origin"] == "поставка"


def test_find_cookbook_broken_marker_is_ignored(tmp_path):
    cfg = tmp_path / "cfg"
    _put(cfg / "docs" / "cookbook.html", _doc("1.1.150-aaaa0000"))
    marker = tmp_path / "mcp_runtime.json"
    marker.write_text("{не json", encoding="utf-8")
    found = bpmkit_cookbook.find_cookbook(cfg, marker)
    assert found["origin"] == "профиль"
    assert [origin for origin, _ in bpmkit_cookbook.candidates(cfg, marker)] == ["профиль"]


def test_find_cookbook_directory_instead_of_file_is_ignored(tmp_path):
    cfg = tmp_path / "cfg"
    (cfg / "docs" / "cookbook.html").mkdir(parents=True)
    assert bpmkit_cookbook.find_cookbook(cfg, tmp_path / "no-marker.json") is None


# --- маршрут хаба ------------------------------------------------------------


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_route_serves_bpmkit_cookbook_without_auth(tmp_path, monkeypatch):
    doc = _put(tmp_path / "cfg" / "docs" / "cookbook.html",
               _doc("1.1.150-aaaa0000", "КУКБУК-BPMKIT-МАРКЕР"))
    monkeypatch.setattr(
        server_module._bpmkit_cookbook, "find_cookbook",
        lambda *a, **k: {"origin": "профиль", "path": doc, "version": "1.1.150-aaaa0000"},
    )
    base_url, *_ = _start_hub(tmp_path)
    status, headers, body = _get(base_url + server_module.BPMKIT_COOKBOOK_ROUTE)
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")
    assert headers.get("Cache-Control") == "no-store"
    assert body == doc.read_bytes()


def test_route_missing_cookbook_is_human_404_page(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module._bpmkit_cookbook, "find_cookbook", lambda *a, **k: None)
    base_url, *_ = _start_hub(tmp_path)
    status, headers, body = _get(base_url + "/bpmkit-cookbook")
    assert status == 404
    assert "text/html" in headers.get("Content-Type", "")
    text = body.decode("utf-8")
    assert "Кукбук BPMkit не найден" in text
    assert 'href="/static/cookbook.html"' in text
    assert "bpmkit.pro" in text


# --- разметка меню -----------------------------------------------------------


def _help_block(html: str) -> str:
    start = html.index('id="help-menu-wrap"')
    end = html.index("</div>\n    </div>", start)
    return html[start:end]


def test_help_button_is_menu_with_both_cookbooks():
    html = (WEB_DIR / "index.html").read_text("utf-8")
    block = _help_block(html)
    assert re.search(r'<button id="help-btn"[^>]*aria-haspopup="menu"', block, re.S)
    assert 'href="/bpmkit-cookbook"' in block
    assert 'href="/static/cookbook.html"' in block
    assert "Кукбук BPMkit<" in block and "Кукбук BPMkitStand<" in block
    # Обработчики .split-btn-menu-item открывают папку логов (setupStatePanel).
    assert "split-btn-menu-item" not in block
    assert block.count('target="_blank"') == 2


def test_about_pane_links_both_cookbooks():
    html = (WEB_DIR / "index.html").read_text("utf-8")
    assert '<a href="/static/cookbook.html" target="_blank" rel="noopener">кукбук BPMkitStand</a>' in html
    assert '<a href="/bpmkit-cookbook" target="_blank" rel="noopener">кукбук BPMkit</a>' in html


def test_app_js_wires_help_menu():
    js = (WEB_DIR / "app.js").read_text("utf-8")
    assert "function setupHelpMenu()" in js
    assert re.search(r"setupStatePanel\(\);\s*setupHelpMenu\(\);", js)
