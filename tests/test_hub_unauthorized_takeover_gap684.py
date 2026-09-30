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
