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


# --- host_kind=k8s (Kubernetes) ---


def test_stand_k8s_fields_default_values():
    stand = Stand(name="demo", stand_dir="/opt/x", host_kind=HostKind.K8S)
    assert stand.k8s_namespace == ""
    assert stand.k8s_deployment is None
    assert stand.k8s_context is None
    assert stand.k8s_container is None
    assert stand.k8s_replicas == 1


def test_stand_validate_k8s_requires_deployment():
    stand = Stand(name="demo", stand_dir="/opt/x", host_kind=HostKind.K8S)
    errors = stand.validate()
    assert any("k8s_deployment" in e for e in errors)


def test_stand_validate_k8s_ok_with_deployment():
    stand = Stand(name="demo", stand_dir="/opt/x", host_kind=HostKind.K8S, k8s_deployment="dep1")
    errors = stand.validate()
    assert not any("k8s" in e for e in errors)


def test_stand_from_dict_host_kind_k8s_roundtrip():
    data = {
        "stand_dir": "/opt/x",
        "host_kind": "k8s",
        "k8s_namespace": "bpmsoft",
        "k8s_deployment": "dep1",
        "k8s_context": "cluster1",
        "k8s_container": "webhost",
        "k8s_replicas": 3,
    }
    stand = Stand.from_dict("demo", data)
    assert stand.host_kind == HostKind.K8S
    assert stand.k8s_namespace == "bpmsoft"
    assert stand.k8s_deployment == "dep1"
    assert stand.k8s_context == "cluster1"
    assert stand.k8s_container == "webhost"
    assert stand.k8s_replicas == 3

    out = stand.to_dict()
    assert out["host_kind"] == "k8s"
    assert out["k8s_namespace"] == "bpmsoft"
    assert out["k8s_deployment"] == "dep1"
    assert out["k8s_context"] == "cluster1"
    assert out["k8s_container"] == "webhost"
    assert out["k8s_replicas"] == 3


def test_stand_from_dict_k8s_empty_namespace_defaults_to_empty_string():
    data = {"stand_dir": "/opt/x", "host_kind": "k8s", "k8s_deployment": "dep1"}
    stand = Stand.from_dict("demo", data)
    assert stand.k8s_namespace == ""


def test_validate_rejects_unknown_stand_scheme():
    stand = Stand(name="s", stand_dir="/opt/x", stand_scheme="ftp")
    errors = stand.validate()
    assert any("stand_scheme" in e for e in errors)


def test_validate_accepts_http_and_https():
    for scheme in ("http", "https", "HTTPS"):
        stand = Stand(name="s", stand_dir="/opt/x", stand_scheme=scheme)
        assert not [e for e in stand.validate() if "stand_scheme" in e]


def test_scheme_and_verify_survive_registry_round_trip():
    data = {
        "stand_dir": "/opt/x",
        "stand_host": "127.0.0.1",
        "stand_port": 5010,
        "stand_scheme": "https",
        "verify_tls": False,
    }
    stand = Stand.from_dict("tls", data)
    assert stand.stand_scheme == "https"
    assert stand.verify_tls is False
    dumped = stand.to_dict()
    assert dumped["stand_scheme"] == "https"
    assert dumped["verify_tls"] is False
    assert Stand.from_dict("tls", dumped).verify_tls is False


def test_scheme_defaults_preserve_old_registries():
    stand = Stand.from_dict("legacy", {"stand_dir": "/opt/x"})
    assert stand.stand_scheme == "http"
    assert stand.verify_tls is True


def test_verify_tls_reads_string_booleans_from_hand_edited_registry():
    assert Stand.from_dict("s", {"stand_dir": "/opt/x", "verify_tls": "false"}).verify_tls is False
    assert Stand.from_dict("s", {"stand_dir": "/opt/x", "verify_tls": "true"}).verify_tls is True
    # мусор — не роняем чтение реестра, остаёмся на безопасном дефолте
    assert Stand.from_dict("s", {"stand_dir": "/opt/x", "verify_tls": "ага"}).verify_tls is True


def test_unknown_scheme_from_registry_falls_back_to_http():
    assert Stand.from_dict("s", {"stand_dir": "/opt/x", "stand_scheme": "ftp"}).stand_scheme == "http"


# --- доверие к сертификату агента: agent_ca / agent_verify_tls (GAP-008) ---


def test_agent_trust_defaults_preserve_old_registries():
    """
    Старая запись без новых полей читается как «сертификат агента проверяем
    штатно» — прежнее поведение канала «хаб → агент» до буквы.
    """
    stand = Stand.from_dict("legacy", {"stand_dir": "/opt/x", "transport": "agent"})
    assert stand.agent_ca is None
    assert stand.agent_verify_tls is True


def test_agent_trust_fields_survive_registry_round_trip():
    data = {
        "stand_dir": "/opt/x",
        "transport": "agent",
        "agent_url": "https://example-host:8765",
        "agent_ca": "/etc/standkit/agent.crt",
        "agent_verify_tls": False,
    }
    stand = Stand.from_dict("remote", data)
    assert stand.agent_ca == "/etc/standkit/agent.crt"
    assert stand.agent_verify_tls is False

    dumped = stand.to_dict()
    assert dumped["agent_ca"] == "/etc/standkit/agent.crt"
    assert dumped["agent_verify_tls"] is False
    assert Stand.from_dict("remote", dumped).agent_verify_tls is False


def test_agent_verify_tls_reads_string_booleans_from_hand_edited_registry():
    """Тот же терпимый ``_coerce_bool``, что у verify_tls: реестр правят руками."""
    base = {"stand_dir": "/opt/x", "transport": "agent", "agent_url": "https://h:8765"}
    assert Stand.from_dict("s", {**base, "agent_verify_tls": "false"}).agent_verify_tls is False
    assert Stand.from_dict("s", {**base, "agent_verify_tls": "0"}).agent_verify_tls is False
    assert Stand.from_dict("s", {**base, "agent_verify_tls": "true"}).agent_verify_tls is True
    # мусор — не роняем чтение реестра и остаёмся на БЕЗОПАСНОМ дефолте
    # (проверять), а не на молчаливом отключении проверки.
    assert Stand.from_dict("s", {**base, "agent_verify_tls": "ага"}).agent_verify_tls is True


def test_agent_trust_is_independent_of_stand_probe_tls():
    """
    Причина GAP-008 в одной строке: verify_tls (проба СТЕНДА) и
    agent_verify_tls (канал до АГЕНТА) — разные поля и не влияют друг на друга.
    """
    stand = Stand.from_dict(
        "remote",
        {
            "stand_dir": "/opt/x",
            "transport": "agent",
            "agent_url": "https://example-host:8765",
            "verify_tls": False,
        },
    )
    assert stand.verify_tls is False
    assert stand.agent_verify_tls is True


def test_validate_accepts_agent_ca_over_https():
    stand = Stand(
        name="remote",
        stand_dir="/opt/x",
        transport=Transport.AGENT,
        agent_url="https://example-host:8765",
        agent_ca="/etc/standkit/agent.crt",
    )
    assert stand.validate() == []


def test_validate_rejects_agent_ca_over_plain_http():
    """
    Молча проигнорированная настройка — это и есть механика GAP-008, поэтому
    agent_ca при agent_url на http:// — явная ошибка, а не «не применится».
    """
    stand = Stand(
        name="remote",
        stand_dir="/opt/x",
        transport=Transport.AGENT,
        agent_url="http://example-host:8765",
        agent_ca="/etc/standkit/agent.crt",
    )
    errors = stand.validate()
    assert any("agent_ca" in e for e in errors)


def test_validate_does_not_touch_filesystem_for_agent_ca():
    """
    ``validate()`` — чистая проверка записи: несуществующий путь в agent_ca не
    ошибка модели (одна и та же запись читается и там, где файла нет и быть не
    должно). Понятный отказ обязан дать клиент хаба в момент обращения.
    """
    stand = Stand(
        name="remote",
        stand_dir="/opt/x",
        transport=Transport.AGENT,
        agent_url="https://example-host:8765",
        agent_ca="/nope/definitely-missing.crt",
    )
    assert stand.validate() == []


def test_validate_allows_agent_ca_left_on_local_transport():
    """
    Запись переключают между local и agent туда-обратно — терять уже введённый
    путь (или падать из-за него) вреднее, чем держать неиспользуемое поле.
    """
    stand = Stand(
        name="local-with-leftover",
        stand_dir="/opt/x",
        transport=Transport.LOCAL,
        agent_ca="/etc/standkit/agent.crt",
    )
    assert stand.validate() == []


# --- Redis: пара redis_host/redis_port (GAP-003) --------------------------------
#
# Половина пары раньше давала молчаливый ``unknown`` в дашборде, и оператор не
# мог отличить «не настроено» от «настроено с опечаткой». Тексты отсюда
# цитируются дословно в docs/COOKBOOK_LINUX.md — менять их без правки кукбука
# нельзя.


def test_validate_redis_pair_complete_is_valid():
    stand = Stand(name="s", stand_dir="/opt/x", redis_host="127.0.0.1", redis_port=6379)
    assert stand.validate() == []


def test_validate_redis_both_empty_is_valid():
    # Redis не используется — это не ошибка записи (проба отдаст unknown с причиной).
    assert Stand(name="s", stand_dir="/opt/x").validate() == []


def test_validate_redis_host_without_port():
    stand = Stand(name="s", stand_dir="/opt/x", redis_host="127.0.0.1")
    assert stand.validate() == ["redis_host задан без корректного redis_port (1–65535)"]


def test_validate_redis_port_without_host():
    stand = Stand(name="s", stand_dir="/opt/x", redis_port=6379)
    assert stand.validate() == ["redis_port задан без redis_host"]


def test_validate_redis_port_out_of_range():
    stand = Stand(name="s", stand_dir="/opt/x", redis_host="127.0.0.1", redis_port=70000)
    assert stand.validate() == ["redis_port должен быть в диапазоне 1–65535"]


def test_validate_redis_negative_port_gives_single_diagnosis():
    """
    Отрицательный порт при заданном host — ОДНА ошибка, а не две
    пересекающиеся («без корректного redis_port» + «должен быть в диапазоне»):
    оператору нужен один точный диагноз.
    """
    stand = Stand(name="s", stand_dir="/opt/x", redis_host="127.0.0.1", redis_port=-1)
    assert stand.validate() == ["redis_port должен быть в диапазоне 1–65535"]


def test_validate_redis_negative_port_without_host_also_single():
    stand = Stand(name="s", stand_dir="/opt/x", redis_port=-1)
    assert stand.validate() == ["redis_port должен быть в диапазоне 1–65535"]


def test_from_dict_reads_redis_port_from_string():
    # Реестр правят руками и внешние инструменты: "6379" строкой — частый случай.
    stand = Stand.from_dict("s", {"stand_dir": "/opt/x", "redis_host": "127.0.0.1", "redis_port": "6379"})
    assert stand.redis_port == 6379
    assert stand.validate() == []


def test_from_dict_garbage_redis_port_becomes_zero_with_clear_error():
    stand = Stand.from_dict("s", {"stand_dir": "/opt/x", "redis_host": "127.0.0.1", "redis_port": "шесть тысяч"})
    assert stand.redis_port == 0  # мусор не роняет чтение реестра
    assert stand.validate() == ["redis_host задан без корректного redis_port (1–65535)"]


def test_from_dict_bool_redis_port_is_not_treated_as_int():
    # True — это int(1) в Python; порт из булева значения читать нельзя.
    stand = Stand.from_dict("s", {"stand_dir": "/opt/x", "redis_port": True})
    assert stand.redis_port == 0


def test_redis_and_logs_dir_round_trip_through_to_dict():
    data = {
        "stand_dir": "/opt/bpmsoft/demo",
        "logs_dir": "/var/log/bpmsoft/demo",
        "redis_host": "10.0.0.7",
        "redis_port": 6380,
    }
    stand = Stand.from_dict("demo", data)
    assert stand.logs_dir == "/var/log/bpmsoft/demo"
    assert stand.redis_host == "10.0.0.7"
    assert stand.redis_port == 6380

    dumped = stand.to_dict()
    assert dumped["logs_dir"] == "/var/log/bpmsoft/demo"
    assert dumped["redis_host"] == "10.0.0.7"
    assert dumped["redis_port"] == 6380

    again = Stand.from_dict("demo", dumped)
    assert (again.logs_dir, again.redis_host, again.redis_port) == (
        stand.logs_dir,
        stand.redis_host,
        stand.redis_port,
    )
    # Типизированные поля не должны утекать в extra (там они жили до 0.8.0).
    assert "redis_host" not in again.extra
    assert "redis_port" not in again.extra
    assert "logs_dir" not in again.extra


def test_string_redis_port_round_trips_as_int():
    # "6379" из старого реестра после записи обязано стать числом, а не строкой.
    stand = Stand.from_dict("s", {"stand_dir": "/opt/x", "redis_host": "h", "redis_port": "6379"})
    assert stand.to_dict()["redis_port"] == 6379
