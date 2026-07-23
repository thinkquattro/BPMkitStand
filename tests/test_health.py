"""
Тесты health-проб: быстрые проверки логики без реальной сети/моков —
заведомо закрытый порт и заведомо несуществующий HTTP-адрес.
"""

import socket

from standkit.health import http_ok, process_alive, tcp_open
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
