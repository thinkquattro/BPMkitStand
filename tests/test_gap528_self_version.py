# -*- coding: utf-8 -*-
"""GAP-528: `GET/POST /api/hub/self-version` — проверка версии ДИСПЕТЧЕРА без
лицензии (для свободной редакции, где окно «Обновления» показывает только
диспетчер), и модуль `standkit_hub.self_version`, на котором маршрут стоит.

Держим три уровня:
  * чистые функции модуля (`parse_version`/`compare_versions`,
    `fetch_pypi_latest`, `cached_pypi_latest`) — без HTTP-сервера вовсе;
  * маршрут хаба: авторизация та же, что у прочих `GET`/`POST /api/*`
    (обычная сессия, БЕЗ лицензии — в этом весь смысл гэпа), pip-режим,
    frozen-режим, сетевая ошибка → поле `error`, а не исключение, кэш между
    обычными `GET`, принудительный обход кэша у `POST .../check`;
  * `standkit_companion.hub_channel.check_hub_pypi` теперь тонкая обёртка над
    тем же модулем — регресс на неё см. `tests/test_hub_channel.py`.
"""
from __future__ import annotations

import pytest

import standkit_hub.server as server_module
from standkit_hub import self_version as sv
from tests.test_hub_server import _close_hub_servers, _request, _start_hub  # noqa: F401


@pytest.fixture(autouse=True)
def _isolated_cache():
    """Кэш PyPI — в памяти ПРОЦЕССА (см. докстринг модуля), значит общий между
    тестами в одном прогоне pytest. Сбрасываем ДО и ПОСЛЕ каждого теста —
    иначе порядок тестов начинает влиять на результат."""
    sv.reset_cache()
    yield
    sv.reset_cache()


def _post(base_url, path, token, *, body=None):
    return _request(base_url, path, token=token, method="POST", body=body,
                    origin=base_url)


class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._payload


def _fake_urlopen_factory(*, latest: str, calls: list):
    def _fake(req, timeout=None):
        calls.append(1)
        body = ('{"info": {"version": "%s"}}' % latest).encode("utf-8")
        return _FakeResponse(body)
    return _fake


def _boom_urlopen(*a, **kw):
    raise OSError("сеть недоступна")


# ======================================================================================
# Уровень 1 — чистые функции модуля
# ======================================================================================
def test_parse_version_segments_are_ints():
    assert sv.parse_version("1.2.10") == (1, 2, 10)
    assert sv.parse_version("v0.12.11") == (0, 12, 11)
    assert sv.parse_version("") == ()
    assert sv.parse_version(None) == ()


def test_compare_versions_is_numeric_not_lexicographic():
    # "0.9.0" < "0.10.0" лексикографически НЕВЕРНО читает как "больше" — сверка
    # обязана быть посегментной, а не строковой.
    assert sv.compare_versions("0.10.0", "0.9.0") > 0
    assert sv.compare_versions("1.2", "1.2.0") == 0
    assert sv.compare_versions("1.2.0", "1.3.0") < 0


def test_fetch_pypi_latest_success(monkeypatch):
    calls = []
    monkeypatch.setattr(sv, "urlopen", _fake_urlopen_factory(latest="9.9.9", calls=calls))
    result = sv.fetch_pypi_latest()
    assert result == {"latest": "9.9.9", "error": None}
    assert calls == [1]


def test_fetch_pypi_latest_network_error_is_not_an_exception(monkeypatch):
    monkeypatch.setattr(sv, "urlopen", _boom_urlopen)
    result = sv.fetch_pypi_latest()
    assert result["latest"] is None
    assert "OSError" in result["error"]


def test_cached_pypi_latest_caches_between_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(sv, "urlopen", _fake_urlopen_factory(latest="9.9.9", calls=calls))
    first = sv.cached_pypi_latest()
    second = sv.cached_pypi_latest()
    assert first == second
    assert calls == [1], "второй обычный вызов обязан отдать кэш, не ходить в сеть повторно"


def test_cached_pypi_latest_force_bypasses_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(sv, "urlopen", _fake_urlopen_factory(latest="9.9.9", calls=calls))
    sv.cached_pypi_latest()
    sv.cached_pypi_latest(force=True)
    assert calls == [1, 1], "force=True обязан обойти кэш и сходить в сеть заново"


# ======================================================================================
# Уровень 2 — маршрут хаба
# ======================================================================================
def test_self_version_get_without_token_is_unauthorized(tmp_path):
    base_url, _token, *_ = _start_hub(tmp_path)
    status, _body, _ = _request(base_url, "/api/hub/self-version")
    assert status == 401


def test_self_version_check_post_without_token_is_forbidden(tmp_path):
    base_url, _token, *_ = _start_hub(tmp_path)
    status, _body, _ = _request(base_url, "/api/hub/self-version/check", method="POST",
                                origin=base_url)
    assert status == 403


def test_self_version_pip_mode_reports_shape_and_update(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "is_frozen", lambda: False)
    import standkit

    current = standkit.__version__
    segments = list(sv.parse_version(current)) or [0]
    segments[-1] += 1
    bumped = ".".join(str(s) for s in segments)
    calls = []
    monkeypatch.setattr(sv, "urlopen", _fake_urlopen_factory(latest=bumped, calls=calls))

    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/hub/self-version", token=token)

    assert status == 200
    assert body["mode"] == "pip"
    assert body["current"] == current
    assert body["latest"] == bumped
    assert body["update_available"] is True
    assert body["source"] == "pypi"
    assert body["checked_at"] is not None
    assert body["error"] is None
    assert body["pip_command"] == "python -m pip install -U standkit"
    assert isinstance(body["on_disk"], str)
    assert body["companion"] == server_module.companion_available()


def test_self_version_pip_mode_up_to_date_is_not_update_available(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "is_frozen", lambda: False)
    import standkit

    current = standkit.__version__
    calls = []
    monkeypatch.setattr(sv, "urlopen", _fake_urlopen_factory(latest=current, calls=calls))

    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/hub/self-version", token=token)
    assert status == 200
    assert body["update_available"] is False


def test_self_version_frozen_mode_skips_pypi(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "is_frozen", lambda: True)

    def _refuse(*a, **kw):
        raise AssertionError("frozen-режим не имеет права спрашивать PyPI")

    monkeypatch.setattr(sv, "urlopen", _refuse)

    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/hub/self-version", token=token)

    assert status == 200
    assert body["mode"] == "frozen"
    assert body["latest"] is None
    assert body["source"] is None
    assert body["checked_at"] is None
    assert body["error"] is None
    assert body["update_available"] is False


def test_self_version_network_error_becomes_error_field(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "is_frozen", lambda: False)
    monkeypatch.setattr(sv, "urlopen", _boom_urlopen)

    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/hub/self-version", token=token)

    assert status == 200
    assert body["mode"] == "pip"
    assert body["latest"] is None
    assert body["update_available"] is False
    assert body["error"] is not None and "OSError" in body["error"]


def test_self_version_get_uses_cache_across_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "is_frozen", lambda: False)
    calls = []
    monkeypatch.setattr(sv, "urlopen", _fake_urlopen_factory(latest="1.0.0", calls=calls))

    base_url, token, *_ = _start_hub(tmp_path)
    _request(base_url, "/api/hub/self-version", token=token)
    _request(base_url, "/api/hub/self-version", token=token)

    assert calls == [1], "второй GET обязан отдать кэш процесса, не сетевой поход"


def test_self_version_check_post_forces_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(sv, "is_frozen", lambda: False)
    calls = []
    monkeypatch.setattr(sv, "urlopen", _fake_urlopen_factory(latest="1.0.0", calls=calls))

    base_url, token, *_ = _start_hub(tmp_path)
    _request(base_url, "/api/hub/self-version", token=token)
    status, body, _ = _post(base_url, "/api/hub/self-version/check", token)

    assert status == 200
    assert body["mode"] == "pip"
    assert calls == [1, 1], "POST .../check обязан обойти кэш и сходить в сеть заново"
