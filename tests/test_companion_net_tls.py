# -*- coding: utf-8 -*-
"""Тесты standkit_companion.net_tls — TLS-доверие канала обновлений.

Модуль — синхронная копия `net_tls` клиентского MCP-сервера (пакет stdlib-only и не может
его импортировать), поэтому здесь проверяется то, что обязано совпадать в обеих копиях:
вшитые корни ISRG, их отпечатки и API. Сеть не нужна.
"""

from __future__ import annotations

import hashlib
import ssl
import urllib.error
import urllib.request

import pytest

from standkit_companion import net_tls
from standkit_companion.backend import BackendClient

ISRG_X1_SHA256 = "96bcec06264976f37460779acf28c5a7cfe8a3c0aae11a8ffcee05c0bddf08c6"
ISRG_X2_SHA256 = "69729b8e15a86efc177a57afb7171dfc64add28c2fca8cf1507e34453ccb1470"


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.delenv(net_tls.ENV_EXTRA_CA, raising=False)
    net_tls.reset_cache()
    yield
    net_tls.reset_cache()


def _der_sha256(pem):
    return hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()


def test_fingerprint_constants_match_expected():
    assert net_tls.ISRG_ROOT_X1_SHA256 == ISRG_X1_SHA256
    assert net_tls.ISRG_ROOT_X2_SHA256 == ISRG_X2_SHA256


def test_embedded_pem_parses_and_matches_fingerprints():
    assert _der_sha256(net_tls.ISRG_ROOT_X1_PEM) == ISRG_X1_SHA256
    assert _der_sha256(net_tls.ISRG_ROOT_X2_PEM) == ISRG_X2_SHA256


def test_embedded_roots_table_is_consistent():
    names = [name for name, _pem, _sha in net_tls.EMBEDDED_ROOTS]
    assert names == ["ISRG Root X1", "ISRG Root X2"]
    for _name, pem, sha in net_tls.EMBEDDED_ROOTS:
        assert _der_sha256(pem) == sha


def test_tls_context_verifies_and_is_cached():
    ctx = net_tls.tls_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert net_tls.tls_context() is ctx


def test_embedded_only_context_holds_exactly_the_two_roots():
    ctx = net_tls.embedded_only_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    shas = {hashlib.sha256(ssl.PEM_cert_to_DER_cert(ssl.DER_cert_to_PEM_cert(
        c))).hexdigest() for c in ctx.get_ca_certs(binary_form=True)}
    assert shas == {ISRG_X1_SHA256, ISRG_X2_SHA256}


def test_bad_extra_ca_file_is_not_fatal(monkeypatch, tmp_path):
    bad = tmp_path / "not_a_pem.pem"
    bad.write_text("not a certificate", encoding="utf-8")
    monkeypatch.setenv(net_tls.ENV_EXTRA_CA, str(bad))
    net_tls.reset_cache()
    ctx = net_tls.tls_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert net_tls.last_extra_ca_error()


def test_urlopen_passes_context_and_timeout(monkeypatch):
    seen = {}

    def fake(req, **kw):
        seen.update(kw)
        return "resp"

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    assert net_tls.urlopen("https://example.invalid/", timeout=7) == "resp"
    assert seen["context"] is net_tls.tls_context()
    assert seen["timeout"] == 7


def test_urlopen_explicit_context_wins(monkeypatch):
    seen = {}
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, **kw: seen.update(kw))
    mine = ssl.create_default_context()
    net_tls.urlopen("https://example.invalid/", context=mine)
    assert seen["context"] is mine


def test_is_certificate_verify_failed_walks_the_chain():
    inner = ssl.SSLCertVerificationError("CERTIFICATE_VERIFY_FAILED: unable to get local issuer")
    outer = urllib.error.URLError(inner)
    assert net_tls.is_certificate_verify_failed(outer) is True
    assert net_tls.is_certificate_verify_failed(OSError("connection refused")) is False


def test_probe_tls_never_raises_on_closed_port():
    res = net_tls.probe_tls("127.0.0.1", 1, timeout=1)
    assert res["ok"] is False
    assert res["reason"] in ("unreachable", "timeout", "error", "handshake_failed")


def test_backend_client_opener_uses_net_tls_context():
    client = BackendClient("https://updates.example", "ENV", timeout=1.0)
    https = [h for h in client._opener.handlers
             if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1
    assert https[0]._context is net_tls.tls_context()
