"""
Тесты preflight рабочих каталогов агента (standkit_agent.__main__).

Проверяется контракт GAP-007: непригодный путь (run/logs/audit) — это отказ
старта ОДНОЙ строкой в stderr и код возврата 1 ДО открытия сокета и ДО печати
«слушаю …», а не traceback посреди рабочего цикла.

Сеть почти нигде не поднимается: ``run_server`` подменяется заглушкой
(preflight по контракту обязан отработать раньше него). Исключение — блок 6:
там сокет привязывается по-настоящему (loopback, эфемерный порт), потому что
проверяется именно порядок «привязались → печатаем «слушаю …»», а на моках он
непроверяем.

Кроссплатформенность: основной каркас (несуществующий родитель-файл,
отсутствующий $HOME, занятый порт, happy path) работает и на Windows, и на
Linux; проверки через ``os.chmod`` пропускаются на Windows (там режим файла
почти не влияет на запись) и под root (CAP_DAC_OVERRIDE игнорирует 0o500).
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import threading

import pytest

from standkit_agent import __main__ as agent_main

# Ссылка на секрет control-токена + имя переменной окружения, из которой её
# резолвит standkit.secrets (STANDKIT_SECRET__<REF в верхнем регистре, всё, что
# не буква/цифра — в "_").
TOKEN_REF = "standkit:test-stand:agent-token"
TOKEN_ENV = "STANDKIT_SECRET__STANDKIT_TEST_STAND_AGENT_TOKEN"

_NOT_WINDOWS = pytest.mark.skipif(os.name == "nt", reason="права каталога через chmod проверяются только на POSIX")
_NOT_ROOT = pytest.mark.skipif(
    getattr(os, "geteuid", lambda: 1)() == 0,
    reason="под root chmod 0o500 не мешает записи (CAP_DAC_OVERRIDE) — тест был бы ложно зелёным",
)


@pytest.fixture()
def agent_env(tmp_path, monkeypatch):
    """
    Минимальное окружение для ``main()``: пустой реестр + секрет токена в env.

    Реестр специально не создаётся файлом — ``Registry.load`` для
    несуществующего пути возвращает пустой реестр, а нам важна только фаза
    путей, идущая после загрузки реестра и резолва секретов.
    """
    monkeypatch.setenv(TOKEN_ENV, "test-control-token")
    return {"registry": str(tmp_path / "projects.json")}


@pytest.fixture()
def run_server_stub(monkeypatch):
    """Заглушка ``run_server``: фиксирует факт вызова и переданные kwargs."""
    calls: list[dict] = []

    def _fake_run_server(registry, authenticator, **kwargs):
        calls.append(kwargs)
        # Заглушка имитирует УСПЕШНЫЙ старт, поэтому обязана вызвать on_ready:
        # настоящий run_server зовёт этот колбэк сразу после привязки сокета,
        # и только он печатает «слушаю …» (GAP-007). Без вызова заглушка
        # изображала бы агент, который стартовал, но на порт не встал.
        on_ready = kwargs.get("on_ready")
        if on_ready is not None:
            on_ready()

    monkeypatch.setattr(agent_main, "run_server", _fake_run_server)
    return calls


def _argv(agent_env, **paths) -> list[str]:
    argv = ["--registry", agent_env["registry"], "--token-ref", TOKEN_REF]
    for flag, value in paths.items():
        if value is not None:
            argv += [f"--{flag.replace('_', '-')}", str(value)]
    return argv


def _single_error_line(captured) -> str:
    """
    Возвращает единственную строку отказа из stderr, попутно проверяя контракт:
    ровно одна строка, префикс агента, никакого traceback.
    """
    lines = [line for line in captured.err.splitlines() if line.strip()]
    assert len(lines) == 1, f"ожидалась ровно одна строка отказа, получено: {lines!r}"
    line = lines[0]
    assert line.startswith("[standkit-agent] ")
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out
    return line


def _assert_not_listening(captured) -> None:
    assert "слушаю" not in captured.out


# --- 1. Каталог логов ---


def test_log_dir_under_file_parent_refuses_before_listening(agent_env, tmp_path, run_server_stub, capsys):
    # Кроссплатформенный способ сделать путь заведомо несоздаваемым для ЛЮБОГО
    # пользователя, включая root: родитель — обычный файл, а не каталог.
    blocker = tmp_path / "blocker"
    blocker.write_text("не каталог", encoding="utf-8")
    bad_log_dir = blocker / "logs"

    rc = agent_main.main(
        _argv(agent_env, run_dir=tmp_path / "run", log_dir=bad_log_dir, audit_log=tmp_path / "audit.log")
    )

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert "каталог логов" in line
    assert str(bad_log_dir) in line
    assert "--log-dir" in line
    _assert_not_listening(captured)
    assert run_server_stub == [], "run_server не должен вызываться после отказа preflight"


@_NOT_WINDOWS
@_NOT_ROOT
def test_unwritable_log_dir_refuses_with_user_and_flag(agent_env, tmp_path, run_server_stub, capsys):
    bad_log_dir = tmp_path / "logs"
    bad_log_dir.mkdir()
    os.chmod(bad_log_dir, 0o500)
    try:
        rc = agent_main.main(
            _argv(agent_env, run_dir=tmp_path / "run", log_dir=bad_log_dir, audit_log=tmp_path / "audit.log")
        )
        captured = capsys.readouterr()
    finally:
        os.chmod(bad_log_dir, 0o700)

    assert rc == 1
    line = _single_error_line(captured)
    # Дословный образец из GAP-007: что недоступно, полный путь, пользователь, флаг.
    assert (
        f"Отказ старта: каталог логов {bad_log_dir} недоступен на запись пользователю "
        f"{agent_main._current_user_name()} — задайте --log-dir или выдайте права"
    ) in line
    _assert_not_listening(captured)
    assert run_server_stub == []


# --- 2. Каталог pid-файлов ---


def test_run_dir_under_file_parent_refuses(agent_env, tmp_path, run_server_stub, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("не каталог", encoding="utf-8")
    bad_run_dir = blocker / "run"

    rc = agent_main.main(
        _argv(agent_env, run_dir=bad_run_dir, log_dir=tmp_path / "logs", audit_log=tmp_path / "audit.log")
    )

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert "каталог pid-файлов" in line
    assert str(bad_run_dir) in line
    assert "--run-dir" in line
    _assert_not_listening(captured)
    assert run_server_stub == []


@_NOT_WINDOWS
@_NOT_ROOT
def test_unwritable_run_dir_refuses_with_user_and_flag(agent_env, tmp_path, run_server_stub, capsys):
    bad_run_dir = tmp_path / "run"
    bad_run_dir.mkdir()
    os.chmod(bad_run_dir, 0o500)
    try:
        rc = agent_main.main(
            _argv(agent_env, run_dir=bad_run_dir, log_dir=tmp_path / "logs", audit_log=tmp_path / "audit.log")
        )
        captured = capsys.readouterr()
    finally:
        os.chmod(bad_run_dir, 0o700)

    assert rc == 1
    line = _single_error_line(captured)
    assert (
        f"Отказ старта: каталог pid-файлов {bad_run_dir} недоступен на запись пользователю "
        f"{agent_main._current_user_name()} — задайте --run-dir или выдайте права"
    ) in line
    _assert_not_listening(captured)
    assert run_server_stub == []


# --- 3. Каталог аудит-лога ---


def test_audit_dir_under_file_parent_refuses(agent_env, tmp_path, run_server_stub, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("не каталог", encoding="utf-8")
    bad_audit = blocker / "audit" / "audit.log"

    rc = agent_main.main(
        _argv(agent_env, run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log=bad_audit)
    )

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert "каталог аудит-лога" in line
    assert str(bad_audit.parent) in line
    assert "--audit-log" in line
    _assert_not_listening(captured)
    assert run_server_stub == []


@_NOT_WINDOWS
@_NOT_ROOT
def test_unwritable_audit_dir_refuses_with_user_and_flag(agent_env, tmp_path, run_server_stub, capsys):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    os.chmod(audit_dir, 0o500)
    try:
        rc = agent_main.main(
            _argv(
                agent_env,
                run_dir=tmp_path / "run",
                log_dir=tmp_path / "logs",
                audit_log=audit_dir / "audit.log",
            )
        )
        captured = capsys.readouterr()
    finally:
        os.chmod(audit_dir, 0o700)

    assert rc == 1
    line = _single_error_line(captured)
    assert (
        f"Отказ старта: каталог аудит-лога {audit_dir} недоступен на запись пользователю "
        f"{agent_main._current_user_name()} — задайте --audit-log или выдайте права"
    ) in line
    _assert_not_listening(captured)
    assert run_server_stub == []


@_NOT_WINDOWS
@_NOT_ROOT
def test_existing_unwritable_audit_file_refuses(agent_env, tmp_path, run_server_stub, capsys):
    # Типовой случай: каталог подготовили из-под root, файл аудита уже создан
    # root'ом, а служба ходит из-под своего аккаунта — каталог писуч, файл нет.
    audit_file = tmp_path / "audit.log"
    audit_file.write_text("", encoding="utf-8")
    os.chmod(audit_file, 0o400)
    try:
        rc = agent_main.main(
            _argv(agent_env, run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log=audit_file)
        )
        captured = capsys.readouterr()
    finally:
        os.chmod(audit_file, 0o600)

    assert rc == 1
    line = _single_error_line(captured)
    assert f"аудит-лог {audit_file} недоступен на запись" in line
    assert "--audit-log" in line
    _assert_not_listening(captured)
    assert run_server_stub == []


# --- 4. Служебный запуск: $HOME не существует, флаги не заданы ---


def test_missing_home_without_flags_names_all_three_flags(agent_env, tmp_path, monkeypatch, run_server_stub, capsys):
    missing_home = tmp_path / "no-such-home"
    # Path.home() читает HOME на POSIX и USERPROFILE на Windows — подменяем оба.
    monkeypatch.setenv("HOME", str(missing_home))
    monkeypatch.setenv("USERPROFILE", str(missing_home))

    rc = agent_main.main(_argv(agent_env))

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert str(missing_home) in line
    for flag in ("--run-dir", "--log-dir", "--audit-log"):
        assert flag in line, f"в тексте отказа нет обязательного флага {flag}: {line}"
    assert "--no-create-home" in line
    # Ничего не создано: каталог ~/.standkit не должен появиться в никуда.
    assert not missing_home.exists()
    assert not (missing_home / ".standkit").exists()
    _assert_not_listening(captured)
    assert run_server_stub == []


def test_missing_home_with_all_three_flags_starts(agent_env, tmp_path, monkeypatch, run_server_stub, capsys):
    # Ровно сценарий systemd-юнита: $HOME нет, но все три пути заданы явно —
    # старт обязан пройти, домашний каталог вообще не должен опрашиваться.
    monkeypatch.setenv("HOME", str(tmp_path / "no-such-home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "no-such-home"))

    rc = agent_main.main(
        _argv(agent_env, run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log=tmp_path / "audit.log")
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "слушаю" in captured.out
    assert len(run_server_stub) == 1


# --- 5. Happy path и резолв дефолтов ---


def test_valid_paths_pass_preflight_and_reach_run_server(agent_env, tmp_path, run_server_stub, capsys):
    run_dir = tmp_path / "run"
    log_dir = tmp_path / "logs"
    audit_log = tmp_path / "audit" / "audit.log"

    rc = agent_main.main(_argv(agent_env, run_dir=run_dir, log_dir=log_dir, audit_log=audit_log))

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.err == ""
    assert "слушаю 127.0.0.1:8765" in captured.out
    assert len(run_server_stub) == 1
    kwargs = run_server_stub[0]
    assert kwargs["run_dir"] == run_dir
    assert kwargs["log_dir"] == log_dir
    assert kwargs["audit_log_path"] == audit_log
    # preflight ничего не создаёт — каталоги появятся уже по месту использования.
    assert not run_dir.exists()
    assert not log_dir.exists()
    assert not audit_log.parent.exists()


def test_defaults_resolved_from_home_and_passed_to_run_server(agent_env, tmp_path, monkeypatch, run_server_stub, capsys):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    rc = agent_main.main(_argv(agent_env))

    captured = capsys.readouterr()
    assert rc == 0
    assert "слушаю" in captured.out
    kwargs = run_server_stub[0]
    assert kwargs["run_dir"] == home / ".standkit" / "run"
    assert kwargs["log_dir"] == home / ".standkit" / "logs"
    assert kwargs["audit_log_path"] == home / ".standkit" / "audit.log"
    # Проверка не должна материализовать ~/.standkit — только проверить $HOME.
    assert not (home / ".standkit").exists()


def test_defaults_match_downstream_constants():
    """
    Дефолты, которые резолвит ``__main__``, обязаны бит-в-бит совпадать с теми,
    что подставляются ниже по стеку — иначе preflight проверял бы не те пути,
    которыми агент реально пользуется.
    """
    from standkit import lifecycle
    from standkit_agent import audit as agent_audit

    run_dir, log_dir, audit_log_path = agent_main.resolve_agent_paths(run_dir=None, log_dir=None, audit_log=None)

    assert run_dir == lifecycle._DEFAULT_RUN_DIR
    assert log_dir == lifecycle._DEFAULT_LOG_DIR
    assert audit_log_path == agent_audit.DEFAULT_AUDIT_LOG_PATH


def test_explicit_paths_are_passed_through_unchanged():
    run_dir, log_dir, audit_log_path = agent_main.resolve_agent_paths(
        run_dir="/tmp/x/run", log_dir="/tmp/x/logs", audit_log="/tmp/x/audit.log"
    )
    assert (run_dir, log_dir, audit_log_path) == (
        Path("/tmp/x/run"),
        Path("/tmp/x/logs"),
        Path("/tmp/x/audit.log"),
    )


# --- 6. Занятый порт и прочие OSError фазы старта ---


def test_address_in_use_is_one_line_without_traceback(agent_env, tmp_path, monkeypatch, capsys):
    def _boom(registry, authenticator, **kwargs):
        raise OSError(errno.EADDRINUSE, "Address already in use")

    monkeypatch.setattr(agent_main, "run_server", _boom)

    rc = agent_main.main(
        _argv(agent_env, run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log=tmp_path / "audit.log")
        + ["--port", "8765"]
    )

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert "127.0.0.1:8765" in line
    assert "порт уже занят другим процессом" in line
    assert "--port" in line


def test_bind_permission_denied_is_one_line(agent_env, tmp_path, monkeypatch, capsys):
    def _boom(registry, authenticator, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(agent_main, "run_server", _boom)

    rc = agent_main.main(
        _argv(agent_env, run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log=tmp_path / "audit.log")
        + ["--port", "443"]
    )

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert "127.0.0.1:443" in line
    assert "нет прав на привязку" in line


def test_other_oserror_at_startup_is_one_line(agent_env, tmp_path, monkeypatch, capsys):
    def _boom(registry, authenticator, **kwargs):
        raise OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")

    monkeypatch.setattr(agent_main, "run_server", _boom)

    rc = agent_main.main(
        _argv(agent_env, run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log=tmp_path / "audit.log")
        + ["--host", "10.0.0.10", "--insecure"]
    )

    captured = capsys.readouterr()
    assert rc == 1
    lines = [line for line in captured.err.splitlines() if line.strip()]
    # --insecure печатает своё громкое предупреждение (это делает run_server, здесь
    # он подменён), поэтому здесь в stderr действительно ровно одна строка отказа.
    assert len(lines) == 1
    assert "Cannot assign requested address" in lines[0]
    assert "10.0.0.10" in lines[0]
    assert "Traceback" not in captured.err


def test_busy_port_never_prints_listening(agent_env, tmp_path, capsys):
    """
    Регрессия GAP-007: «слушаю …» печатается ТОЛЬКО после фактической привязки.

    Здесь ничего не подменяется — порт занимается настоящим сокетом, и
    ``run_server`` честно падает на bind. До фикса stdout содержал бодрое
    «слушаю 127.0.0.1:<порт>», а stderr — «порт уже занят»: по журналу службы
    нельзя было понять, поднялся агент или нет.
    """
    import socket

    # SO_REUSEADDR у держателя НЕ ставим сознательно: на Windows именно он
    # разрешает второму процессу занять уже слушаемый порт, и тест перестал бы
    # проверять то, ради чего написан (см. server._windows_port_is_taken).
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    busy_port = holder.getsockname()[1]

    # main() здесь настоящий и на регрессии УХОДИТ В serve_forever навсегда
    # (именно так это и всплыло на Windows: SO_REUSEADDR позволял занять
    # слушаемый порт, агент стартовал вторым и подвешивал весь прогон).
    # Поэтому вызов — в демон-потоке со сторожевым таймером: регрессия должна
    # падать тестом, а не вешать набор.
    result: dict = {}

    def _run() -> None:
        try:
            result["rc"] = agent_main.main(
                _argv(agent_env, run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log=tmp_path / "audit.log")
                + ["--port", str(busy_port)]
            )
        except BaseException as exc:  # noqa: BLE001 — переносим в основной поток
            result["exc"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    try:
        worker.start()
        worker.join(timeout=30)
        captured = capsys.readouterr()
    finally:
        holder.close()

    assert not worker.is_alive(), (
        "агент не отказался стартовать на занятом порту, а ушёл в serve_forever — "
        "на Windows это тихий перехват чужого порта (см. server._windows_port_is_taken)"
    )
    if "exc" in result:
        raise result["exc"]
    rc = result["rc"]
    assert rc == 1
    line = _single_error_line(captured)
    assert f"127.0.0.1:{busy_port}" in line
    assert "порт уже занят другим процессом" in line
    _assert_not_listening(captured)


def test_run_server_calls_on_ready_only_after_successful_bind():
    """
    Контракт ``run_server``: занятый порт → ``OSError`` и НИ ОДНОГО вызова
    ``on_ready`` (иначе «слушаю …» уехало бы в stdout при закрытом сокете).
    """
    import socket

    from standkit.registry import Registry
    from standkit_agent.security import Authenticator
    from standkit_agent.server import run_server

    # SO_REUSEADDR у держателя НЕ ставим сознательно: на Windows именно он
    # разрешает второму процессу занять уже слушаемый порт, и тест перестал бы
    # проверять то, ради чего написан (см. server._windows_port_is_taken).
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    busy_port = holder.getsockname()[1]
    ready: list[str] = []
    try:
        with pytest.raises(OSError):
            run_server(
                Registry(path=Path("projects.json")),
                Authenticator("token"),
                host="127.0.0.1",
                port=busy_port,
                on_ready=lambda: ready.append("ready"),
            )
    finally:
        holder.close()
    assert ready == []


def test_run_server_on_ready_fires_after_bind_and_before_serve_forever():
    """
    Позитивная половина того же контракта: сокет привязан (порт занят уже
    НАМИ и снаружи больше не привязывается), колбэк вызван, ``serve_forever``
    ещё не начат. Выход из блокирующего вызова — исключением из самого
    колбэка: ``finally`` в ``run_server`` закрывает сокет.
    """
    import socket

    from standkit.registry import Registry
    from standkit_agent.security import Authenticator
    from standkit_agent.server import run_server

    class _Ready(Exception):
        pass

    seen: list[int] = []

    def _on_ready() -> None:
        # Порт уже занят серверным сокетом run_server — второй bind обязан
        # провалиться, значит колбэк вызван ПОСЛЕ фактической привязки.
        probe = socket.socket()
        try:
            with pytest.raises(OSError):
                probe.bind(("127.0.0.1", port))
                probe.listen(1)
        finally:
            probe.close()
        seen.append(1)
        raise _Ready

    # Свободный порт: занимаем, тут же отпускаем и отдаём его агенту.
    tmp_sock = socket.socket()
    tmp_sock.bind(("127.0.0.1", 0))
    port = tmp_sock.getsockname()[1]
    tmp_sock.close()

    with pytest.raises(_Ready):
        run_server(
            Registry(path=Path("projects.json")),
            Authenticator("token"),
            host="127.0.0.1",
            port=port,
            on_ready=_on_ready,
        )
    assert seen == [1]


def test_run_server_on_ready_fires_after_tls_wrap(monkeypatch):
    """
    TLS-вариант: колбэк обязан сработать ПОСЛЕ ``wrap_socket``, а не раньше —
    иначе «слушаю … (tls=on)» печаталось бы до того, как сокет действительно
    стал TLS-сокетом. Настоящие сертификаты не нужны: проверяется порядок,
    поэтому подменён ``build_ssl_context``.
    """
    import socket

    from standkit.registry import Registry
    from standkit_agent import server as agent_server
    from standkit_agent.security import Authenticator

    order: list[str] = []

    class _FakeCtx:
        def wrap_socket(self, sock, server_side=False):
            order.append("wrap")
            return sock

    monkeypatch.setattr(agent_server._security, "build_ssl_context", lambda *a, **kw: _FakeCtx())

    class _Ready(Exception):
        pass

    def _on_ready() -> None:
        order.append("ready")
        raise _Ready

    tmp_sock = socket.socket()
    tmp_sock.bind(("127.0.0.1", 0))
    port = tmp_sock.getsockname()[1]
    tmp_sock.close()

    with pytest.raises(_Ready):
        agent_server.run_server(
            Registry(path=Path("projects.json")),
            Authenticator("token"),
            host="127.0.0.1",
            port=port,
            tls_cert="cert.pem",
            tls_key="key.pem",
            on_ready=_on_ready,
        )
    assert order == ["wrap", "ready"]


# --- 7. Порядок проверок: небезопасный bind отказывает раньше путей ---


def test_insecure_bind_refused_before_path_preflight(agent_env, tmp_path, run_server_stub, capsys):
    # Non-loopback без TLS и без --insecure — отказ по безопасности; пути при
    # этом заведомо битые, но до них дело дойти не должно.
    blocker = tmp_path / "blocker"
    blocker.write_text("не каталог", encoding="utf-8")

    rc = agent_main.main(
        _argv(agent_env, run_dir=blocker / "run", log_dir=blocker / "logs", audit_log=blocker / "audit.log")
        + ["--host", "0.0.0.0"]
    )

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert "не loopback" in line
    assert "каталог" not in line
    _assert_not_listening(captured)
    assert run_server_stub == []


# --- 8. Юнит-уровень: preflight_paths как чистая проверка ---


def test_preflight_paths_accepts_creatable_dirs(tmp_path):
    agent_main.preflight_paths(
        run_dir=tmp_path / "a" / "run",
        log_dir=tmp_path / "b" / "logs",
        audit_log_path=tmp_path / "c" / "audit.log",
    )


def test_preflight_paths_raises_startup_path_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(agent_main.StartupPathError):
        agent_main.preflight_paths(
            run_dir=blocker / "run",
            log_dir=tmp_path / "logs",
            audit_log_path=tmp_path / "audit.log",
        )


def test_log_dir_pointing_to_file_is_reported_as_not_a_dir(tmp_path):
    not_a_dir = tmp_path / "logs"
    not_a_dir.write_text("x", encoding="utf-8")
    with pytest.raises(agent_main.StartupPathError) as excinfo:
        agent_main.preflight_paths(
            run_dir=tmp_path / "run",
            log_dir=not_a_dir,
            audit_log_path=tmp_path / "audit.log",
        )
    message = str(excinfo.value)
    assert "не каталог (по этому пути лежит файл)" in message
    assert "--log-dir" in message


# --- 9. Дословные тексты отказа «недоступен на запись» на любой платформе ---
#
# Реально снять права можно только на POSIX и только не под root (см. блок 1-3),
# а формулировка отказа — это контракт GAP-007, который должен проверяться
# всегда. Поэтому здесь подменяется единственная точка, где preflight узнаёт
# «писать нельзя» — проба записи ``_probe_writable``.


@pytest.fixture()
def deny_write(monkeypatch):
    """Заставляет пробу записи отвечать «нельзя» для конкретного каталога."""

    def _deny(target: Path):
        real = agent_main._probe_writable

        def _fake(directory: Path):
            if Path(directory) == Path(target):
                return "Permission denied"
            return real(directory)

        monkeypatch.setattr(agent_main, "_probe_writable", _fake)

    return _deny


def test_unwritable_log_dir_message_matches_gap_sample(agent_env, tmp_path, deny_write, run_server_stub, capsys):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    deny_write(log_dir)

    rc = agent_main.main(
        _argv(agent_env, run_dir=tmp_path / "run", log_dir=log_dir, audit_log=tmp_path / "audit.log")
    )

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert line == (
        f"[standkit-agent] Отказ старта: каталог логов {log_dir} недоступен на запись пользователю "
        f"{agent_main._current_user_name()} — задайте --log-dir или выдайте права"
    )
    _assert_not_listening(captured)
    assert run_server_stub == []


def test_unwritable_run_dir_message_matches_gap_sample(tmp_path, deny_write):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    deny_write(run_dir)

    with pytest.raises(agent_main.StartupPathError) as excinfo:
        agent_main.preflight_paths(
            run_dir=run_dir, log_dir=tmp_path / "logs", audit_log_path=tmp_path / "audit.log"
        )
    assert str(excinfo.value) == (
        f"Отказ старта: каталог pid-файлов {run_dir} недоступен на запись пользователю "
        f"{agent_main._current_user_name()} — задайте --run-dir или выдайте права"
    )


def test_unwritable_audit_dir_message_matches_gap_sample(tmp_path, deny_write):
    audit_dir = tmp_path / "audit"
    audit_dir.mkdir()
    deny_write(audit_dir)

    with pytest.raises(agent_main.StartupPathError) as excinfo:
        agent_main.preflight_paths(
            run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log_path=audit_dir / "audit.log"
        )
    assert str(excinfo.value) == (
        f"Отказ старта: каталог аудит-лога {audit_dir} недоступен на запись пользователю "
        f"{agent_main._current_user_name()} — задайте --audit-log или выдайте права"
    )


def test_uncreatable_dir_message_names_parent_and_flag(tmp_path, deny_write):
    # Каталога ещё нет, а в ближайшего существующего предка писать нельзя.
    deny_write(tmp_path)
    with pytest.raises(agent_main.StartupPathError) as excinfo:
        agent_main.preflight_paths(
            run_dir=tmp_path / "run", log_dir=tmp_path / "logs", audit_log_path=tmp_path / "audit.log"
        )
    assert str(excinfo.value) == (
        f"Отказ старта: каталог pid-файлов {tmp_path / 'run'} не существует и не может быть создан "
        f"пользователем {agent_main._current_user_name()}: нет прав на запись в {tmp_path} "
        "(Permission denied) — задайте --run-dir или выдайте права"
    )


# --- 10. Сама проба записи ``_probe_writable`` (без skip на любой платформе) ---
#
# Блоки 1-3 снимают права по-настоящему (chmod) и потому пропускаются на
# Windows и под root — а CI обычно root. Чтобы негативная ветка самой пробы
# («попытка создать файл упала») исполнялась ВСЕГДА, здесь падение подменяется
# в единственном месте, где проба трогает ФС, — ``tempfile.NamedTemporaryFile``.


def test_probe_writable_returns_reason_when_write_fails(tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(agent_main.tempfile, "NamedTemporaryFile", _boom)

    reason = agent_main._probe_writable(tmp_path)

    assert reason is not None, "проба обязана вернуть текст причины, а не None"
    assert "Permission denied" in reason


def test_probe_writable_reason_falls_back_to_str_without_strerror(tmp_path, monkeypatch):
    # У OSError без errno strerror пустой — причина не должна становиться пустой
    # строкой (пустая строка ложно читается как «писать можно»).
    def _boom(*args, **kwargs):
        raise OSError("файловая система только для чтения")

    monkeypatch.setattr(agent_main.tempfile, "NamedTemporaryFile", _boom)

    reason = agent_main._probe_writable(tmp_path)

    assert reason == "файловая система только для чтения"


def test_probe_writable_ok_on_writable_dir_and_leaves_no_files(tmp_path):
    assert agent_main._probe_writable(tmp_path) is None
    assert list(tmp_path.iterdir()) == [], "после пробы во временном каталоге не должно остаться мусора"


def test_failed_write_probe_refuses_startup_on_any_platform(agent_env, tmp_path, monkeypatch, run_server_stub, capsys):
    """Та же подмена, но по всему пути main(): отказ старта одной строкой и без «слушаю …»."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    real = agent_main.tempfile.NamedTemporaryFile

    def _boom(*args, **kwargs):
        if Path(kwargs.get("dir", "")) == log_dir:
            raise PermissionError(13, "Permission denied")
        return real(*args, **kwargs)

    monkeypatch.setattr(agent_main.tempfile, "NamedTemporaryFile", _boom)

    rc = agent_main.main(
        _argv(agent_env, run_dir=tmp_path / "run", log_dir=log_dir, audit_log=tmp_path / "audit.log")
    )

    captured = capsys.readouterr()
    assert rc == 1
    line = _single_error_line(captured)
    assert f"каталог логов {log_dir} недоступен на запись" in line
    _assert_not_listening(captured)
    assert run_server_stub == []
