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


def test_sid_match_writes_accepted_and_proceeds_to_bind(tmp_path, monkeypatch, _no_real_bind):
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
    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["status"] == "accepted"


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
