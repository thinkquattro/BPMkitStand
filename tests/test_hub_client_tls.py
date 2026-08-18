"""
Тесты доверия к TLS-сертификату АГЕНТА в канале «хаб → агент»
(standkit_hub.client, GAP-008).

Проверяются три вещи:

1. как строится ssl-контекст по полям записи стенда (``agent_ca`` /
   ``agent_verify_tls``) — включая то, что для ``http://`` он не строится вовсе;
2. что параметры доверия реально доезжают из записи реестра до
   ``_agent_request``, а не теряются по дороге;
3. что отказ объясняется РЕЦЕПТОМ, а не голым «агент недоступен» с хвостом
   из ``_ssl.c``.

Живые сценарии (настоящий HTTPS-сервер с самоподписанным сертификатом)
проверяются без моков — ровно тот контур, из-за которого гэп и завели.
Сертификат для сервера ВШИТ константой в tests/test_health.py и переиспользуется
здесь: генерация сертификата на лету потребовала бы openssl/cryptography, а
инварианты проекта — stdlib-only и тесты, не зависящие от окружения машины.
Там, где живой сценарий воспроизвести нельзя (несовпадение имени в SAN), —
подменяется ``urllib.request.urlopen``, тем же приёмом, что в tests/test_health.py.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import shutil
import ssl
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import standkit_hub.client as client_module
from standkit.models import Stand, Transport
from standkit.registry import Registry
from standkit_hub.client import (
    FederatedClient,
    RemoteCallError,
    _agent_request,
    _agent_trust,
    _build_agent_ssl_context,
)

from tests.test_health import _SELF_SIGNED_PEM


AGENT_URL = "https://127.0.0.1:8765"


def _ca_file(tmp_path: Path) -> Path:
    """
    Кладёт на диск ТОЛЬКО сертификат из вшитой PEM-пары — именно так выглядит
    файл, который оператор кладёт в ``agent_ca`` (приватный ключ остаётся на
    хосте агента и хабу не нужен).
    """
    cert = _SELF_SIGNED_PEM.split("-----BEGIN PRIVATE KEY-----", 1)[0]
    path = tmp_path / "agent.crt"
    path.write_text(cert, encoding="ascii")
    return path


# --- построение ssl-контекста ------------------------------------------------


def test_context_is_none_for_plain_http():
    """На http контекст не нужен и не строится: TLS там нет вовсе."""
    assert _build_agent_ssl_context("http://127.0.0.1:8765/status") is None


def test_context_is_none_for_plain_http_even_with_ca(tmp_path):
    ca = _ca_file(tmp_path)
    assert _build_agent_ssl_context("http://127.0.0.1:8765/status", ca_file=str(ca)) is None


def test_context_is_none_for_https_without_ca_keeping_default_behaviour():
    """
    Штатный случай (доверенный сертификат) обязан остаться ровно прежним:
    ``None`` — это «поведение urllib по умолчанию», проверка по системному
    хранилищу.
    """
    assert _build_agent_ssl_context("https://127.0.0.1:8765/status") is None


def test_context_uses_cafile_when_agent_ca_is_set(tmp_path):
    ca = _ca_file(tmp_path)
    context = _build_agent_ssl_context("https://127.0.0.1:8765/status", ca_file=str(ca))
    assert isinstance(context, ssl.SSLContext)
    # Сертификат агента реально загружен в хранилище контекста...
    assert context.get_ca_certs(), "сертификат из agent_ca не попал в контекст"
    # ...и при этом проверка цепочки и имени осталась ВКЛЮЧЕННОЙ: agent_ca
    # добавляет доверие, а не отключает проверку.
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_context_disables_verification_when_agent_verify_tls_is_false():
    context = _build_agent_ssl_context("https://127.0.0.1:8765/status", verify=False)
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_NONE
    # check_hostname обязан сниматься ПЕРВЫМ (иначе ssl не даст выставить
    # CERT_NONE) — проверяем результат обоих присваиваний.
    assert context.check_hostname is False


def test_explicit_opt_out_wins_over_stale_agent_ca(tmp_path):
    """
    Оператор явно снял agent_verify_tls — соединение должно состояться, даже
    если в agent_ca осталась старая ссылка.
    """
    ca = _ca_file(tmp_path)
    context = _build_agent_ssl_context(
        "https://127.0.0.1:8765/status", ca_file=str(ca), verify=False
    )
    assert context.verify_mode == ssl.CERT_NONE


# --- параметры доверия доезжают из записи реестра ----------------------------


def test_agent_trust_reads_both_fields_from_stand():
    stand = Stand(
        name="remote",
        stand_dir="/opt/x",
        transport=Transport.AGENT,
        agent_url=AGENT_URL,
        agent_ca="/etc/standkit/agent.crt",
        agent_verify_tls=False,
    )
    assert _agent_trust(stand) == {"ca_file": "/etc/standkit/agent.crt", "verify": False}


def test_agent_trust_empty_ca_is_none_not_empty_string():
    stand = Stand(name="remote", stand_dir="/opt/x", transport=Transport.AGENT, agent_url=AGENT_URL)
    assert _agent_trust(stand) == {"ca_file": None, "verify": True}


def _registry_with_agent_stand(tmp_path: Path, **fields) -> Registry:
    record = {
        "transport": "agent",
        "stand_dir": "/opt/bpmsoft/remote",
        "agent_url": AGENT_URL,
        "agent_secret_ref": "standkit:remote:agent-token",
    }
    record.update(fields)
    path = tmp_path / "projects.json"
    path.write_text(
        json.dumps({"default": "remote", "projects": {"remote": record}}), encoding="utf-8"
    )
    return Registry.load(path)


def test_status_passes_trust_params_from_registry_record(tmp_path, monkeypatch):
    """
    Регрессия на самое обидное: поля в реестре есть, а до запроса не доезжают.
    """
    seen: dict = {}

    def _fake_agent_request(agent_url, path, token, **kwargs):
        seen.update(kwargs)
        return {"name": "remote", "process": "ok", "http": "ok"}

    monkeypatch.setattr(client_module, "_agent_request", _fake_agent_request)
    monkeypatch.setattr(client_module, "get_secret", lambda ref: "token-value")

    registry = _registry_with_agent_stand(
        tmp_path, agent_ca="/etc/standkit/agent.crt", agent_verify_tls=False
    )
    FederatedClient(registry).status("remote")

    assert seen["ca_file"] == "/etc/standkit/agent.crt"
    assert seen["verify"] is False


def test_logs_and_actions_pass_trust_params_too(tmp_path, monkeypatch):
    """Не только status: команды start/stop и хвост логов идут по тому же каналу."""
    seen: list[dict] = []

    def _fake_agent_request(agent_url, path, token, **kwargs):
        seen.append(kwargs)
        return {"lines": [], "pid": 42}

    monkeypatch.setattr(client_module, "_agent_request", _fake_agent_request)
    monkeypatch.setattr(client_module, "get_secret", lambda ref: "token-value")

    registry = _registry_with_agent_stand(tmp_path, agent_ca="/etc/standkit/agent.crt")
    client = FederatedClient(registry)
    client.logs("remote", n=10)
    client.start("remote")

    assert len(seen) == 2
    for kwargs in seen:
        assert kwargs["ca_file"] == "/etc/standkit/agent.crt"
        assert kwargs["verify"] is True


# --- диагнозы вместо «агент недоступен» --------------------------------------


def _urlopen_raising(exc: BaseException):
    """Фейковый urlopen, который всегда падает заданным исключением."""

    def _fake(req, timeout=None, context=None):
        raise exc

    return _fake


def test_cert_verify_failure_explains_and_recommends_agent_ca(monkeypatch):
    exc = urllib.error.URLError(ssl.SSLCertVerificationError("self-signed certificate"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))

    with pytest.raises(RemoteCallError) as excinfo:
        _agent_request(AGENT_URL, "/stand/remote/status", "token")

    detail = excinfo.value.detail
    assert detail.startswith("сертификат агента не доверенный")
    assert "self-signed certificate" in detail
    assert "укажите agent_ca" in detail


def test_hostname_mismatch_has_its_own_recipe(monkeypatch):
    """
    Другой отказ — другой рецепт: добавлять доверие бесполезно, если имя в
    agent_url просто не то, на которое выписан сертификат.
    """
    cause = ssl.SSLCertVerificationError(
        "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Hostname mismatch, "
        "certificate is not valid for 'example-host'. (_ssl.c:1007)"
    )
    cause.verify_message = "Hostname mismatch, certificate is not valid for 'example-host'."
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(urllib.error.URLError(cause)))

    with pytest.raises(RemoteCallError) as excinfo:
        _agent_request(AGENT_URL, "/stand/remote/status", "token")

    detail = excinfo.value.detail
    assert detail.startswith("имя в сертификате агента не совпадает с адресом")
    assert "CN/SAN" in detail
    assert "укажите agent_ca" not in detail


def test_missing_agent_ca_file_names_the_path(tmp_path):
    missing = tmp_path / "nope" / "agent.crt"

    with pytest.raises(RemoteCallError) as excinfo:
        _agent_request(AGENT_URL, "/stand/remote/status", "token", ca_file=str(missing))

    detail = excinfo.value.detail
    assert "не найден" in detail
    assert str(missing) in detail
    assert "agent_ca" in detail


def test_unreadable_agent_ca_file_names_the_path(tmp_path):
    """Файл есть, но это не PEM — путь всё равно обязан прозвучать."""
    garbage = tmp_path / "agent.crt"
    garbage.write_text("это не сертификат", encoding="utf-8")

    with pytest.raises(RemoteCallError) as excinfo:
        _agent_request(AGENT_URL, "/stand/remote/status", "token", ca_file=str(garbage))

    detail = excinfo.value.detail
    assert str(garbage) in detail
    assert "agent_ca" in detail


def test_unknown_network_error_is_not_swallowed(monkeypatch):
    """Неузнанный отказ уходит как раньше — настоящим текстом, без «улучшений»."""
    exc = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))

    with pytest.raises(RemoteCallError) as excinfo:
        _agent_request(AGENT_URL, "/stand/remote/status", "token")

    assert "Connection refused" in excinfo.value.detail
    assert "agent_ca" not in excinfo.value.detail


def test_http_error_detail_unchanged(monkeypatch):
    exc = urllib.error.HTTPError(AGENT_URL, 401, "Unauthorized", None, None)
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_raising(exc))

    with pytest.raises(RemoteCallError) as excinfo:
        _agent_request(AGENT_URL, "/stand/remote/status", "token")

    assert excinfo.value.detail == "HTTP 401"


# --- живой контур: агент с самоподписанным сертификатом ----------------------


@contextlib.contextmanager
def _self_signed_agent():
    """
    Поднимает локальный HTTPS-сервер, отвечающий как ``standkit_agent``
    (JSON на любой GET), с тем же вшитым самоподписанным сертификатом на
    127.0.0.1. Отдаёт порт и список увиденных Authorization-заголовков.
    """
    seen_auth: list[str] = []
    tmpdir = tempfile.mkdtemp()
    pem = Path(tmpdir) / "agent.pem"
    pem.write_text(_SELF_SIGNED_PEM, encoding="ascii")

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 — имя диктует BaseHTTPRequestHandler
            seen_auth.append(self.headers.get("Authorization", ""))
            payload = json.dumps({"name": "remote", "process": "ok", "http": "ok"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # тишина в выводе pytest
            return

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(pem))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], seen_auth
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_live_self_signed_agent_is_reachable_with_agent_ca(tmp_path, monkeypatch):
    """
    Критерий приёмки GAP-008: оператор указал agent_ca — и стенд за агентом с
    самоподписанным сертификатом виден из дашборда, без правки SSL_CERT_FILE и
    перезапуска процесса.
    """
    monkeypatch.setattr(client_module, "get_secret", lambda ref: "token-value")
    ca = _ca_file(tmp_path)

    with _self_signed_agent() as (port, seen_auth):
        registry = _registry_with_agent_stand(
            tmp_path,
            agent_url=f"https://127.0.0.1:{port}",
            agent_ca=str(ca),
        )
        status = FederatedClient(registry).status("remote")

    assert status.name == "remote"
    assert status.process.value == "ok"
    assert seen_auth == ["Bearer token-value"]


def test_live_self_signed_agent_is_refused_without_agent_ca(tmp_path, monkeypatch):
    """
    Вторая половина критерия приёмки: без явного разрешения неизвестный
    сертификат по-прежнему даёт отказ — но уже с рецептом, а не с хвостом
    из _ssl.c.
    """
    monkeypatch.setattr(client_module, "get_secret", lambda ref: "token-value")

    with _self_signed_agent() as (port, _seen_auth):
        registry = _registry_with_agent_stand(tmp_path, agent_url=f"https://127.0.0.1:{port}")
        with pytest.raises(RemoteCallError) as excinfo:
            FederatedClient(registry).status("remote")

    detail = excinfo.value.detail
    assert detail.startswith("сертификат агента не доверенный")
    assert "укажите agent_ca" in detail


def test_live_self_signed_agent_is_reachable_with_verification_disabled(tmp_path, monkeypatch):
    """Дев-контур: осознанно снятый agent_verify_tls тоже должен работать."""
    monkeypatch.setattr(client_module, "get_secret", lambda ref: "token-value")

    with _self_signed_agent() as (port, _seen_auth):
        registry = _registry_with_agent_stand(
            tmp_path,
            agent_url=f"https://127.0.0.1:{port}",
            agent_verify_tls=False,
        )
        status = FederatedClient(registry).status("remote")

    assert status.process.value == "ok"


def test_live_status_all_survives_untrusted_agent(tmp_path, monkeypatch):
    """
    Один недоверенный агент не роняет опрос всего реестра: причина оседает в
    ``details["error"]`` карточки стенда — там её и увидит оператор.
    """
    monkeypatch.setattr(client_module, "get_secret", lambda ref: "token-value")

    with _self_signed_agent() as (port, _seen_auth):
        registry = _registry_with_agent_stand(tmp_path, agent_url=f"https://127.0.0.1:{port}")
        statuses = FederatedClient(registry).status_all()

    assert "укажите agent_ca" in statuses["remote"].details["error"]
