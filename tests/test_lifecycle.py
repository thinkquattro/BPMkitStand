"""
Тесты standkit.lifecycle: резолв ``dotnet`` из PATH/абсолютного пути
(``_resolve_dotnet``) и проверка "процесс не умер сразу после старта" внутри
``start()`` — оба фикса лечат "тихий" провал старта стенда: раньше start()
мог вернуть pid как успех, хотя стенд так и не поднялся (неверный/отсутствующий
dotnet в PATH либо .NET-хост падал за доли секунды после спавна), без единой
ошибки в UI хаба.

Все тесты мокают ``shutil.which``/``standkit.platform.spawn_hidden``/
``standkit.platform.is_alive`` — ни один не спавнит реальный процесс и не
трогает системный PATH.
"""

from __future__ import annotations

import shutil

import pytest

from standkit import lifecycle
from standkit.lifecycle import LifecycleError, _resolve_dotnet, start
from standkit.models import Stand


def _make_stand(tmp_path, **overrides) -> Stand:
    stand_dir = tmp_path / "stand"
    stand_dir.mkdir(exist_ok=True)
    kwargs = dict(name="demo", stand_dir=str(stand_dir))
    kwargs.update(overrides)
    return Stand(**kwargs)


# --- _resolve_dotnet: путь к файлу vs голое имя из PATH ---


def test_resolve_dotnet_existing_path_used_as_is_without_which(tmp_path, monkeypatch):
    fake_dotnet = tmp_path / "dotnet.exe"
    fake_dotnet.write_text("stub", encoding="utf-8")

    def _boom(name):
        raise AssertionError("shutil.which не должен вызываться для уже существующего пути")

    monkeypatch.setattr(shutil, "which", _boom)
    assert _resolve_dotnet(str(fake_dotnet)) == str(fake_dotnet)


def test_resolve_dotnet_bare_name_resolved_via_which(monkeypatch):
    resolved = r"C:\Program Files\dotnet\dotnet.exe"
    monkeypatch.setattr(shutil, "which", lambda name: resolved if name == "dotnet" else None)
    assert _resolve_dotnet("dotnet") == resolved


def test_resolve_dotnet_not_found_raises_lifecycle_error(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(LifecycleError, match="dotnet"):
        _resolve_dotnet("dotnet")


# --- start(): резолв dotnet выполняется ДО spawn_hidden ---


def test_start_raises_before_spawn_when_dotnet_not_found(tmp_path, monkeypatch):
    stand = _make_stand(tmp_path, dotnet="dotnet-that-does-not-exist")

    monkeypatch.setattr(shutil, "which", lambda name: None)

    def _boom(*a, **kw):
        raise AssertionError("spawn_hidden не должен вызываться, если dotnet не резолвится")

    monkeypatch.setattr(lifecycle._platform, "spawn_hidden", _boom)

    with pytest.raises(LifecycleError, match="dotnet"):
        start(stand, run_dir=tmp_path / "run", log_dir=tmp_path / "logs", startup_check_delay=0)


# --- start(): проверка "процесс жив" сразу после спавна ---


def test_start_raises_when_process_dies_immediately(tmp_path, monkeypatch):
    fake_dotnet = tmp_path / "dotnet.exe"
    fake_dotnet.write_text("stub", encoding="utf-8")
    stand = _make_stand(tmp_path, dotnet=str(fake_dotnet))

    monkeypatch.setattr(lifecycle._platform, "spawn_hidden", lambda *a, **kw: 9999)
    monkeypatch.setattr(lifecycle._platform, "is_alive", lambda pid: False)

    run_dir = tmp_path / "run"
    with pytest.raises(LifecycleError, match="логи"):
        start(stand, run_dir=run_dir, log_dir=tmp_path / "logs", startup_check_delay=0)

    # pidfile не должен быть создан — "тихий" провал не должен считаться успехом.
    assert not lifecycle.pidfile_path(stand, run_dir).exists()


def test_start_succeeds_and_writes_pidfile_when_process_alive(tmp_path, monkeypatch):
    fake_dotnet = tmp_path / "dotnet.exe"
    fake_dotnet.write_text("stub", encoding="utf-8")
    stand = _make_stand(tmp_path, dotnet=str(fake_dotnet))

    monkeypatch.setattr(lifecycle._platform, "spawn_hidden", lambda *a, **kw: 4242)
    monkeypatch.setattr(lifecycle._platform, "is_alive", lambda pid: True)

    run_dir = tmp_path / "run"
    pid = start(stand, run_dir=run_dir, log_dir=tmp_path / "logs", startup_check_delay=0)

    assert pid == 4242
    assert lifecycle.pidfile_path(stand, run_dir).read_text(encoding="utf-8").strip() == "4242"


def test_start_returns_existing_pid_without_resolving_dotnet(tmp_path, monkeypatch):
    # Если стенд уже жив (по pidfile) — start() обязан вернуться раньше, чем
    # дойдёт до резолва dotnet/спавна нового процесса.
    stand = _make_stand(tmp_path, dotnet="dotnet-should-not-be-touched")
    run_dir = tmp_path / "run"
    pf = lifecycle.pidfile_path(stand, run_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("123", encoding="utf-8")

    monkeypatch.setattr(lifecycle._platform, "is_alive", lambda pid: pid == 123)

    def _boom(name):
        raise AssertionError("резолв dotnet не нужен, если стенд уже жив")

    monkeypatch.setattr(shutil, "which", _boom)

    assert start(stand, run_dir=run_dir, log_dir=tmp_path / "logs", startup_check_delay=0) == 123


# --- stop: честный отказ, если стенд запущен НЕ диспетчером (нет pidfile) ---


def test_stop_honest_refusal_when_no_pidfile_but_stand_is_up(tmp_path, monkeypatch):
    # Нет pidfile (стенд поднят вне диспетчера), но порт отвечает → мы НЕ знаем
    # pid и не можем убить процесс → честный LifecycleError, а не фейковый успех.
    from standkit import health as health_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5000)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: True)

    with pytest.raises(LifecycleError, match="не диспетчером"):
        lifecycle.stop(stand, run_dir=run_dir)


def test_stop_returns_true_when_no_pidfile_and_stand_not_running(tmp_path, monkeypatch):
    # Нет pidfile и порт закрыт → останавливать нечего, возвращаем True.
    from standkit import health as health_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5000)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: False)

    assert lifecycle.stop(stand, run_dir=run_dir) is True
