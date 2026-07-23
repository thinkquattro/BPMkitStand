"""
Тесты standkit_hub.config.HubConfig — БЕЗ веб-слоя (модуль config.py
намеренно не импортирует http.server, чтобы быть тестируемым в изоляции).
"""

from __future__ import annotations

import json
from pathlib import Path

from standkit.registry import default_registry_path
from standkit_hub.config import HubConfig, RemoteAgent


def test_load_missing_file_returns_defaults(tmp_path):
    cfg = HubConfig.load(tmp_path / "does_not_exist.json")

    assert cfg.registry_path == str(default_registry_path())
    assert cfg.run_dir == ""
    assert cfg.log_dir == ""
    assert cfg.refresh_interval_sec == 10
    assert cfg.agents == []
    assert cfg.agent_host == "127.0.0.1"
    assert cfg.agent_port == 8765
    assert cfg.token_ref == ""
    assert cfg.readonly_token_ref == ""
    assert cfg.tls_cert == ""
    assert cfg.tls_key == ""
    assert cfg.tls_client_ca == ""
    assert cfg.insecure is False
    assert cfg.audit_log == ""
    assert cfg.lockout_max_failures == 5
    assert cfg.lockout_window_sec == 300.0


def test_save_load_roundtrip_preserves_all_fields(tmp_path):
    path = tmp_path / "standkit-hub.json"
    cfg = HubConfig(
        registry_path=str(tmp_path / "projects.json"),
        run_dir=str(tmp_path / "run"),
        log_dir=str(tmp_path / "logs"),
        refresh_interval_sec=30,
        agents=[
            RemoteAgent(name="remote-a", url="https://a:8765", token_ref="standkit:a:agent-token"),
            RemoteAgent(name="remote-b", url="https://b:8765", token_ref="standkit:b:agent-token"),
        ],
        agent_host="0.0.0.0",
        agent_port=9999,
        token_ref="standkit:local:agent-token",
        readonly_token_ref="standkit:local:agent-readonly-token",
        tls_cert=str(tmp_path / "agent.crt"),
        tls_key=str(tmp_path / "agent.key"),
        tls_client_ca=str(tmp_path / "clients-ca.crt"),
        insecure=True,
        audit_log=str(tmp_path / "audit.log"),
        lockout_max_failures=10,
        lockout_window_sec=120.5,
    )
    cfg.save(path)

    reloaded = HubConfig.load(path)

    assert reloaded == cfg


def test_save_writes_only_token_refs_not_secret_values(tmp_path):
    path = tmp_path / "standkit-hub.json"
    cfg = HubConfig(
        token_ref="standkit:local:agent-token",
        readonly_token_ref="standkit:local:agent-readonly-token",
        agents=[RemoteAgent(name="remote", url="https://remote:8765", token_ref="standkit:remote:agent-token")],
    )
    cfg.save(path)

    raw = json.loads(path.read_text(encoding="utf-8"))

    # Только ссылки на секреты (*_ref) — ни одного поля с "живым" значением токена.
    assert raw["token_ref"] == "standkit:local:agent-token"
    assert raw["readonly_token_ref"] == "standkit:local:agent-readonly-token"
    assert raw["agents"][0]["token_ref"] == "standkit:remote:agent-token"
    assert "token" not in {k for k in raw.keys()} - {"token_ref", "readonly_token_ref"}
    assert set(raw["agents"][0].keys()) == {"name", "url", "token_ref"}


def test_load_tolerates_bom(tmp_path):
    path = tmp_path / "standkit-hub.json"
    payload = {"refresh_interval_sec": 42, "agent_host": "127.0.0.1"}
    path.write_text(json.dumps(payload), encoding="utf-8-sig")

    cfg = HubConfig.load(path)

    assert cfg.refresh_interval_sec == 42
    assert cfg.agent_host == "127.0.0.1"


def test_load_ignores_unknown_fields(tmp_path):
    path = tmp_path / "standkit-hub.json"
    payload = {"refresh_interval_sec": 5, "future_field_from_newer_hub": "whatever"}
    path.write_text(json.dumps(payload), encoding="utf-8")

    cfg = HubConfig.load(path)

    assert cfg.refresh_interval_sec == 5
    assert not hasattr(cfg, "future_field_from_newer_hub")


def test_load_corrupt_json_falls_back_to_defaults(tmp_path):
    path = tmp_path / "standkit-hub.json"
    path.write_text("{not valid json", encoding="utf-8")

    cfg = HubConfig.load(path)

    assert cfg.registry_path == str(default_registry_path())


def test_config_path_lives_under_bpmkit_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("standkit.registry.sys.platform", "linux")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    result = HubConfig.config_path()

    assert result == tmp_path / ".config" / "BPMkit" / "standkit-hub.json"


def test_save_creates_parent_directory(tmp_path):
    nested = tmp_path / "a" / "b" / "standkit-hub.json"
    cfg = HubConfig()

    cfg.save(nested)

    assert nested.exists()


def test_to_dict_from_dict_roundtrip():
    cfg = HubConfig(refresh_interval_sec=99, agents=[RemoteAgent(name="x", url="http://x", token_ref="r")])

    rebuilt = HubConfig.from_dict(cfg.to_dict())

    assert rebuilt == cfg
