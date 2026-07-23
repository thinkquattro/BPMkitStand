"""
Точка входа агента: ``python -m standkit_agent`` (или консольный скрипт
``standkit-agent`` после установки пакета).

БЕЗОПАСНОСТЬ (см. также standkit_agent/security.py, README.md → раздел
"Безопасность"): агент управляет жизненным циклом процессов на хосте стенда
(start/stop/restart) — это RCE-поверхность по дизайну. Secure-defaults:
    - ``--host`` по умолчанию ``127.0.0.1`` (НЕ 0.0.0.0);
    - non-loopback host без TLS ОТКАЗЫВАЕТСЯ стартовать (fail-closed),
      если явно не передан ``--insecure`` (только dev/тест, НЕ прод);
    - Bearer-токен сравнивается через hmac.compare_digest (защита от
      timing-атак);
    - lockout по source-IP после серии неудачных аутентификаций;
    - append-only JSON-lines аудит-лог всех запросов (без токенов).

Примеры:
    # dev, loopback, без TLS (secure default при отсутствии удалённого доступа)
    python -m standkit_agent --registry ./projects.json \\
        --token-ref standkit:my-stand:agent-token

    # прод, удалённый доступ, TLS + mTLS
    python -m standkit_agent --host 0.0.0.0 --port 8765 \\
        --registry /opt/standkit/projects.json \\
        --token-ref standkit:my-stand:agent-token \\
        --readonly-token-ref standkit:my-stand:agent-readonly-token \\
        --tls-cert /etc/standkit/agent.crt --tls-key /etc/standkit/agent.key \\
        --tls-client-ca /etc/standkit/clients-ca.crt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from standkit.registry import Registry, default_registry_path
from standkit.secrets import SecretError, get_secret
from standkit_agent.security import (
    Authenticator,
    InsecureBindError,
    LockoutTracker,
    DEFAULT_LOCKOUT_MAX_FAILURES,
    DEFAULT_LOCKOUT_WINDOW_SECONDS,
    validate_bind_security,
)
from standkit_agent.server import run_server


def _resolve_token(token_ref: str, *, label: str) -> str:
    try:
        return get_secret(token_ref)
    except SecretError as exc:
        print(f"[standkit-agent] ОШИБКА: не удалось получить секрет {label} ({token_ref!r}): {exc}", file=sys.stderr)
        sys.exit(1)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="standkit-agent",
        description=(
            "Headless-агент standkit для хоста стенда. RCE-поверхность по дизайну "
            "(start/stop/restart процессов) — secure-defaults: loopback-only без TLS, "
            "fail-closed на non-loopback без TLS. См. README.md → раздел «Безопасность»."
        ),
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="адрес, на котором слушать (по умолчанию 127.0.0.1 — loopback-only, secure default)",
    )
    parser.add_argument("--port", type=int, default=8765, help="порт (по умолчанию 8765)")
    parser.add_argument(
        "--registry",
        default=None,
        help=(
            "путь к реестру стендов, которыми управляет этот агент (по умолчанию — "
            "тот же реестр, что резолвит BPMkit MCP: env BPMSOFT_PROJECTS_FILE, "
            "иначе %%APPDATA%%\\BPMkit\\projects.json / ~/.config/BPMkit/projects.json, "
            "иначе ./projects.json; см. standkit.registry.default_registry_path)"
        ),
    )
    parser.add_argument(
        "--token-ref",
        required=True,
        help="ссылка на секрет control-токена (start/stop/restart + read), Secret-first (см. standkit.secrets)",
    )
    parser.add_argument(
        "--readonly-token-ref",
        default=None,
        help="ссылка на секрет readonly-токена (только GET /stands, /status, /logs) — опционально",
    )
    parser.add_argument("--run-dir", default=None, help="каталог pid-файлов (по умолчанию ~/.standkit/run)")
    parser.add_argument("--log-dir", default=None, help="каталог лог-файлов (по умолчанию ~/.standkit/logs)")
    parser.add_argument(
        "--audit-log",
        default=None,
        help="путь к JSON-lines аудит-логу (по умолчанию ~/.standkit/audit.log)",
    )
    parser.add_argument(
        "--tls-cert",
        default=None,
        help="путь к серверному TLS-сертификату (PEM). Требуется вместе с --tls-key для включения TLS",
    )
    parser.add_argument(
        "--tls-key",
        default=None,
        help="путь к приватному ключу серверного TLS-сертификата (PEM)",
    )
    parser.add_argument(
        "--tls-client-ca",
        default=None,
        help="путь к CA (PEM) для проверки клиентских сертификатов — включает mTLS "
        "(CERT_REQUIRED); без этого флага TLS работает без проверки клиента",
    )
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="ОСОЗНАННЫЙ обход fail-closed проверки: разрешить открытый HTTP на "
        "non-loopback адресе без TLS. ТОЛЬКО для dev/тестовых сценариев за "
        "изолированным периметром — НЕ для прода/недоверенной сети. Выводит "
        "громкое предупреждение в stderr",
    )
    parser.add_argument(
        "--lockout-max-failures",
        type=int,
        default=DEFAULT_LOCKOUT_MAX_FAILURES,
        help=f"порог неудачных аутентификаций с одного IP до блокировки (по умолчанию {DEFAULT_LOCKOUT_MAX_FAILURES})",
    )
    parser.add_argument(
        "--lockout-window",
        type=float,
        default=DEFAULT_LOCKOUT_WINDOW_SECONDS,
        help=f"окно (сек) для подсчёта неудачных аутентификаций (по умолчанию {DEFAULT_LOCKOUT_WINDOW_SECONDS:.0f})",
    )
    args = parser.parse_args(argv)

    registry_path = Path(args.registry) if args.registry else default_registry_path()
    registry = Registry.load(registry_path)

    control_token = _resolve_token(args.token_ref, label="control-токена агента")
    readonly_token = (
        _resolve_token(args.readonly_token_ref, label="readonly-токена агента")
        if args.readonly_token_ref
        else None
    )
    authenticator = Authenticator(control_token, readonly_token)
    lockout = LockoutTracker(max_failures=args.lockout_max_failures, window_seconds=args.lockout_window)

    run_dir = Path(args.run_dir) if args.run_dir else None
    log_dir = Path(args.log_dir) if args.log_dir else None
    audit_log_path = Path(args.audit_log) if args.audit_log else None

    tls_enabled = bool(args.tls_cert and args.tls_key)

    # Fail-closed bind-проверка ДО любого вывода "слушаю ..." — если конфигурация
    # небезопасна, агент не должен даже создавать впечатление, что он стартовал.
    try:
        validate_bind_security(args.host, tls_enabled=tls_enabled, insecure=args.insecure)
    except InsecureBindError as exc:
        print(f"[standkit-agent] {exc}", file=sys.stderr)
        return 1

    print(
        f"[standkit-agent] слушаю {args.host}:{args.port} "
        f"(tls={'on' if tls_enabled else 'off'}"
        f"{'+mtls' if tls_enabled and args.tls_client_ca else ''}), "
        f"реестр={registry_path}, стендов={len(registry)}, readonly-токен={'да' if readonly_token else 'нет'}"
    )

    try:
        run_server(
            registry,
            authenticator,
            host=args.host,
            port=args.port,
            run_dir=run_dir,
            log_dir=log_dir,
            tls_cert=args.tls_cert,
            tls_key=args.tls_key,
            tls_client_ca=args.tls_client_ca,
            insecure=args.insecure,
            lockout=lockout,
            audit_log_path=audit_log_path,
        )
    except InsecureBindError as exc:
        print(f"[standkit-agent] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
