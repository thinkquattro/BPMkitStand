"""Тесты моделей ядра: Stand и StandStatus — конструирование из словаря и обратно."""

from standkit.models import HostKind, ProbeState, Stand, StandStatus, Transport


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


# --- host_kind (ADR-0001: hosting backends) ---


def test_stand_host_kind_defaults_to_kestrel():
    stand = Stand(name="demo", stand_dir="/opt/x")
    assert stand.host_kind == HostKind.KESTREL


def test_stand_from_dict_host_kind_iis():
    data = {"stand_dir": "/opt/x", "host_kind": "iis", "iis_site": "Site1", "iis_app_pool": "Pool1"}
    stand = Stand.from_dict("demo", data)
    assert stand.host_kind == HostKind.IIS
    assert stand.iis_site == "Site1"
    assert stand.iis_app_pool == "Pool1"


def test_stand_from_dict_host_kind_docker_roundtrip():
    data = {"stand_dir": "/opt/x", "host_kind": "docker", "docker_container": "c1"}
    stand = Stand.from_dict("demo", data)
    assert stand.host_kind == HostKind.DOCKER
    out = stand.to_dict()
    assert out["host_kind"] == "docker"
    assert out["docker_container"] == "c1"


def test_stand_from_dict_unknown_host_kind_coerces_to_kestrel():
    data = {"stand_dir": "/opt/x", "host_kind": "future-hosting-kind"}
    stand = Stand.from_dict("demo", data)
    assert stand.host_kind == HostKind.KESTREL


def test_stand_host_kind_k8s_schema_allowed():
    stand = Stand(name="demo", stand_dir="/opt/x", host_kind=HostKind.K8S)
    assert stand.host_kind == HostKind.K8S
    out = stand.to_dict()
    assert out["host_kind"] == "k8s"


def test_stand_validate_iis_requires_site_or_pool():
    stand = Stand(name="demo", stand_dir="/opt/x", host_kind=HostKind.IIS)
    errors = stand.validate()
    assert any("iis_site" in e or "iis_app_pool" in e for e in errors)


def test_stand_validate_iis_ok_with_only_app_pool():
    stand = Stand(name="demo", stand_dir="/opt/x", host_kind=HostKind.IIS, iis_app_pool="Pool1")
    errors = stand.validate()
    assert not any("iis" in e for e in errors)


def test_stand_validate_docker_requires_container_or_compose_pair():
    stand = Stand(name="demo", stand_dir="/opt/x", host_kind=HostKind.DOCKER)
    errors = stand.validate()
    assert any("docker_container" in e for e in errors)


def test_stand_validate_docker_ok_with_container():
    stand = Stand(name="demo", stand_dir="/opt/x", host_kind=HostKind.DOCKER, docker_container="c1")
    errors = stand.validate()
    assert not any("docker" in e for e in errors)


def test_stand_validate_docker_requires_both_compose_fields_together():
    stand = Stand(
        name="demo", stand_dir="/opt/x", host_kind=HostKind.DOCKER, docker_compose_file="/opt/x/compose.yml"
    )
    errors = stand.validate()
    assert any("docker_container" in e for e in errors)


def test_stand_validate_docker_ok_with_compose_pair():
    stand = Stand(
        name="demo",
        stand_dir="/opt/x",
        host_kind=HostKind.DOCKER,
        docker_compose_file="/opt/x/compose.yml",
        docker_compose_service="webhost",
    )
    errors = stand.validate()
    assert not any("docker" in e for e in errors)
