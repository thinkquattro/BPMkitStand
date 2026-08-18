"""
Тесты health-проб: быстрые проверки логики без реальной сети/моков —
заведомо закрытый порт и заведомо несуществующий HTTP-адрес.
"""

import contextlib
import http.server
import os
import shutil
import socket
import ssl
import tempfile
import threading

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


# --- HTTPS-проба ------------------------------------------------------------
#
# Самоподписанный сертификат для локального TLS-сервера в тестах. Он ВШИТ
# константой, а не генерируется на лету: генерация требует openssl или
# cryptography, а инварианты проекта — stdlib-only и тесты, которые не ходят
# наружу и не зависят от того, что установлено на машине. Сертификат выписан
# на 127.0.0.1 сроком до 2126 года; тест на verify=True от срока не зависит
# вовсе (он проверяет, что НЕдоверенная цепочка честно даёт DOWN).
_SELF_SIGNED_PEM = """\
-----BEGIN CERTIFICATE-----
MIIDHDCCAgSgAwIBAgIUGlYBVv00M0bMPMEnR49FYVDLJ2swDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJMTI3LjAuMC4xMCAXDTI2MDgxODExMDAxMVoYDzIxMjYw
NzI1MTEwMDExWjAUMRIwEAYDVQQDDAkxMjcuMC4wLjEwggEiMA0GCSqGSIb3DQEB
AQUAA4IBDwAwggEKAoIBAQCDS1pNIC5k1zVOb5WDER+AIk5JXRK84tNeJ4UP+5QU
CIh9p0/7JDrnmNMbaSyR75FT/pHXgLQNq+a/MvVF5JaOQW+omDPC5ohu4RqZ+zaP
Y4gc0+aetsGF/hg1q+Val5/tS0R9CcPG0cJxzw7RhEiMnumR+cwPG9tKChw6MyEG
hmluDEdoU0PFtbNRB78hDRHC2dzp3u7cfoDnCekMui/ObT91eEyslokFF6MAjjsH
9sNfUbElHu4q9Uyuiv20gVqWVvDRVQkgLkVwoyeNMcMRtpk2PluShw/YUU7w2O3o
TzqshMdH0SsQ1IbOYXYGamLmMDlwViZlNM0aYPsKUy/rAgMBAAGjZDBiMB0GA1Ud
DgQWBBRBofB9dUP0mcIGVR8ZTS3IVBUiozAfBgNVHSMEGDAWgBRBofB9dUP0mcIG
VR8ZTS3IVBUiozAPBgNVHRMBAf8EBTADAQH/MA8GA1UdEQQIMAaHBH8AAAEwDQYJ
KoZIhvcNAQELBQADggEBABmbJowjbBAr9MnDBcyIAp2lSm6s/33W2Ct6mwyzV795
Pp5G8DFXgqWZqreU1NDpLk30LuQuCv6y5vCiYmKgYnjlS/dopOVP/QfSbIK1Getf
XXTeMqMx5krUNH2MbLws6zmfF+9GuTnOwqpdon2/uICPFxrLD04A5Ccf6U9JkN0N
NXbRanKcsWLiZ+Mcg9ALBb1NOxx0Iy65Yhx3Y5MsGKo/LLsm+YQFFwiUbOaImNu6
AbEbnP0u9UO+3T7WAwQwBp+NnLhhT0inVKkgire/gjNGgPjKTHkyfshmPfl6OOQb
pIGSzOp7qaV0U6Ua+U24e4rWac0c6bTK4rmdgpZs6zc=
-----END CERTIFICATE-----
-----BEGIN PRIVATE KEY-----
MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKgwggSkAgEAAoIBAQCDS1pNIC5k1zVO
b5WDER+AIk5JXRK84tNeJ4UP+5QUCIh9p0/7JDrnmNMbaSyR75FT/pHXgLQNq+a/
MvVF5JaOQW+omDPC5ohu4RqZ+zaPY4gc0+aetsGF/hg1q+Val5/tS0R9CcPG0cJx
zw7RhEiMnumR+cwPG9tKChw6MyEGhmluDEdoU0PFtbNRB78hDRHC2dzp3u7cfoDn
CekMui/ObT91eEyslokFF6MAjjsH9sNfUbElHu4q9Uyuiv20gVqWVvDRVQkgLkVw
oyeNMcMRtpk2PluShw/YUU7w2O3oTzqshMdH0SsQ1IbOYXYGamLmMDlwViZlNM0a
YPsKUy/rAgMBAAECggEAIojzDz9sRKUhBeku7CNYZFVhv0VmlN2bGHSPuRUFLcHS
2S5lyNsOTXXy7Y5cJWTdFrlq9kMJ2WDCmL9YKdLHUVLgAnpKfzUxZOz8GM2t28ij
+GU6j7vlqo+cIZ39/bbNX9cBBFzJrOXm3hXHQZAonyh7qqSIqt66bz66jwp84OCg
rv+QzpbTksNEUGiHn2YACq8Otf/ewmamV+1GPXCMX+UFUcyEEsAcz+iHF9swcuVV
UVZywIV/JlmHMYKd0FYJ8SzD6wcoKN0u3VmqBWoALReBadau4veKuV8QrvhpU6DB
ysZXFx9WZQf6K4TDHIppJjpSZnHkSD17UFvEnZaHpQKBgQC3+c1ma7vx/VeAjSHX
ZvKicGxtBvUC+Aq2E3LVgzsE5dYJdfQUHOzklIaln4e692YnAISRhzJ61f0geNSU
ZVOca00hTBlrkvTfvL31UErfmi2rRF1+USqxiLNYXVK2kBYE1Ukt5S7+5EVuAu5O
l73/m5MN5Au4IkJwtQsOFH+JxwKBgQC2scZevuSN7QGoCXAQ8Cg9QDglzc12zqNo
HyXLXE9UXVEmjmkGMRtdy7D4Yi/cmq12TlXnC2Aaf6Wl3y5+taRKQ637NrAEpLSE
6v16izF9FDRqUeZv8jV1EhZTVPNLxVfztgo+30ZOpsIPiv4IpnOTFAgMD5/CGRJm
aE31b0HIvQKBgAQ/Tx+jMxaWG7QLDhHz/XwEjmxB8dwcr9qePlNxkSY+zB8xyu2/
8TQhva4LLc4CMiiKWYUmkuLFF+/s+jNm13RQAdrX7+pM3TxhFh2YufHJlG5UyLfG
1e59Um6i0OsIDooUBnl5xgj6aiPtC2VjGW7SP6XdcuvQVqpVc6jijkM3AoGBAIf+
LTu5vUgodGMxI0p4enudoi4B1D/r8ZdAGFIYlLSoAhBBUcxaIZTgWwuJizcbrKO0
DB3ASflvq06do26Op4zgdFHbk4rhT77hbW4azuvcbmf2LyKFmWVb4WKGidSNQbsY
duf2K8/AMhR/0jl+Len9rz/LIZDKOPgiDGX2O3HBAoGBAI5EhUKShbPxs9V4SGon
voZoQe8Zp179HnG3V/q4Jwfd7N0KZVzDkUWrjHMUjgao/gxPRTl3Ao5i2ErFM/1/
eWykfQv0APjbCgue0xN5ny9/HsHTn63DmC4qpETfhTTHS5CwYgvm2Rf8LS1bOQ9O
m5S3tV7Myy9ZBWwUgZnrWCeb
-----END PRIVATE KEY-----
"""


@contextlib.contextmanager
def _self_signed_https_server():
    """
    Поднимает локальный HTTPS-сервер с самоподписанным сертификатом и отдаёт
    его порт. Ровно тот случай, ради которого правка и делалась: дев-контур
    BPMSoft за TLS, сертификат которому никто не выписывал.
    """
    tmpdir = tempfile.mkdtemp()
    pem = os.path.join(tmpdir, "self-signed.pem")
    with open(pem, "w", encoding="ascii") as fh:
        fh.write(_SELF_SIGNED_PEM)

    class _Quiet(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — имя диктует BaseHTTPRequestHandler
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # тишина в выводе pytest
            return

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(pem)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _https_stand(port: int, **kwargs) -> Stand:
    return Stand(
        name="tls",
        stand_dir="/opt/x",
        stand_host="127.0.0.1",
        stand_port=port,
        db_host="",
        db_port=0,
        **kwargs,
    )


def test_probe_http_uses_scheme_from_registry(monkeypatch):
    """stand_scheme=https → в http_ok уходит https://-адрес (и verify из записи)."""
    seen = {}

    def _fake_http_ok(url, *, timeout=None, verify=True):
        seen["url"] = url
        seen["verify"] = verify
        return True

    monkeypatch.setattr("standkit.health.http_ok", _fake_http_ok)
    stand = _https_stand(5010, stand_scheme="https", verify_tls=False)
    status = check_stand(stand)
    assert seen["url"] == "https://127.0.0.1:5010/"
    assert seen["verify"] is False
    assert status.http == ProbeState.OK


def test_probe_http_defaults_to_plain_http(monkeypatch):
    """Регресс обратной совместимости: реестр без stand_scheme → http:// и verify=True."""
    seen = {}

    def _fake_http_ok(url, *, timeout=None, verify=True):
        seen["url"] = url
        seen["verify"] = verify
        return True

    monkeypatch.setattr("standkit.health.http_ok", _fake_http_ok)
    stand = Stand.from_dict(
        "legacy",
        {"stand_dir": "/opt/x", "stand_host": "127.0.0.1", "stand_port": 5000},
    )
    check_stand(stand)
    assert seen["url"] == "http://127.0.0.1:5000/"
    assert seen["verify"] is True


def test_check_stand_http_ok_on_self_signed_when_verify_disabled():
    """verify_tls=false — живой стенд за self-signed TLS показывается ok, а не down."""
    with _self_signed_https_server() as port:
        stand = _https_stand(port, stand_scheme="https", verify_tls=False)
        status = check_stand(stand)
        assert status.http == ProbeState.OK


def test_check_stand_http_down_on_self_signed_when_verify_enabled():
    """verify_tls=true — недоверенная цепочка даёт честный down, а не исключение наружу."""
    with _self_signed_https_server() as port:
        stand = _https_stand(port, stand_scheme="https", verify_tls=True)
        status = check_stand(stand)
        assert status.http == ProbeState.DOWN


def test_http_ok_verify_flag_does_not_affect_plain_http():
    """verify=False не меняет поведения для http://-адресов (контекст не строится)."""
    port = _find_closed_port()
    assert http_ok(f"http://127.0.0.1:{port}/", timeout=0.5, verify=False) is False
