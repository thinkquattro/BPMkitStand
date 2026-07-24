"""
Тесты standkit.hosting (ADR-0001) и диспетчеризации lifecycle по host_kind.

Ничего не обращается к реальному Docker/IIS/сети: все внешние вызовы
(``appcmd``/``docker``) мокаются через monkeypatch поверх
``standkit.hosting.subprocess.run`` (или напрямую поверх
``standkit.hosting._run``/``_resolve_appcmd``/``_resolve_docker``, где это
удобнее сформулировать тест). TCP-фолбэк мокается поверх
``standkit.health.tcp_open``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from standkit import health as health_module
from standkit import hosting
from standkit import lifecycle
from standkit.hosting import DockerBackend, HostingError, IisBackend, KestrelBackend, get_backend
from standkit.models import HostKind, Stand


def _make_stand(**overrides) -> Stand:
    kwargs = dict(name="demo", stand_dir="/opt/bpmsoft/demo", stand_host="127.0.0.1", stand_port=5000)
    kwargs.update(overrides)
    return Stand(**kwargs)


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=cmd, returncode=returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------
# get_backend
# --------------------------------------------------------------------------


def test_get_backend_returns_kestrel_backend_by_default():
    stand = _make_stand()
    assert isinstance(get_backend(stand), KestrelBackend)


def test_get_backend_returns_iis_backend():
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1")
    assert isinstance(get_backend(stand), IisBackend)


def test_get_backend_returns_docker_backend():
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert isinstance(get_backend(stand), DockerBackend)


def test_get_backend_raises_for_k8s_placeholder():
    stand = _make_stand(host_kind=HostKind.K8S)
    with pytest.raises(HostingError, match="k8s"):
        get_backend(stand)


# --------------------------------------------------------------------------
# KestrelBackend — делегирует в lifecycle._kestrel_* (не в публичный lifecycle.start и т.п.)
# --------------------------------------------------------------------------


def test_kestrel_backend_start_delegates_to_private_kestrel_start(monkeypatch):
    calls = {}

    def _fake(stand, *, run_dir=None, log_dir=None):
        calls["args"] = (stand, run_dir, log_dir)
        return 4242

    monkeypatch.setattr(lifecycle, "_kestrel_start", _fake)
    stand = _make_stand()
    pid = KestrelBackend().start(stand, run_dir="RD", log_dir="LD")
    assert pid == 4242
    assert calls["args"] == (stand, "RD", "LD")


def test_kestrel_backend_stop_delegates_to_private_kestrel_stop(monkeypatch):
    calls = {}
    monkeypatch.setattr(lifecycle, "_kestrel_stop", lambda stand, *, run_dir=None: calls.setdefault("ok", True) or True)
    assert KestrelBackend().stop(_make_stand(), run_dir="RD") is True
    assert calls["ok"] is True


def test_kestrel_backend_restart_delegates_to_private_kestrel_restart(monkeypatch):
    monkeypatch.setattr(lifecycle, "_kestrel_restart", lambda stand, *, run_dir=None, log_dir=None: 999)
    assert KestrelBackend().restart(_make_stand()) == 999


def test_kestrel_backend_is_running_delegates_to_private_kestrel_is_running(monkeypatch):
    monkeypatch.setattr(lifecycle, "_kestrel_is_running", lambda stand, *, run_dir=None: True)
    assert KestrelBackend().is_running(_make_stand()) is True


def test_kestrel_backend_read_logs_returns_none():
    assert KestrelBackend().read_logs(_make_stand(), 50) is None


# --------------------------------------------------------------------------
# lifecycle: диспетчеризация по host_kind
# --------------------------------------------------------------------------


def test_lifecycle_start_dispatches_kestrel_to_private_function_without_touching_hosting(monkeypatch):
    calls = {}

    def _fake_kestrel_start(stand, **kw):
        calls["kestrel"] = True
        return 111

    monkeypatch.setattr(lifecycle, "_kestrel_start", _fake_kestrel_start)

    def _boom(stand):
        raise AssertionError("hosting.get_backend не должен вызываться для host_kind=kestrel")

    monkeypatch.setattr(hosting, "get_backend", _boom)
    stand = _make_stand(host_kind=HostKind.KESTREL)
    assert lifecycle.start(stand) == 111
    assert calls["kestrel"] is True


def test_lifecycle_start_dispatches_k8s_placeholder_to_kestrel_path(monkeypatch):
    # k8s пока не имеет своего бэкенда — диспетчер использует kestrel-путь (ADR-0001).
    monkeypatch.setattr(lifecycle, "_kestrel_start", lambda stand, **kw: 222)
    stand = _make_stand(host_kind=HostKind.K8S)
    assert lifecycle.start(stand) == 222


def test_lifecycle_start_dispatches_iis_to_hosting_backend(monkeypatch):
    calls = {}

    class _FakeBackend:
        def start(self, stand, *, run_dir=None, log_dir=None):
            calls["args"] = (stand, run_dir, log_dir)
            return None

    monkeypatch.setattr(hosting, "get_backend", lambda stand: _FakeBackend())

    def _boom(stand, **kw):
        raise AssertionError("_kestrel_start не должен вызываться для host_kind=iis")

    monkeypatch.setattr(lifecycle, "_kestrel_start", _boom)

    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1")
    assert lifecycle.start(stand, run_dir="RD", log_dir="LD") is None
    assert calls["args"] == (stand, "RD", "LD")


def test_lifecycle_stop_dispatches_docker_to_hosting_backend(monkeypatch):
    class _FakeBackend:
        def stop(self, stand, *, run_dir=None):
            return True

    monkeypatch.setattr(hosting, "get_backend", lambda stand: _FakeBackend())
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert lifecycle.stop(stand) is True


def test_lifecycle_restart_dispatches_docker_to_hosting_backend(monkeypatch):
    class _FakeBackend:
        def restart(self, stand, *, run_dir=None, log_dir=None):
            return None

    monkeypatch.setattr(hosting, "get_backend", lambda stand: _FakeBackend())
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert lifecycle.restart(stand) is None


def test_lifecycle_is_running_dispatches_iis_to_hosting_backend(monkeypatch):
    class _FakeBackend:
        def is_running(self, stand, *, run_dir=None):
            return True

    monkeypatch.setattr(hosting, "get_backend", lambda stand: _FakeBackend())
    stand = _make_stand(host_kind=HostKind.IIS, iis_site="site1")
    assert lifecycle.is_running(stand) is True


def test_lifecycle_dispatch_requires_local_transport_even_for_iis_docker(monkeypatch):
    from standkit.models import Transport
    from standkit.lifecycle import LifecycleError

    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1", transport=Transport.AGENT, agent_url="https://x")
    with pytest.raises(LifecycleError, match="agent"):
        lifecycle.start(stand)


# --------------------------------------------------------------------------
# IisBackend
# --------------------------------------------------------------------------


def test_iis_backend_raises_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1")
    with pytest.raises(HostingError, match="Windows"):
        IisBackend().start(stand)


def test_iis_backend_raises_when_appcmd_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("WINDIR", str(tmp_path))  # appcmd.exe заведомо не существует внутри
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1")
    with pytest.raises(HostingError, match="appcmd"):
        IisBackend().start(stand)


def _prep_iis_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    inetsrv = tmp_path / "system32" / "inetsrv"
    inetsrv.mkdir(parents=True)
    appcmd = inetsrv / "appcmd.exe"
    appcmd.write_text("stub", encoding="utf-8")
    monkeypatch.setenv("WINDIR", str(tmp_path))
    return str(appcmd)


def test_iis_backend_start_calls_appcmd_start_apppool_and_site(monkeypatch, tmp_path):
    appcmd = _prep_iis_windows(monkeypatch, tmp_path)
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _completed(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1", iis_site="site1")
    IisBackend().start(stand)

    assert [appcmd, "start", "apppool", "/apppool.name:pool1"] in calls
    assert [appcmd, "start", "site", "/site.name:site1"] in calls


def test_iis_backend_start_raises_hostingerror_on_nonzero_returncode(monkeypatch, tmp_path):
    _prep_iis_windows(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(cmd, returncode=1, stderr="boom"))
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1")
    with pytest.raises(HostingError, match="boom"):
        IisBackend().start(stand)


def test_iis_backend_restart_recycles_apppool_when_configured(monkeypatch, tmp_path):
    appcmd = _prep_iis_windows(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _completed(cmd, returncode=0))
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1", iis_site="site1")
    IisBackend().restart(stand)
    assert [appcmd, "recycle", "apppool", "/apppool.name:pool1"] in calls
    assert not any("stop" in c for c in calls)


def test_iis_backend_restart_stop_start_site_when_only_site_configured(monkeypatch, tmp_path):
    appcmd = _prep_iis_windows(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _completed(cmd, returncode=0))
    stand = _make_stand(host_kind=HostKind.IIS, iis_site="site1")
    IisBackend().restart(stand)
    assert [appcmd, "stop", "site", "/site.name:site1"] in calls
    assert [appcmd, "start", "site", "/site.name:site1"] in calls


def test_iis_backend_is_running_true_when_apppool_state_started(monkeypatch, tmp_path):
    _prep_iis_windows(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(cmd, returncode=0, stdout="Started"))
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1")
    assert IisBackend().is_running(stand) is True


def test_iis_backend_is_running_false_falls_back_to_tcp(monkeypatch, tmp_path):
    _prep_iis_windows(monkeypatch, tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(cmd, returncode=0, stdout="Stopped"))
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: True)
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1")
    assert IisBackend().is_running(stand) is True


def test_iis_backend_is_running_appcmd_missing_uses_tcp_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: False)
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1")
    assert IisBackend().is_running(stand) is False


def test_iis_backend_read_logs_none_when_dir_missing(tmp_path):
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1", stand_dir=str(tmp_path / "nope"))
    assert IisBackend().read_logs(stand, 10) is None


def test_iis_backend_read_logs_tails_latest_log_file(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "stdout_1.log").write_text("line1\nline2\n", encoding="utf-8")
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1", iis_stdout_log_dir=str(log_dir))
    lines = IisBackend().read_logs(stand, 10)
    assert lines == ["line1", "line2"]


# --------------------------------------------------------------------------
# DockerBackend
# --------------------------------------------------------------------------


def test_docker_backend_raises_when_docker_not_in_path(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: None)
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    with pytest.raises(HostingError, match="docker"):
        DockerBackend().start(stand)


def test_docker_backend_raises_when_neither_container_nor_compose_configured(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    stand = _make_stand(host_kind=HostKind.DOCKER)
    with pytest.raises(HostingError, match="docker_container"):
        DockerBackend().start(stand)


def test_docker_backend_start_single_container_command(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _completed(cmd, returncode=0))
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    DockerBackend().start(stand)
    assert calls[-1] == ["/usr/bin/docker", "start", "c1"]


def test_docker_backend_start_compose_command(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _completed(cmd, returncode=0))
    stand = _make_stand(
        host_kind=HostKind.DOCKER, docker_compose_file="/opt/x/docker-compose.yml", docker_compose_service="webhost"
    )
    DockerBackend().start(stand)
    assert calls[-1] == [
        "/usr/bin/docker",
        "compose",
        "-f",
        "/opt/x/docker-compose.yml",
        "up",
        "-d",
        "webhost",
    ]


def test_docker_backend_stop_and_restart_single_container(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _completed(cmd, returncode=0))
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert DockerBackend().stop(stand) is True
    assert calls[-1] == ["/usr/bin/docker", "stop", "c1"]
    DockerBackend().restart(stand)
    assert calls[-1] == ["/usr/bin/docker", "restart", "c1"]


def test_docker_backend_is_running_single_container_true(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(cmd, returncode=0, stdout="true\n"))
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert DockerBackend().is_running(stand) is True


def test_docker_backend_is_running_single_container_false_falls_back_to_tcp(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(cmd, returncode=0, stdout="false\n"))
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: True)
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert DockerBackend().is_running(stand) is True


def test_docker_backend_is_running_compose_parses_ps_output(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    ps_output = "NAME       IMAGE   SERVICE   STATUS\nproj-webhost-1  img   webhost   Up 2 hours\n"
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(cmd, returncode=0, stdout=ps_output))
    stand = _make_stand(
        host_kind=HostKind.DOCKER, docker_compose_file="/opt/x/docker-compose.yml", docker_compose_service="webhost"
    )
    assert DockerBackend().is_running(stand) is True


def test_docker_backend_is_running_docker_missing_uses_tcp_fallback(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: None)
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: False)
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert DockerBackend().is_running(stand) is False


def test_docker_backend_is_running_subprocess_error_falls_back_to_tcp(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")

    def _boom(cmd, **kw):
        raise OSError("no such command")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: True)
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert DockerBackend().is_running(stand) is True


def test_docker_backend_read_logs_single_container(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _completed(cmd, returncode=0, stdout="line1\nline2\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    lines = DockerBackend().read_logs(stand, 50)
    assert lines == ["line1", "line2"]
    assert calls[-1] == ["/usr/bin/docker", "logs", "--tail", "50", "c1"]


def test_docker_backend_read_logs_compose_service(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/bin/docker")
    calls = []

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _completed(cmd, returncode=0, stdout="a\nb\n")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    stand = _make_stand(
        host_kind=HostKind.DOCKER, docker_compose_file="/opt/x/docker-compose.yml", docker_compose_service="webhost"
    )
    lines = DockerBackend().read_logs(stand, 20)
    assert lines == ["a", "b"]
    assert calls[-1] == [
        "/usr/bin/docker",
        "compose",
        "-f",
        "/opt/x/docker-compose.yml",
        "logs",
        "--tail",
        "20",
        "webhost",
    ]


def test_docker_backend_read_logs_none_when_docker_missing(monkeypatch):
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: None)
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1")
    assert DockerBackend().read_logs(stand, 10) is None


# --------------------------------------------------------------------------
# health.check_stand — проба «процесс» для iis/docker консультируется с бэкендом
# --------------------------------------------------------------------------


def test_check_stand_iis_process_ok_via_backend(monkeypatch):
    from standkit.health import check_stand

    class _FakeBackend:
        def is_running(self, stand, *, run_dir=None):
            return True

    monkeypatch.setattr(hosting, "get_backend", lambda stand: _FakeBackend())
    stand = _make_stand(host_kind=HostKind.IIS, iis_app_pool="pool1", db_host="", db_port=0)
    status = check_stand(stand)
    from standkit.models import ProbeState

    assert status.process == ProbeState.OK


def test_check_stand_docker_process_down_when_backend_false_and_port_closed(monkeypatch):
    from standkit.health import check_stand
    from standkit.models import ProbeState

    class _FakeBackend:
        def is_running(self, stand, *, run_dir=None):
            return False

    monkeypatch.setattr(hosting, "get_backend", lambda stand: _FakeBackend())
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: False)
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1", db_host="", db_port=0)
    status = check_stand(stand)
    assert status.process == ProbeState.DOWN


def test_check_stand_docker_process_backend_exception_falls_back_to_tcp(monkeypatch):
    from standkit.health import check_stand
    from standkit.models import ProbeState

    def _boom(stand):
        raise RuntimeError("backend exploded")

    monkeypatch.setattr(hosting, "get_backend", _boom)
    monkeypatch.setattr(health_module, "tcp_open", lambda host, port, **kw: True)
    stand = _make_stand(host_kind=HostKind.DOCKER, docker_container="c1", db_host="", db_port=0)
    status = check_stand(stand)
    assert status.process == ProbeState.OK
