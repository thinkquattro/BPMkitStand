"""
Тесты ``POST /api/stand/register`` (standkit_hub.server::_api_stand_register)
— кнопка "Зарегистрировать стенд" веб-дашборда. Пишет запись УЖЕ существующего
стенда в общий реестр (projects.json), НЕ провижининг.

Переиспользует лёгкие хелперы поднятия хаба в daemon-потоке из
tests/test_hub_server.py (тот же паттерн, без реальной сети/БД).
"""

from __future__ import annotations

import json

from standkit.registry import Registry

from tests.test_hub_server import _request, _start_hub, _write_config


# --- успех: запись реально появляется в файле реестра ---


def test_register_minimal_local_stand_succeeds_and_persists(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    payload = {
        "name": "new-stand",
        "transport": "local",
        "host_kind": "kestrel",
        "stand_dir": str(tmp_path / "new-stand"),
        "stand_host": "127.0.0.1",
        "stand_port": "5050",
        "db_type": "postgres",
        "db_host": "127.0.0.1",
        "db_port": "5432",
        "db_name": "new_stand_db",
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )

    assert status == 200
    assert body == {"ok": True, "name": "new-stand"}

    reloaded = Registry.load(registry_path)
    assert "new-stand" in reloaded
    stand = reloaded.get("new-stand")
    assert stand.stand_dir == str(tmp_path / "new-stand")
    assert stand.stand_port == 5050
    assert stand.db_name == "new_stand_db"
    # исходная запись "existing" не пострадала
    assert "existing" in reloaded


def test_register_docker_stand_with_all_conditional_fields(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    payload = {
        "name": "docker-stand",
        "transport": "local",
        "host_kind": "docker",
        "stand_dir": str(tmp_path / "docker-stand"),
        "docker_container": "bpmsoft-docker-stand",
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )
    assert status == 200

    reloaded = Registry.load(registry_path)
    stand = reloaded.get("docker-stand")
    assert stand.host_kind.value == "docker"
    assert stand.docker_container == "bpmsoft-docker-stand"


def test_register_agent_stand_with_agent_fields(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    payload = {
        "name": "remote-stand",
        "transport": "agent",
        "host_kind": "kestrel",
        "stand_dir": str(tmp_path / "remote-stand"),
        "agent_url": "https://remote-host:8765",
        "agent_secret_ref": "standkit:remote-stand:agent-token",
    }
    status, body, _ = _request(
        base_url, "/api/stand/register", token=token, method="POST", origin=base_url, body=payload
    )
    assert status == 200

    reloaded = Registry.load(registry_path)
    stand = reloaded.get("remote-stand")
    assert stand.transport.value == "agent"
    assert stand.agent_url == "https://remote-host:8765"
    assert stand.agent_secret_ref == "standkit:remote-stand:agent-token"


# --- 400: валидация ---


def test_register_missing_name_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={"stand_dir": str(tmp_path / "x")},
    )
    assert status == 400
    assert "name" in body.get("fields", [])


def test_register_agent_transport_without_agent_url_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-agent-stand",
            "transport": "agent",
            "stand_dir": str(tmp_path / "bad-agent-stand"),
        },
    )
    assert status == 400
    assert "error" in body


def test_register_docker_host_kind_without_container_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-docker-stand",
            "host_kind": "docker",
            "stand_dir": str(tmp_path / "bad-docker-stand"),
        },
    )
    assert status == 400
    assert "error" in body


def test_register_invalid_transport_value_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-transport-stand",
            "transport": "teleport",
            "stand_dir": str(tmp_path / "bad-transport-stand"),
        },
    )
    assert status == 400
    assert "transport" in body.get("fields", [])


def test_register_non_numeric_port_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "bad-port-stand",
            "stand_dir": str(tmp_path / "bad-port-stand"),
            "stand_port": "not-a-number",
        },
    )
    assert status == 400
    assert "stand_port" in body.get("fields", [])


def test_register_password_field_is_rejected_with_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "pwd-stand",
            "stand_dir": str(tmp_path / "pwd-stand"),
            "db_password": "hunter2",
        },
    )
    assert status == 400
    assert "db_password" in body.get("fields", [])


def test_register_password_field_never_reaches_registry(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")
    _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={
            "name": "pwd-stand-2",
            "stand_dir": str(tmp_path / "pwd-stand-2"),
            "db_password": "hunter2",
        },
    )
    raw = registry_path.read_text(encoding="utf-8")
    assert "hunter2" not in raw
    assert "pwd-stand-2" not in raw


# --- 409: дубликат имени ---


def test_register_duplicate_name_returns_409_and_does_not_overwrite(tmp_path):
    base_url, token, config_path, registry_path, _ = _start_hub(tmp_path, stand_name="existing")

    status, body, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin=base_url,
        body={"name": "existing", "stand_dir": str(tmp_path / "some-other-dir")},
    )
    assert status == 409
    assert "error" in body

    reloaded = Registry.load(registry_path)
    stand = reloaded.get("existing")
    # исходный stand_dir не перезаписан
    assert stand.stand_dir == str(tmp_path / "existing")


# --- 401/403: CSRF / Origin ---


def test_register_without_token_is_unauthorized_or_forbidden(tmp_path):
    base_url, *_ = _start_hub(tmp_path, stand_name="existing")
    status, _, _ = _request(
        base_url,
        "/api/stand/register",
        method="POST",
        origin=base_url,
        body={"name": "no-token-stand", "stand_dir": str(tmp_path / "no-token-stand")},
    )
    assert status in (401, 403)


def test_register_with_cookie_only_no_header_is_forbidden(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, _, _ = _request(
        base_url,
        "/api/stand/register",
        cookie_token=token,
        method="POST",
        origin=base_url,
        body={"name": "cookie-only-stand", "stand_dir": str(tmp_path / "cookie-only-stand")},
    )
    assert status == 403


def test_register_with_wrong_origin_is_forbidden(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path, stand_name="existing")
    status, _, _ = _request(
        base_url,
        "/api/stand/register",
        token=token,
        method="POST",
        origin="http://evil.example.com",
        body={"name": "evil-origin-stand", "stand_dir": str(tmp_path / "evil-origin-stand")},
    )
    assert status == 403


def test_register_with_wrong_token_is_forbidden(tmp_path):
    base_url, *_ = _start_hub(tmp_path, stand_name="existing")
    status, _, _ = _request(
        base_url,
        "/api/stand/register",
        token="wrong-token",
        method="POST",
        origin=base_url,
        body={"name": "wrong-token-stand", "stand_dir": str(tmp_path / "wrong-token-stand")},
    )
    assert status == 403
