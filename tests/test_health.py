"""
Тесты health-проб: быстрые проверки логики без реальной сети/моков —
заведомо закрытый порт и заведомо несуществующий HTTP-адрес.
"""

import contextlib
import http.client
import http.server
import os
import shutil
import socket
import ssl
import tempfile
import threading
import urllib.error
import urllib.request

from standkit.health import http_ok, process_alive, process_running, tcp_open
from standkit.models import ProbeState
from standkit.health import (
    HINT_SELF_SIGNED,
    HINT_TLS_SCHEME,
    HttpProbeResult,
    check_stand,
    http_probe,
)
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


def _fake_http_probe(seen: dict, *, ok: bool = True):
    """
    Подменяет ``http_probe`` — именно ЕЁ вызывает ``_probe_http`` с тех пор, как
    проба обязана объяснять отказ (GAP-002). ``http_ok`` осталась публичным
    булевым фасадом над той же функцией, поэтому здесь перехватывается общий для
    обеих код, а не только его булев срез.
    """

    def _probe(url, *, timeout=None, verify=True):
        seen["url"] = url
        seen["verify"] = verify
        return HttpProbeResult(ok=ok)

    return _probe


def test_probe_http_uses_scheme_from_registry(monkeypatch):
    """stand_scheme=https → в пробу уходит https://-адрес (и verify из записи)."""
    seen = {}

    monkeypatch.setattr("standkit.health.http_probe", _fake_http_probe(seen))
    stand = _https_stand(5010, stand_scheme="https", verify_tls=False)
    status = check_stand(stand)
    assert seen["url"] == "https://127.0.0.1:5010/"
    assert seen["verify"] is False
    assert status.http == ProbeState.OK


def test_probe_http_defaults_to_plain_http(monkeypatch):
    """Регресс обратной совместимости: реестр без stand_scheme → http:// и verify=True."""
    seen = {}

    monkeypatch.setattr("standkit.health.http_probe", _fake_http_probe(seen))
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


# --- http_probe: отказ обязан объяснять себя (GAP-002) ----------------------
#
# Ветки классификатора проверяются подменой ``urllib.request.urlopen``: поднять
# в тесте настоящий сломанный DNS или «TLS-порт, отвечающий не по HTTP» нельзя
# (и не нужно — тесты не ходят наружу), а исключение прилетает ровно то же
# самое, что и в бою. Где живой сценарий воспроизводится локально — он проверен
# без моков (закрытый порт, self-signed сервер ниже).


def _urlopen_raising(exc: BaseException):
    """Фейковый urlopen, который всегда падает заданным исключением."""

    def _fake(req, timeout=None, context=None):
        raise exc

    return _fake


class _FakeResponse:
    """Минимальный ответ urlopen: контекстный менеджер со ``.status``."""

    def __init__(self, status: int):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_http_probe_ok_has_no_reason(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(200))
    result = http_probe("http://127.0.0.1:5000/")
    assert result.ok is True
    assert result.reason == ""
    assert result.hint == ""


def test_http_probe_401_is_alive(monkeypatch):
    """401 до логина — не авария: сервер ответил, значит живой."""
    exc = urllib.error.HTTPError("http://127.0.0.1:5000/", 401, "Unauthorized", None, None)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    assert http_probe("http://127.0.0.1:5000/").ok is True


def test_http_probe_5xx_reports_code(monkeypatch):
    exc = urllib.error.HTTPError("http://127.0.0.1:5000/", 502, "Bad Gateway", None, None)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("http://127.0.0.1:5000/")
    assert result.ok is False
    assert result.reason == "сервер ответил 502"


def test_http_probe_5xx_without_exception_reports_code(monkeypatch):
    """5xx может прийти и обычным ответом (если opener не поднял HTTPError)."""
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **kw: _FakeResponse(503))
    result = http_probe("http://127.0.0.1:5000/")
    assert result.ok is False
    assert result.reason == "сервер ответил 503"


def test_http_probe_cert_verification_explains_and_hints(monkeypatch):
    exc = urllib.error.URLError(ssl.SSLCertVerificationError("self-signed certificate"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("https://127.0.0.1:5000/")
    assert result.ok is False
    assert result.reason.startswith("сертификат не прошёл проверку")
    assert "self-signed certificate" in result.reason
    assert result.hint == HINT_SELF_SIGNED


def test_http_probe_ssl_handshake_error(monkeypatch):
    exc = urllib.error.URLError(ssl.SSLError("[SSL: WRONG_VERSION_NUMBER] wrong version number"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("https://127.0.0.1:5000/")
    assert result.ok is False
    assert result.reason.startswith("ошибка TLS-рукопожатия")
    assert "WRONG_VERSION_NUMBER" in result.reason


def test_http_probe_connection_refused_names_endpoint(monkeypatch):
    exc = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("http://10.0.0.5:5000/")
    assert result.reason == "соединение отклонено — на 10.0.0.5:5000 никто не слушает"
    assert result.hint == ""


def test_http_probe_refused_on_closed_port_live():
    """
    Тот же случай без моков: заведомо закрытый локальный порт.

    Точный диагноз здесь диктует ОС, а не наш код. На POSIX ядро отвечает RST,
    и проба обязана сказать «соединение отклонено». На **Windows** тот же
    сценарий регулярно даёт не RST, а молчание: SYN на только что освобождённый
    порт отбрасывается (фильтр/состояние стека), и проба честно упирается в
    таймаут. Обе формулировки — рабочий диагноз, поэтому здесь проверяется
    контракт, а не конкретная ветка классификатора: проба не упала, вернула
    ok=False и внятную причину. Разбор самих формулировок — в соседних тестах
    на моках (`test_http_probe_connection_refused_*`, `test_http_probe_timeout_*`),
    там поведение ОС ни при чём.
    """
    port = _find_closed_port()
    result = http_probe(f"http://127.0.0.1:{port}/", timeout=0.5)
    assert result.ok is False
    if "соединение отклонено" in result.reason:
        assert f"127.0.0.1:{port}" in result.reason
    else:
        assert result.reason == "нет ответа за 0.5 с", (
            f"ожидался отказ соединения или таймаут, получено: {result.reason!r}"
        )


def test_http_probe_timeout_reports_seconds(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(TimeoutError("timed out")))
    result = http_probe("http://127.0.0.1:5000/", timeout=0.5)
    assert result.reason == "нет ответа за 0.5 с"


def test_http_probe_timeout_wrapped_in_urlerror(monkeypatch):
    exc = urllib.error.URLError(socket.timeout("timed out"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    assert http_probe("http://127.0.0.1:5000/", timeout=2).reason == "нет ответа за 2 с"


def test_http_probe_timeout_as_urlerror_string(monkeypatch):
    """URLError иногда несёт таймаут не исключением, а строкой — тоже таймаут."""
    exc = urllib.error.URLError("timed out")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    assert http_probe("http://127.0.0.1:5000/", timeout=1.5).reason == "нет ответа за 1.5 с"


def test_http_probe_dns_failure(monkeypatch):
    exc = urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("http://stand.invalid:5000/")
    assert result.reason == "имя хоста не разрешается"


def test_http_probe_remote_disconnected_hints_tls(monkeypatch):
    """
    Ключевой случай GAP-002: запрос по http:// ушёл в TLS-порт, сервер закрыл
    соединение. Оператор должен получить не голый down, а «задайте
    stand_scheme=https».
    """
    exc = http.client.RemoteDisconnected("Remote end closed connection without response")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("http://127.0.0.1:5000/")
    assert result.ok is False
    assert result.reason == "сервер ответил не по протоколу HTTP"
    assert result.hint == HINT_TLS_SCHEME


def test_http_probe_bad_status_line_is_caught(monkeypatch):
    """
    Регресс: BadStatusLine — чистый HTTPException, он НЕ наследник OSError и
    раньше вылетал из пробы наружу необработанным.
    """
    exc = http.client.BadStatusLine("\x16\x03\x01\x02\x00")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("http://127.0.0.1:5000/")
    assert result.ok is False
    assert result.reason == "сервер ответил не по протоколу HTTP"
    assert result.hint == HINT_TLS_SCHEME


def test_http_probe_no_tls_hint_for_https_url(monkeypatch):
    """Подсказка про stand_scheme=https бессмысленна, если проба уже шла по https://."""
    exc = http.client.RemoteDisconnected("Remote end closed connection without response")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("https://127.0.0.1:5000/")
    assert result.reason == "сервер ответил не по протоколу HTTP"
    assert result.hint == ""


def test_http_probe_connection_reset_over_plain_http_hints_tls(monkeypatch):
    exc = urllib.error.URLError(ConnectionResetError(104, "Connection reset by peer"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("http://127.0.0.1:5000/")
    assert result.reason == "соединение сброшено сервером"
    assert result.hint == HINT_TLS_SCHEME


def test_http_probe_self_signed_cert_live():
    """Без моков: живой сервер с self-signed сертификатом и verify=True."""
    with _self_signed_https_server() as port:
        result = http_probe(f"https://127.0.0.1:{port}/", verify=True)
        assert result.ok is False
        assert result.reason.startswith("сертификат не прошёл проверку")
        assert result.hint == HINT_SELF_SIGNED


def test_http_ok_delegates_to_http_probe(monkeypatch):
    """Прежний публичный API сохранён: та же сигнатура, тот же bool наружу."""
    seen = {}

    def _probe(url, *, timeout=None, verify=True):
        seen.update(url=url, timeout=timeout, verify=verify)
        return HttpProbeResult(ok=True)

    monkeypatch.setattr("standkit.health.http_probe", _probe)
    assert http_ok("https://stand.local/", timeout=0.25, verify=False) is True
    assert seen == {"url": "https://stand.local/", "timeout": 0.25, "verify": False}


def test_http_ok_false_when_probe_failed(monkeypatch):
    monkeypatch.setattr(
        "standkit.health.http_probe",
        lambda url, **kwargs: HttpProbeResult(ok=False, reason="сервер ответил 502"),
    )
    assert http_ok("http://stand.local/") is False


def test_safe_url_drops_credentials_and_query():
    """В причину нельзя тащить секреты: userinfo и query из URL вырезаются."""
    from standkit.health import _safe_url

    assert (
        _safe_url("https://user:pass@stand.local:8443/health?token=secret")
        == "https://stand.local:8443/health"
    )


def test_safe_url_keeps_ipv6_brackets():
    """URL из подсказки обязан копироваться в браузер: скобки IPv6 не теряются."""
    from standkit.health import _safe_url

    assert _safe_url("http://[::1]:8080/x") == "http://[::1]:8080/x"
    assert _safe_url("https://[2001:db8::1]/health") == "https://[2001:db8::1]:443/health"


def test_safe_url_keeps_host_case():
    # urlsplit().hostname приводил хост к нижнему регистру — в тексте причины
    # должен стоять адрес ровно такой, какой оператор написал в реестре.
    from standkit.health import _safe_url

    assert _safe_url("http://Stand-A.Corp:5000/") == "http://Stand-A.Corp:5000/"


def test_endpoint_ipv6_and_defaults():
    from standkit.health import _endpoint

    assert _endpoint("http://[::1]:8080/x") == "[::1]:8080"
    assert _endpoint("http://[fe80::1]/x") == "[fe80::1]:80"
    assert _endpoint("https://stand.local/health") == "stand.local:443"
    # Порт-мусор не роняет формирование причины — подставляется дефолт схемы.
    assert _endpoint("http://stand.local:порт/x") == "stand.local:80"


def test_http_probe_refused_names_ipv6_endpoint_with_brackets(monkeypatch):
    exc = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    result = http_probe("http://[::1]:5000/")
    assert result.reason == "соединение отклонено — на [::1]:5000 никто не слушает"


# --- details: причина доезжает до StandStatus -------------------------------


def test_probe_http_puts_reason_and_url_into_details(monkeypatch):
    exc = http.client.RemoteDisconnected("Remote end closed connection without response")
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    stand = Stand(
        name="tls",
        stand_dir="/opt/x",
        stand_host="127.0.0.1",
        stand_port=5001,
        db_host="",
        db_port=0,
    )
    status = check_stand(stand)
    assert status.http == ProbeState.DOWN
    reason = status.details["http_reason"]
    assert "сервер ответил не по протоколу HTTP" in reason
    assert HINT_TLS_SCHEME in reason
    assert "(URL: http://127.0.0.1:5001/)" in reason


def test_probe_http_no_reason_when_ok(monkeypatch):
    monkeypatch.setattr("standkit.health.http_probe", _fake_http_probe({}))
    stand = _https_stand(5010)
    status = check_stand(stand)
    assert status.http == ProbeState.OK
    assert "http_reason" not in status.details


def test_check_stand_collects_reasons_from_several_probes(monkeypatch):
    """
    Пишущих проб теперь несколько — проверяем, что сборка деталей в вызывающем
    потоке не теряет ни одну причину.
    """
    exc = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))
    port = _find_closed_port()
    stand = Stand(
        name="both",
        stand_dir="/opt/x",
        stand_host="127.0.0.1",
        stand_port=port,
        db_host="",
        db_port=0,
    )
    status = check_stand(stand)
    assert "http_reason" in status.details
    assert "redis_reason" in status.details


# --- проба Redis: поля модели, фолбэк на extra, причина (GAP-003) -----------


@contextlib.contextmanager
def _listening_port():
    """Занимает свободный порт слушающим сокетом и отдаёт его номер."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def _redis_stand(**kwargs) -> Stand:
    """Стенд без процесса/HTTP/БД — в фокусе теста только проба Redis."""
    return Stand(
        name="redis",
        stand_dir="/opt/x",
        stand_host="",
        stand_port=0,
        db_host="",
        db_port=0,
        **kwargs,
    )


def test_probe_redis_ok_from_model_fields():
    with _listening_port() as port:
        status = check_stand(_redis_stand(redis_host="127.0.0.1", redis_port=port))
        assert status.redis == ProbeState.OK
        assert "redis_reason" not in status.details


def test_probe_redis_ok_from_extra_fallback():
    """Старые реестры держали адрес Redis в нетипизированном extra — они обязаны работать."""
    with _listening_port() as port:
        stand = _redis_stand(extra={"redis_host": "127.0.0.1", "redis_port": str(port)})
        status = check_stand(stand)
        assert status.redis == ProbeState.OK


def test_probe_redis_model_fields_win_over_extra(monkeypatch):
    seen = {}

    def _fake_tcp_open(host, port, **kwargs):
        seen["endpoint"] = (host, port)
        return True

    monkeypatch.setattr("standkit.health.tcp_open", _fake_tcp_open)
    stand = _redis_stand(
        redis_host="10.0.0.10",
        redis_port=6379,
        extra={"redis_host": "127.0.0.1", "redis_port": 6380},
    )
    status = check_stand(stand)
    assert seen["endpoint"] == ("10.0.0.10", 6379)
    assert status.redis == ProbeState.OK


def test_probe_redis_unknown_explains_not_configured():
    status = check_stand(_redis_stand())
    assert status.redis == ProbeState.UNKNOWN
    assert status.details["redis_reason"] == (
        "адрес Redis не задан в реестре (redis_host/redis_port)"
    )


def test_probe_redis_down_explains_endpoint_and_probe_host():
    port = _find_closed_port()
    status = check_stand(_redis_stand(redis_host="127.0.0.1", redis_port=port))
    assert status.redis == ProbeState.DOWN
    reason = status.details["redis_reason"]
    assert f"Redis не отвечает на 127.0.0.1:{port}" in reason
    assert "с хоста, где выполняется проба" in reason


def test_probe_redis_garbage_port_is_unknown_not_crash():
    """Мусор в порту — «не задан», а не исключение внутри пробы."""
    stand = _redis_stand(extra={"redis_host": "127.0.0.1", "redis_port": "шесть тысяч"})
    status = check_stand(stand)
    assert status.redis == ProbeState.UNKNOWN
    assert "не задан" in status.details["redis_reason"]
