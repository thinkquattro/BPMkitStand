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
    monkeypatch.setattr(releases.mcp_mutex, "server_mutex_exists", lambda *a, **k: False)
    return state, ctx, binary, marker


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


def test_execute_applies_installer_with_swapbusy_and_records_before_launch(env, monkeypatch):
    state, ctx, binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls)

    payload = _run(state, ctx)

    assert (payload["ok"], payload["decision"]) == (True, "applied")
    assert (payload["version"], payload["current"]) == ("1.3.20", "1.3.19")
    assert payload["artifact"]["kind"] == "installer"
    name, target, swap_busy, auto_at_launch = calls.log[-1]
    assert (name, target, swap_busy) == ("apply_installer", "1.3.20", True)
    assert auto_at_launch["applied_version"] == "1.3.20", (
        "запись «поставлено автоматически» обязана лечь ДО запуска установщика")
    auto = state.releases["auto"]
    assert auto["binary"] == str(binary) and auto["via"] == "installer"
    assert auto["previous_version"] == "1.3.19" and auto["trigger"] == "schedule"
    assert Path(auto["backup"]).read_bytes() == b"MZ stary 1.3.19", "запасная точка отката"
    assert state.releases["last_auto_event"]["kind"] == "applied"
    assert state.releases["last_auto_decision"]["decision"] == "applied"


def test_execute_falls_back_to_exe_path_with_swap(env, monkeypatch):
    state, ctx, _binary, _marker = env
    calls = _Calls()
    _stub_channel(monkeypatch, calls, installer=False)

    payload = _run(state, ctx)

    assert payload["decision"] == "applied"
    assert payload["artifact"]["kind"] == "exe"
    assert ("apply_staged", True) in calls.log
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
    проверяется, установщик запускается тихо и с ключом /SWAPBUSY."""
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
    assert releases.SWAPBUSY_FLAG in launched["args"]
    assert "/VERYSILENT" in launched["args"]


def test_precheck_without_network():
    assert autoupdate.precheck(_settings(enabled=False))["reason"] == "channel_disabled"
    assert autoupdate.precheck(_settings(update_mode="off"))["reason"] == "mode_off"
    assert autoupdate.precheck(_settings()) is None


# --------------------------------------------------------------------------------------
# Автооткат и подтверждение
# --------------------------------------------------------------------------------------
def _pending(state, binary, *, via="installer", backup=None):
    state.releases["auto"] = {"applied_version": "1.3.20", "previous_version": "1.3.19",
                              "binary": str(binary), "backup": backup, "via": via,
                              "applied_at": "2026-10-20T11:00:00Z", "confirmed_at": None}


def _marker(path, *, version="1.3.20", attempts=2, ok=False,
            last="2026-10-20T11:55:00Z"):
    path.write_text(json.dumps({"version": version, "attempts": attempts, "ok": ok,
                                "first_attempt_at": "2026-10-20T11:50:00Z",
                                "last_attempt_at": last, "ok_at": None}), encoding="utf-8")


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
    _pending(state, binary)
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
