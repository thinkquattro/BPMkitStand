"""
Тесты `standkit_hub.__main__` для GAP-311: проверка учётки-инициатора
(``--initiator-sid``/``--result-file``, п.4), режим одноразовой операции
(``--elevated-op``, п.6) и проброс режима окна (``--desktop``) в
``bind_hub_server`` (п.3).

Реальный bind/serve_forever здесь ни один тест не запускает — только
проверка, ЧТО было (или не было) вызвано.
"""

from __future__ import annotations

import json

import pytest

from standkit_hub import __main__ as hub_main
from standkit_hub import elevated_op
from standkit_hub import instance as _instance
from standkit_hub.elevation import ReparseGuardError
from standkit_hub.server import HubAlreadyRunning


class _StopHere(Exception):
    """Сигнал "дошли до bind_hub_server" — дальше в реальный bind не идём."""


@pytest.fixture()
def _no_real_bind(monkeypatch):
    calls = []

    def _fake_bind(*args, **kwargs):
        calls.append((args, kwargs))
        raise _StopHere()

    monkeypatch.setattr(hub_main, "bind_hub_server", _fake_bind)
    return calls


def test_sid_mismatch_refuses_before_bind(tmp_path, monkeypatch, _no_real_bind):
    monkeypatch.setattr(hub_main, "current_user_sid", lambda: "S-1-5-21-BBB")
    monkeypatch.setattr(hub_main, "current_user_name", lambda: "CORP\\other")
    result_file = tmp_path / "result.json"
    config_path = tmp_path / "hub.json"

    rc = hub_main.main(
        [
            "--config", str(config_path),
            "--initiator-sid", "S-1-5-21-AAA",
            "--result-file", str(result_file),
            "--takeover",
        ]
    )

    assert rc == 3
    assert _no_real_bind == []  # bind_hub_server НЕ вызывался
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "refused"
    assert data["user"] == "CORP\\other"


def test_sid_match_writes_nothing_and_proceeds_to_bind(tmp_path, monkeypatch, _no_real_bind):
    """Протокол В4/В5: при совпадении SID ничего в result-file не пишется —
    "accepted" убран, статус появится только после реального bind'а
    ("serving"/"failed"), которого этот тест не достигает (bind подменён)."""
    monkeypatch.setattr(hub_main, "current_user_sid", lambda: "S-1-5-21-AAA")
    result_file = tmp_path / "result.json"
    config_path = tmp_path / "hub.json"

    with pytest.raises(_StopHere):
        hub_main.main(
            [
                "--config", str(config_path),
                "--initiator-sid", "S-1-5-21-AAA",
                "--result-file", str(result_file),
                "--takeover",
            ]
        )

    assert _no_real_bind  # дошли до bind_hub_server
    assert not result_file.exists()


def test_unknown_sid_does_not_block(tmp_path, monkeypatch, _no_real_bind):
    """Ни одна из сторон SID не определила (не Windows) — сверку не делаем."""
    monkeypatch.setattr(hub_main, "current_user_sid", lambda: None)
    result_file = tmp_path / "result.json"
    config_path = tmp_path / "hub.json"

    with pytest.raises(_StopHere):
        hub_main.main(
            [
                "--config", str(config_path),
                "--initiator-sid", "S-1-5-21-AAA",
                "--result-file", str(result_file),
                "--takeover",
            ]
        )

    assert _no_real_bind


def test_desktop_flag_is_passed_to_bind_hub_server(tmp_path, _no_real_bind):
    config_path = tmp_path / "hub.json"

    with pytest.raises(_StopHere):
        hub_main.main(["--config", str(config_path), "--desktop", "--no-browser"])

    (_args, kwargs) = _no_real_bind[0]
    assert kwargs["desktop_mode"] is True


def test_desktop_flag_defaults_to_false(tmp_path, _no_real_bind):
    config_path = tmp_path / "hub.json"

    with pytest.raises(_StopHere):
        hub_main.main(["--config", str(config_path), "--no-browser"])

    (_args, kwargs) = _no_real_bind[0]
    assert kwargs["desktop_mode"] is False


# --- режим --elevated-op: обрабатывается ДО bind/mutex/state/handoff ---


def test_elevated_op_mode_never_touches_bind(tmp_path, monkeypatch, _no_real_bind):
    captured = {}

    def _fake_run(*, stand, action, result_file, config_path, initiator_sid):
        captured.update(
            stand=stand, action=action, result_file=result_file,
            config_path=config_path, initiator_sid=initiator_sid,
        )
        return 0

    monkeypatch.setattr(elevated_op, "run", _fake_run)
    result_file = tmp_path / "op-result.json"

    rc = hub_main.main(
        [
            "--elevated-op", "restart",
            "--stand", "iis1",
            "--result-file", str(result_file),
            "--initiator-sid", "S-1-5-21-AAA",
        ]
    )

    assert rc == 0
    assert captured["stand"] == "iis1"
    assert captured["action"] == "restart"
    assert captured["result_file"] == result_file
    assert captured["initiator_sid"] == "S-1-5-21-AAA"
    assert _no_real_bind == []  # bind_hub_server не вызывался вовсе


def test_elevated_op_mode_requires_stand_and_result_file(tmp_path, _no_real_bind):
    rc = hub_main.main(["--elevated-op", "start"])

    assert rc == 1
    assert _no_real_bind == []


# --- _bind_with_retries (В4/В6): строгий bind повторяется до успеха/таймаута ---


def test_bind_with_retries_returns_first_success_without_retry():
    calls = []

    def bind_fn():
        calls.append(1)
        return "server"

    result = hub_main._bind_with_retries(bind_fn, sleep=lambda s: None)

    assert result == "server"
    assert len(calls) == 1


def test_bind_with_retries_retries_on_hub_already_running_then_succeeds():
    attempts = []

    def bind_fn():
        attempts.append(1)
        if len(attempts) < 3:
            raise HubAlreadyRunning("127.0.0.1", 8770)
        return "server"

    slept = []
    result = hub_main._bind_with_retries(
        bind_fn, timeout=5.0, poll_interval=0.01, sleep=slept.append
    )

    assert result == "server"
    assert len(attempts) == 3
    assert len(slept) == 2


def test_bind_with_retries_reraises_after_timeout():
    def bind_fn():
        raise OSError("порт всё ещё занят")

    fake_clock = iter([0.0, 0.0, 10.0])  # первая проверка -> deadline, вторая (после sleep) -> истёк

    def clock():
        try:
            return next(fake_clock)
        except StopIteration:
            return 999.0

    with pytest.raises(OSError):
        hub_main._bind_with_retries(
            bind_fn, timeout=5.0, poll_interval=0.001, sleep=lambda s: None, clock=clock
        )


# --- _write_relaunch_result (В4/В5): формат payload, best-effort на ошибках ----


def test_write_relaunch_result_writes_status_and_message(tmp_path):
    result_file = tmp_path / "result.json"
    hub_main._write_relaunch_result(str(result_file), status="serving", pid=123, port=8770)

    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "serving"
    assert data["pid"] == 123
    assert data["port"] == 8770
    assert "at" in data


def test_write_relaunch_result_swallows_reparse_guard_error(tmp_path, monkeypatch, capsys):
    def fake_write_atomic(path, payload):
        raise ReparseGuardError("подложенный симлинк")

    monkeypatch.setattr(hub_main, "write_result_atomic", fake_write_atomic)

    hub_main._write_relaunch_result(str(tmp_path / "result.json"), status="failed", message="x")

    assert "не удалось записать результат" in capsys.readouterr().err


def test_write_relaunch_result_swallows_oserror(tmp_path, monkeypatch, capsys):
    def fake_write_atomic(path, payload):
        raise OSError("диск только на чтение")

    monkeypatch.setattr(hub_main, "write_result_atomic", fake_write_atomic)

    hub_main._write_relaunch_result(str(tmp_path / "result.json"), status="failed")

    assert "не удалось записать результат" in capsys.readouterr().err


# --- _takeover_running_instance (Б1): стоп-запрос вместо убийства дерева ------


def _write_running_state(run_dir, *, pid=4242, elevated=False):
    state = _instance.HubInstanceState(pid=pid, host="127.0.0.1", port=8770, elevated=elevated)
    _instance.write_state(_instance.state_path(run_dir), state)
    return state


def test_takeover_calls_stop_running_instance_not_platform_stop_directly(tmp_path, monkeypatch):
    """Б1: перехват порта идёт через `_instance.stop_running_instance`
    (файл-запрос + условная эскалация с tree=False внутри неё) — НЕ напрямую
    через `platform.stop(..., tree=True)`, как было раньше."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_running_state(run_dir)
    state_file = _instance.state_path(run_dir)

    calls = []

    def fake_stop_running_instance(state, *, run_dir, requester_pid=None, **kw):
        calls.append({"pid": state.pid, "requester_pid": requester_pid})
        return True

    monkeypatch.setattr(_instance, "stop_running_instance", fake_stop_running_instance)
    monkeypatch.setattr(_instance, "wait_port_released", lambda host, port, **kw: True)
    monkeypatch.setattr(hub_main, "is_elevated", lambda: True)
    monkeypatch.setattr(_instance, "is_alive", lambda pid: True)

    exc = HubAlreadyRunning("127.0.0.1", 8770)
    result = hub_main._takeover_running_instance(
        exc, state_file, run_dir, explicit=True, our_sid=None
    )

    assert result is True
    assert len(calls) == 1
    assert calls[0]["pid"] == 4242
    import os as _os
    assert calls[0]["requester_pid"] == _os.getpid()


def test_takeover_fails_when_stop_running_instance_fails(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_running_state(run_dir)
    state_file = _instance.state_path(run_dir)

    monkeypatch.setattr(_instance, "stop_running_instance", lambda *a, **kw: False)
    wait_called = []
    monkeypatch.setattr(
        _instance, "wait_port_released", lambda *a, **kw: wait_called.append(1) or True
    )
    monkeypatch.setattr(hub_main, "is_elevated", lambda: True)
    monkeypatch.setattr(_instance, "is_alive", lambda pid: True)

    exc = HubAlreadyRunning("127.0.0.1", 8770)
    result = hub_main._takeover_running_instance(
        exc, state_file, run_dir, explicit=True, our_sid=None
    )

    assert result is False
    assert wait_called == []  # не ждём порт, если остановить не удалось


def test_takeover_returns_false_when_should_not_takeover(tmp_path, monkeypatch):
    """Не elevated и без --takeover — should_takeover() отказывает, к
    stop_running_instance дело даже не доходит."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_running_state(run_dir, elevated=True)
    state_file = _instance.state_path(run_dir)

    called = []
    monkeypatch.setattr(_instance, "stop_running_instance", lambda *a, **kw: called.append(1) or True)
    monkeypatch.setattr(hub_main, "is_elevated", lambda: False)

    exc = HubAlreadyRunning("127.0.0.1", 8770)
    result = hub_main._takeover_running_instance(
        exc, state_file, run_dir, explicit=False, our_sid=None
    )

    assert result is False
    assert called == []


def test_takeover_waits_for_port_without_stopping_when_no_state_file(tmp_path, monkeypatch):
    """`--takeover` без файла состояния — кого гасить неизвестно, просто
    ждём освобождения порта, не зовя stop_running_instance вовсе."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state_file = _instance.state_path(run_dir)  # не создаём файл

    called = []
    monkeypatch.setattr(_instance, "stop_running_instance", lambda *a, **kw: called.append(1) or True)
    monkeypatch.setattr(_instance, "wait_port_released", lambda *a, **kw: True)
    monkeypatch.setattr(hub_main, "is_elevated", lambda: True)

    exc = HubAlreadyRunning("127.0.0.1", 8770)
    result = hub_main._takeover_running_instance(
        exc, state_file, run_dir, explicit=True, our_sid=None
    )

    assert result is True
    assert called == []


def test_takeover_fails_when_port_never_released(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_running_state(run_dir)
    state_file = _instance.state_path(run_dir)

    monkeypatch.setattr(_instance, "stop_running_instance", lambda *a, **kw: True)
    monkeypatch.setattr(_instance, "wait_port_released", lambda *a, **kw: False)
    monkeypatch.setattr(hub_main, "is_elevated", lambda: True)
    monkeypatch.setattr(_instance, "is_alive", lambda pid: True)

    exc = HubAlreadyRunning("127.0.0.1", 8770)
    result = hub_main._takeover_running_instance(
        exc, state_file, run_dir, explicit=True, our_sid=None
    )

    assert result is False


# --- полный путь main(): serving/failed в result-file через реальный перехват --


def test_main_writes_serving_result_after_successful_takeover_and_bind(tmp_path, monkeypatch):
    """В4/В5 сквозной сценарий: SID совпал -> перехват (stop_running_instance
    подменён на успех) -> строгий bind (подменён на успех) -> result-file
    получает статус ``serving`` с реальным pid/портом."""
    run_dir = tmp_path / "run"
    config_path = tmp_path / "hub.json"
    result_file = tmp_path / "result.json"

    _write_running_state(run_dir, pid=111, elevated=False)

    class _FakeConfig:
        def __init__(self, *a, **kw):
            pass

        def ensure_registry_dir(self):
            pass

        def resolve_run_dir(self):
            return run_dir

        @staticmethod
        def config_path():
            return config_path

    monkeypatch.setattr(hub_main.HubConfig, "load", staticmethod(lambda p: _FakeConfig()))
    monkeypatch.setattr(hub_main, "current_user_sid", lambda: "S-1-5-21-AAA")
    monkeypatch.setattr(hub_main, "acquire_hub_mutex", lambda: True)
    monkeypatch.setattr(_instance, "stop_running_instance", lambda *a, **kw: True)
    monkeypatch.setattr(_instance, "wait_port_released", lambda *a, **kw: True)
    monkeypatch.setattr(hub_main, "is_elevated", lambda: True)
    monkeypatch.setattr(_instance, "is_alive", lambda pid: True)

    class _FakeServer:
        server_address = ("127.0.0.1", 8770)

        def serve_forever(self):
            raise KeyboardInterrupt()  # выходим из обслуживания сразу же

        def server_close(self):
            pass

    def _fake_bind(*args, **kwargs):
        return _FakeServer()

    monkeypatch.setattr(hub_main, "bind_hub_server", _fake_bind)

    rc = hub_main.main(
        [
            "--config", str(config_path),
            "--initiator-sid", "S-1-5-21-AAA",
            "--result-file", str(result_file),
            "--takeover",
            "--no-browser",
        ]
    )

    assert rc == 0
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "serving"
    assert data["port"] == 8770


# --- M14: WARN, если run_dir сконфигурирован вне профиля пользователя ---


def test_warn_if_run_dir_outside_profile_warns_when_outside_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home" / "someone"
    home.mkdir(parents=True)
    outside = tmp_path / "shared" / "run"
    outside.mkdir(parents=True)
    monkeypatch.setattr(hub_main.Path, "home", classmethod(lambda cls: home))

    hub_main._warn_if_run_dir_outside_profile(outside)

    captured = capsys.readouterr()
    assert "ВНИМАНИЕ" in captured.err
    assert str(outside.resolve()) in captured.err


def test_warn_if_run_dir_outside_profile_silent_when_inside_home(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home" / "someone"
    inside = home / "AppData" / "Local" / "BPMkit"
    inside.mkdir(parents=True)
    monkeypatch.setattr(hub_main.Path, "home", classmethod(lambda cls: home))

    hub_main._warn_if_run_dir_outside_profile(inside)

    captured = capsys.readouterr()
    assert captured.err == ""
