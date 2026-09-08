# -*- coding: utf-8 -*-
"""Нативный выбор файла/каталога: ``standkit_hub/pick_dialog.py`` и ``POST /api/pick``.

НИ ОДНО настоящее окно здесь не открывается — и не может: набор гоняется в CI без
рабочего стола, а зависший модальный диалог остановил бы прогон намертво. Поэтому
проверяется то, что ломается молча:

* СБОРКА команды под каждую ОС — по подставным ``platform``/``which``. Для Windows
  критичны два свойства, без которых диалог теряется за окном браузера и выглядит
  зависшим интерфейсом: ключ ``-STA`` и владелец-форма с ``TopMost``;
* разбор вывода: путь, отмена (пустой вывод ИЛИ ненулевой код) и отсутствие диалога
  (``no_dialog``) — три РАЗНЫХ исхода, которые UI показывает по-разному;
* подстановка пользовательского текста в PowerShell: кавычка в заголовке не имеет
  права разорвать команду.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

from standkit.models import Stand
from standkit.registry import Registry
from standkit_hub import pick_dialog
from standkit_hub.config import HubConfig
from standkit_hub.security import generate_session_token
from standkit_hub.server import create_hub_server


def _no_tools(_name):
    """``which``, который не находит ничего: машина без zenity/kdialog/osascript."""
    return None


def _has(*names):
    return lambda name: f"/usr/bin/{name}" if name in names else None


# ======================================================================================
# Сборка команды
# ======================================================================================


@pytest.mark.parametrize("kind,dialog", [("file", "OpenFileDialog"),
                                         ("dir", "FolderBrowserDialog")])
def test_windows_command_uses_winforms_dialog(kind, dialog):
    argv = pick_dialog.build_command(kind, title="Выберите", platform="win32",
                                     which=_no_tools)

    assert argv[0] == "powershell"
    # -STA обязателен: без однопоточной COM-модели ни один из диалогов не открывается.
    assert "-STA" in argv
    script = argv[-1]
    assert dialog in script
    assert "System.Windows.Forms" in script


def test_windows_dialog_has_topmost_owner_form():
    """Диалог обязан всплыть ПОВЕРХ браузера — иначе он не виден вовсе."""
    script = pick_dialog.build_command("file", platform="win32", which=_no_tools)[-1]

    assert "$owner.TopMost = $true" in script
    assert "ShowDialog($owner)" in script
    # Путь печатается в UTF-8: иначе кириллица в пути превращается в «??????».
    assert "OutputEncoding" in script


def test_windows_script_escapes_quotes_in_user_text():
    """Кавычка в заголовке не имеет права разорвать команду PowerShell."""
    script = pick_dialog.build_command(
        "file", title="Файл 'ключа'", initial=r"C:\Ключи", file_filter="Лицензия (*.lic)|*.lic",
        platform="win32", which=_no_tools)[-1]

    assert "'Файл ''ключа'''" in script
    assert r"'C:\Ключи'" in script
    assert "'Лицензия (*.lic)|*.lic'" in script


def test_windows_file_filter_defaults_to_all_files():
    script = pick_dialog.build_command("file", platform="win32", which=_no_tools)[-1]
    assert pick_dialog.DEFAULT_FILTER in script


def test_newlines_are_stripped_from_user_text():
    """Перевод строки в заголовке ломает разбор команды — вырезаем, а не экранируем."""
    script = pick_dialog.build_command("file", title="Первая\nВторая", platform="win32",
                                       which=_no_tools)[-1]
    assert "\n" not in script


def test_linux_prefers_zenity():
    argv = pick_dialog.build_command("dir", title="Каталог логов", initial="/var/log",
                                     platform="linux", which=_has("zenity", "kdialog"))

    assert argv[0] == "zenity"
    assert "--file-selection" in argv
    assert "--directory" in argv
    assert "--title=Каталог логов" in argv
    assert "--filename=/var/log" in argv


def test_linux_falls_back_to_kdialog():
    argv = pick_dialog.build_command("file", title="Ключ", initial="/opt",
                                     platform="linux", which=_has("kdialog"))

    assert argv[:3] == ["kdialog", "--getopenfilename", "/opt"]


def test_macos_uses_osascript():
    argv = pick_dialog.build_command("dir", title="Каталог", platform="darwin",
                                     which=_has("osascript"))

    assert argv[0] == "osascript"
    assert "choose folder" in argv[-1]


def test_no_dialog_available_returns_none():
    assert pick_dialog.build_command("file", platform="linux", which=_no_tools) is None


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        pick_dialog.build_command("printer", platform="linux", which=_no_tools)


# ======================================================================================
# Разбор вывода
# ======================================================================================


@pytest.mark.parametrize("rc,stdout,expected", [
    (0, "/home/user/license.lic\n", "/home/user/license.lic"),
    (0, "C:\\Ключи\\license.lic\r\n", "C:\\Ключи\\license.lic"),
])
def test_parse_output_returns_selected_path(rc, stdout, expected):
    assert pick_dialog.parse_output(rc, stdout) == {"path": expected}


@pytest.mark.parametrize("rc,stdout", [
    (0, ""),          # PowerShell: отмена = код 0 и пустой вывод
    (1, ""),          # zenity/kdialog/osascript: отмена = код 1
    (1, "мусор"),     # отказ утилиты — для UI это та же отмена, без ложного пути
])
def test_parse_output_treats_cancel_as_null_path(rc, stdout):
    assert pick_dialog.parse_output(rc, stdout) == {"path": None}


def test_pick_returns_no_dialog_when_nothing_available():
    """Отмена и «диалога нет» — РАЗНЫЕ исходы: во втором UI просит ввести путь руками."""
    result = pick_dialog.pick("file", platform="linux", which=_no_tools)

    assert result == {"path": None, "error": pick_dialog.NO_DIALOG}


def test_pick_passes_command_to_runner_and_parses_result():
    calls: list = []

    def _run(argv):
        calls.append(argv)
        return 0, "/tmp/license.lic\n", ""

    result = pick_dialog.pick("file", title="Ключ", run=_run, platform="linux",
                              which=_has("zenity"))

    assert result == {"path": "/tmp/license.lic"}
    assert calls[0][0] == "zenity"


def test_pick_does_not_open_real_dialog_in_tests():
    """Страховка самого набора: без `run` вызов ушёл бы в настоящую утилиту."""
    result = pick_dialog.pick("dir", platform="linux", which=_no_tools)
    assert result["error"] == pick_dialog.NO_DIALOG


# ======================================================================================
# Маршрут хаба
# ======================================================================================


def _start_hub(tmp_path):
    registry_path = tmp_path / "projects.json"
    Registry(path=registry_path, default="demo",
             stands={"demo": Stand(name="demo", stand_dir=str(tmp_path / "demo"))}).save()
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path)).save(config_path)
    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return f"http://127.0.0.1:{port}", token, httpd


@pytest.fixture()
def hub(tmp_path):
    base_url, token, httpd = _start_hub(tmp_path)
    yield base_url, token
    for attr in ("status_poller", "companion_runner"):
        worker = getattr(httpd, attr, None)
        if worker is not None:
            try:
                worker.stop(timeout=0.2)
            except Exception:  # noqa: BLE001 - уборка не роняет тест
                pass
    stopper = threading.Thread(target=httpd.shutdown, daemon=True)
    stopper.start()
    stopper.join(timeout=1.0)
    try:
        httpd.server_close()
    except Exception:  # noqa: BLE001 - уборка не роняет тест
        pass


def _post(base_url, path, token, body, *, origin=None):
    req = urllib.request.Request(base_url + path, method="POST")
    req.add_header("X-Standkit-Token", token)
    req.add_header("Origin", origin if origin is not None else base_url)
    req.add_header("Content-Type", "application/json")
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        with urllib.request.urlopen(req, data=data, timeout=5.0) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else {})


def test_api_pick_returns_selected_path(hub, monkeypatch):
    seen: list = []

    def _fake(kind, **kwargs):
        seen.append((kind, kwargs))
        return {"path": "C:\\Ключи\\license.lic"}

    monkeypatch.setattr(pick_dialog, "pick", _fake)
    base_url, token = hub

    status, body = _post(base_url, "/api/pick", token,
                         {"kind": "file", "title": "Файл лицензии",
                          "initial": "C:\\Ключи", "filter": "Лицензия (*.lic)|*.lic"})

    assert status == 200
    assert body == {"path": "C:\\Ключи\\license.lic"}
    kind, kwargs = seen[0]
    assert kind == "file"
    assert kwargs["title"] == "Файл лицензии"
    assert kwargs["initial"] == "C:\\Ключи"
    assert kwargs["file_filter"] == "Лицензия (*.lic)|*.lic"


def test_api_pick_defaults_to_file_kind(hub, monkeypatch):
    seen: list = []
    monkeypatch.setattr(pick_dialog, "pick",
                        lambda kind, **kw: seen.append(kind) or {"path": None})
    base_url, token = hub

    status, body = _post(base_url, "/api/pick", token, {})

    assert status == 200
    assert body == {"path": None}
    assert seen == ["file"]


def test_api_pick_rejects_unknown_kind(hub):
    base_url, token = hub
    status, body = _post(base_url, "/api/pick", token, {"kind": "printer"})

    assert status == 400
    assert "kind" in body["error"]


def test_api_pick_requires_local_origin(hub):
    base_url, token = hub
    status, _body = _post(base_url, "/api/pick", token, {"kind": "file"},
                          origin="http://evil.example")
    assert status == 403


def test_api_pick_reports_missing_dialog(hub, monkeypatch):
    monkeypatch.setattr(pick_dialog, "pick",
                        lambda kind, **kw: {"path": None, "error": pick_dialog.NO_DIALOG})
    base_url, token = hub

    status, body = _post(base_url, "/api/pick", token, {"kind": "dir"})

    assert status == 200
    assert body["error"] == "no_dialog"


def test_api_pick_survives_dialog_failure(hub, monkeypatch):
    """Сбой утилиты диалога не имеет права уронить хаб или оборвать соединение."""
    def _boom(kind, **kwargs):
        raise RuntimeError("подсистема окон недоступна")

    monkeypatch.setattr(pick_dialog, "pick", _boom)
    base_url, token = hub

    status, body = _post(base_url, "/api/pick", token, {"kind": "file"})

    assert status == 500
    assert body["path"] is None
    assert "RuntimeError" in body["error"]
