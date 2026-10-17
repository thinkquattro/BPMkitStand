# -*- coding: utf-8 -*-
"""Тесты автоматического обновления MCP: `standkit_companion.autoupdate`.

Что закреплено (без сети: функции канала подменяются, время передаётся явно):

* политика `decide` — чистая функция: режим, обновление, плохая версия, пауза издателя,
  установка «для всех пользователей», неизвестная раскатка, кольцо `stable`, неподтверждённая
  прошлая установка — каждая ветка своим исходом;
* исполнитель `execute`: установщик целевой версии с `swap_busy=True`, иначе путь «только
  exe»; пауза — без скачивания; кольцо — со скачиванием заранее; ошибки сети/занятости —
  `waiting`, не исключение; запись «поставлено автоматически» — до запуска установщика и
  откатывается, если запуск не удался; второй процесс автообновления — `waiting`;
* автооткат `maybe_rollback` по маркеру `mcp_startup.json`: две неудачные попытки старта,
  пауза после последней, `bpmkit.exe` → `.bad`, `.old` (или копия) → `bpmkit.exe`, версия —
  в `bad_versions`, событие `rolled_back`; без лицензионного контекста;
* `reconcile_pending`: рабочий старт подтверждает установку; установщик отработал, а
  поставка осталась прежней — неудача считается, ожидание снимается.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from standkit_companion import autoupdate, releases
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from standkit_hub.config import CompanionSettings
from tests.test_companion_installer_channel import _installer_meta, _installer_sidecar
from tests.test_companion_releases import FakeClient, FakeCtx, _blob

NOW = "2026-10-20T12:00:00Z"


# --------------------------------------------------------------------------------------
# Политика
# --------------------------------------------------------------------------------------
def _decide(**overrides):
    values = dict(update_mode="auto", available=True, version="1.3.20", bad_versions=(),
                  rollout={"paused": False, "published_at": None, "stable_after": None},
                  ring="stable", now=NOW, install_writable=True, pending_restart=False,
                  channel_enabled=True, install_failures=0)
    values.update(overrides)
    result = autoupdate.decide(**values)
    return result["decision"], result["reason"], result["stage"], result["apply"]


@pytest.mark.parametrize("overrides,expected", [
    ({}, ("applied", "policy_ok", True, True)),
    ({"channel_enabled": False}, ("skipped", "channel_disabled", False, False)),
    ({"update_mode": "off"}, ("skipped", "mode_off", False, False)),
    ({"available": False}, ("skipped", "no_update", False, False)),
    ({"bad_versions": ["1.3.20.0"]}, ("skipped", "bad_version", False, False)),
    ({"rollout": {"paused": True}}, ("skipped", "paused", False, False)),
    ({"update_mode": "notify"}, ("skipped", "mode_notify", True, False)),
    ({"install_writable": False}, ("skipped", "machine_scope", True, False)),
    ({"install_failures": autoupdate.INSTALL_FAILURES_LIMIT},
     ("skipped", "install_failed", False, False)),
    ({"rollout": None}, ("waiting", "rollout_unknown", True, False)),
    ({"rollout": {"stable_after": "2026-10-21T00:00:00Z"}}, ("waiting", "ring", True, False)),
    ({"rollout": {"stable_after": "2026-10-19T00:00:00Z"}},
     ("applied", "policy_ok", True, True)),
    ({"rollout": {"stable_after": "2026-10-21T00:00:00Z"}, "ring": "early"},
     ("applied", "policy_ok", True, True)),
    ({"rollout": {"stable_after": "не дата"}}, ("applied", "policy_ok", True, True)),
    ({"pending_restart": True}, ("staged", "pending_restart", True, False)),
])
def test_decide_table(overrides, expected):
    assert _decide(**overrides) == expected


def test_decide_pause_wins_over_notify_and_ring():
    """Пауза — не только «не ставить», но и «не скачивать»: в любом режиме и кольце."""
    assert _decide(update_mode="notify", rollout={"paused": True})[:3] == (
        "skipped", "paused", False)
    assert _decide(rollout={"paused": True, "stable_after": "2026-10-21T00:00:00Z"})[2] is False


def test_rollout_for_other_version_is_unknown():
    rel = {"rollout": {"paused": False}, "rollout_version": "1.3.19"}

    assert autoupdate.rollout_for(rel, "1.3.20") is None
    assert autoupdate.rollout_for(rel, "1.3.19") == {"paused": False}
    assert autoupdate.rollout_for({"rollout": None}, "1.3.20") is None


def test_install_dir_writable(tmp_path):
    binary = tmp_path / "server" / "bpmkit.exe"
    binary.parent.mkdir()

    assert autoupdate.install_dir_writable(binary) is True
    assert list(binary.parent.iterdir()) == [], "проба не оставляет файлов"
    assert autoupdate.install_dir_writable(tmp_path / "нет" / "bpmkit.exe") is False
    assert autoupdate.install_dir_writable("") is False


# --------------------------------------------------------------------------------------
# Исполнитель
# --------------------------------------------------------------------------------------
class _Session:
    def __init__(self, client, ctx) -> None:
        self.client = client
        self.ctx = ctx


class _Calls:
    def __init__(self) -> None:
        self.log: list = []


@pytest.fixture()
def env(tmp_path, monkeypatch):
    install = tmp_path / "BPMkit"
    binary = install / "server" / "bpmkit.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ stary 1.3.19")
    (install / "manifest.json").write_text(json.dumps({"version": "1.3.19"}),
                                           encoding="utf-8")
    state = CompanionState(tmp_path / "companion-state.json")
    state.releases["rollout"] = {"paused": False, "published_at": None, "stable_after": None}
    state.releases["rollout_version"] = "1.3.20"
    ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary))
    marker = tmp_path / "mcp_startup.json"
    monkeypatch.setattr(autoupdate, "startup_marker_path", lambda: marker)
    # Маркер работающего процесса — машины прогона (живой BPMkit) — тестам не виден.
    monkeypatch.setattr(releases, "read_runtime_marker", lambda: None)
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: False)
    monkeypatch.setattr(releases.mcp_mutex, "pinned_server_running", lambda: False)
    # Копия бинаря в кэше версий (вне каталога установки), из которой исполняется
    # перенесённый сервер, — по образцу самопереноса поставки.
    cache = tmp_path / "LocalAppData" / "BPMkit" / "run" / "1.3.19-0a1b2c3d" / "bpmkit.exe"
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"MZ stary 1.3.19 (kopiya)")
    _CACHE["copy"] = str(cache)
    return state, ctx, binary, marker


#: Путь копии в кэше версий текущего теста (кладёт фикстура `env`).
_CACHE: dict = {}

#: Настоящее чтение маркера процесса (фикстура `env` его глушит).
_REAL_READ_RUNTIME_MARKER = releases.read_runtime_marker


def _settings(**overrides) -> CompanionSettings:
    values = {"enabled": True, "update_mode": "auto", "update_ring": "early"}
    values.update(overrides)
    return CompanionSettings(**values)


def _check(version="1.3.20", current="1.3.19", **extra) -> dict:
    out = {"available": True, "latest": version, "current": current, "target": version,
           "requires_installer": False}
    out.update(extra)
    return out


def _stub_channel(monkeypatch, calls, *, installer=True, apply_error=None):
    """Подменить подготовку/установку: журнал вызовов, установщик есть или нет."""
    def stage_installer(client, state, ctx, version="latest"):
        calls.log.append(("stage_installer", version))
        if not installer:
            raise ChannelError("нет", kind="installer_not_available")
        record = {"version": version, "filename": f"bpmkit-setup-{version}.exe",
                  "path": "x", "sha256": "0" * 64}
        state.releases["installer_staged"] = dict(record)
        return record

    def stage(client, state, ctx, version="latest", **kw):
        calls.log.append(("stage", version))
        return {"version": version, "filename": f"bpmkit-{version}.exe", "path": "y"}

    def apply_installer(state, ctx, *, target=None, swap_busy=False):
        calls.log.append(("apply_installer", target, swap_busy,
                          dict(state.releases.get("auto") or {})))
        if apply_error is not None:
            raise apply_error
        return {"launched": True, "pid": 77, "log": "setup.log"}

    def apply_staged(state, ctx, *, target=None, swap_busy=False):
        calls.log.append(("apply_staged", swap_busy))
        if apply_error is not None:
            raise apply_error
        return {"applied": True, "backup": "b.exe", "old_path": "o", "restart_required": True}

    monkeypatch.setattr(releases, "stage_installer", stage_installer)
    monkeypatch.setattr(releases, "stage", stage)
    monkeypatch.setattr(releases, "apply_installer", apply_installer)
    monkeypatch.setattr(releases, "apply_staged", apply_staged)


def _run(state, ctx, *, check=None, settings=None, trigger="schedule"):
    return autoupdate.execute(state, _Session(object(), ctx), settings or _settings(),
                              trigger=trigger, now=NOW, check=check or _check())


def test_execute_applies_installer_and_records_before_launch(env, monkeypatch):
    state, ctx, binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["ok"], payload["decision"]) == (True, "applied")
    assert (payload["version"], payload["current"]) == ("1.3.20", "1.3.19")
    assert payload["artifact"]["kind"] == "installer"
    name, target, swap_busy, auto_at_launch = calls.log[-1]
    assert (name, target, swap_busy) == ("apply_installer", "1.3.20", False), (
        "сервер не запущен — /SWAPBUSY не нужен")
    assert auto_at_launch["applied_version"] == "1.3.20", (
        "запись «поставлено автоматически» обязана лечь ДО запуска установщика")
    auto = state.releases["auto"]
    assert auto["binary"] == str(binary) and auto["via"] == "installer"
    assert auto["previous_version"] == "1.3.19" and auto["trigger"] == "schedule"
    assert Path(auto["backup"]).read_bytes() == b"MZ stary 1.3.19", "запасная точка отката"
    assert state.releases["last_auto_event"]["kind"] == "applied"
    assert state.releases["last_auto_decision"]["decision"] == "applied"


def test_execute_falls_back_to_exe_path(env, monkeypatch):
    state, ctx, _binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls, installer=False)

    payload = _run(state, ctx)

    assert payload["decision"] == "applied"
    assert payload["artifact"]["kind"] == "exe"
    assert ("apply_staged", False) in calls.log
    assert state.releases["auto"]["via"] == "exe"
    assert state.releases["auto"]["backup"] == "b.exe"


def test_execute_requires_installer_without_installer_is_skipped(env, monkeypatch):
    state, ctx, _binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls, installer=False)

    payload = _run(state, ctx, check=_check(requires_installer=True))

    assert (payload["decision"], payload["reason"]) == ("skipped", "requires_installer")
    assert not [c for c in calls.log if c[0] in ("stage", "apply_staged", "apply_installer")]


def test_execute_paused_does_not_even_download(env, monkeypatch):
    state, ctx, _binary, _marker = env
    state.releases["rollout"] = {"paused": True}
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("skipped", "paused")
    assert calls.log == []


def test_execute_ring_waits_but_prepares_in_advance(env, monkeypatch):
    state, ctx, _binary, _marker = env
    state.releases["rollout"] = {"paused": False, "stable_after": "2026-10-22T12:00:00Z"}
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx, settings=_settings(update_ring="stable"))

    assert (payload["decision"], payload["reason"]) == ("waiting", "ring")
    assert [c[0] for c in calls.log] == ["stage_installer"]
    assert payload["artifact"]["kind"] == "installer"
    assert state.releases.get("auto") is None


def test_execute_machine_scope_behaves_like_notify(env, monkeypatch):
    state, ctx, _binary, _marker = env
    monkeypatch.setattr(autoupdate, "install_dir_writable", lambda binary: False)
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("skipped", "machine_scope")
    assert [c[0] for c in calls.log] == ["stage_installer"], "подготовка — как в notify"


def test_execute_notify_prepares_without_installing(env, monkeypatch):
    state, ctx, _binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx, settings=_settings(update_mode="notify"))

    assert (payload["decision"], payload["reason"]) == ("skipped", "mode_notify")
    assert [c[0] for c in calls.log] == ["stage_installer"]


def test_execute_reuses_already_staged_installer(env, monkeypatch):
    state, ctx, _binary, _marker = env
    staged = Path(ctx.workdir) / "bpmkit-setup-1.3.20.exe"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"MZ")
    state.releases["installer_staged"] = {"version": "1.3.20", "path": str(staged),
                                          "filename": staged.name}
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert payload["artifact"]["reused"] is True
    assert [c[0] for c in calls.log] == ["apply_installer"], "второй раз не качаем"


def test_execute_pending_restart_stages_but_does_not_install_on_top(env, monkeypatch):
    state, ctx, _binary, _marker = env
    state.releases["auto"] = {"applied_version": "1.3.19", "confirmed_at": None,
                              "via": "exe", "binary": ctx.binary_path}
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("staged", "pending_restart")
    assert [c[0] for c in calls.log] == ["stage_installer"]


def test_execute_waits_while_installer_is_running(env, monkeypatch):
    state, ctx, _binary, _marker = env
    monkeypatch.setattr(releases, "installer_status",
                        lambda st, **k: {"staged": None, "launched": {"running": True}})
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("waiting", "installer_running")
    assert not [c for c in calls.log if c[0] == "apply_installer"]


def test_execute_second_process_waits_on_machine_lock(env, monkeypatch):
    state, ctx, _binary, _marker = env
    lock = Path(ctx.workdir) / autoupdate.LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("{}", encoding="utf-8")
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("waiting", "busy_other_process")
    assert calls.log == []
    assert lock.exists(), "чужой замок не снимается"


def test_execute_stale_lock_is_taken_over(env, monkeypatch):
    state, ctx, _binary, _marker = env
    lock = Path(ctx.workdir) / autoupdate.LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("{}", encoding="utf-8")
    old = time.time() - autoupdate.LOCK_STALE_SEC - 10
    os.utime(lock, (old, old))
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert payload["decision"] == "applied"
    assert not lock.exists(), "свой замок снимается после установки"


@pytest.mark.parametrize("error,expected", [
    (ChannelError("занят", kind="mcp_running"), ("waiting", "busy")),
    (ChannelError("нет сети", kind="offline"), ("waiting", "offline")),
    (ChannelError("UAC", kind="elevation_required"), ("skipped", "machine_scope")),
    (RuntimeError("неожиданное"), ("waiting", "unknown")),
])
def test_execute_apply_failure_is_payload_and_reverts_record(env, monkeypatch, error, expected):
    state, ctx, _binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls, apply_error=error)

    payload = _run(state, ctx)

    assert payload["ok"] is False
    assert (payload["decision"], payload["reason"]) == expected
    assert state.releases.get("auto") is None, "незапущенная установка не ждёт подтверждения"
    assert state.releases.get("last_auto_event") is None


def test_execute_stage_network_error_is_waiting(env, monkeypatch):
    state, ctx, _binary, _marker = env

    def offline(*a, **k):
        raise ChannelError("обрыв", kind="offline")

    monkeypatch.setattr(releases, "stage_installer", offline)

    payload = _run(state, ctx)

    assert (payload["ok"], payload["decision"], payload["reason"]) == (False, "waiting", "offline")


def test_execute_end_to_end_with_real_installer_channel(env, monkeypatch):
    """Настоящие `stage_installer`/`apply_installer` поверх подставного бэкенда: подпись
    проверяется, установщик запускается тихо (сервер не запущен — без /SWAPBUSY)."""
    state, ctx, _binary, _marker = env
    blob = _blob("1.3.20")
    filename = "bpmkit-setup-1.3.20.exe"
    client = FakeClient(_installer_meta("1.3.20", blob, filename=filename), blob,
                        sidecar=_installer_sidecar(filename, blob))
    launched = {}

    class _FakeProcess:
        pid = 4545

    def _fake_popen(args, **kwargs):
        launched["args"] = list(args)
        return _FakeProcess()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    payload = autoupdate.execute(state, _Session(client, ctx), _settings(), trigger="logon",
                                 now=NOW, check=_check())

    assert payload["decision"] == "applied", payload
    assert launched["args"][0].endswith(filename)
    assert releases.SWAPBUSY_FLAG not in launched["args"], "сервер не запущен — без ключа"
    assert "/VERYSILENT" in launched["args"]


def test_precheck_without_network():
    assert autoupdate.precheck(_settings(enabled=False))["reason"] == "channel_disabled"
    assert autoupdate.precheck(_settings(update_mode="off"))["reason"] == "mode_off"
    assert autoupdate.precheck(_settings()) is None


# --------------------------------------------------------------------------------------
# Автооткат и подтверждение
# --------------------------------------------------------------------------------------
def _pending(state, binary, *, via="installer", backup=None, swap_busy=False):
    state.releases["auto"] = {"applied_version": "1.3.20", "previous_version": "1.3.19",
                              "binary": str(binary), "backup": backup, "via": via,
                              "applied_at": "2026-10-20T11:00:00Z", "swap_busy": swap_busy,
                              "confirmed_at": None}


def _marker(path, *, version="1.3.20", attempts=2, ok=False,
            first="2026-10-20T11:50:00Z", last="2026-10-20T11:55:00Z", ok_at=None):
    if ok and ok_at is None:
        ok_at = last
    path.write_text(json.dumps({"version": version, "attempts": attempts, "ok": ok,
                                "first_attempt_at": first,
                                "last_attempt_at": last, "ok_at": ok_at}), encoding="utf-8")


def _broken_install(binary):
    old = binary.with_name(binary.name + releases.OLD_SUFFIX)
    old.write_bytes(binary.read_bytes())
    binary.write_bytes(b"MZ slomannyj 1.3.20")
    manifest = binary.parent.parent / "manifest.json"
    manifest.write_text(json.dumps({"version": "1.3.20"}), encoding="utf-8")
    return old


def test_rollback_after_two_failed_starts(env):
    state, _ctx, binary, marker = env
    old = _broken_install(binary)
    sidecar = binary.with_name(binary.name + ".sha256")
    sidecar.write_text("0" * 64 + "  bpmkit.exe\n", encoding="utf-8")
    _pending(state, binary, swap_busy=True)  # `.old` — от ЭТОЙ установки (/SWAPBUSY)
    _marker(marker)

    payload = autoupdate.maybe_rollback(state, now=NOW, trigger="logon")

    assert payload["decision"] == "rolled_back"
    assert binary.read_bytes() == b"MZ stary 1.3.19"
    bad = binary.with_name(binary.name + releases.BAD_SUFFIX)
    assert bad.read_bytes() == b"MZ slomannyj 1.3.20"
    assert not old.exists()
    rel = state.releases
    assert rel["bad_versions"] == ["1.3.20"]
    event = rel["last_auto_event"]
    assert (event["kind"], event["from"], event["to"]) == ("rolled_back", "1.3.20", "1.3.19")
    assert rel["current"]["version"] == "1.3.19"
    assert rel["restart_required"] is True
    assert rel["auto"] is None
    manifest = json.loads((binary.parent.parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version"] == "1.3.19", "версия поставки — вслед за бинарём"
    assert not sidecar.read_text(encoding="utf-8").startswith("0" * 64), "эталон sha пересчитан"
    # Плохая версия больше не ставится автоматически.
    assert _decide(bad_versions=rel["bad_versions"])[:2] == ("skipped", "bad_version")


@pytest.mark.parametrize("marker_kwargs", [
    {"attempts": 1},
    {"ok": True},
    {"version": "1.3.19"},
    {"last": "2026-10-20T11:59:30Z"},
])
def test_rollback_not_yet(env, marker_kwargs):
    state, _ctx, binary, marker = env
    _broken_install(binary)
    _pending(state, binary)
    _marker(marker, **marker_kwargs)

    assert autoupdate.maybe_rollback(state, now=NOW) is None
    assert binary.read_bytes() == b"MZ slomannyj 1.3.20"


def test_rollback_from_backup_copy_when_no_old(env):
    state, _ctx, binary, marker = env
    backup = binary.parent.parent / "backup-1.3.19.exe"
    backup.write_bytes(b"MZ stary 1.3.19")
    binary.write_bytes(b"MZ slomannyj 1.3.20")
    _pending(state, binary, backup=str(backup))
    _marker(marker)

    payload = autoupdate.maybe_rollback(state, now=NOW)

    assert payload["decision"] == "rolled_back"
    assert binary.read_bytes() == b"MZ stary 1.3.19"
    assert backup.is_file(), "копия не расходуется"


def test_rollback_without_source_marks_bad_and_says_so(env):
    state, _ctx, binary, marker = env
    binary.write_bytes(b"MZ slomannyj 1.3.20")
    _pending(state, binary)
    _marker(marker)

    payload = autoupdate.maybe_rollback(state, now=NOW)

    assert (payload["decision"], payload["reason"]) == ("skipped", "rollback_unavailable")
    assert state.releases["bad_versions"] == ["1.3.20"]
    assert state.releases["last_auto_event"]["kind"] == "rollback_failed"
    assert binary.read_bytes() == b"MZ slomannyj 1.3.20"


def test_reconcile_confirms_working_start(env):
    state, _ctx, binary, marker = env
    _pending(state, binary)
    _marker(marker, attempts=1, ok=True)

    assert autoupdate.reconcile_pending(state, now=NOW)["kind"] == "confirmed"
    assert autoupdate.pending_record(state) is None
    assert autoupdate.maybe_rollback(state, now=NOW) is None


def test_reconcile_counts_failed_installer_run(env, monkeypatch):
    state, _ctx, binary, _marker_path = env
    _pending(state, binary)
    state.releases["installer_launched"] = {"pid": 0, "log": "setup.log",
                                            "launched_at": "2026-10-20T11:00:00Z"}

    result = autoupdate.reconcile_pending(state, now=NOW)

    assert result["kind"] == "install_failed"
    assert state.releases["auto"] is None
    assert state.releases["auto_failures"] == {"1.3.20": 1}
    assert state.releases["last_auto_event"]["kind"] == "install_failed"


def test_reconcile_keeps_waiting_when_installer_did_its_job(env):
    """Поставка на диске уже новая, рабочего старта ещё не было — ждём перезапуска хоста."""
    state, _ctx, binary, _marker_path = env
    (binary.parent.parent / "manifest.json").write_text(json.dumps({"version": "1.3.20"}),
                                                       encoding="utf-8")
    _pending(state, binary)

    assert autoupdate.reconcile_pending(state, now=NOW) is None
    assert autoupdate.pending_record(state) is not None


def test_reconcile_confirms_by_runtime_marker_when_server_has_no_startup_marker(env, monkeypatch):
    """Сервер без `mcp_startup.json` (версия до маркера) — подтверждение по маркеру
    работающего процесса, иначе автоматика встала бы на первой же версии навсегда."""
    state, _ctx, binary, _marker_path = env
    _pending(state, binary)
    monkeypatch.setattr(releases, "read_runtime_marker", lambda: {
        "version": "1.3.20", "started_at": "2026-10-20T11:30:00Z", "pid": 1,
        "frozen": True, "binary": str(binary)})

    assert autoupdate.reconcile_pending(state, now=NOW)["kind"] == "confirmed"
    assert state.releases["auto"]["confirmed_via"] == "runtime"


def test_reconcile_ignores_runtime_marker_started_before_install(env, monkeypatch):
    state, _ctx, binary, _marker_path = env
    (binary.parent.parent / "manifest.json").write_text(json.dumps({"version": "1.3.20"}),
                                                       encoding="utf-8")
    _pending(state, binary)
    monkeypatch.setattr(releases, "read_runtime_marker", lambda: {
        "version": "1.3.20", "started_at": "2026-10-20T10:30:00Z", "pid": 1,
        "frozen": True, "binary": str(binary)})

    assert autoupdate.reconcile_pending(state, now=NOW) is None
    assert autoupdate.pending_record(state) is not None



# --------------------------------------------------------------------------------------
# Окно простоя: при работающем сервере — ждать; подмена — только при самопереносе
# --------------------------------------------------------------------------------------
@pytest.mark.parametrize("overrides,expected", [
    ({"server_running": True}, ("waiting", "mcp_running", True, False)),
    ({"server_running": True, "self_relocate": True}, ("applied", "policy_ok", True, True)),
    ({"server_running": False, "self_relocate": False}, ("applied", "policy_ok", True, True)),
    ({"server_running": True, "pending_restart": True}, ("staged", "pending_restart", True, False)),
])
def test_decide_idle_window(overrides, expected):
    """Работающий MCP-сервер: живой процесс после подмены своего exe ломается (ленивые
    импорты PyInstaller читают новый файл) — подмена только при самопереносе."""
    assert _decide(**overrides) == expected


def _relocating_install(binary, *, features=("self_relocate",), where="root"):
    manifest = (binary.parent.parent if where == "root" else binary.parent) / "manifest.json"
    manifest.write_text(json.dumps({"version": "1.3.19", "features": list(features)}),
                        encoding="utf-8")


_UNSET = object()


def _runtime(monkeypatch, *, relocated, pid=None, binary="", version="1.3.19",
             started_at="2026-10-20T09:00:00Z", relocated_binary=_UNSET):
    """Маркер работающего процесса: по умолчанию — ЖИВОЙ процесс (этот); перенесённый —
    из копии в кэше версий (`relocated_binary`), `binary` — установленный exe."""
    copy = (_CACHE.get("copy", "") if relocated else "") if relocated_binary is _UNSET \
        else relocated_binary
    monkeypatch.setattr(releases, "read_runtime_marker", lambda: {
        "version": version, "started_at": started_at,
        "pid": os.getpid() if pid is None else pid, "frozen": True,
        "binary": binary, "relocated": relocated,
        "relocated_from": binary if relocated else "", "relocated_binary": copy})


@pytest.mark.parametrize("where", ["root", "server"])
def test_self_relocate_needs_feature_and_runtime_flag(env, monkeypatch, where):
    _state, _ctx, binary, _marker = env
    _relocating_install(binary, where=where)
    _runtime(monkeypatch, relocated=True)
    assert autoupdate.self_relocate_active(binary) is True

    _runtime(monkeypatch, relocated=False)
    assert autoupdate.self_relocate_active(binary) is False, "процесс не перенесён"

    _relocating_install(binary, features=("other",), where=where)
    _runtime(monkeypatch, relocated=True)
    assert autoupdate.self_relocate_active(binary) is False, "поставка не умеет самоперенос"


def test_self_relocate_without_manifest_or_marker_is_false(env, monkeypatch):
    _state, _ctx, binary, _marker = env  # manifest.json фикстуры — без features
    _runtime(monkeypatch, relocated=True)
    assert autoupdate.self_relocate_active(binary) is False
    _relocating_install(binary)
    monkeypatch.setattr(releases, "read_runtime_marker", lambda: None)
    assert autoupdate.self_relocate_active(binary) is False


def test_execute_waits_while_server_runs_and_does_not_swap(env, monkeypatch):
    state, ctx, _binary, _marker = env
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: True)
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("waiting", "mcp_running")
    assert [c[0] for c in calls.log] == ["stage_installer"], "скачать заранее — можно"
    assert state.releases.get("auto") is None


def test_execute_swaps_only_with_self_relocation(env, monkeypatch):
    state, ctx, binary, _marker = env
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: True)
    _relocating_install(binary)
    _runtime(monkeypatch, relocated=True)
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert payload["decision"] == "applied"
    name, target, swap_busy, _auto = calls.log[-1]
    assert (name, swap_busy) == ("apply_installer", True), "/SWAPBUSY — только при самопереносе"


def test_execute_idle_server_installs_without_swapbusy(env, monkeypatch):
    """Сервер не запущен — установщик без /SWAPBUSY (подменять нечего)."""
    state, ctx, _binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    _run(state, ctx)

    name, _target, swap_busy, _auto = calls.log[-1]
    assert (name, swap_busy) == ("apply_installer", False)


def test_execute_exe_path_swap_only_with_self_relocation(env, monkeypatch):
    state, ctx, binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls, installer=False)
    _run(state, ctx)
    assert ("apply_staged", False) in calls.log

    state.releases["auto"] = None
    state.save()  # под замком общие ключи перечитываются с диска
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: True)
    _relocating_install(binary)
    _runtime(monkeypatch, relocated=True)
    calls2 = _Calls()
    _stub_channel(monkeypatch, calls2, installer=False)
    payload = _run(state, ctx)
    assert ("apply_staged", True) in calls2.log, payload


def test_exit_trigger_is_an_ordinary_pass_and_logged(env, monkeypatch):
    """`exit` (сервер при выходе из хоста) — обычный проход политики; триггер — в событии."""
    state, ctx, _binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls)
    assert "exit" in autoupdate.TRIGGERS

    payload = _run(state, ctx, trigger="exit")

    assert (payload["decision"], payload["trigger"]) == ("applied", "exit")
    assert state.releases["last_auto_event"]["trigger"] == "exit"
    assert state.releases["last_auto_decision"]["trigger"] == "exit"



@pytest.mark.parametrize("manifest,expected", [
    # основной путь: признак поставки в `_meta["pro.bpmkit"]` (верхний `features` ломает
    # упаковку MCPB — её схема manifest строгая)
    ({"_meta": {"pro.bpmkit": {"features": ["self_relocate"]}}}, True),
    # запасной путь — верхнеуровневый `features`
    ({"features": ["self_relocate"]}, True),
    ({"_meta": {"pro.bpmkit": {"features": ["other"]}}, "features": ["self_relocate"]}, True),
    # мусор и отсутствие — признака нет
    ({"_meta": {"pro.bpmkit": {"features": "self_relocate"}}}, False),
    ({"_meta": {"pro.bpmkit": {"features": {"self_relocate": True}}}}, False),
    ({"_meta": {"pro.bpmkit": {}}}, False),
    ({"_meta": {"pro.bpmkit": None}}, False),
    ({"_meta": "мусор"}, False),
    ({"features": "self_relocate"}, False),
    ({"features": {"self_relocate": True}}, False),
    ({"version": "1.3.19"}, False),
])
def test_self_relocate_feature_location_in_manifest(env, monkeypatch, manifest, expected):
    _state, _ctx, binary, _marker = env
    (binary.parent.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    _runtime(monkeypatch, relocated=True)

    assert autoupdate.self_relocate_active(binary) is expected



# --------------------------------------------------------------------------------------
# Маркер старта засчитывается только ПОСЛЕ автоустановки; источник отката — копия ЭТОЙ
# установки, `.old` — запасной
# --------------------------------------------------------------------------------------
def test_marker_older_than_install_neither_confirms_nor_rolls_back(env):
    """Смоук/VM на машине кольца early написали маркер этой версии ДО установки: он не
    доказывает ни рабочий старт, ни провал новой версии."""
    state, _ctx, binary, marker = env
    _broken_install(binary)
    _pending(state, binary)  # applied_at 11:00
    _marker(marker, attempts=3, first="2026-10-20T09:00:00Z", last="2026-10-20T09:10:00Z")

    assert autoupdate.maybe_rollback(state, now=NOW) is None
    assert autoupdate.reconcile_pending(state, now=NOW) is None
    assert binary.read_bytes() == b"MZ slomannyj 1.3.20"

    _marker(marker, attempts=1, ok=True, first="2026-10-20T09:00:00Z",
            last="2026-10-20T09:00:05Z", ok_at="2026-10-20T09:00:06Z")
    assert autoupdate.reconcile_pending(state, now=NOW) is None
    assert autoupdate.pending_record(state) is not None, "старый ok не подтверждает установку"


def test_marker_attempts_before_install_are_not_counted(env):
    """Счётчик маркера этой версии в момент установки (смоук того же exe до неё)
    запоминается: откат — только при двух попытках СВЕРХ него."""
    state, _ctx, binary, marker = env
    _broken_install(binary)
    _pending(state, binary, swap_busy=True)
    state.releases["auto"]["startup_attempts_at_install"] = 2
    _marker(marker, attempts=3, first="2026-10-20T10:00:00Z", last="2026-10-20T11:55:00Z")

    assert autoupdate.maybe_rollback(state, now=NOW) is None, "после установки — 1 попытка"

    _marker(marker, attempts=4, first="2026-10-20T10:00:00Z", last="2026-10-20T11:55:00Z")
    assert autoupdate.maybe_rollback(state, now=NOW)["decision"] == "rolled_back"


def test_apply_remembers_startup_attempts_of_this_version(env, monkeypatch):
    state, ctx, _binary, marker = env
    _marker(marker, version="1.3.20", attempts=3, first="2026-10-20T09:00:00Z",
            last="2026-10-20T09:10:00Z")
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    _run(state, ctx)

    record = state.releases["auto"]
    assert record["startup_attempts_at_install"] == 3
    assert record["server_running"] is False
    assert record["workdir"] == str(Path(ctx.workdir))


def test_rollback_prefers_this_installs_backup_over_old(env):
    """`.old` мог остаться от ПРЕДЫДУЩЕЙ подмены (позапрошлая версия) — откат «через
    версию»; первой берётся копия, снятая этой установкой."""
    state, _ctx, binary, marker = env
    old = binary.with_name(binary.name + releases.OLD_SUFFIX)
    old.write_bytes(b"MZ pozaproshlaya 1.3.18")
    backup = binary.parent.parent / "backup-1.3.19.exe"
    backup.write_bytes(b"MZ stary 1.3.19")
    binary.write_bytes(b"MZ slomannyj 1.3.20")
    _pending(state, binary, backup=str(backup))
    _marker(marker)

    payload = autoupdate.maybe_rollback(state, now=NOW)

    assert payload["decision"] == "rolled_back"
    assert binary.read_bytes() == b"MZ stary 1.3.19"
    assert old.read_bytes() == b"MZ pozaproshlaya 1.3.18", ".old не тронут"


def test_self_relocate_needs_live_process_running_from_cache_copy(env, monkeypatch):
    """Маркер умершего процесса или процесса, исполняющегося из самого `bpmkit.exe`, —
    не самоперенос: файл держит (или может держать) сервер."""
    _state, _ctx, binary, _marker = env
    _relocating_install(binary)
    monkeypatch.setattr(autoupdate, "_pid_alive", lambda pid: False)
    _runtime(monkeypatch, relocated=True)
    assert autoupdate.self_relocate_active(binary) is False, "процесс маркера не жив"

    monkeypatch.setattr(autoupdate, "_pid_alive", lambda pid: True)
    _runtime(monkeypatch, relocated=True, relocated_binary=str(binary))
    assert autoupdate.self_relocate_active(binary) is False, "исполняется из bpmkit.exe"
    inside = binary.parent / "cache" / "bpmkit.exe"
    inside.parent.mkdir()
    inside.write_bytes(b"MZ")
    _runtime(monkeypatch, relocated=True, relocated_binary=str(inside))
    assert autoupdate.self_relocate_active(binary) is False, "копия в каталоге установки"
    _runtime(monkeypatch, relocated=True, relocated_binary="")
    assert autoupdate.self_relocate_active(binary) is False, "откуда — не известно"
    _runtime(monkeypatch, relocated=True,
             relocated_binary=str(binary.parent.parent.parent / "net" / "bpmkit.exe"))
    assert autoupdate.self_relocate_active(binary) is False, "копии на диске нет"
    _runtime(monkeypatch, relocated=True)
    assert autoupdate.self_relocate_active(binary) is True
    monkeypatch.setattr(releases.mcp_mutex, "pinned_server_running", lambda: True)
    assert autoupdate.self_relocate_active(binary) is False, (
        "виден BPMkitMcpServerPinned — второй сервер держит сам bpmkit.exe")


def test_self_relocate_contract_with_copy_marker_sample(env, monkeypatch, tmp_path):
    """Контракт с самопереносом поставки: образец `mcp_runtime.json`, который пишет копия
    (`binary` — установленный exe, путь копии — `relocated_binary`)."""
    _state, _ctx, binary, _marker = env
    _relocating_install(binary)
    marker = tmp_path / "mcp_runtime.json"
    sample = {"version": "1.3.19", "started_at": "2026-10-20T09:00:00Z", "pid": os.getpid(),
              "frozen": True, "binary": str(binary), "relocated": True,
              "relocated_from": str(binary), "relocated_binary": _CACHE["copy"]}
    marker.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(releases, "read_runtime_marker", _REAL_READ_RUNTIME_MARKER)
    monkeypatch.setattr(releases, "_runtime_marker_path", lambda: marker)

    read = releases.read_runtime_marker()
    assert (read["binary"], read["relocated_binary"], read["relocated_from"]) == (
        str(binary), _CACHE["copy"], str(binary)), "поле relocated_binary не выбрасывается"
    assert autoupdate.self_relocate_active(binary) is True

    # Обычный процесс (прежние пять полей) — не самоперенос.
    marker.write_text(json.dumps({key: sample[key] for key in (
        "version", "started_at", "pid", "frozen", "binary")}), encoding="utf-8")
    assert releases.read_runtime_marker()["relocated_binary"] == ""
    assert autoupdate.self_relocate_active(binary) is False


# --------------------------------------------------------------------------------------
# Ревью: повторная установка, битый конфиг, живой процесс, несогласованный `.old`,
# неизвестная версия, два процесса, пауза без метки, окно простоя под замком
# --------------------------------------------------------------------------------------
class _VersionClient(FakeClient):
    """Подставной бэкенд с `GET /v1/version/latest` (раскатка) поверх релизного канала."""

    def __init__(self, *a, latest=None, rollout=None, **k):
        super().__init__(*a, **k)
        self._latest = latest
        self._rollout = rollout if rollout is not None else {"paused": False}

    def get_json(self, path, *, params=None, authorized=True, etag=None):
        if path == "/v1/version/latest":
            self.calls.append(("GET", path))
            return {"latest": self._latest, "rollout": self._rollout}, {}
        return super().get_json(path, params=params, authorized=authorized, etag=etag)


def test_installed_by_installer_is_not_installed_again(env, monkeypatch):
    """Установщик не пишет `releases.current`; после установки и подтверждения та же
    версия не ставится снова — установленную версию даёт `manifest.json` поставки."""
    from tests.test_companion_releases import _meta

    state, ctx, binary, _m = env
    (binary.parent.parent / "manifest.json").write_text(json.dumps({"version": "1.3.20"}),
                                                       encoding="utf-8")
    ctx.mcp_version = "1.3.20"
    state.releases["current"] = {"version": "1.3.19", "applied_at": "2026-10-01T00:00:00Z",
                                 "path": str(binary)}
    state.releases["auto"] = {"applied_version": "1.3.20", "previous_version": "1.3.19",
                              "binary": str(binary), "via": "installer",
                              "applied_at": "2026-10-20T10:00:00Z",
                              "confirmed_at": "2026-10-20T10:30:00Z"}
    blob = _blob("1.3.20")
    client = _VersionClient(_meta("1.3.20", blob), blob, latest="1.3.20")
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = autoupdate.execute(state, _Session(client, ctx), _settings(),
                                 trigger="schedule", now=NOW)

    assert (payload["decision"], payload["reason"]) == ("skipped", "no_update")
    assert payload["current"] == "1.3.20"
    assert not [c for c in calls.log if c[0] in ("apply_installer", "apply_staged")]


def test_confirmation_of_installer_records_installed_version(env):
    state, _ctx, binary, marker = env
    state.releases["current"] = {"version": "1.3.19"}
    _pending(state, binary)
    _marker(marker, attempts=1, ok=True)

    assert autoupdate.reconcile_pending(state, now=NOW)["kind"] == "confirmed"
    assert state.releases["current"]["version"] == "1.3.20"
    assert state.releases["current"]["via"] == "installer"


def test_unknown_update_mode_or_broken_config_does_not_install(env, monkeypatch, tmp_path):
    """Битый конфиг (или незнакомый режим) — не выбор человека: автоматика ведёт себя как
    `notify` (проверяет и готовит, но не ставит) с причиной `settings_unreadable`."""
    from standkit_hub.config import SETTINGS_UNREADABLE, HubConfig

    state, ctx, _binary, _m = env
    cfg = tmp_path / "standkit-hub.json"
    cfg.write_text('{"companion": {"update_mode": "notify", "releases": {"enabled": tru',
                   encoding="utf-8")
    broken = HubConfig.load(cfg).companion
    cfg.write_text(json.dumps({"companion": {"update_mode": "turbo"}}), encoding="utf-8")
    unknown = HubConfig.load(cfg).companion

    for settings in (broken, unknown):
        assert settings.load_problem == SETTINGS_UNREADABLE
        state.releases["auto"] = None
        state.save()
        calls = _Calls()
        _stub_channel(monkeypatch, calls)
        payload = _run(state, ctx, settings=settings)
        assert (payload["decision"], payload["reason"]) == ("skipped", "settings_unreadable")
        assert [c[0] for c in calls.log] == ["stage_installer"], "подготовка — как в notify"


def test_live_process_postpones_rollback_but_never_confirms(env, monkeypatch):
    """Две попытки без `ok`, процесс этой версии жив (возможно, повис до рабочего старта):
    откат откладывается, но установка НЕ подтверждается; процесс завершился — откат."""
    state, _ctx, binary, marker = env
    old = binary.with_name(binary.name + releases.OLD_SUFFIX)
    old.write_bytes(binary.read_bytes())
    binary.write_bytes(b"MZ visit 1.3.20")
    (binary.parent.parent / "manifest.json").write_text(json.dumps({"version": "1.3.20"}),
                                                       encoding="utf-8")
    _pending(state, binary, swap_busy=True)
    _marker(marker, attempts=2, ok=False, last="2026-10-20T11:55:00Z")
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: True)
    _runtime(monkeypatch, relocated=False, version="1.3.20", binary=str(binary),
             started_at="2026-10-20T11:55:00Z")

    assert autoupdate.maybe_rollback(state, now=NOW) is None, "жив — откат отложен"
    assert autoupdate.reconcile_pending(state, now=NOW) is None, "живость — не ok"
    assert autoupdate.pending_record(state) is not None
    assert not state.releases.get("bad_versions")

    _runtime(monkeypatch, relocated=False, version="1.3.20", binary=str(binary),
             started_at="2026-10-20T11:55:00Z", pid=999999)
    monkeypatch.setattr(autoupdate, "_pid_alive", lambda pid: False)
    assert autoupdate.maybe_rollback(state, now=NOW)["decision"] == "rolled_back"


def test_liveness_without_ok_never_confirms(env, monkeypatch):
    """Попытка 1 без `ok`, мьютекс держит какой-то сервер, маркер процесса этой версии —
    подтверждения нет: подтверждает только явный `ok` маркера старта."""
    state, _ctx, binary, marker = env
    (binary.parent.parent / "manifest.json").write_text(json.dumps({"version": "1.3.20"}),
                                                       encoding="utf-8")
    _pending(state, binary)
    _marker(marker, attempts=1, ok=False, last="2026-10-20T11:50:00Z")
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: True)
    for pid in (999999, None):
        _runtime(monkeypatch, relocated=False, version="1.3.20", binary=str(binary),
                 started_at="2026-10-20T11:50:00Z", pid=pid)
        assert autoupdate.reconcile_pending(state, now=NOW) is None
        assert autoupdate.pending_record(state) is not None


def test_rollback_does_not_use_inconsistent_old(env):
    """Установка без `/SWAPBUSY` `.old` не создаёт: оставшийся `.old` — от прежней подмены.
    Без копии этой установки откатывать им нельзя — честное «откатить нечем»."""
    state, _ctx, binary, marker = env
    old = binary.with_name(binary.name + releases.OLD_SUFFIX)
    old.write_bytes(b"MZ pozaproshlaya 1.3.18")
    binary.write_bytes(b"MZ slomannyj 1.3.20")
    _pending(state, binary, swap_busy=False)
    _marker(marker)

    payload = autoupdate.maybe_rollback(state, now=NOW)

    assert (payload["decision"], payload["reason"]) == ("skipped", "rollback_unavailable")
    assert binary.read_bytes() == b"MZ slomannyj 1.3.20"
    assert old.read_bytes() == b"MZ pozaproshlaya 1.3.18"


@pytest.mark.parametrize("latest", [None, "", "latest", "1.3.20-beta"])
def test_unknown_version_is_never_installed(env, monkeypatch, latest):
    """Номер новой версии не известен — не проверить ни плохую версию, ни раскатку:
    `skipped / version_unknown`, ничего не качается и не ставится."""
    state, ctx, _binary, _m = env
    state.releases["bad_versions"] = ["1.3.20"]
    state.releases["rollout_version"] = "1.3.19"
    assert autoupdate.rollout_for(state.releases, latest) is None
    calls = _Calls()
    _stub_channel(monkeypatch, calls)
    check = {"available": True, "latest": latest, "current": "1.3.19", "target": "latest",
             "requires_installer": False, "reason": "version_unknown_use_latest"}

    payload = _run(state, ctx, check=check)

    assert (payload["decision"], payload["reason"]) == ("skipped", "version_unknown")
    assert calls.log == []


def test_staged_artifact_of_other_version_is_not_installed(env, monkeypatch):
    state, ctx, _binary, _m = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    def stage_other(client, st, c, version="latest"):
        calls.log.append(("stage_installer", version))
        return {"version": "1.3.21", "filename": "bpmkit-setup-1.3.21.exe", "path": "x"}

    monkeypatch.setattr(releases, "stage_installer", stage_other)

    payload = _run(state, ctx)

    assert (payload["ok"], payload["decision"], payload["reason"]) == (
        False, "skipped", "artifact_version_mismatch")
    assert not [c for c in calls.log if c[0] == "apply_installer"]


def test_second_process_bad_version_is_seen_under_lock_and_kept(env, monkeypatch):
    """Два процесса: CLI пометил версию плохой на диске, у диспетчера в памяти — старая
    копия. Под замком решение принимается по диску, а запись диспетчера не затирает
    запись CLI."""
    state, ctx, _binary, _m = env
    state.save()
    other = CompanionState.load(state.path)
    other.releases["bad_versions"] = ["1.3.20"]
    other.save()
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("skipped", "bad_version")
    assert not [c for c in calls.log if c[0] == "apply_installer"]
    disk = json.loads(Path(state.path).read_text(encoding="utf-8"))
    assert disk["releases"]["bad_versions"] == ["1.3.20"]


def test_save_merges_keys_written_by_another_process(tmp_path):
    """Общие ключи, которые этот процесс не менял, при записи берутся с диска;
    изменённые здесь — из памяти."""
    path = tmp_path / "companion-state.json"
    hub = CompanionState(path)
    hub.save()
    cli = CompanionState.load(path)
    cli.releases["bad_versions"] = ["1.3.20"]
    cli.releases["installer_launched"] = {"pid": 5, "launched_at": NOW}
    cli.save()

    hub.releases["last_auto_decision"] = {"decision": "waiting"}
    hub.patterns["since"] = "2026-10-20T00:00:00Z"
    hub.save()

    disk = json.loads(path.read_text(encoding="utf-8"))
    assert disk["releases"]["bad_versions"] == ["1.3.20"]
    assert disk["releases"]["installer_launched"]["pid"] == 5
    assert disk["releases"]["last_auto_decision"] == {"decision": "waiting"}
    assert disk["patterns"]["since"] == "2026-10-20T00:00:00Z"
    assert hub.releases["bad_versions"] == ["1.3.20"], "память догоняет диск"


@pytest.mark.parametrize("last", [None, "мусор"])
def test_rollback_grace_applies_without_last_attempt(env, last):
    """Нет `last_attempt_at` (или мусор) — пауза всё равно выдерживается: отсчёт от
    последней записи маркера, а не «паузы нет»."""
    state, _ctx, binary, marker = env
    old = binary.with_name(binary.name + releases.OLD_SUFFIX)
    old.write_bytes(binary.read_bytes())
    binary.write_bytes(b"MZ novyj 1.3.20")
    _pending(state, binary, swap_busy=True)
    marker.write_text(json.dumps({"version": "1.3.20", "attempts": 2, "ok": False,
                                  "first_attempt_at": "2026-10-20T11:30:00Z",
                                  "last_attempt_at": last}), encoding="utf-8")
    written = autoupdate.parse_iso(NOW).timestamp() - 30
    os.utime(marker, (written, written))

    assert autoupdate.maybe_rollback(state, now=NOW) is None, "маркер писался 30 с назад"
    later = "2026-10-20T12:05:00Z"
    assert autoupdate.maybe_rollback(state, now=later)["decision"] == "rolled_back"


def test_server_started_while_downloading_defers_install(env, monkeypatch):
    """Мьютекс перепроверяется под замком прямо перед установкой: сервер, запущенный,
    пока качался файл, — `waiting / mcp_running`, без установки и без счёта неудач."""
    state, ctx, _binary, _m = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls)
    seen = []

    def mutex(*a, **k):
        seen.append(k.get("strict"))
        return len(seen) > 2  # видим только после подготовки

    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", mutex)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("waiting", "mcp_running")
    assert not [c for c in calls.log if c[0] == "apply_installer"]
    assert set(seen) == {True}, "автоматика спрашивает мьютекс строго"
    assert not state.releases.get("auto_failures")


#: Хвост журнала установщика BPMkit при отказе из-за запущенного MCP-сервера (образец).
_INSTALLER_REFUSED_LOG = (
    "2026-10-20 11:00:02.000   BPMkit: обнаружены запущенные компоненты (MCP-сервер "
    "(мьютекс BPMkitMcpServer)) -- разбор гейта.\n"
    "2026-10-20 11:00:02.100   BPMkit: установка отменена ДО копирования файлов -- "
    "закройте приложение с BPMkit.\n")


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp1251"])
def test_installer_refused_by_running_server_is_not_a_version_failure(env, monkeypatch,
                                                                      tmp_path, encoding):
    """Отказ установщика из-за сервера — по его журналу: ожидание снимается без счёта."""
    state, _ctx, binary, _m = env
    log = tmp_path / "installer_setup.log"
    log.write_bytes(("Log opened.\n" * 50 + _INSTALLER_REFUSED_LOG).encode(encoding))
    _pending(state, binary)
    state.releases["installer_launched"] = {"pid": 0, "log": str(log),
                                            "launched_at": "2026-10-20T11:00:00Z"}

    result = autoupdate.reconcile_pending(state, now=NOW)

    assert result["kind"] == "install_deferred"
    assert not state.releases.get("auto_failures")
    assert state.releases["auto"] is None


def test_server_visible_at_launch_without_swapbusy_is_deferred(env):
    state, _ctx, binary, _m = env
    _pending(state, binary)
    state.releases["auto"]["server_running"] = True
    state.releases["installer_launched"] = {"pid": 0, "log": "net.log",
                                            "launched_at": "2026-10-20T11:00:00Z"}

    assert autoupdate.reconcile_pending(state, now=NOW)["kind"] == "install_deferred"


def test_mutex_at_reconcile_time_does_not_excuse_failed_install(env, monkeypatch, tmp_path):
    """Сервер поднялся уже после работы установщика: мьютекс в момент сверки ничего не
    доказывает — неудача версии считается."""
    state, _ctx, binary, _m = env
    log = tmp_path / "installer_setup.log"
    log.write_text("Log opened.\nInstallation process failed: disk full\n", encoding="utf-8")
    _pending(state, binary)
    state.releases["installer_launched"] = {"pid": 0, "log": str(log),
                                            "launched_at": "2026-10-20T11:00:00Z"}
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: True)

    assert autoupdate.reconcile_pending(state, now=NOW)["kind"] == "install_failed"
    assert state.releases["auto_failures"] == {"1.3.20": 1}


def test_rollback_and_clear_wait_for_machine_lock(env):
    """Автооткат и снятие запрета — под тем же замком машины, что и установка."""
    from standkit_companion.errors import ChannelError as _ChannelError

    state, ctx, binary, marker = env
    _broken_install(binary)
    _pending(state, binary, swap_busy=True)
    state.releases["auto"]["workdir"] = ctx.workdir
    state.releases["bad_versions"] = ["1.3.18"]
    state.save()
    _marker(marker)
    lock = Path(ctx.workdir) / autoupdate.LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "at": NOW}), encoding="utf-8")

    payload = autoupdate.maybe_rollback(state, now=NOW)
    assert (payload["decision"], payload["reason"]) == ("waiting", "busy_other_process")
    assert binary.read_bytes() == b"MZ slomannyj 1.3.20", "под чужим замком не откатываем"
    with pytest.raises(_ChannelError):
        autoupdate.clear_bad_versions(state)
    assert state.releases["bad_versions"] == ["1.3.18"]

    lock.unlink()
    assert autoupdate.maybe_rollback(state, now=NOW)["decision"] == "rolled_back"
    assert not lock.exists(), "свой замок снимается"


def test_pending_with_unreadable_manifest_is_not_eternal(env):
    state, _ctx, binary, _m = env
    (binary.parent.parent / "manifest.json").write_text("{битый", encoding="utf-8")
    _pending(state, binary)  # applied_at 11:00

    assert autoupdate.reconcile_pending(state, now=NOW) is None, "ещё ждём"
    later = "2026-10-20T17:30:00Z"
    result = autoupdate.reconcile_pending(state, now=later)
    assert result["kind"] == "install_unverified"
    assert state.releases["auto"] is None
    assert state.releases["auto_failures"] == {"1.3.20": 1}
    assert state.releases["last_auto_event"]["kind"] == "install_unverified"


def test_machine_lock_of_dead_process_is_taken_over(env, monkeypatch):
    state, ctx, _binary, _m = env
    lock = Path(ctx.workdir) / autoupdate.LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": 999999, "at": NOW}), encoding="utf-8")
    monkeypatch.setattr(autoupdate, "_pid_alive", lambda pid: False)
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    assert _run(state, ctx)["decision"] == "applied"


def test_machine_lock_of_live_process_waits(env, monkeypatch):
    state, ctx, _binary, _m = env
    lock = Path(ctx.workdir) / autoupdate.LOCK_FILENAME
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({"pid": os.getpid(), "at": NOW}), encoding="utf-8")
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["decision"], payload["reason"]) == ("waiting", "busy_other_process")
    assert lock.exists()


def test_clear_bad_versions(env):
    state, _ctx, _binary, _m = env
    state.releases["bad_versions"] = ["1.3.18", "1.3.20"]
    state.releases["auto_failures"] = {"1.3.21": 3}
    state.save()

    one = autoupdate.clear_bad_versions(state, version="1.3.20")
    assert one["cleared"] == ["1.3.20"]
    assert state.releases["bad_versions"] == ["1.3.18"]

    rest = autoupdate.clear_bad_versions(state)
    assert rest["cleared"] == ["1.3.18", "1.3.21"]
    assert (state.releases["bad_versions"], state.releases["auto_failures"]) == ([], {})
    disk = json.loads(Path(state.path).read_text(encoding="utf-8"))
    assert disk["releases"]["bad_versions"] == []
    assert autoupdate.clear_bad_versions(state)["reason"] == "nothing_to_clear"

