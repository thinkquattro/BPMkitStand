"""
Тесты прод-харденинга headless-агента (standkit_agent.security / .audit).

Юнит-уровень: валидаторы/хелперы тестируются напрямую, без реальной сети и
без реальных TLS-сертификатов (для TLS проверяется только корректная
конфигурация SSLContext через фабрику — флаги minimum_version/verify_mode,
без хендшейка).
"""

from __future__ import annotations

import json
import ssl
import time

import pytest

from standkit_agent import audit as _audit
from standkit_agent import security


# --- 1. Fail-closed bind-проверка (без открытия сокета) ---


def test_loopback_without_tls_is_allowed():
    # Дефолтный dev-сценарий: loopback, открытый HTTP — разрешено.
    security.validate_bind_security("127.0.0.1", tls_enabled=False, insecure=False)
    security.validate_bind_security("localhost", tls_enabled=False, insecure=False)
    security.validate_bind_security("::1", tls_enabled=False, insecure=False)


def test_non_loopback_without_tls_refuses_to_start():
    with pytest.raises(security.InsecureBindError):
        security.validate_bind_security("0.0.0.0", tls_enabled=False, insecure=False)


def test_non_loopback_without_tls_but_with_explicit_insecure_flag_is_allowed():
    # Осознанный обход — не бросает, но вызывающая сторона (run_server)
    # обязана громко предупредить в stderr (проверяется отдельно на уровне
    # run_server/CLI, не входит в юнит-проверку чистой функции).
    security.validate_bind_security("0.0.0.0", tls_enabled=False, insecure=True)


def test_non_loopback_with_tls_is_allowed_without_insecure_flag():
    security.validate_bind_security("0.0.0.0", tls_enabled=True, insecure=False)


def test_is_loopback_host_recognizes_common_forms():
    assert security.is_loopback_host("127.0.0.1") is True
    assert security.is_loopback_host("127.0.0.5") is True
    assert security.is_loopback_host("localhost") is True
    assert security.is_loopback_host("LOCALHOST") is True
    assert security.is_loopback_host("::1") is True
    assert security.is_loopback_host("0.0.0.0") is False
    assert security.is_loopback_host("10.0.0.5") is False
    assert security.is_loopback_host("example.com") is False


# --- 2. hmac.compare_digest — поведенческая проверка через Authenticator ---


def test_authenticator_rejects_wrong_control_token():
    auth = security.Authenticator(control_token="correct-horse-battery-staple")
    assert auth.check("wrong-token") is None


def test_authenticator_accepts_correct_control_token():
    auth = security.Authenticator(control_token="correct-horse-battery-staple")
    assert auth.check("correct-horse-battery-staple") == security.SCOPE_CONTROL


def test_authenticator_rejects_empty_or_missing_token():
    auth = security.Authenticator(control_token="secret")
    assert auth.check(None) is None
    assert auth.check("") is None


def test_authenticator_without_configured_token_rejects_everything():
    auth = security.Authenticator(control_token=None, readonly_token=None)
    assert auth.check("anything") is None


# --- 3. Rate limiting / lockout по source-IP ---


def test_lockout_locks_ip_after_max_failures():
    lockout = security.LockoutTracker(max_failures=3, window_seconds=300.0)
    ip = "203.0.113.7"

    assert lockout.is_locked(ip) is False
    lockout.record_failure(ip)
    lockout.record_failure(ip)
    assert lockout.is_locked(ip) is False  # ещё не достигли порога
    lockout.record_failure(ip)
    assert lockout.is_locked(ip) is True  # 3-я неудача -> блокировка


def test_lockout_success_resets_failure_counter():
    lockout = security.LockoutTracker(max_failures=2, window_seconds=300.0)
    ip = "203.0.113.8"

    lockout.record_failure(ip)
    lockout.record_success(ip)
    lockout.record_failure(ip)
    assert lockout.is_locked(ip) is False  # счётчик был сброшен успехом


def test_lockout_expires_after_window():
    lockout = security.LockoutTracker(max_failures=1, window_seconds=0.05)
    ip = "203.0.113.9"

    lockout.record_failure(ip)
    assert lockout.is_locked(ip) is True
    time.sleep(0.1)
    assert lockout.is_locked(ip) is False  # окно истекло


def test_lockout_is_per_ip():
    lockout = security.LockoutTracker(max_failures=1, window_seconds=300.0)
    lockout.record_failure("203.0.113.10")
    assert lockout.is_locked("203.0.113.10") is True
    assert lockout.is_locked("203.0.113.11") is False


# --- 4. Скоупы control/readonly ---


def test_readonly_scope_cannot_perform_control_actions():
    for action in ("start", "stop", "restart"):
        assert security.Authenticator.scope_allows(security.SCOPE_READONLY, action) is False


def test_readonly_scope_can_perform_read_actions():
    for action in ("stands", "status", "logs"):
        assert security.Authenticator.scope_allows(security.SCOPE_READONLY, action) is True


def test_control_scope_can_perform_everything():
    for action in ("start", "stop", "restart", "stands", "status", "logs"):
        assert security.Authenticator.scope_allows(security.SCOPE_CONTROL, action) is True


def test_none_scope_cannot_perform_anything():
    for action in ("start", "stop", "restart", "stands", "status", "logs"):
        assert security.Authenticator.scope_allows(None, action) is False


def test_authenticator_distinguishes_control_and_readonly_tokens():
    auth = security.Authenticator(control_token="control-tok", readonly_token="ro-tok")
    assert auth.check("control-tok") == security.SCOPE_CONTROL
    assert auth.check("ro-tok") == security.SCOPE_READONLY
    assert auth.check("neither") is None


# --- 5. Input-hardening: лимит тела запроса, кап на n логов, имя стенда ---


def test_validate_content_length_accepts_within_limit():
    assert security.validate_content_length("100", max_bytes=1024) == 100
    assert security.validate_content_length(None, max_bytes=1024) == 0
    assert security.validate_content_length("", max_bytes=1024) == 0


def test_validate_content_length_rejects_over_limit():
    with pytest.raises(ValueError):
        security.validate_content_length(str(64 * 1024 + 1), max_bytes=64 * 1024)


def test_validate_content_length_rejects_negative_and_garbage():
    with pytest.raises(ValueError):
        security.validate_content_length("-1", max_bytes=1024)
    with pytest.raises(ValueError):
        security.validate_content_length("not-a-number", max_bytes=1024)


def test_clamp_logs_n_caps_huge_value():
    assert security.clamp_logs_n("999999999", max_n=10_000) == 10_000


def test_clamp_logs_n_passes_through_small_value():
    assert security.clamp_logs_n("50", max_n=10_000) == 50


def test_clamp_logs_n_rejects_negative():
    with pytest.raises(ValueError):
        security.clamp_logs_n("-5", max_n=10_000)


def test_clamp_logs_n_rejects_garbage():
    with pytest.raises(ValueError):
        security.clamp_logs_n("abc", max_n=10_000)


def test_validate_stand_name_accepts_typical_names():
    assert security.validate_stand_name("demo9") is True
    assert security.validate_stand_name("my-stand_01.local") is True


def test_validate_stand_name_rejects_path_traversal_and_empty():
    assert security.validate_stand_name("") is False
    assert security.validate_stand_name("../etc/passwd") is False
    assert security.validate_stand_name("a/b") is False
    assert security.validate_stand_name("a b") is False
    assert security.validate_stand_name("a" * 200) is False


# --- TLS: конфигурация SSLContext через фабрику, без реального хендшейка ---


def test_tls_hardening_sets_minimum_tls_version():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    security._apply_tls_hardening(ctx, require_client_cert=False)
    assert ctx.minimum_version == ssl.TLSVersion.TLSv1_2


def test_tls_hardening_without_client_ca_does_not_require_client_cert():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    security._apply_tls_hardening(ctx, require_client_cert=False)
    assert ctx.verify_mode == ssl.CERT_NONE


def test_tls_hardening_with_client_ca_requires_client_cert():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    security._apply_tls_hardening(ctx, require_client_cert=True)
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_peer_identity_from_cert_extracts_common_name():
    peer_cert = {"subject": ((("commonName", "agent-client-01"),),)}
    assert security.peer_identity_from_cert(peer_cert) == "agent-client-01"


def test_peer_identity_from_cert_none_when_no_cert():
    assert security.peer_identity_from_cert(None) is None
    assert security.peer_identity_from_cert({}) is None


# --- 6. Аудит-лог: запись без токена, структурные поля ---


def test_audit_event_writes_json_line_without_token(tmp_path):
    log_path = tmp_path / "audit.log"
    logger = _audit.build_audit_logger(log_path)

    secret_token = "top-secret-control-token-should-never-leak"
    _audit.audit_event(
        logger,
        src_ip="192.0.2.1",
        identity=security.SCOPE_CONTROL,
        method="POST",
        path="/stand/demo/restart",
        action="restart",
        result="ok",
        code=200,
    )
    _audit.audit_event(
        logger,
        src_ip="192.0.2.2",
        identity="-",
        method="POST",
        path="/stand/demo/restart",
        action="restart",
        result="denied",
        code=401,
    )
    for handler in logger.handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert secret_token not in content  # токен никогда не участвует в записи

    lines = [line for line in content.splitlines() if line.strip()]
    assert len(lines) == 2

    ok_entry = json.loads(lines[0])
    assert ok_entry["result"] == "ok"
    assert ok_entry["code"] == 200
    assert ok_entry["identity"] == security.SCOPE_CONTROL
    assert ok_entry["action"] == "restart"
    assert "ts" in ok_entry and "src_ip" in ok_entry

    denied_entry = json.loads(lines[1])
    assert denied_entry["result"] == "denied"
    assert denied_entry["code"] == 401
    assert denied_entry["identity"] == "-"


def test_audit_event_never_raises_on_broken_logger():
    class _BrokenLogger:
        def info(self, *_args, **_kwargs):
            raise OSError("disk full")

    # Не должно бросать исключение наружу — аудит не может ронять обработку запроса.
    _audit.audit_event(
        _BrokenLogger(),
        src_ip="192.0.2.3",
        identity="-",
        method="GET",
        path="/stands",
        action="stands",
        result="ok",
        code=200,
    )
