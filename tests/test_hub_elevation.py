"""
Тесты перезапуска диспетчера «от имени администратора» (standkit_hub.elevation
и standkit_hub.instance) и связанного с ним API хаба.

ЗАЧЕМ ЭТО ЕСТЬ. Управление стендами IIS невозможно без прав администратора:
``appcmd.exe`` без elevation не читает даже собственный ``redirection.config``.
Совет «запустите диспетчер от имени администратора» пользователь выполнить не
мог: повторный запуск ярлыка упирался в single-instance проверку, видел
работающий экземпляр на том же порту и просто открывал браузер на нём — на
СТАРОМ, неэлевированном. Поэтому проверяем три вещи:

  - решение о перехвате порта (``should_takeover``) — только вверх по правам;
  - одноразовость и протухание файла передачи сессии (в нём сессионный токен);
  - классификацию исхода ``ShellExecuteW``: отказ в UAC — отдельный случай,
    а не «что-то пошло не так».

Реального окна UAC ни один тест не показывает: ``ShellExecuteW`` подменяется.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from standkit_hub import elevation, instance
from standkit_hub.config import HubConfig
from standkit_hub.instance import HubInstanceState
from standkit_hub.security import generate_session_token
from standkit_hub.server import create_hub_server

# --- файл передачи сессии ---


def test_handoff_round_trip_and_single_use(tmp_path):
    path = elevation.handoff_path(tmp_path)
    elevation.write_handoff(path, "секрет-сессии")

    assert elevation.read_handoff(path) == "секрет-сессии"
    # Одноразовость: файл с токеном не должен пережить собственное чтение.
    assert not path.exists()
    assert elevation.read_handoff(path) is None


def test_handoff_expires(tmp_path):
    path = elevation.handoff_path(tmp_path)
    elevation.write_handoff(path, "секрет", now=1000.0)

    assert elevation.read_handoff(path, ttl=60.0, now=1000.0 + 61.0) is None
    # Протухший файл тоже удаляется — токен не должен лежать на диске.
    assert not path.exists()


def test_handoff_broken_content_is_not_fatal(tmp_path):
    path = elevation.handoff_path(tmp_path)
    path.write_text("{это не json", encoding="utf-8")

    assert elevation.read_handoff(path) is None
    assert not path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="права posix-режима на Windows не применяются")
def test_handoff_is_owner_only(tmp_path):
    path = elevation.handoff_path(tmp_path)
    elevation.write_handoff(path, "секрет")

    assert (path.stat().st_mode & 0o777) == 0o600


# --- аргументы перезапуска ---


def test_build_relaunch_params_has_takeover_and_no_browser(tmp_path):
    params = elevation.build_relaunch_params(
        port=8770, handoff=tmp_path / "h.json", config_path=tmp_path / "hub.json"
    )

    assert params[:2] == ["-m", "standkit_hub"]
    # Без --takeover новый (elevated) экземпляр упёрся бы в single-instance
    # проверку и молча вышел — ровно тот дефект, ради которого всё затевалось.
    assert "--takeover" in params
    # Браузер, запущенный из elevated-процесса, сам был бы elevated.
    assert "--no-browser" in params
    assert params[params.index("--port") + 1] == "8770"
    assert params[params.index("--session-token-file") + 1] == str(tmp_path / "h.json")


def test_quote_params_quotes_paths_with_spaces():
    line = elevation.quote_params(["-m", "standkit_hub", "--config", r"C:\Program Files\hub.json"])

    assert '"C:\\Program Files\\hub.json"' in line
    assert line.startswith("-m standkit_hub")


# --- запрос повышения прав ---


@pytest.fixture()
def _windows(monkeypatch):
    """Делает вид, что мы на Windows: сам ShellExecuteW всё равно подменён."""
    monkeypatch.setattr(elevation, "elevation_supported", lambda: True)


def test_relaunch_elevated_success(monkeypatch, _windows):
    calls = []

    def _fake(executable, params, cwd):
        calls.append((executable, params, cwd))
        return 42  # > 32 — успех по контракту ShellExecuteW

    elevation.relaunch_elevated(["-m", "standkit_hub"], executable="pythonw.exe", shell_execute=_fake)

    assert calls and calls[0][0] == "pythonw.exe"
    assert calls[0][1] == "-m standkit_hub"


def test_relaunch_elevated_cancelled_is_its_own_error(monkeypatch, _windows):
    def _fake(executable, params, cwd):
        return elevation.ERROR_CANCELLED

    with pytest.raises(elevation.ElevationCancelled):
        elevation.relaunch_elevated(["-m", "standkit_hub"], executable="pythonw.exe", shell_execute=_fake)


def test_relaunch_elevated_failure_code(monkeypatch, _windows):
    with pytest.raises(elevation.ElevationError) as exc:
        elevation.relaunch_elevated(
            ["-m", "standkit_hub"], executable="pythonw.exe", shell_execute=lambda *a: 5
        )

    assert "код 5" in str(exc.value)


def test_relaunch_elevated_winapi_crash_is_wrapped(monkeypatch, _windows):
    def _boom(executable, params, cwd):
        raise OSError("ctypes упал")

    with pytest.raises(elevation.ElevationError):
        elevation.relaunch_elevated(["-m", "standkit_hub"], executable="pythonw.exe", shell_execute=_boom)


def test_can_restart_elevated_reasons(monkeypatch):
    monkeypatch.setattr(elevation, "elevation_supported", lambda: False)
    can, reason = elevation.can_restart_elevated()
    assert can is False and "Windows" in reason

    monkeypatch.setattr(elevation, "elevation_supported", lambda: True)
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    can, reason = elevation.can_restart_elevated()
    assert can is False and "уже работает" in reason

    monkeypatch.setattr(elevation, "is_elevated", lambda: False)
    can, reason = elevation.can_restart_elevated()
    assert can is True and reason == ""


# --- состояние экземпляра и перехват порта ---


def test_state_round_trip(tmp_path):
    path = instance.state_path(tmp_path)
    state = instance.current_state("127.0.0.1", 8770, elevated=False)
    instance.write_state(path, state)

    loaded = instance.read_state(path)
    assert loaded is not None
    assert loaded.pid == os.getpid()
    assert loaded.port == 8770
    assert loaded.elevated is False


def test_state_of_dead_process_is_ignored(tmp_path, monkeypatch):
    path = instance.state_path(tmp_path)
    instance.write_state(path, HubInstanceState(pid=424242, host="127.0.0.1", port=8770, elevated=False))
    monkeypatch.setattr(instance, "is_alive", lambda pid: False)

    # Файл мог остаться от процесса, убитого по питанию, — это не «занято».
    assert instance.read_state(path) is None
    assert instance.read_state(path, require_alive=False) is not None


def test_clear_state_does_not_touch_foreign_record(tmp_path):
    path = instance.state_path(tmp_path)
    instance.write_state(path, HubInstanceState(pid=777, host="127.0.0.1", port=8770))

    # Уходящий экземпляр не должен снести файл, уже переписанный тем, кто
    # перехватил у него порт.
    instance.clear_state(path, pid=666)
    assert path.exists()

    instance.clear_state(path, pid=777)
    assert not path.exists()


@pytest.mark.parametrize(
    "running_elevated, we_elevated, explicit, expected",
    [
        (False, True, False, True),    # повышение прав — единственный автоповод
        (True, False, False, False),   # понижать права молча нельзя
        (True, True, False, False),    # равные — прежнее поведение
        (False, False, False, False),
        (None, None, False, False),    # не Windows — перехватывать нечего
        (True, False, True, True),     # явный --takeover решает всё
    ],
)
def test_should_takeover(running_elevated, we_elevated, explicit, expected):
    running = HubInstanceState(pid=1, host="127.0.0.1", port=8770, elevated=running_elevated)

    assert instance.should_takeover(running, we_elevated=we_elevated, explicit=explicit) is expected


def test_should_takeover_without_state():
    assert instance.should_takeover(None, we_elevated=True, explicit=False) is False
    # --takeover без файла состояния: гасить некого, но отступать не надо —
    # уходящий экземпляр освободит порт сам.
    assert instance.should_takeover(None, we_elevated=True, explicit=True) is True


# --- API хаба ---


def _free_hub(tmp_path):
    registry_path = tmp_path / "projects.json"
    registry_path.write_text('{"projects": {}}', encoding="utf-8")
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path), run_dir=str(tmp_path / "run")).save(config_path)
    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token, poll=False)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    return httpd, f"http://127.0.0.1:{port}", token


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"хаб не поднялся на порту {port} за {timeout}s")


def _shutdown(httpd) -> None:
    threading.Thread(target=httpd.shutdown, daemon=True).start()
    httpd.server_close()


def _request(url, *, token=None, method="GET", origin=None):
    req = urllib.request.Request(url, method=method)
    if token is not None:
        req.add_header("X-Standkit-Token", token)
        req.add_header("Cookie", f"standkit_session={token}")
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw.strip().startswith("{") else {})


def test_api_elevation_requires_auth(tmp_path):
    httpd, base, token = _free_hub(tmp_path)
    try:
        status, _ = _request(f"{base}/api/hub/elevation")
        assert status == 401

        status, data = _request(f"{base}/api/hub/elevation", token=token)
        assert status == 200
        assert data["supported"] is (sys.platform == "win32")
        assert set(data) == {"supported", "elevated", "can_restart", "reason"}
    finally:
        _shutdown(httpd)


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows отказ зависит от прав процесса")
def test_api_restart_elevated_rejected_off_windows(tmp_path):
    httpd, base, token = _free_hub(tmp_path)
    try:
        status, data = _request(
            f"{base}/api/hub/restart-elevated", token=token, method="POST", origin=base
        )
        # Не Windows — честный отказ с человеческим текстом, а не 500.
        assert status == 400
        assert "Windows" in data["error"]
    finally:
        _shutdown(httpd)


def test_api_restart_elevated_needs_csrf_header(tmp_path):
    httpd, base, token = _free_hub(tmp_path)
    try:
        # Без X-Standkit-Token (только cookie) мутация запрещена — перезапуск
        # процесса диспетчера тем более.
        req = urllib.request.Request(f"{base}/api/hub/restart-elevated", method="POST")
        req.add_header("Cookie", f"standkit_session={token}")
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3.0)
        assert exc.value.code == 403
    finally:
        _shutdown(httpd)
