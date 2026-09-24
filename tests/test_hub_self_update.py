# -*- coding: utf-8 -*-
"""Тесты помощника самообновления диспетчера (GAP-523): `standkit_hub.self_update`.

Реального перезапуска здесь НЕТ — `is_alive`/`spawn_hidden` подставные, файлы временные.
Что проверяется:
1. помощник ждёт выхода `wait_pid` (опрос `is_alive`) и не подменяет target, пока тот жив;
2. таймаут ожидания -> `RC_WAIT_TIMEOUT`, target не тронут;
3. успешный путь: бэкап `target.old`, подмена `target` содержимым помощника, запуск
   `target` через `spawn_hidden` (не голый `subprocess.Popen` — GAP-138);
4. `ERROR_ACCESS_DENIED` (winerror=5) при подмене -> `RC_ELEVATION_REQUIRED`, без UAC.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from standkit_hub import self_update


def test_wait_for_exit_returns_true_immediately_for_dead_pid():
    assert self_update._wait_for_exit(0) is True
    assert self_update._wait_for_exit(-5) is True


def test_wait_for_exit_polls_until_alive_becomes_false(monkeypatch):
    calls = {"n": 0}

    def _is_alive(pid):
        calls["n"] += 1
        return calls["n"] < 3

    monkeypatch.setattr(self_update, "is_alive", _is_alive)
    slept = []
    result = self_update._wait_for_exit(123, timeout=10.0, poll=0.01,
                                        sleep=slept.append, clock=lambda: 0.0)
    assert result is True
    assert calls["n"] == 3
    assert len(slept) == 2


def test_wait_for_exit_times_out(monkeypatch):
    monkeypatch.setattr(self_update, "is_alive", lambda pid: True)
    clock = {"t": 0.0}

    def _clock():
        return clock["t"]

    def _sleep(_secs):
        clock["t"] += 1.0

    result = self_update._wait_for_exit(123, timeout=3.0, poll=1.0,
                                        sleep=_sleep, clock=_clock)
    assert result is False


def test_run_self_update_helper_full_success(tmp_path, monkeypatch):
    target = tmp_path / "bpmkit-hub.exe"
    target.write_bytes(b"OLD VERSION")
    helper_src = tmp_path / "staged" / "bpmkit-hub-0.13.0.exe"
    helper_src.parent.mkdir(parents=True, exist_ok=True)
    helper_src.write_bytes(b"NEW VERSION")

    monkeypatch.setattr(self_update, "is_alive", lambda pid: False)  # старый уже вышел
    spawned = {}

    def _fake_spawn_hidden(cmd, cwd, log_path):
        spawned["cmd"] = list(cmd)
        return 555

    monkeypatch.setattr(self_update, "spawn_hidden", _fake_spawn_hidden)

    rc = self_update.run_self_update_helper(target=str(target), wait_pid=4242,
                                            self_path=str(helper_src))

    assert rc == self_update.RC_OK
    assert target.read_bytes() == b"NEW VERSION"
    backup = target.with_suffix(target.suffix + ".old")
    assert backup.read_bytes() == b"OLD VERSION"
    assert spawned["cmd"] == [str(target)]


def test_run_self_update_helper_wait_timeout_does_not_touch_target(tmp_path, monkeypatch):
    target = tmp_path / "bpmkit-hub.exe"
    target.write_bytes(b"OLD VERSION")
    helper_src = tmp_path / "bpmkit-hub-new.exe"
    helper_src.write_bytes(b"NEW VERSION")

    monkeypatch.setattr(self_update, "is_alive", lambda pid: True)  # никогда не выходит
    monkeypatch.setattr(self_update, "WAIT_EXIT_TIMEOUT_S", 0.05)
    monkeypatch.setattr(self_update, "WAIT_EXIT_POLL_S", 0.01)

    spawned = {"called": False}
    monkeypatch.setattr(self_update, "spawn_hidden",
                        lambda *a, **k: spawned.__setitem__("called", True))

    rc = self_update.run_self_update_helper(target=str(target), wait_pid=4242,
                                            self_path=str(helper_src))

    assert rc == self_update.RC_WAIT_TIMEOUT
    assert target.read_bytes() == b"OLD VERSION"
    assert spawned["called"] is False


def test_run_self_update_helper_elevation_required(tmp_path, monkeypatch):
    target = tmp_path / "bpmkit-hub.exe"
    target.write_bytes(b"OLD VERSION")
    helper_src = tmp_path / "bpmkit-hub-new.exe"
    helper_src.write_bytes(b"NEW VERSION")

    monkeypatch.setattr(self_update, "is_alive", lambda pid: False)

    def _denied(src, dst, **kw):
        err = OSError("access denied")
        err.winerror = 5
        raise err

    monkeypatch.setattr(self_update, "_replace_with_retry", _denied)
    spawned = {"called": False}
    monkeypatch.setattr(self_update, "spawn_hidden",
                        lambda *a, **k: spawned.__setitem__("called", True))

    rc = self_update.run_self_update_helper(target=str(target), wait_pid=4242,
                                            self_path=str(helper_src))

    assert rc == self_update.RC_ELEVATION_REQUIRED
    assert target.read_bytes() == b"OLD VERSION"
    assert spawned["called"] is False


def test_replace_with_retry_retries_then_succeeds(tmp_path, monkeypatch):
    src = tmp_path / "src.exe"
    src.write_bytes(b"NEW")
    dst = tmp_path / "dst.exe"
    dst.write_bytes(b"OLD")

    attempts = {"n": 0}
    import shutil as _shutil
    real_copy2 = _shutil.copy2

    def _flaky_copy2(s, d):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise OSError("busy")
        return real_copy2(s, d)

    monkeypatch.setattr(self_update.shutil, "copy2", _flaky_copy2)
    self_update._replace_with_retry(src, dst, attempts=5, pause=0.0, sleep=lambda s: None)
    assert dst.read_bytes() == b"NEW"
    assert attempts["n"] == 3


def test_replace_with_retry_raises_after_exhausting_attempts(tmp_path, monkeypatch):
    src = tmp_path / "src.exe"
    src.write_bytes(b"NEW")
    dst = tmp_path / "dst.exe"
    dst.write_bytes(b"OLD")

    def _always_fail(s, d):
        raise OSError("permanently busy")

    monkeypatch.setattr(self_update.shutil, "copy2", _always_fail)
    with pytest.raises(OSError):
        self_update._replace_with_retry(src, dst, attempts=3, pause=0.0, sleep=lambda s: None)
    assert dst.read_bytes() == b"OLD"
