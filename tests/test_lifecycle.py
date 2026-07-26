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


# --- stop: стенд запущен НЕ диспетчером (нет pidfile) → усыновление ---


def _up(monkeypatch, *, up: bool = True) -> None:
    """Мокает TCP-пробу «стенд отвечает по порту» (реальная сеть не трогается)."""
    from standkit import health as health_module

    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: up)


def _candidate(stand, pid: int = 12345, **overrides):
    from standkit.adopt import AdoptCandidate

    kwargs = dict(
        pid=pid,
        port=stand.stand_port,
        image="dotnet.exe",
        cwd=stand.stand_dir,
    )
    kwargs.update(overrides)
    return AdoptCandidate(**kwargs)


def test_stop_returns_true_when_no_pidfile_and_stand_not_running(tmp_path, monkeypatch):
    # Нет pidfile и порт закрыт → останавливать нечего, возвращаем True.
    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5000)
    run_dir = tmp_path / "run"
    _up(monkeypatch, up=False)

    assert lifecycle.stop(stand, run_dir=run_dir) is True


def test_stop_without_pidfile_raises_adoption_required_and_does_not_kill(tmp_path, monkeypatch):
    # Кандидат найден и валиден, но подтверждения (force) не было → наверх
    # уходит AdoptionRequired с кандидатом, процесс НЕ убивается, pidfile НЕ
    # пишется. Молчаливый kill по номеру порта запрещён.
    from standkit import adopt as adopt_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    run_dir = tmp_path / "run"
    _up(monkeypatch)
    monkeypatch.setattr(adopt_module, "find_candidate", lambda s: _candidate(s))

    def _boom(*a, **kw):
        raise AssertionError("процесс не должен убиваться без force")

    monkeypatch.setattr(lifecycle._platform, "stop", _boom)

    with pytest.raises(lifecycle.AdoptionRequired) as excinfo:
        lifecycle.stop(stand, run_dir=run_dir)

    assert excinfo.value.candidate.pid == 12345
    assert not lifecycle.pidfile_path(stand, run_dir).exists()


def test_stop_with_force_adopts_writes_pidfile_and_stops(tmp_path, monkeypatch):
    from standkit import adopt as adopt_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    run_dir = tmp_path / "run"
    _up(monkeypatch)
    monkeypatch.setattr(adopt_module, "find_candidate", lambda s: _candidate(s))
    stopped = []
    monkeypatch.setattr(lifecycle._platform, "stop", lambda pid, **kw: stopped.append(pid) or True)

    assert lifecycle.stop(stand, run_dir=run_dir, force=True) is True
    assert stopped == [12345]
    # pidfile удаляется после успешной остановки — стенда больше нет.
    assert not lifecycle.pidfile_path(stand, run_dir).exists()


def test_stop_refuses_when_candidate_is_a_stranger(tmp_path, monkeypatch):
    # Порт занят чужим процессом (каталог не совпал) — даже с force усыновления
    # не происходит, отказ с внятным текстом.
    from standkit import adopt as adopt_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    other = tmp_path / "other"
    other.mkdir()
    run_dir = tmp_path / "run"
    _up(monkeypatch)
    monkeypatch.setattr(
        adopt_module, "find_candidate", lambda s: _candidate(s, cwd=str(other))
    )
    monkeypatch.setattr(
        lifecycle._platform,
        "stop",
        lambda pid, **kw: (_ for _ in ()).throw(AssertionError("чужой процесс убивать нельзя")),
    )

    with pytest.raises(LifecycleError, match="не похож на этот стенд"):
        lifecycle.stop(stand, run_dir=run_dir, force=True)


def test_stop_raises_adoption_unavailable_when_owner_not_found(tmp_path, monkeypatch):
    # Стенд жив, но владельца порта определить не удалось (нет прав/утилит) —
    # честный отказ отдельного типа (хаб отвечает на него 404).
    from standkit import adopt as adopt_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    run_dir = tmp_path / "run"
    _up(monkeypatch)
    monkeypatch.setattr(adopt_module, "find_candidate", lambda s: None)

    with pytest.raises(lifecycle.AdoptionUnavailable, match="вне диспетчера"):
        lifecycle.stop(stand, run_dir=run_dir)


def test_stop_with_stale_pidfile_reestablishes_via_adoption(tmp_path, monkeypatch):
    # Протухший pidfile (процесс из него мёртв) не должен уводить нас в
    # platform.stop() по чужому/переиспользованному pid: файл удаляется, стенд
    # усыновляется заново по реальному владельцу порта.
    from standkit import adopt as adopt_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    run_dir = tmp_path / "run"
    pf = lifecycle.pidfile_path(stand, run_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("111", encoding="utf-8")

    _up(monkeypatch)
    monkeypatch.setattr(lifecycle._platform, "is_alive", lambda pid: False)
    monkeypatch.setattr(adopt_module, "find_candidate", lambda s: _candidate(s, pid=22222))
    stopped = []
    monkeypatch.setattr(lifecycle._platform, "stop", lambda pid, **kw: stopped.append(pid) or True)

    assert lifecycle.stop(stand, run_dir=run_dir, force=True) is True
    assert stopped == [22222], "остановлен должен быть реальный владелец порта, а не протухший pid"


def test_stop_with_stale_pidfile_and_dead_stand_returns_true(tmp_path, monkeypatch):
    # Протухший pidfile и закрытый порт → останавливать нечего; протухший файл
    # при этом убран.
    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    run_dir = tmp_path / "run"
    pf = lifecycle.pidfile_path(stand, run_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("111", encoding="utf-8")

    _up(monkeypatch, up=False)
    monkeypatch.setattr(lifecycle._platform, "is_alive", lambda pid: False)

    assert lifecycle.stop(stand, run_dir=run_dir) is True
    assert not pf.exists()


def test_restart_of_external_stand_requires_adoption(tmp_path, monkeypatch):
    # Рестарт падал вместе со stop — теперь он тоже спрашивает подтверждение,
    # а не валится с общим отказом.
    from standkit import adopt as adopt_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    run_dir = tmp_path / "run"
    _up(monkeypatch)
    monkeypatch.setattr(adopt_module, "find_candidate", lambda s: _candidate(s))

    with pytest.raises(lifecycle.AdoptionRequired):
        lifecycle.restart(stand, run_dir=run_dir, log_dir=tmp_path / "logs")


# --- adopt(): усыновление как отдельный шаг ---


def test_adopt_writes_pidfile_and_does_not_stop_process(tmp_path, monkeypatch):
    from standkit import adopt as adopt_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    run_dir = tmp_path / "run"
    monkeypatch.setattr(adopt_module, "find_candidate", lambda s: _candidate(s))
    monkeypatch.setattr(
        lifecycle._platform,
        "stop",
        lambda pid, **kw: (_ for _ in ()).throw(AssertionError("adopt не останавливает стенд")),
    )

    candidate = lifecycle.adopt(stand, run_dir=run_dir)

    assert candidate.pid == 12345
    assert lifecycle.pidfile_path(stand, run_dir).read_text(encoding="utf-8").strip() == "12345"


def test_adopt_raises_unavailable_when_no_candidate(tmp_path, monkeypatch):
    from standkit import adopt as adopt_module

    stand = _make_stand(tmp_path, stand_host="127.0.0.1", stand_port=5030)
    monkeypatch.setattr(adopt_module, "find_candidate", lambda s: None)

    with pytest.raises(lifecycle.AdoptionUnavailable):
        lifecycle.adopt(stand, run_dir=tmp_path / "run")


def test_adopt_rejects_non_kestrel_host_kind(tmp_path):
    from standkit.models import HostKind

    stand = _make_stand(tmp_path, host_kind=HostKind.DOCKER, docker_container="c1")
    with pytest.raises(LifecycleError, match="kestrel"):
        lifecycle.adopt(stand, run_dir=tmp_path / "run")


# --- is_managed(): основа бейджа «вне диспетчера» ---


def test_is_managed_false_without_pidfile(tmp_path):
    stand = _make_stand(tmp_path)
    assert lifecycle.is_managed(stand, run_dir=tmp_path / "run") is False


def test_is_managed_true_with_live_pidfile(tmp_path, monkeypatch):
    stand = _make_stand(tmp_path)
    run_dir = tmp_path / "run"
    pf = lifecycle.pidfile_path(stand, run_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(lifecycle._platform, "is_alive", lambda pid: pid == 4242)

    assert lifecycle.is_managed(stand, run_dir=run_dir) is True


def test_is_managed_false_with_stale_pidfile(tmp_path, monkeypatch):
    stand = _make_stand(tmp_path)
    run_dir = tmp_path / "run"
    pf = lifecycle.pidfile_path(stand, run_dir)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(lifecycle._platform, "is_alive", lambda pid: False)

    assert lifecycle.is_managed(stand, run_dir=run_dir) is False


def test_is_managed_true_for_non_kestrel_host_kinds(tmp_path):
    # У iis/docker/k8s понятия «pidfile диспетчера» нет — бейдж «вне
    # диспетчера» им не показываем, docker stop работает и без усыновления.
    from standkit.models import HostKind

    stand = _make_stand(tmp_path, host_kind=HostKind.DOCKER, docker_container="c1")
    assert lifecycle.is_managed(stand, run_dir=tmp_path / "run") is True
