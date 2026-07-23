"""Тесты моделей ядра: Stand и StandStatus — конструирование из словаря и обратно."""

from standkit.models import ProbeState, Stand, StandStatus, Transport


def test_stand_from_dict_basic_fields():
    data = {
        "transport": "local",
        "stand_dir": "/opt/bpmsoft/demo",
        "stand_dll": "BPMSoft.WebHost.dll",
        "stand_host": "127.0.0.1",
        "stand_port": 5000,
        "db_type": "postgres",
        "db_host": "127.0.0.1",
        "db_port": 5432,
    }
    stand = Stand.from_dict("demo", data)

    assert stand.name == "demo"
    assert stand.transport == Transport.LOCAL
    assert stand.stand_dir == "/opt/bpmsoft/demo"
    assert stand.stand_port == 5000
    assert stand.db_port == 5432


def test_stand_from_dict_agent_transport_and_extra_fields_roundtrip():
    data = {
        "transport": "agent",
        "agent_url": "https://host:8765",
        "agent_secret_ref": "standkit:demo:agent-token",
        "stand_dir": "/opt/bpmsoft/demo",
        "unknown_future_field": "kept-as-extra",
    }
    stand = Stand.from_dict("demo", data)

    assert stand.transport == Transport.AGENT
    assert stand.agent_url == "https://host:8765"
    assert stand.extra["unknown_future_field"] == "kept-as-extra"

    # round-trip: to_dict должен вернуть неизвестное поле обратно
    out = stand.to_dict()
    assert out["unknown_future_field"] == "kept-as-extra"
    assert out["transport"] == "agent"
    assert "name" not in out


def test_stand_validate_requires_stand_dir():
    stand = Stand(name="broken", stand_dir="")
    errors = stand.validate()
    assert any("stand_dir" in e for e in errors)


def test_stand_validate_agent_requires_agent_url():
    stand = Stand(name="broken-agent", stand_dir="/opt/x", transport=Transport.AGENT)
    errors = stand.validate()
    assert any("agent_url" in e for e in errors)


def test_stand_status_roundtrip_and_is_healthy():
    status = StandStatus(name="demo", process=ProbeState.OK, http=ProbeState.OK)
    assert status.is_healthy is True

    data = status.to_dict()
    assert data["process"] == "ok"

    restored = StandStatus.from_dict(data)
    assert restored.process == ProbeState.OK
    assert restored.name == "demo"


def test_stand_status_unhealthy_when_process_down():
    status = StandStatus(name="demo", process=ProbeState.DOWN, http=ProbeState.OK)
    assert status.is_healthy is False
