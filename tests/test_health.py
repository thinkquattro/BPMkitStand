"""
Тесты health-проб: быстрые проверки логики без реальной сети/моков —
заведомо закрытый порт и заведомо несуществующий HTTP-адрес.
"""

import socket

from standkit.health import http_ok, process_alive, process_running, tcp_open
from standkit.models import ProbeState
from standkit.health import check_stand
from standkit.models import Stand, Transport


def _find_closed_port() -> int:
    """Находит порт, который точно закрыт: занимает и сразу освобождает его."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_tcp_open_false_on_closed_port():
    port = _find_closed_port()
    assert tcp_open("127.0.0.1", port) is False


def test_tcp_open_false_on_empty_host_or_port():
    assert tcp_open("", 5432) is False
    assert tcp_open("127.0.0.1", 0) is False


def test_http_ok_false_on_unreachable_host():
    # Заведомо закрытый локальный порт — надёжнее, чем "чёрная дыра" TEST-NET-1
    # (в некоторых песочницах/сетях с прозрачным прокси такие адреса неожиданно
    # отвечают, что делает тест по внешнему IP нестабильным).
    port = _find_closed_port()
    assert http_ok(f"http://127.0.0.1:{port}/", timeout=0.5) is False


def test_process_alive_false_when_pidfile_missing(tmp_path):
    assert process_alive(tmp_path / "nonexistent.pid") is False


def test_process_alive_false_on_garbage_pidfile(tmp_path):
    pf = tmp_path / "garbage.pid"
    pf.write_text("not-a-pid", encoding="utf-8")
    assert process_alive(pf) is False


def test_check_stand_unknown_when_no_pidfile_and_no_host():
    # Явно очищаем host/port — у Stand по умолчанию они заполнены
    # (127.0.0.1:5000), что делает пробы "проверяемыми" (DOWN, а не UNKNOWN).
    stand = Stand(
        name="empty",
        stand_dir="/opt/x",
        transport=Transport.LOCAL,
        stand_host="",
        stand_port=0,
        db_host="",
        db_port=0,
    )
    status = check_stand(stand)
    assert status.process == ProbeState.UNKNOWN
    assert status.http == ProbeState.UNKNOWN
    assert status.db == ProbeState.UNKNOWN


def test_check_stand_down_on_closed_ports(tmp_path):
    port = _find_closed_port()
    stand = Stand(
        name="down",
        stand_dir="/opt/x",
        stand_host="127.0.0.1",
        stand_port=port,
        db_host="127.0.0.1",
        db_port=port,
    )
    pidfile = tmp_path / "down.pid"
    status = check_stand(stand, pidfile=pidfile)

    assert status.process == ProbeState.DOWN
    assert status.http == ProbeState.DOWN
    assert status.db == ProbeState.DOWN
    assert status.is_healthy is False


# --- process_running: TCP-фолбэк для процессов, поднятых извне standkit ---


def test_process_running_false_when_pidfile_missing_and_no_host_port(tmp_path):
    assert process_running(tmp_path / "nonexistent.pid") is False


def test_process_running_true_via_pidfile(monkeypatch, tmp_path):
    from standkit import health as health_module

    pf = tmp_path / "alive.pid"
    pf.write_text("123", encoding="utf-8")
    monkeypatch.setattr(health_module, "process_alive", lambda pidfile: True)
    assert process_running(pf) is True


def test_process_running_true_via_open_stand_port(monkeypatch):
    # Даже без pidfile (либо он не жив) — открытый TCP-порт стенда сам по себе
    # считается признаком "процесс запущен" (стенд поднят вручную/извне).
    calls = {}

    def _fake_tcp_open(host, port, **kwargs):
        calls["host"] = host
        calls["port"] = port
        return True

    import standkit.health as health_module

    monkeypatch.setattr(health_module, "tcp_open", _fake_tcp_open)
    assert process_running(None, "127.0.0.1", 5000) is True
    assert calls == {"host": "127.0.0.1", "port": 5000}


def test_process_running_false_when_port_closed_and_no_pidfile():
    port = _find_closed_port()
    assert process_running(None, "127.0.0.1", port) is False


def test_check_stand_process_up_via_open_port_without_alive_pidfile(tmp_path):
    # pidfile передан, но не существует/не жив — процесс всё равно "up", если
    # слушается TCP-порт стенда (стенд поднят вне standkit).
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        stand = Stand(
            name="external",
            stand_dir="/opt/x",
            stand_host="127.0.0.1",
            stand_port=port,
            db_host="",
            db_port=0,
        )
        pidfile = tmp_path / "missing.pid"  # никогда не создавался standkit'ом
        status = check_stand(stand, pidfile=pidfile)
        assert status.process == ProbeState.OK
    finally:
        listener.close()
