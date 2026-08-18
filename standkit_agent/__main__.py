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

ЭКСПЛУАТАЦИЯ: перед открытием сокета проверяются рабочие каталоги агента
(run/logs/audit) — см. ``preflight_paths``. Недоступный на запись путь — это
отказ старта ОДНОЙ понятной строкой в stderr и код возврата 1, а не голый
traceback посреди рабочего цикла (см. docs/GAPs/GAP-007). Строка «слушаю …»
печатается из колбэка ``on_ready`` уже ПОСЛЕ фактической привязки сокета — по
журналу службы её наличие означает «агент встал на порт», а не «агент дошёл
до попытки старта».

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
import errno
import getpass
import os
import sys
import tempfile
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

# Имя каталога, в котором живут дефолтные run/logs/audit агента внутри $HOME.
_AGENT_HOME_DIRNAME = ".standkit"

# "Порт занят" / "нет прав на привязку" — коды отличаются между POSIX и Windows
# (там errno сокетных ошибок — это WSA*-коды: 10048/10013, а не 98/13).
# getattr с фолбэком: на POSIX WSA-констант в модуле errno нет.
_ADDR_IN_USE_ERRNOS = frozenset({errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)})
_BIND_DENIED_ERRNOS = frozenset({errno.EACCES, getattr(errno, "WSAEACCES", errno.EACCES)})


class StartupPathError(Exception):
    """
    Отказ старта агента: рабочие каталоги (run/logs/audit) непригодны.

    Сделано по образцу ``InsecureBindError`` (standkit_agent.security):
    бросается ДО открытия сокета и ДО печати «слушаю …», перехватывается в
    ``main()`` и превращается в ОДНУ строку в stderr плюс код возврата 1.
    Никакого traceback: в ``journalctl -u standkit-agent`` оператор должен
    видеть диагноз и подсказку, какой флаг задать, а не стек вызовов
    ``logging.FileHandler`` (см. docs/GAPs/GAP-007).
    """


def _current_user_name() -> str:
    """
    Имя текущего пользователя для текста отказа — «кому именно не хватает прав».

    ``getpass.getuser()`` кроссплатформенный: на Windows берёт USERNAME, на
    POSIX — LOGNAME/USER/LNAME/USERPROFILE, иначе ``pwd`` по euid. Но именно в
    служебном окружении он умеет падать: у сервисного аккаунта может не быть
    записи в /etc/passwd и не выставлено ни одной из переменных (в Python 3.13+
    это OSError, раньше — KeyError). Диагностическое сообщение из-за этого
    падать не должно — поэтому широкий except и фолбэк на uid.
    """
    try:
        return getpass.getuser()
    except Exception:  # определение имени пользователя не должно ронять диагностику
        for var in ("USER", "USERNAME", "LOGNAME"):
            value = os.environ.get(var)
            if value:
                return value
        geteuid = getattr(os, "geteuid", None)  # на Windows такого вызова нет
        return f"uid={geteuid()}" if geteuid else "неизвестен"


def _probe_writable(directory: Path) -> str | None:
    """
    Проверяет запись в каталог РЕАЛЬНОЙ попыткой создать и удалить временный
    файл. Возвращает ``None``, если писать можно, иначе — текст причины.

    Почему не ``os.access(path, os.W_OK)``: он врёт ровно в тех случаях, ради
    которых мы и делаем preflight. Под root он вернёт True даже для ``chmod
    0500`` (CAP_DAC_OVERRIDE), на Windows он смотрит на read-only атрибут и
    игнорирует ACL, а на сетевых/FUSE-ФС (NFS, SMB, overlay в контейнере)
    отвечает по битам режима, которые сервер может не соблюдать. Единственная
    честная проверка «смогу ли я сюда писать» — попробовать записать.
    ``NamedTemporaryFile`` удаляет файл на ``close()`` (на Windows — через
    O_TEMPORARY), поэтому после preflight мусора не остаётся.
    """
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".standkit-preflight-", suffix=".tmp"):
            pass
    except OSError as exc:
        return exc.strerror or str(exc)
    return None


def _nearest_existing_parent(path: Path) -> Path:
    """
    Ближайший существующий предок пути — цель проверки для ещё не созданного
    каталога: агент создаст его сам (``mkdir(parents=True)`` в lifecycle/audit),
    но только если в этого предка можно писать.
    """
    parents = list(path.parents)
    for candidate in parents:
        if candidate.exists():
            return candidate
    # До корня дойти и не найти существующего предка практически невозможно,
    # но возвращать что-то осмысленное всё равно надо.
    return parents[-1] if parents else path


def _check_writable_dir(path: Path, *, what: str, flag: str) -> None:
    """
    Один каталог агента: существует и доступен на запись — либо может быть
    создан. Любой отказ — ``StartupPathError`` с полным путём, именем
    пользователя и подсказкой, какой флаг задать.
    """
    user = _current_user_name()
    if path.exists():
        if not path.is_dir():
            raise StartupPathError(
                f"Отказ старта: {what} {path} — не каталог (по этому пути лежит файл) — "
                f"задайте {flag} или уберите файл"
            )
        if _probe_writable(path) is None:
            return
        raise StartupPathError(
            f"Отказ старта: {what} {path} недоступен на запись пользователю {user} — "
            f"задайте {flag} или выдайте права"
        )

    parent = _nearest_existing_parent(path)
    if not parent.is_dir():
        raise StartupPathError(
            f"Отказ старта: {what} {path} не существует и не может быть создан пользователем "
            f"{user}: {parent} — не каталог, а файл — задайте {flag}"
        )
    reason = _probe_writable(parent)
    if reason is None:
        return
    raise StartupPathError(
        f"Отказ старта: {what} {path} не существует и не может быть создан пользователем "
        f"{user}: нет прав на запись в {parent} ({reason}) — задайте {flag} или выдайте права"
    )


def preflight_paths(*, run_dir: Path, log_dir: Path, audit_log_path: Path) -> None:
    """
    Проверяет, что все каталоги агента доступны на запись, ДО старта.

    Проверяются ровно те три пути, которые агент реально использует:
    ``run_dir`` (pid-файлы, ``standkit.lifecycle``), ``log_dir`` (лог-файлы
    стендов) и каталог файла аудита (``standkit_agent.audit``). Каждый из них
    создаётся лениво — в момент первого start/stop и первой аудит-записи, то
    есть уже ПОСЛЕ того, как агент напечатал «слушаю …» и принял запрос.
    Именно поэтому проверка вынесена в старт: отказ должен случиться до
    открытия сокета, пока оператор ещё смотрит на вывод запуска.

    Ничего не создаёт (в т.ч. не создаёт ``~/.standkit``) — только проверяет.
    Бросает ``StartupPathError`` с готовым текстом для stderr.
    """
    _check_writable_dir(Path(run_dir), what="каталог pid-файлов", flag="--run-dir")
    _check_writable_dir(Path(log_dir), what="каталог логов", flag="--log-dir")

    audit_path = Path(audit_log_path)
    _check_writable_dir(audit_path.parent, what="каталог аудит-лога", flag="--audit-log")
    # Сам файл аудита мог быть создан заранее другим пользователем (типовой
    # случай: каталог подготовили из-под root, а служба ходит из-под standkit).
    # Каталог при этом писуч, а вот дописать в файл нельзя — logging.FileHandler
    # упадёт на первом же запросе, поэтому проверяем и файл тоже.
    if audit_path.exists():
        user = _current_user_name()
        if audit_path.is_dir():
            raise StartupPathError(
                f"Отказ старта: аудит-лог {audit_path} — не файл, а каталог — задайте --audit-log"
            )
        try:
            with open(audit_path, "a", encoding="utf-8"):
                pass
        except OSError:
            raise StartupPathError(
                f"Отказ старта: аудит-лог {audit_path} недоступен на запись пользователю {user} — "
                "задайте --audit-log или выдайте права"
            ) from None


def resolve_agent_paths(
    *,
    run_dir: str | None,
    log_dir: str | None,
    audit_log: str | None,
) -> tuple[Path, Path, Path]:
    """
    Разворачивает аргументы CLI в ФАКТИЧЕСКИЕ пути, которыми будет пользоваться
    агент (run-каталог, каталог логов, файл аудита).

    Зачем резолвить здесь, а не ниже по стеку: дефолты ``~/.standkit/run``,
    ``~/.standkit/logs`` и ``~/.standkit/audit.log`` живут в
    ``standkit.lifecycle._DEFAULT_RUN_DIR`` / ``_DEFAULT_LOG_DIR`` и
    ``standkit_agent.audit.DEFAULT_AUDIT_LOG_PATH`` и подставляются в момент
    ИСПОЛЬЗОВАНИЯ. Проверять на старте надо не «то, что передали», а «то, что
    будет использовано», поэтому дефолт разворачивается до preflight и дальше
    уходит в ``run_server`` уже развёрнутым — проверенный путь и используемый
    путь обязаны быть одним и тем же объектом.

    Значения совпадают с константами ниже по стеку бит-в-бит (проверяется
    тестом ``tests/test_agent_startup_preflight.py``). Импортировать сами
    константы нельзя: они приватные и вычисляются один раз на импорте модуля,
    а нам нужен ``Path.home()`` на момент старта процесса.

    Отдельная ветка — служебный запуск: у аккаунта, созданного с
    ``useradd --no-create-home``, домашнего каталога нет вообще. Создавать
    ``~/.standkit`` в такой ситуации бессмысленно (и вредно: каталог уедет в
    несуществующий или чужой $HOME) — вместо этого сразу говорим, какие флаги
    обязательны.
    """
    if run_dir and log_dir and audit_log:
        # Все три пути заданы явно — $HOME вообще не участвует (штатный режим
        # systemd-юнита, см. standkit_agent/deploy/standkit-agent.service).
        return Path(run_dir), Path(log_dir), Path(audit_log)

    hint = (
        "при запуске под сервисным аккаунтом (useradd --no-create-home, systemd) "
        "задайте все три пути явно: --run-dir, --log-dir и --audit-log"
    )
    try:
        home = Path.home()
    except RuntimeError as exc:
        raise StartupPathError(
            f"Отказ старта: не удалось определить домашний каталог пользователя "
            f"{_current_user_name()} ({exc}), дефолты ~/.standkit/run, ~/.standkit/logs и "
            f"~/.standkit/audit.log неприменимы — {hint}"
        ) from None
    if not home.is_dir():
        raise StartupPathError(
            f"Отказ старта: домашний каталог {home} недоступен (не существует или не каталог), "
            f"поэтому дефолты ~/.standkit/run, ~/.standkit/logs и ~/.standkit/audit.log "
            f"нерабочие — {hint}"
        )

    base = home / _AGENT_HOME_DIRNAME
    return (
        Path(run_dir) if run_dir else base / "run",
        Path(log_dir) if log_dir else base / "logs",
        Path(audit_log) if audit_log else base / "audit.log",
    )


def _describe_startup_oserror(exc: OSError, *, host: str, port: int) -> str:
    """
    Текст отказа для ``OSError`` на фазе привязки/старта сервера.

    Самый частый случай эксплуатации — ``[Errno 98] Address already in use``
    (перезапуск службы, пока старый процесс ещё держит порт). Оператору нужна
    строка «порт занят», а не traceback из глубины ``socketserver``.
    """
    if exc.errno in _ADDR_IN_USE_ERRNOS:
        return (
            f"Отказ старта: не удалось привязаться к {host}:{port} — порт уже занят другим "
            "процессом (возможно, агент уже запущен); освободите порт или задайте другой через --port"
        )
    if exc.errno in _BIND_DENIED_ERRNOS:
        return (
            f"Отказ старта: не удалось привязаться к {host}:{port} — нет прав на привязку к этому "
            "порту (порты меньше 1024 требуют привилегий); задайте порт из непривилегированного "
            "диапазона через --port"
        )
    return f"Отказ старта: не удалось запустить сервер на {host}:{port}: {exc}"


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
    parser.add_argument(
        "--run-dir",
        default=None,
        help="каталог pid-файлов (по умолчанию ~/.standkit/run); под сервисным аккаунтом без $HOME задавать обязательно",
    )
    parser.add_argument(
        "--log-dir",
        default=None,
        help="каталог лог-файлов (по умолчанию ~/.standkit/logs); под сервисным аккаунтом без $HOME задавать обязательно",
    )
    parser.add_argument(
        "--audit-log",
        default=None,
        help="путь к JSON-lines аудит-логу (по умолчанию ~/.standkit/audit.log); "
        "под сервисным аккаунтом без $HOME задавать обязательно",
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

    tls_enabled = bool(args.tls_cert and args.tls_key)

    # Fail-closed bind-проверка ДО любого вывода "слушаю ..." — если конфигурация
    # небезопасна, агент не должен даже создавать впечатление, что он стартовал.
    try:
        validate_bind_security(args.host, tls_enabled=tls_enabled, insecure=args.insecure)
    except InsecureBindError as exc:
        print(f"[standkit-agent] {exc}", file=sys.stderr)
        return 1

    # Preflight рабочих каталогов — тоже ДО строки "слушаю ...". Порядок здесь
    # содержательный: сначала отказ по безопасности, затем отказ по путям, и
    # только потом любое сообщение, из которого оператор мог бы заключить, что
    # агент поднялся.
    try:
        run_dir, log_dir, audit_log_path = resolve_agent_paths(
            run_dir=args.run_dir, log_dir=args.log_dir, audit_log=args.audit_log
        )
        preflight_paths(run_dir=run_dir, log_dir=log_dir, audit_log_path=audit_log_path)
    except StartupPathError as exc:
        print(f"[standkit-agent] {exc}", file=sys.stderr)
        return 1

    def _announce_listening() -> None:
        """
        «слушаю …» — ТОЛЬКО по факту привязки сокета.

        Печатается из колбэка ``on_ready`` внутри ``run_server`` (после
        bind/listen и, при TLS, после ``wrap_socket``), а не перед вызовом:
        на занятом порту оператор получал в stdout бодрое «слушаю
        127.0.0.1:8765», а следом в stderr — «порт уже занят», и по журналу
        нельзя было понять, поднялся агент или нет (GAP-007). Текст и порядок
        полей строки не менялись — изменился только момент печати.
        """
        print(
            f"[standkit-agent] слушаю {args.host}:{args.port} "
            f"(tls={'on' if tls_enabled else 'off'}"
            f"{'+mtls' if tls_enabled and args.tls_client_ca else ''}), "
            f"реестр={registry_path}, стендов={len(registry)}, "
            f"readonly-токен={'да' if readonly_token else 'нет'}"
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
            on_ready=_announce_listening,
        )
    except InsecureBindError as exc:
        print(f"[standkit-agent] {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # Перехват сознательно ограничен фазой привязки/старта: сокет
        # создаётся внутри run_server (ThreadingHTTPServer + wrap_socket), и
        # именно оттуда прилетает EADDRINUSE/EACCES. После успешного старта
        # рабочий цикл наружу OSError не выпускает — ошибки соединений
        # обрабатываются в потоках-обработчиков (socketserver.handle_error),
        # serve_forever их не пробрасывает. Поэтому OSError здесь практически
        # всегда означает "не смогли встать на порт", и глушения рабочего
        # цикла не происходит: мы не продолжаем работу, а печатаем строку и
        # выходим с кодом 1.
        print(f"[standkit-agent] {_describe_startup_oserror(exc, host=args.host, port=args.port)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
