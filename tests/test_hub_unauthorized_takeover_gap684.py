# -*- coding: utf-8 -*-
"""«сессия дашборда не подтверждена» не должна вести по кругу.

Ярлык при живом хабе открывал браузер БЕЗ токена; без сессионной cookie вкладка получала 401,
а совет «запустите диспетчер ярлыком» снова открывал тот же процесс. Теперь работающий хаб
отмечает заход без сессии файлом-маркером (токена в нём нет), а следующий запуск по свежему
маркеру перезапускает хаб (takeover) и открывает ссылку со свежим токеном.
"""

from __future__ import annotations

import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from standkit_hub import instance as _instance
from standkit_hub.instance import HubInstanceState, should_takeover

from .test_hub_license_api import _start_hub


def _state(**kw):
    base = dict(pid=1234, host="127.0.0.1", port=8770, elevated=False, version="1.0")
    base.update(kw)
    return HubInstanceState(**base)


# ---------------------------------------------------------------- маркер

def test_marker_roundtrip(tmp_path):
    sf = tmp_path / "standkit-hub.json"
    assert not _instance.unauthorized_seen(sf)
    _instance.mark_unauthorized(sf)
    assert _instance.unauthorized_seen(sf)
    _instance.clear_unauthorized(sf)
    assert not _instance.unauthorized_seen(sf)


def test_marker_does_not_contain_token_like_data(tmp_path):
    sf = tmp_path / "standkit-hub.json"
    _instance.mark_unauthorized(sf)
    text = _instance.unauthorized_marker_path(sf).read_text(encoding="utf-8")
    assert set(__import__("json").loads(text)) == {"at"}


def test_stale_marker_is_ignored(tmp_path):
    sf = tmp_path / "standkit-hub.json"
    _instance.mark_unauthorized(sf)
    path = _instance.unauthorized_marker_path(sf)
    old = time.time() - _instance.UNAUTHORIZED_MAX_AGE_SEC - 5
    os.utime(path, (old, old))
    assert not _instance.unauthorized_seen(sf)


def test_clear_without_marker_is_silent(tmp_path):
    _instance.clear_unauthorized(tmp_path / "standkit-hub.json")


# ---------------------------------------------------------------- решение о перехвате

def test_takeover_when_unauthorized_seen():
    assert should_takeover(_state(), we_elevated=False, our_version="1.0",
                           unauthorized_seen=True) is True


def test_no_takeover_without_marker():
    assert should_takeover(_state(), we_elevated=False, our_version="1.0") is False


def test_unauthorized_marker_never_overrides_other_account():
    running = _state(user_sid="S-1-5-21-AAA")
    assert should_takeover(running, we_elevated=False, our_sid="S-1-5-21-BBB",
                           our_version="1.0", unauthorized_seen=True) is False


def test_no_state_means_no_takeover_even_with_marker():
    assert should_takeover(None, we_elevated=False, unauthorized_seen=True) is False


# ---------------------------------------------------------------- хаб отмечает заход

@pytest.fixture()
def running_hub(tmp_path):
    base_url, token, httpd = _start_hub(tmp_path)
    state_file = tmp_path / "run" / "standkit-hub.json"
    state_file.parent.mkdir()
    httpd.instance_state_file = state_file
    yield base_url, token, state_file
    for attr in ("status_poller", "companion_runner"):
        worker = getattr(httpd, attr, None)
        if worker is not None:
            try:
                worker.stop(timeout=0.2)
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=httpd.shutdown, daemon=True).start()
    try:
        httpd.server_close()
    except Exception:  # noqa: BLE001
        pass


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5.0) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_root_without_session_marks_and_authed_root_clears(running_hub):
    base_url, token, state_file = running_hub
    _get(base_url + "/")
    assert _instance.unauthorized_seen(state_file)
    _get(base_url + f"/?t={token}")
    assert not _instance.unauthorized_seen(state_file)


# ====================================================================================
# GAP-821 п.5: хаб, поднятый в фоне и ни разу не открытый со своим токеном
# ====================================================================================

_OLD = time.time() - 3600          # хаб поднят час назад
_GRACE = _instance.SESSION_UNCONFIRMED_GRACE_SEC


class TestGap821UnconfirmedSession:
    def test_background_hub_without_confirmed_session_is_unconfirmed(self, tmp_path):
        sf = tmp_path / "standkit-hub.json"
        assert _instance.session_unconfirmed(sf, _state(started_at=_OLD)) is True

    def test_confirmed_session_is_not_unconfirmed(self, tmp_path):
        sf = tmp_path / "standkit-hub.json"
        _instance.mark_session_confirmed(sf)
        assert _instance.session_unconfirmed(sf, _state(started_at=_OLD)) is False

    def test_marker_older_than_hub_start_is_a_leftover(self, tmp_path):
        """Остаток маркера от прошлого процесса (удаление при старте best-effort) не в счёт."""
        sf = tmp_path / "standkit-hub.json"
        _instance.mark_session_confirmed(sf)
        path = _instance.session_ok_marker_path(sf)
        os.utime(path, (_OLD - 100, _OLD - 100))
        assert _instance.session_unconfirmed(sf, _state(started_at=_OLD)) is True

    def test_fresh_hub_is_not_taken_over_during_double_click(self, tmp_path):
        """Двойной клик по ярлыку: первая вкладка ещё грузится, хаб жить обязан."""
        sf = tmp_path / "standkit-hub.json"
        young = _state(started_at=time.time() - _GRACE / 3)
        assert _instance.session_unconfirmed(sf, young) is False

    def test_unknown_start_time_is_never_unconfirmed(self, tmp_path):
        sf = tmp_path / "standkit-hub.json"
        assert _instance.session_unconfirmed(sf, _state(started_at=0.0)) is False
        assert _instance.session_unconfirmed(sf, None) is False

    def test_shortcut_takes_over_background_hub_of_same_version(self):
        assert should_takeover(_state(), we_elevated=False, our_version="1.0",
                               session_unconfirmed=True) is True

    def test_no_takeover_flag_keeps_old_behaviour(self):
        assert should_takeover(_state(), we_elevated=False, our_version="1.0",
                               session_unconfirmed=True, no_takeover=True) is False

    def test_other_account_hub_is_never_taken_over(self):
        running = _state(user_sid="S-1-5-21-AAA")
        assert should_takeover(running, we_elevated=False, our_sid="S-1-5-21-BBB",
                               our_version="1.0", session_unconfirmed=True) is False

    def test_marker_contains_no_token_like_data(self, tmp_path):
        sf = tmp_path / "standkit-hub.json"
        _instance.mark_session_confirmed(sf)
        text = _instance.session_ok_marker_path(sf).read_text(encoding="utf-8")
        assert set(__import__("json").loads(text)) == {"at"}

    def test_clear_without_marker_is_silent(self, tmp_path):
        _instance.clear_session_confirmed(tmp_path / "standkit-hub.json")

    def test_hub_writes_marker_on_first_authed_root_only(self, running_hub):
        base_url, token, state_file = running_hub
        _get(base_url + "/")                              # без сессии -- не подтверждение
        assert not _instance.session_ok_marker_path(state_file).exists()
        _get(base_url + f"/?t={token}")                   # со своим токеном -- подтверждение
        marker = _instance.session_ok_marker_path(state_file)
        assert marker.exists()
        first = marker.stat().st_mtime_ns
        _get(base_url + f"/?t={token}")
        assert marker.stat().st_mtime_ns == first         # повторно файл не переписывается

    def test_takeover_function_stops_unconfirmed_background_hub(self, monkeypatch, tmp_path):
        """Сквозной выбор в __main__._takeover_running_instance: перехват без маркера 401."""
        from standkit_hub import __main__ as hub_main
        from standkit_hub.server import HubAlreadyRunning

        run_dir = tmp_path
        sf = _instance.state_path(run_dir)
        running = _state(started_at=_OLD, version=hub_main._standkit_version, user_sid=None)
        monkeypatch.setattr(_instance, "read_state", lambda path: running)
        stopped = []
        monkeypatch.setattr(_instance, "stop_running_instance",
                            lambda st, *, run_dir, requester_pid=None: (stopped.append(st.pid), (True, ""))[1])
        monkeypatch.setattr(_instance, "wait_port_released", lambda host, port: True)
        monkeypatch.setattr(hub_main, "is_elevated", lambda: False)

        ok, reason = hub_main._takeover_running_instance(
            HubAlreadyRunning("127.0.0.1", 8770, running.version), sf, run_dir,
            explicit=False, our_sid=None)

        assert (ok, reason) == (True, "")
        assert stopped == [running.pid]

    def test_takeover_function_leaves_confirmed_hub_alone(self, monkeypatch, tmp_path):
        from standkit_hub import __main__ as hub_main
        from standkit_hub.server import HubAlreadyRunning

        sf = _instance.state_path(tmp_path)
        _instance.mark_session_confirmed(sf)
        running = _state(started_at=_OLD, version=hub_main._standkit_version)
        monkeypatch.setattr(_instance, "read_state", lambda path: running)
        monkeypatch.setattr(hub_main, "is_elevated", lambda: False)

        ok, _reason = hub_main._takeover_running_instance(
            HubAlreadyRunning("127.0.0.1", 8770, running.version), sf, tmp_path,
            explicit=False, our_sid=None)

        assert ok is False
