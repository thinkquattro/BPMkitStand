# -*- coding: utf-8 -*-
"""Меню «Справка» диспетчера снова даёт оба кукбука (29.10.2026).

Что было. У издателя кнопка «?» открывала только кукбук диспетчера. Меню с двумя
пунктами (кукбук BPMkit + кукбук BPMkitStand) есть в standkit с 0.12.10, но окно
показывал НЕ pip-диспетчер 0.12.28, а ``BPMkit-hub.exe`` от 16.09 из давней поставки
BPMkit 1.1.51 (standkit 0.10, до меню): с 0.12.27 pip-запуск при установленном exe
передаёт ему управление БЕЗ сверки версий, а pip-экземпляр не отбирает порт у exe.
Второе: кукбук пользователя искался в папке поставки только по маркеру MCP
``mcp_runtime.json``, а маркер у издателя указывал во временную песочницу релизного
смоука -- копия поставки не находилась.

Держим:
  * pip не передаёт управление exe, который СТАРЕЕ его (версия -- из исходников
    ``{app}\\BPMkitStand`` той же поставки); равный/новее/неизвестный -- передаёт;
  * pip перехватывает порт у работающего exe только если тот заведомо старее;
    exe не вытесняет pip-экземпляр новее себя (кроме повышения прав);
  * кукбук BPMkit находится в ``{app}\\docs`` и без маркера (каталог установки,
    каталог своего exe), маршрут ``/bpmkit-cookbook`` его отдаёт;
  * в меню пункт кукбука BPMkit -- первым, кукбука диспетчера -- вторым.
"""
from __future__ import annotations

import json
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import standkit_hub.server as server_module
from standkit import __version__ as OUR_VERSION
from standkit_hub import __main__ as hub_main
from standkit_hub import bpmkit_cookbook, installed_exe, instance
from tests.test_hub_server import _close_hub_servers, _start_hub  # noqa: F401 - autouse-фикстура

WEB_DIR = Path(server_module.__file__).parent / "web"


def _app_with_hub(tmp_path: Path, standkit_version: str | None, *, pyproject_only=False) -> Path:
    """Поставка BPMkit: ``{app}\\BPMkit-hub.exe`` + исходники ``{app}\\BPMkitStand``."""
    app = tmp_path / "Programs" / "BPMkit"
    app.mkdir(parents=True)
    (app / installed_exe.HUB_EXE_NAME).write_bytes(b"MZ")
    if standkit_version:
        stand = app / "BPMkitStand"
        (stand / "standkit").mkdir(parents=True)
        (stand / "pyproject.toml").write_text(
            f'[project]\nname = "standkit"\nversion = "{standkit_version}"\n', encoding="utf-8")
        if not pyproject_only:
            (stand / "standkit" / "__init__.py").write_text(
                f'"""doc"""\n\n__version__ = "{standkit_version}"\n', encoding="utf-8")
    return app


def _args(**kw):
    base = dict(takeover=False, result_file=None, initiator_sid=None, elevated_op=None,
                apply_self_update=False, hub_stop=False, session_token_file=None,
                no_browser=False)
    base.update(kw)
    return types.SimpleNamespace(**base)


@pytest.fixture
def pip_start(monkeypatch):
    monkeypatch.delenv(installed_exe.ALLOW_PIP_ENV, raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)


def _use_exe(monkeypatch, app: Path) -> str:
    exe = str(app / installed_exe.HUB_EXE_NAME)
    monkeypatch.setattr(installed_exe, "find_installed_hub_exe", lambda: exe)
    return exe


# --- версия exe в поставке ----------------------------------------------------


def test_installed_hub_version_from_bundled_sources(tmp_path):
    app = _app_with_hub(tmp_path, "0.10.0")
    assert installed_exe.installed_hub_version(str(app / "BPMkit-hub.exe")) == "0.10.0"


def test_installed_hub_version_pyproject_fallback(tmp_path):
    app = _app_with_hub(tmp_path, "0.12.5", pyproject_only=True)
    assert installed_exe.installed_hub_version(str(app / "BPMkit-hub.exe")) == "0.12.5"


def test_installed_hub_version_unknown(tmp_path):
    app = _app_with_hub(tmp_path, None)
    assert installed_exe.installed_hub_version(str(app / "BPMkit-hub.exe")) is None
    assert installed_exe.installed_hub_version(None) is None


# --- передача управления pip -> exe -------------------------------------------


def test_repro_pip_does_not_hand_over_to_older_exe(tmp_path, monkeypatch, pip_start):
    """Воспроизведение 29.10: поставка 1.1.51 (standkit 0.10.0) + pip текущей версии."""
    _use_exe(monkeypatch, _app_with_hub(tmp_path, "0.10.0"))
    assert hub_main._should_hand_over_to_installed_exe(_args()) is False


@pytest.mark.parametrize("exe_version", [OUR_VERSION, "999.0.0"])
def test_pip_hands_over_to_same_or_newer_exe(tmp_path, monkeypatch, pip_start, exe_version):
    _use_exe(monkeypatch, _app_with_hub(tmp_path, exe_version))
    assert hub_main._should_hand_over_to_installed_exe(_args()) is True


def test_pip_hands_over_when_exe_version_unknown(tmp_path, monkeypatch, pip_start):
    _use_exe(monkeypatch, _app_with_hub(tmp_path, None))
    assert hub_main._should_hand_over_to_installed_exe(_args()) is True


# --- перехват порта ----------------------------------------------------------

_EXE = r"C:\Users\u\AppData\Local\Programs\BPMkit\BPMkit-hub.exe"
_PIP = r"C:\Python310\pythonw.exe"


def _running(executable: str, version: str, **kw) -> instance.HubInstanceState:
    return instance.HubInstanceState(pid=1, host="127.0.0.1", port=8770, elevated=False,
                                     version=version, executable=executable, **kw)


def test_pip_takes_over_older_running_exe():
    assert instance.should_takeover(_running(_EXE, "0.12.10"), we_elevated=False,
                                    our_version="0.12.29", our_frozen=False) is True


@pytest.mark.parametrize("exe_version", ["0.12.29", "0.12.30", ""])
def test_pip_leaves_same_newer_or_unknown_exe(exe_version):
    assert instance.should_takeover(_running(_EXE, exe_version), we_elevated=True,
                                    our_version="0.12.29", our_frozen=False) is False


def test_pip_no_takeover_flag_and_foreign_sid_respected():
    run = _running(_EXE, "0.12.10", user_sid="S-other")
    assert instance.should_takeover(run, we_elevated=False, our_version="0.12.29",
                                    our_frozen=False, no_takeover=True) is False
    assert instance.should_takeover(run, we_elevated=False, our_version="0.12.29",
                                    our_sid="S-me", our_frozen=False) is False


def test_exe_does_not_displace_newer_pip():
    run = _running(_PIP, "0.12.29")
    assert instance.should_takeover(run, we_elevated=False, our_version="0.12.28",
                                    our_frozen=True) is False
    # ради прав администратора -- по-прежнему перехватывает
    assert instance.should_takeover(run, we_elevated=True, our_version="0.12.28",
                                    our_frozen=True) is True


def test_exe_displaces_older_or_equal_pip():
    for version in ("0.12.28", "0.12.27"):
        assert instance.should_takeover(_running(_PIP, version), we_elevated=False,
                                        our_version="0.12.28", our_frozen=True) is True


# --- поиск кукбука BPMkit ----------------------------------------------------


def _doc(version: str, marker: str = "") -> str:
    return (f'<!doctype html><html><head><meta name="bpmkit-cookbook-version" '
            f'content="{version}"></head><body>{marker}</body></html>')


def _sandbox_marker(tmp_path: Path) -> Path:
    """Маркер MCP, как у издателя 29.10: binary -- в удалённой песочнице смоука."""
    marker = tmp_path / "marker" / "mcp_runtime.json"
    marker.parent.mkdir(parents=True)
    gone = tmp_path / "Temp" / "bpmkit_smoke_sandbox_x" / "pkg" / "server" / "BPMkit.exe"
    marker.write_text(json.dumps({"version": "1.4.44", "binary": str(gone),
                                  "relocated": True}), encoding="utf-8")
    return marker


def test_cookbook_found_in_install_dir_when_marker_points_to_sandbox(tmp_path):
    app = _app_with_hub(tmp_path, "0.12.28")
    (app / "docs").mkdir()
    (app / "docs" / "cookbook.html").write_text(_doc("1.18-aaaa0000"), encoding="utf-8")
    found = bpmkit_cookbook.find_cookbook(tmp_path / "cfg", _sandbox_marker(tmp_path),
                                          install_dirs=[str(app)])
    assert found is not None
    assert found["origin"] == "поставка"
    assert found["path"] == app / "docs" / "cookbook.html"


def test_cookbook_candidates_include_own_exe_dir(tmp_path, monkeypatch):
    app = tmp_path / "BPMkit"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "BPMkit-hub.exe"))
    monkeypatch.setattr(installed_exe, "install_dirs", lambda: [])
    paths = [p for _, p in bpmkit_cookbook.candidates(tmp_path / "cfg", tmp_path / "none.json")]
    assert app / "docs" / "cookbook.html" in paths


def test_cookbook_candidates_dedup(tmp_path):
    app = tmp_path / "BPMkit"
    marker = tmp_path / "m.json"
    marker.write_text(json.dumps({"binary": str(app / "server" / "BPMkit.exe")}), encoding="utf-8")
    paths = [p for _, p in bpmkit_cookbook.candidates(tmp_path / "cfg", marker,
                                                       install_dirs=[str(app)])]
    assert paths.count(app / "docs" / "cookbook.html") == 1


def _get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        return err.code, err.read()


def test_route_serves_install_dir_cookbook_without_marker(tmp_path, monkeypatch):
    app = _app_with_hub(tmp_path, "0.12.28")
    (app / "docs").mkdir()
    (app / "docs" / "cookbook.html").write_text(_doc("1.18-bbbb0000", "ПОСТАВКА-МАРКЕР"),
                                                encoding="utf-8")
    monkeypatch.setattr(bpmkit_cookbook, "bpmkit_config_dir", lambda: tmp_path / "cfg")
    monkeypatch.setattr(bpmkit_cookbook, "_runtime_marker_path", lambda: _sandbox_marker(tmp_path))
    monkeypatch.setattr(bpmkit_cookbook, "_install_dirs", lambda: [str(app)])
    base_url, *_ = _start_hub(tmp_path)
    status, body = _get(base_url + server_module.BPMKIT_COOKBOOK_ROUTE)
    assert status == 200
    assert "ПОСТАВКА-МАРКЕР" in body.decode("utf-8")


def test_route_missing_page_names_install_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(server_module._bpmkit_cookbook, "find_cookbook", lambda *a, **k: None)
    base_url, *_ = _start_hub(tmp_path)
    status, body = _get(base_url + "/bpmkit-cookbook")
    text = body.decode("utf-8")
    assert status == 404
    assert "по записи установщика BPMkit" in text
    assert 'href="/static/cookbook.html"' in text


# --- меню --------------------------------------------------------------------


def test_help_menu_bpmkit_first_stand_second():
    html = (WEB_DIR / "index.html").read_text("utf-8")
    start = html.index('id="help-menu"')
    first = html.index('id="help-menu-bpmkit"', start)
    second = html.index('id="help-menu-stand"', start)
    assert first < second
    assert html.index('href="/bpmkit-cookbook"', start) < html.index('href="/static/cookbook.html"', start)
