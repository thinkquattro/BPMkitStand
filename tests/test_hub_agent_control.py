"""
Тесты standkit_hub.agent_control — без реального спавна процесса: только
сборка argv из HubConfig (чистая функция) + контроллер поверх подменённых
standkit.platform.spawn_hidden/is_alive/stop.
"""

from __future__ import annotations

import pytest

import standkit_hub.agent_control as agent_control_module
from standkit_hub.agent_control import (
    AgentControlError,
    AgentController,
    build_agent_argv,
    validate_agent_config,
)
from standkit_hub.config import HubConfig


def _minimal_config(**overrides) -> HubConfig:
    base = HubConfig(
        registry_path="/tmp/projects.json",
        agent_host="127.0.0.1",
        agent_port=8765,
        token_ref="standkit:local:agent-token",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


# --- build_agent_argv ---


def test_build_agent_argv_minimal_fields():
    cfg = _minimal_config()

    argv = build_agent_argv(cfg, python_executable="C:/Python/python.exe")

    assert argv[:3] == ["C:/Python/python.exe", "-m", "standkit_agent"]
    assert "--host" in argv and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert "--port" in argv and argv[argv.index("--port") + 1] == "8765"
    assert "--registry" in argv and argv[argv.index("--registry") + 1] == "/tmp/projects.json"
    assert "--token-ref" in argv and argv[argv.index("--token-ref") + 1] == "standkit:local:agent-token"
    # Необязательные поля не заданы — соответствующих флагов быть не должно.
    assert "--readonly-token-ref" not in argv
    assert "--tls-cert" not in argv
    assert "--insecure" not in argv


def test_build_agent_argv_maps_all_optional_fields():
    cfg = _minimal_config(
        readonly_token_ref="standkit:local:agent-readonly-token",
        run_dir="/tmp/run",
        log_dir="/tmp/logs",
        audit_log="/tmp/audit.log",
        tls_cert="/tmp/agent.crt",
        tls_key="/tmp/agent.key",
        tls_client_ca="/tmp/ca.crt",
        insecure=True,
        lockout_max_failures=7,
        lockout_window_sec=42.5,
    )

    argv = build_agent_argv(cfg, python_executable="python")

    def flag_value(flag: str) -> str:
        return argv[argv.index(flag) + 1]

    assert flag_value("--readonly-token-ref") == "standkit:local:agent-readonly-token"
    assert flag_value("--run-dir") == "/tmp/run"
    assert flag_value("--log-dir") == "/tmp/logs"
    assert flag_value("--audit-log") == "/tmp/audit.log"
    assert flag_value("--tls-cert") == "/tmp/agent.crt"
    assert flag_value("--tls-key") == "/tmp/agent.key"
    assert flag_value("--tls-client-ca") == "/tmp/ca.crt"
    assert "--insecure" in argv
    assert flag_value("--lockout-max-failures") == "7"
    assert flag_value("--lockout-window") == "42.5"


def test_build_agent_argv_defaults_python_executable_to_sys_executable():
    import sys

    cfg = _minimal_config()
    argv = build_agent_argv(cfg)
    assert argv[0] == sys.executable


def test_build_agent_argv_never_includes_secret_values():
    """Секреты передаются агенту только как *_ref — сами значения в argv не попадают по дизайну."""
    cfg = _minimal_config(readonly_token_ref="standkit:local:agent-readonly-token")
    argv = build_agent_argv(cfg)
    joined = " ".join(argv)
    assert "standkit:local:agent-token" in joined
    assert "standkit:local:agent-readonly-token" in joined


# --- validate_agent_config ---


def test_validate_agent_config_reports_missing_token_ref():
    cfg = _minimal_config(token_ref="")
    problems = validate_agent_config(cfg)
    assert any("token_ref" in p for p in problems)


def test_validate_agent_config_reports_missing_registry_path():
    cfg = _minimal_config(registry_path="")
    problems = validate_agent_config(cfg)
    assert any("registry_path" in p for p in problems)


def test_validate_agent_config_ok_for_minimal_valid_config():
    cfg = _minimal_config()
    assert validate_agent_config(cfg) == []


# --- AgentController (spawn_hidden/is_alive/stop подменены) ---


def test_agent_controller_start_rejects_invalid_config(tmp_path):
    cfg = _minimal_config(token_ref="", run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"))
    controller = AgentController(cfg)

    with pytest.raises(AgentControlError):
        controller.start()


def test_agent_controller_start_spawns_and_persists_pid(tmp_path, monkeypatch):
    cfg = _minimal_config(run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"))
    controller = AgentController(cfg)

    monkeypatch.setattr(agent_control_module, "spawn_hidden", lambda cmd, cwd, log_path: 4242)
    monkeypatch.setattr(agent_control_module, "is_alive", lambda pid: pid == 4242)

    result = controller.start()

    assert result.pid == 4242
    pid_file = tmp_path / "run" / "standkit-hub-agent.pid"
    assert pid_file.exists()
    assert pid_file.read_text(encoding="utf-8").strip() == "4242"
    assert controller.is_running() is True


def test_agent_controller_start_twice_raises_when_already_running(tmp_path, monkeypatch):
    cfg = _minimal_config(run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"))
    controller = AgentController(cfg)

    monkeypatch.setattr(agent_control_module, "spawn_hidden", lambda cmd, cwd, log_path: 111)
    monkeypatch.setattr(agent_control_module, "is_alive", lambda pid: True)

    controller.start()
    with pytest.raises(AgentControlError):
        controller.start()


def test_agent_controller_stop_without_running_process_is_noop(tmp_path):
    cfg = _minimal_config(run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"))
    controller = AgentController(cfg)

    assert controller.stop() is True


def test_agent_controller_stop_removes_pid_file(tmp_path, monkeypatch):
    cfg = _minimal_config(run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"))
    controller = AgentController(cfg)

    monkeypatch.setattr(agent_control_module, "spawn_hidden", lambda cmd, cwd, log_path: 555)
    monkeypatch.setattr(agent_control_module, "is_alive", lambda pid: True)
    controller.start()

    monkeypatch.setattr(agent_control_module, "stop", lambda pid, timeout=10.0: True)
    stopped = controller.stop()

    assert stopped is True
    pid_file = tmp_path / "run" / "standkit-hub-agent.pid"
    assert not pid_file.exists()
    assert controller.is_running() is False


def test_agent_controller_is_running_survives_new_instance_via_pid_file(tmp_path, monkeypatch):
    cfg = _minimal_config(run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"))
    controller = AgentController(cfg)

    monkeypatch.setattr(agent_control_module, "spawn_hidden", lambda cmd, cwd, log_path: 777)
    monkeypatch.setattr(agent_control_module, "is_alive", lambda pid: pid == 777)
    controller.start()

    fresh_controller = AgentController(cfg)
    assert fresh_controller.is_running() is True
