"""
Тесты standkit_hub.server: security-модель (сессионный токен/cookie,
double-submit + Origin-проверка на мутациях, fail-closed bind) и API
(/api/stands агрегация, start/stop/restart через lifecycle, секреты не
логируются, санитайзинг статики).

Сервер поднимается в daemon-потоке на свободном порту на время теста (тот же
лёгкий паттерн, что и tests/test_agent_server_integration.py) — без реального
браузера/дисплея.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import standkit_hub.client as hub_client_module
import standkit_hub.server as server_module
from standkit.registry import Registry
from standkit_hub.config import HubConfig
from standkit_hub.security import InsecureBindError, generate_session_token
from standkit_hub.server import create_hub_server


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_registry(tmp_path, *, stand_name="demo"):
    from standkit.models import Stand

    registry_path = tmp_path / "projects.json"
    registry = Registry(
        path=registry_path,
        default=stand_name,
        stands={stand_name: Stand(name=stand_name, stand_dir=str(tmp_path / stand_name))},
    )
    registry.save()
    return registry_path


def _write_config(tmp_path, *, registry_path):
    config_path = tmp_path / "standkit-hub.json"
    cfg = HubConfig(registry_path=str(registry_path))
    cfg.save(config_path)
    return config_path


def _start_hub(tmp_path, *, stand_name="demo"):
    registry_path = _write_registry(tmp_path, stand_name=stand_name)
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()

    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)

    return f"http://127.0.0.1:{port}", session_token, config_path, registry_path, httpd


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"хаб не поднялся на порту {port} за {timeout}s")


def _request(
    base_url: str,
    path: str,
    *,
    token: str | None = None,
    cookie_token: str | None = None,
    method: str = "GET",
    body: dict | None = None,
    origin: str | None = None,
):
    req = urllib.request.Request(base_url + path, method=method)
    if token is not None:
        req.add_header("X-Standkit-Token", token)
    if cookie_token is not None:
        req.add_header("Cookie", f"standkit_session={cookie_token}")
    if origin is not None:
        req.add_header("Origin", origin)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    def _maybe_json(payload: str, headers) -> dict:
        content_type = headers.get("Content-Type", "")
        if not payload or "application/json" not in content_type:
            return {}
        return json.loads(payload)

    try:
        with urllib.request.urlopen(req, data=data, timeout=3.0) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, _maybe_json(payload, resp.headers), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        return exc.code, _maybe_json(payload, exc.headers), dict(exc.headers)


# --- аутентификация: GET /api/* ---


def test_get_api_without_token_is_unauthorized(tmp_path):
    base_url, _, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/stands")
    assert status == 401


def test_get_api_with_wrong_token_is_unauthorized(tmp_path):
    base_url, _, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/stands", token="wrong")
    assert status == 401


def test_get_api_with_header_token_is_ok(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stands", token=token)
    assert status == 200
    assert "stands" in body


def test_get_api_with_cookie_token_is_ok(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stands", cookie_token=token)
    assert status == 200
    assert "stands" in body


# --- root: установка cookie по токену в query ---


def test_root_with_valid_query_token_sets_cookie_and_injects_token(tmp_path):
    # По ссылке /?t=<token> хаб отдаёт index НАПРЯМУЮ (без редиректа — иначе токен
    # теряется до загрузки JS), ставит session-cookie и инжектит токен в <meta>,
    # чтобы фронтенд мог класть X-Standkit-Token в мутации.
    base_url, token, *_ = _start_hub(tmp_path)
    try:
        resp = urllib.request.urlopen(f"{base_url}/?t={token}", timeout=3.0)
        status = resp.status
        headers = dict(resp.headers)
        body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = dict(exc.headers)
        body = exc.read().decode("utf-8")

    assert status == 200
    assert "standkit_session=" + token in headers.get("Set-Cookie", "")
    assert "HttpOnly" in headers.get("Set-Cookie", "")
    assert "SameSite=Strict" in headers.get("Set-Cookie", "")
    assert headers.get("Location") is None
    # токен инжектнут в страницу, плейсхолдер заменён
    assert token in body
    assert "__STANDKIT_TOKEN__" not in body


def test_root_without_token_does_not_leak_token(tmp_path):
    # Неаутентифицированный GET "/" отдаёт index, но БЕЗ токена (плейсхолдер пуст).
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/")
    assert status == 200
    assert token not in (body or "")
    assert "__STANDKIT_TOKEN__" not in (body or "")


def test_root_without_token_serves_index_without_auth(tmp_path):
    base_url, *_ = _start_hub(tmp_path)
    status, _, headers = _request(base_url, "/")
    assert status == 200


# --- мутации: double-submit + Origin ---


def test_post_stand_action_without_header_token_is_forbidden(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    # Только cookie — без X-Standkit-Token заголовка мутация должна отклоняться.
    status, _, _ = _request(
        base_url, "/api/stand/demo/start", cookie_token=token, method="POST", origin=base_url
    )
    assert status == 403


def test_post_stand_action_with_wrong_origin_is_forbidden(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(
        base_url,
        "/api/stand/demo/start",
        token=token,
        method="POST",
        origin="http://evil.example.com",
    )
    assert status == 403


def test_post_stand_action_with_correct_header_and_origin_succeeds(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)

    started = {}

    def _fake_start(stand):
        started["name"] = stand.name

    monkeypatch.setattr(hub_client_module.lifecycle, "start", _fake_start)

    status, body, _ = _request(
        base_url, "/api/stand/demo/start", token=token, method="POST", origin=base_url
    )

    assert status == 200
    assert body.get("ok") is True
    assert started["name"] == "demo"


def test_post_stand_action_dispatches_stop_and_restart(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)

    calls = []
    monkeypatch.setattr(hub_client_module.lifecycle, "stop", lambda stand: calls.append(("stop", stand.name)))
    monkeypatch.setattr(hub_client_module.lifecycle, "restart", lambda stand: calls.append(("restart", stand.name)))

    status1, _, _ = _request(base_url, "/api/stand/demo/stop", token=token, method="POST", origin=base_url)
    status2, _, _ = _request(base_url, "/api/stand/demo/restart", token=token, method="POST", origin=base_url)

    assert status1 == 200
    assert status2 == 200
    assert ("stop", "demo") in calls
    assert ("restart", "demo") in calls


def test_unknown_stand_action_returns_404(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(
        base_url, "/api/stand/does-not-exist/start", token=token, method="POST", origin=base_url
    )
    assert status == 404


def test_invalid_stand_name_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(
        base_url, "/api/stand/..%2f..%2fetc/status", token=token
    )
    assert status in (400, 404)


# --- API: /api/stands агрегация ---


def test_api_stands_aggregates_registry_entries(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stands", token=token)
    assert status == 200
    names = [s["name"] for s in body["stands"]]
    assert names == ["demo"]
    assert body["stands"][0]["transport"] == "local"
    assert "status" in body["stands"][0]


# --- API: секреты не логируются/не возвращаются ---


def test_secret_post_never_returns_value(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)

    captured = {}

    def _fake_set_secret(ref, value):
        captured["ref"] = ref
        captured["value"] = value

    monkeypatch.setattr(server_module, "set_secret", _fake_set_secret)

    status, body, _ = _request(
        base_url,
        "/api/secret/standkit:demo:agent-token",
        token=token,
        method="POST",
        origin=base_url,
        body={"value": "super-secret-value"},
    )

    assert status == 200
    assert captured["value"] == "super-secret-value"
    assert "super-secret-value" not in json.dumps(body)
    assert body == {"ok": True, "ref": "standkit:demo:agent-token"}


def test_secret_get_returns_only_has_secret_flag(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)
    monkeypatch.setattr(server_module, "has_secret", lambda ref: True)

    status, body, _ = _request(base_url, "/api/secret/standkit:demo:agent-token", token=token)

    assert status == 200
    assert body == {"ref": "standkit:demo:agent-token", "has_secret": True}


# --- API: настройки ---


def test_settings_get_and_post_roundtrip(tmp_path):
    base_url, token, config_path, *_ = _start_hub(tmp_path)

    status, body, _ = _request(base_url, "/api/settings", token=token)
    assert status == 200
    assert body["refresh_interval_sec"] == 10

    status, body, _ = _request(
        base_url,
        "/api/settings",
        token=token,
        method="POST",
        origin=base_url,
        body={"refresh_interval_sec": 42},
    )
    assert status == 200
    assert body["refresh_interval_sec"] == 42

    reloaded = HubConfig.load(config_path)
    assert reloaded.refresh_interval_sec == 42


# --- статика: санитайзинг путей ---


def test_static_traversal_is_rejected(tmp_path):
    import http.client

    base_url, *_ = _start_hub(tmp_path)
    host_port = base_url.replace("http://", "")
    host, port = host_port.split(":")

    # Реальный path-traversal (не percent-encoded) — отправляем сырым
    # http.client, чтобы клиентская библиотека не "нормализовала" URL за нас.
    conn = http.client.HTTPConnection(host, int(port), timeout=3.0)
    try:
        conn.request("GET", "/static/../../../../etc/passwd")
        resp = conn.getresponse()
        status = resp.status
        resp.read()
    finally:
        conn.close()

    assert status == 404


def test_static_known_file_is_served(tmp_path):
    base_url, *_ = _start_hub(tmp_path)
    status, _, headers = _request(base_url, "/static/style.css")
    assert status == 200
    assert "text/css" in headers.get("Content-Type", "")


def test_static_missing_file_is_404(tmp_path):
    base_url, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/static/does-not-exist.js")
    assert status == 404


# --- fail-closed bind ---


def test_create_hub_server_rejects_non_loopback_without_insecure(tmp_path):
    config_path = _write_config(tmp_path, registry_path=tmp_path / "projects.json")
    with pytest.raises(InsecureBindError):
        create_hub_server(
            "0.0.0.0",
            0,
            config_path=config_path,
            session_token=generate_session_token(),
        )


def test_create_hub_server_allows_non_loopback_with_insecure(tmp_path):
    config_path = _write_config(tmp_path, registry_path=tmp_path / "projects.json")
    httpd = create_hub_server(
        "0.0.0.0",
        0,
        config_path=config_path,
        session_token=generate_session_token(),
        insecure=True,
    )
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()


def test_create_hub_server_allows_loopback_without_insecure(tmp_path):
    config_path = _write_config(tmp_path, registry_path=tmp_path / "projects.json")
    httpd = create_hub_server(
        "127.0.0.1",
        0,
        config_path=config_path,
        session_token=generate_session_token(),
    )
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()


def test_unsupported_method_returns_405(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/stands", token=token, method="PUT")
    assert status == 405
