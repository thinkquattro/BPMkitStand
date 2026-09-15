"""
Тесты одноразовой операции с правами администратора над стендом IIS
(GAP-311 п.6, ``standkit_hub.elevated_op`` + ``POST /api/hub/elevated-op`` /
``GET /api/hub/elevated-op/<op_id>``), наблюдателя за перезапуском всего
диспетчера (GAP-311 п.5) и классификации ошибки прав у обычных
start/stop/restart стенда (GAP-311 п.1).

Реального ``ShellExecuteW``/UAC здесь так же не бывает, как и в
tests/test_hub_elevation.py — только подмена.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from standkit.hosting import IisElevationError
from standkit.models import HostKind, Stand
from standkit.registry import Registry
from standkit_hub import elevated_op, elevation
from standkit_hub.client import FederatedClient
from standkit_hub.config import HubConfig
from standkit_hub.security import generate_session_token
from standkit_hub.server import _resolve_stand_action, create_hub_server, _watch_restart_result

# --- elevation_required у обычных действий над стендом (GAP-311 п.1) ---


def _registry_with_iis_stand(tmp_path) -> Path:
    registry_path = tmp_path / "projects.json"
    reg = Registry(path=registry_path)
    reg.add_existing(Stand(name="iis1", stand_dir=str(tmp_path / "iis1"), host_kind=HostKind.IIS, iis_app_pool="pool1"))
    reg.save()
    return registry_path


def _config_with_registry(tmp_path, registry_path: Path) -> Path:
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path), run_dir=str(tmp_path / "run")).save(config_path)
    return config_path


def test_resolve_stand_action_sets_elevation_required_on_iis_elevation_error(tmp_path, monkeypatch):
    registry_path = _registry_with_iis_stand(tmp_path)
    config_path = _config_with_registry(tmp_path, registry_path)

    def _boom(self, name):
        raise IisElevationError("appcmd отказал: нет прав администратора")

    monkeypatch.setattr(FederatedClient, "start", _boom)

    code, payload = _resolve_stand_action(config_path, "iis1", "start")

    assert code == 400
    assert payload["elevation_required"] is True
    assert "прав" in payload["error"]


def test_resolve_stand_action_plain_hosting_error_has_no_elevation_flag(tmp_path, monkeypatch):
    from standkit.hosting import HostingError

    registry_path = _registry_with_iis_stand(tmp_path)
    config_path = _config_with_registry(tmp_path, registry_path)

    def _boom(self, name):
        raise HostingError("appcmd.exe не найден")

    monkeypatch.setattr(FederatedClient, "start", _boom)

    code, payload = _resolve_stand_action(config_path, "iis1", "start")

    assert code == 400
    assert "elevation_required" not in payload


# --- standkit_hub.elevated_op.run() ---


def test_elevated_op_run_refuses_on_sid_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(elevated_op, "current_user_sid", lambda: "S-1-5-21-BBB")
    monkeypatch.setattr(elevated_op, "current_user_name", lambda: "CORP\\ivanov")
    result_file = tmp_path / "result.json"

    rc = elevated_op.run(
        stand="iis1", action="start", result_file=result_file, initiator_sid="S-1-5-21-AAA"
    )

    assert rc == 3
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "refused"
    assert "CORP\\ivanov" in data["message"]


def test_elevated_op_run_accepts_matching_sid_and_proceeds(tmp_path, monkeypatch):
    monkeypatch.setattr(elevated_op, "current_user_sid", lambda: "S-1-5-21-AAA")
    registry_path = _registry_with_iis_stand(tmp_path)
    config_path = _config_with_registry(tmp_path, registry_path)
    monkeypatch.setattr(FederatedClient, "start", lambda self, name: 4242)
    result_file = tmp_path / "result.json"

    rc = elevated_op.run(
        stand="iis1",
        action="start",
        result_file=result_file,
        config_path=config_path,
        initiator_sid="S-1-5-21-AAA",
    )

    assert rc == 0
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "ok"
    assert data["pid"] == 4242


def test_elevated_op_run_unknown_stand_is_error(tmp_path):
    registry_path = _registry_with_iis_stand(tmp_path)
    config_path = _config_with_registry(tmp_path, registry_path)
    result_file = tmp_path / "result.json"

    rc = elevated_op.run(stand="nope", action="start", result_file=result_file, config_path=config_path)

    assert rc == 1
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "error"
    assert "не найден" in data["message"]


def test_elevated_op_run_rejects_non_iis_stand(tmp_path):
    registry_path = tmp_path / "projects.json"
    reg = Registry(path=registry_path)
    reg.add_existing(Stand(name="kestrel1", stand_dir=str(tmp_path / "kestrel1"), host_kind=HostKind.KESTREL))
    reg.save()
    config_path = _config_with_registry(tmp_path, registry_path)
    result_file = tmp_path / "result.json"

    rc = elevated_op.run(stand="kestrel1", action="start", result_file=result_file, config_path=config_path)

    assert rc == 1
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "error"
    assert "IIS" in data["message"]


def test_elevated_op_run_backend_failure_is_written_as_error_not_raised(tmp_path, monkeypatch):
    registry_path = _registry_with_iis_stand(tmp_path)
    config_path = _config_with_registry(tmp_path, registry_path)

    def _boom(self, name):
        raise IisElevationError("appcmd отказал")

    monkeypatch.setattr(FederatedClient, "start", _boom)
    result_file = tmp_path / "result.json"

    rc = elevated_op.run(stand="iis1", action="start", result_file=result_file, config_path=config_path)

    assert rc == 1
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "error"


# --- наблюдатель за перезапуском (GAP-311 п.5) ---


class _FakeServer:
    def __init__(self):
        self.restart_lock = threading.Lock()
        self.restart_state = {"status": "pending", "at": time.time()}
        self.shutdown_called = False

    def shutdown(self):
        self.shutdown_called = True


def test_watch_restart_result_accepted_schedules_shutdown(tmp_path, monkeypatch):
    server = _FakeServer()
    result_file = tmp_path / "result.json"
    handoff = tmp_path / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")
    elevation.write_result_atomic(result_file, {"status": "accepted", "at": time.time()})

    shutdown_calls = []
    monkeypatch.setattr(
        "standkit_hub.server._schedule_shutdown", lambda srv: shutdown_calls.append(srv)
    )

    _watch_restart_result(
        server, result_file, handoff, ttl=1.0, poll_interval=0.01, clock=time.monotonic, sleep=lambda s: None
    )

    assert server.restart_state is None
    assert shutdown_calls == [server]
    assert not handoff.exists()
    assert not result_file.exists()


def test_watch_restart_result_refused_sets_state(tmp_path, monkeypatch):
    server = _FakeServer()
    result_file = tmp_path / "result.json"
    handoff = tmp_path / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")
    elevation.write_result_atomic(
        result_file, {"status": "refused", "message": "не та учётка", "at": 123.0}
    )
    monkeypatch.setattr("standkit_hub.server._schedule_shutdown", lambda srv: pytest.fail("не должен звонить"))

    _watch_restart_result(
        server, result_file, handoff, ttl=1.0, poll_interval=0.01, clock=time.monotonic, sleep=lambda s: None
    )

    assert server.restart_state["status"] == "refused"
    assert server.restart_state["message"] == "не та учётка"
    assert not handoff.exists()


def test_watch_restart_result_timeout_sets_failed(tmp_path, monkeypatch):
    server = _FakeServer()
    result_file = tmp_path / "nope.json"  # никогда не появится
    handoff = tmp_path / "handoff.json"
    handoff.write_text("{}", encoding="utf-8")

    # Фейковые часы: первый вызов clock() = 0 (дедлайн), дальше >= 1 (истекло).
    ticks = iter([0.0, 0.0, 2.0, 2.0])
    monkeypatch.setattr("standkit_hub.server._schedule_shutdown", lambda srv: pytest.fail("не должен звонить"))

    _watch_restart_result(
        server,
        result_file,
        handoff,
        ttl=1.0,
        poll_interval=0.0,
        clock=lambda: next(ticks, 3.0),
        sleep=lambda s: None,
    )

    assert server.restart_state["status"] == "failed"
    assert "не ответил" in server.restart_state["message"]


# --- HTTP: POST/GET /api/hub/elevated-op и повторный запрос перезапуска ---


def _hub_with_iis_stand(tmp_path):
    registry_path = _registry_with_iis_stand(tmp_path)
    config_path = _config_with_registry(tmp_path, registry_path)
    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token, poll=False)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    return httpd, f"http://127.0.0.1:{port}", token, config_path


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    import socket

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


def _request(url, *, token=None, method="GET", origin=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    if token is not None:
        req.add_header("X-Standkit-Token", token)
        req.add_header("Cookie", f"standkit_session={token}")
    if origin is not None:
        req.add_header("Origin", origin)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw.strip().startswith("{") else {})


def test_elevated_op_endpoint_rejects_non_iis_stand(tmp_path):
    registry_path = tmp_path / "projects.json"
    reg = Registry(path=registry_path)
    reg.add_existing(Stand(name="kestrel1", stand_dir=str(tmp_path / "k1"), host_kind=HostKind.KESTREL))
    reg.save()
    config_path = _config_with_registry(tmp_path, registry_path)
    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token, poll=False)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    base = f"http://127.0.0.1:{port}"
    try:
        status, data = _request(
            f"{base}/api/hub/elevated-op", token=token, method="POST", origin=base,
            body={"stand": "kestrel1", "action": "start"},
        )
        assert status == 400
        assert "IIS" in data["error"]
    finally:
        _shutdown(httpd)


def test_elevated_op_endpoint_unknown_stand_is_404(tmp_path):
    httpd, base, token, _ = _hub_with_iis_stand(tmp_path)
    try:
        status, data = _request(
            f"{base}/api/hub/elevated-op", token=token, method="POST", origin=base,
            body={"stand": "nope", "action": "start"},
        )
        assert status == 404
    finally:
        _shutdown(httpd)


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="на Windows elevation_supported честно True")
def test_elevated_op_endpoint_rejects_off_windows(tmp_path):
    httpd, base, token, _ = _hub_with_iis_stand(tmp_path)
    try:
        status, data = _request(
            f"{base}/api/hub/elevated-op", token=token, method="POST", origin=base,
            body={"stand": "iis1", "action": "start"},
        )
        assert status == 400
        assert "Windows" in data["error"]
    finally:
        _shutdown(httpd)


def test_elevated_op_status_unknown_op_id_is_404(tmp_path):
    httpd, base, token, _ = _hub_with_iis_stand(tmp_path)
    try:
        status, data = _request(f"{base}/api/hub/elevated-op/{'a' * 32}", token=token)
        assert status == 404
    finally:
        _shutdown(httpd)


def test_elevated_op_status_invalid_op_id_is_400(tmp_path):
    httpd, base, token, _ = _hub_with_iis_stand(tmp_path)
    try:
        status, data = _request(f"{base}/api/hub/elevated-op/not-hex", token=token)
        assert status == 400
    finally:
        _shutdown(httpd)


def test_elevated_op_status_pending_then_ok(tmp_path):
    httpd, base, token, config_path = _hub_with_iis_stand(tmp_path)
    try:
        op_id = "b" * 32
        with httpd.elevated_op_lock:
            httpd.elevated_ops[op_id] = time.monotonic()

        status, data = _request(f"{base}/api/hub/elevated-op/{op_id}", token=token)
        assert status == 200 and data["status"] == "pending"

        config = HubConfig.load(config_path)
        result_file = config.resolve_run_dir() / f"standkit-hub-elevated-op-{op_id}.json"
        elevation.write_result_atomic(result_file, {"status": "ok", "message": "", "at": time.time()})

        status, data = _request(f"{base}/api/hub/elevated-op/{op_id}", token=token)
        assert status == 200 and data["status"] == "ok"
        assert not result_file.exists()

        # op_id забыт — повторный опрос уже отданного результата даёт 404.
        status, _ = _request(f"{base}/api/hub/elevated-op/{op_id}", token=token)
        assert status == 404
    finally:
        _shutdown(httpd)


def test_elevated_op_status_expired_after_ttl(tmp_path, monkeypatch):
    httpd, base, token, _ = _hub_with_iis_stand(tmp_path)
    try:
        monkeypatch.setattr(elevation, "HANDOFF_TTL_SEC", 0.0)
        op_id = "c" * 32
        with httpd.elevated_op_lock:
            httpd.elevated_ops[op_id] = time.monotonic() - 10.0

        status, data = _request(f"{base}/api/hub/elevated-op/{op_id}", token=token)
        assert status == 200 and data["status"] == "expired"

        status, _ = _request(f"{base}/api/hub/elevated-op/{op_id}", token=token)
        assert status == 404
    finally:
        _shutdown(httpd)


# --- повторный POST /api/hub/restart-elevated при pending ---


def test_restart_elevated_returns_409_while_pending(tmp_path):
    registry_path = tmp_path / "projects.json"
    registry_path.write_text('{"projects": {}}', encoding="utf-8")
    config_path = _config_with_registry(tmp_path, registry_path)
    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token, poll=False)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    base = f"http://127.0.0.1:{port}"
    try:
        with httpd.restart_lock:
            httpd.restart_state = {"status": "pending", "at": time.time()}

        status, data = _request(f"{base}/api/hub/restart-elevated", token=token, method="POST", origin=base)
        assert status == 409
        assert "дождитесь" in data["error"]
    finally:
        _shutdown(httpd)


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="ElevationCancelled требует подмены на Windows")
def test_restart_elevated_cancelled_returns_409(tmp_path, monkeypatch):
    registry_path = tmp_path / "projects.json"
    registry_path.write_text('{"projects": {}}', encoding="utf-8")
    config_path = _config_with_registry(tmp_path, registry_path)
    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token, poll=False)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    base = f"http://127.0.0.1:{port}"
    try:
        monkeypatch.setattr(elevation, "elevation_supported", lambda: True)
        monkeypatch.setattr(elevation, "is_elevated", lambda: False)

        def _cancel(params, **kwargs):
            raise elevation.ElevationCancelled("отклонено пользователем")

        monkeypatch.setattr(elevation, "relaunch_elevated", _cancel)

        status, data = _request(f"{base}/api/hub/restart-elevated", token=token, method="POST", origin=base)
        assert status == 409
        assert data.get("cancelled") is True
    finally:
        _shutdown(httpd)
