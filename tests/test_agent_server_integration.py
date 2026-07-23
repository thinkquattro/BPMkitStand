"""
Лёгкие end-to-end тесты HTTP-обработчика агента на loopback (127.0.0.1),
plain HTTP (loopback без TLS — разрешённый secure-default, см.
test_agent_security.py::test_loopback_without_tls_is_allowed).

Сервер поднимается в daemon-потоке на свободном порту на время теста;
явный shutdown не делается (daemon-поток завершается вместе с процессом
pytest) — общепринятый лёгкий паттерн для такого рода тестов, не требующий
изменения публичного API run_server ради тестируемости.
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
from standkit_agent.security import Authenticator, LockoutTracker
from standkit_agent.server import run_server


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_agent(tmp_path, *, control_token="control-tok", readonly_token=None, lockout=None):
    port = _free_port()
    registry = Registry(
        path=tmp_path / "projects.json",
        default="demo",
        stands={"demo": Stand(name="demo", stand_dir=str(tmp_path / "demo"))},
    )
    authenticator = Authenticator(control_token, readonly_token)
    lockout = lockout or LockoutTracker(max_failures=3, window_seconds=300.0)
    audit_path = tmp_path / "audit.log"

    thread = threading.Thread(
        target=run_server,
        kwargs=dict(
            registry=registry,
            authenticator=authenticator,
            host="127.0.0.1",
            port=port,
            run_dir=tmp_path / "run",
            log_dir=tmp_path / "logs",
            lockout=lockout,
            audit_log_path=audit_path,
        ),
        daemon=True,
    )
    thread.start()
    _wait_for_port(port)
    return f"http://127.0.0.1:{port}", audit_path


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"агент не поднялся на порту {port} за {timeout}s")


def _request(base_url: str, path: str, *, token: str | None = None, method: str = "GET"):
    req = urllib.request.Request(base_url + path, method=method)
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_no_token_is_unauthorized(tmp_path):
    base_url, _ = _start_agent(tmp_path)
    status, _ = _request(base_url, "/stands")
    assert status == 401


def test_wrong_token_is_unauthorized(tmp_path):
    base_url, _ = _start_agent(tmp_path)
    status, _ = _request(base_url, "/stands", token="wrong")
    assert status == 401


def test_correct_control_token_lists_stands(tmp_path):
    base_url, _ = _start_agent(tmp_path)
    status, body = _request(base_url, "/stands", token="control-tok")
    assert status == 200
    assert body["stands"] == ["demo"]


def test_readonly_token_can_read_status_but_not_control(tmp_path):
    base_url, _ = _start_agent(tmp_path, readonly_token="ro-tok")

    status, body = _request(base_url, "/stand/demo/status", token="ro-tok")
    assert status == 200
    assert body["name"] == "demo"

    status, body = _request(base_url, "/stand/demo/start", token="ro-tok", method="POST")
    assert status == 403


def test_readonly_token_can_list_stands(tmp_path):
    base_url, _ = _start_agent(tmp_path, readonly_token="ro-tok")
    status, _ = _request(base_url, "/stands", token="ro-tok")
    assert status == 200


def test_lockout_returns_429_after_repeated_failures(tmp_path):
    lockout = LockoutTracker(max_failures=3, window_seconds=300.0)
    base_url, _ = _start_agent(tmp_path, lockout=lockout)

    codes = []
    for _ in range(5):
        status, _ = _request(base_url, "/stands", token="definitely-wrong")
        codes.append(status)

    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429
    assert codes[4] == 429


def test_unknown_stand_name_status_returns_404(tmp_path):
    base_url, _ = _start_agent(tmp_path)
    status, _ = _request(base_url, "/stand/does-not-exist/status", token="control-tok")
    assert status == 404


def test_invalid_stand_name_returns_400(tmp_path):
    base_url, _ = _start_agent(tmp_path)
    status, _ = _request(base_url, "/stand/..%2f..%2fetc/status", token="control-tok")
    assert status in (400, 404)


def test_logs_n_over_cap_does_not_error(tmp_path):
    base_url, _ = _start_agent(tmp_path)
    status, body = _request(base_url, "/stand/demo/logs?n=999999999", token="control-tok")
    assert status == 200
    assert isinstance(body["lines"], list)


def test_logs_invalid_n_returns_400(tmp_path):
    base_url, _ = _start_agent(tmp_path)
    status, _ = _request(base_url, "/stand/demo/logs?n=not-a-number", token="control-tok")
    assert status == 400


def test_unsupported_method_returns_405(tmp_path):
    base_url, _ = _start_agent(tmp_path)
    status, _ = _request(base_url, "/stand/demo/status", token="control-tok", method="DELETE")
    assert status == 405


def test_audit_log_records_denied_and_ok_without_leaking_token(tmp_path):
    base_url, audit_path = _start_agent(tmp_path)

    _request(base_url, "/stands", token="wrong-token-value")
    _request(base_url, "/stands", token="control-tok")

    # Даём файловому хендлеру логгера время сбросить буфер на диск.
    deadline = time.monotonic() + 3.0
    content = ""
    while time.monotonic() < deadline:
        if audit_path.exists():
            content = audit_path.read_text(encoding="utf-8")
            if content.strip():
                break
        time.sleep(0.05)

    assert "wrong-token-value" not in content
    assert "control-tok" not in content  # сам токен не пишется, только identity-скоуп

    lines = [json.loads(l) for l in content.splitlines() if l.strip()]
    results = {entry["result"] for entry in lines}
    assert "denied" in results
    assert "ok" in results
