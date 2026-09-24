# -*- coding: utf-8 -*-
"""Тесты границы ядра для двух новых каналов (GAP-523/GAP-288): маршруты
``/api/companion/{check,stage,apply}-{hub,skills}``, ``/api/hub/open-folder`` и
самообновление диспетчера (``apply_hub`` завершает ТЕКУЩИЙ процесс тем же
путём, что ``POST /api/hub/shutdown``).

Переиспользует инфраструктуру ``tests/test_hub_companion_api.py`` (``_start_hub``,
``_post``, ``_StubRunner``, фикстуру гашения серверов) — заводить вторую копию
означало бы вторую версию, которая разойдётся с первой (тот же приём, что у
``test_companion_installer_channel.py`` относительно ``test_companion_releases.py``).
"""
from __future__ import annotations

import pytest

import standkit_hub.server as server_module
from standkit_hub.config import CompanionSettings
from tests.test_hub_companion_api import (
    _StubRunner,
    _close_hub_servers,  # noqa: F401 - фикстура, автоприменяется через autouse ниже
    _install_stub_runner,
    _post,
    _start_hub,
)

# Новые маршруты этой серии — ДОСЛОВНО, тот же приём "поймать переименование",
# что у ACTION_ROUTES в test_hub_companion_api.py.
NEW_ACTION_ROUTES = {
    "/api/companion/check-hub": "check_hub",
    "/api/companion/stage-hub": "stage_hub",
    "/api/companion/apply-hub": "apply_hub",
    "/api/companion/check-skills": "check_skills",
    "/api/companion/stage-skills": "stage_skills",
    "/api/companion/apply-skills": "apply_skills",
}


# `_close_hub_servers` (импортирована выше) — autouse-фикстура из
# test_hub_companion_api.py: сам факт импорта функции, помеченной
# `@pytest.fixture(autouse=True)`, в пространство имён ЭТОГО модуля регистрирует
# её как фикстуру и здесь (pytest собирает фикстуры по объектам в модуле, а не
# по месту исходного определения) — второй копии уборки заводить не нужно.


# --------------------------------------------------------------------------------------
# Таблица маршрутов -- никакого дрейфа имён действий
# --------------------------------------------------------------------------------------
def test_new_routes_are_registered_in_server_table():
    for path, action in NEW_ACTION_ROUTES.items():
        assert server_module.COMPANION_ACTION_ROUTES.get(path) == action, path


def test_new_error_kinds_map_to_409():
    for kind in ("hub_not_available", "skills_not_available", "self_update_unsupported"):
        assert server_module.COMPANION_ERROR_STATUS[kind] == 409


@pytest.mark.parametrize("path,action", sorted(NEW_ACTION_ROUTES.items()))
def test_route_calls_expected_runner_action(tmp_path, monkeypatch, path, action):
    runner = _install_stub_runner(monkeypatch, _StubRunner(result={"done": action}))
    base_url, token, *_ = _start_hub(tmp_path)

    status, body, _ = _post(base_url, path, token)

    assert status == 200, body
    assert runner.calls == [(action, None)]
    assert body["ok"] is True
    assert body["result"] == {"done": action}


# --------------------------------------------------------------------------------------
# apply_hub -- успешный запуск помощника завершает ТЕКУЩИЙ процесс диспетчера
# --------------------------------------------------------------------------------------
def test_apply_hub_success_triggers_self_shutdown(tmp_path, monkeypatch):
    _install_stub_runner(monkeypatch, _StubRunner(
        result={"launched": True, "pid": 4242, "version": "0.13.0"}))
    base_url, token, _config_path, httpd = _start_hub(tmp_path)

    shutdown_calls = {"n": 0}
    original_request_self_shutdown = httpd.request_self_shutdown

    def _tracking_shutdown():
        shutdown_calls["n"] += 1
        # НЕ зовём оригинал -- реальный shutdown() убил бы сервер теста раньше,
        # чем HTTP-клиент дочитает ответ; предмет теста -- САМ ФАКТ вызова
        # (тот же путь, что POST /api/hub/shutdown), не его последствия.

    monkeypatch.setattr(httpd, "request_self_shutdown", _tracking_shutdown)

    status, body, _ = _post(base_url, "/api/companion/apply-hub", token)

    assert status == 200, body
    assert body["result"]["launched"] is True
    assert shutdown_calls["n"] == 1


def test_apply_hub_failure_does_not_trigger_shutdown(tmp_path, monkeypatch):
    """`run_action` кинул типизированный отказ (`nothing_staged` и т.п.) —
    процесс диспетчера не имеет права начать завершаться."""
    from standkit_companion.errors import ChannelError

    _install_stub_runner(monkeypatch, _StubRunner(
        error=ChannelError("нет подготовленного диспетчера", kind="nothing_staged")))
    base_url, token, _config_path, httpd = _start_hub(tmp_path)

    shutdown_calls = {"n": 0}
    monkeypatch.setattr(httpd, "request_self_shutdown",
                        lambda: shutdown_calls.__setitem__("n", shutdown_calls["n"] + 1))

    status, _body, _ = _post(base_url, "/api/companion/apply-hub", token)

    assert status == 409
    assert shutdown_calls["n"] == 0


def test_apply_hub_result_without_launched_flag_does_not_trigger_shutdown(tmp_path, monkeypatch):
    """`run_action` вернулся успешно, но БЕЗ ``launched: True`` (например,
    заглушка теста) -- хаб не имеет права трактовать это как команду выйти."""
    _install_stub_runner(monkeypatch, _StubRunner(result={"launched": False}))
    base_url, token, _config_path, httpd = _start_hub(tmp_path)

    shutdown_calls = {"n": 0}
    monkeypatch.setattr(httpd, "request_self_shutdown",
                        lambda: shutdown_calls.__setitem__("n", shutdown_calls["n"] + 1))

    status, _body, _ = _post(base_url, "/api/companion/apply-hub", token)

    assert status == 200
    assert shutdown_calls["n"] == 0


# --------------------------------------------------------------------------------------
# /api/hub/open-folder -- цель ФИКСИРОВАНА, путь из тела не принимается
# --------------------------------------------------------------------------------------
def test_open_folder_rejects_unknown_target(tmp_path, monkeypatch):
    _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, *_ = _start_hub(tmp_path)

    status, body, _ = _post(base_url, "/api/hub/open-folder", token,
                            body={"target": "../../../etc"})
    assert status == 400
    assert "plugin" in body["error"]


def test_open_folder_plugin_calls_logs_browser_open_folder(tmp_path, monkeypatch):
    _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, *_ = _start_hub(tmp_path)

    calls = []

    class _Result:
        ok = True
        message = "открыто"

    def _fake_open_folder(path):
        calls.append(path)
        return _Result()

    monkeypatch.setattr(server_module.logs_browser, "open_folder", _fake_open_folder)

    status, body, _ = _post(base_url, "/api/hub/open-folder", token, body={"target": "plugin"})
    assert status == 200, body
    assert body["ok"] is True
    assert len(calls) == 1
