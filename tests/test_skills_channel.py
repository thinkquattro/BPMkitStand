# -*- coding: utf-8 -*-
"""Тесты канала скиллов/плагина (kind=skills, GAP-288): `standkit_companion.skills_channel`.

Переиспользует `FakeClient`/`FakeCtx` (tests/test_companion_releases.py) и тестовый
подписыватель Ed25519 (tests/test_companion_signature.py) — тот же контракт, что у
`hub_channel`/`releases`.

Что проверяется:
1. `check_skills` не качает файл;
2. `kind` сайдкара ОБЯЗАН быть `"skills"`;
3. распаковка `.plugin` защищена от zip-slip (запись вне целевой папки отклоняется);
4. `apply_skills` вызывает CLI `setup skills-install <client> --app-dir <распакованное>`
   для каждого клиента из `install_config.json`, пишет маркер `installed.json`, и один
   упавший клиент не блокирует остальных;
5. `install_summary_lite` не запускает CLI и не резолвит лицензионный контекст.
"""
from __future__ import annotations

import base64
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from standkit_companion import skills_channel
from standkit_companion import signature as sigmod
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from tests.test_companion_releases import FakeClient, FakeCtx
from tests.test_companion_signature import FAKE_SEED, _public_key, _sign

KEY_ID = hashlib.sha256(_public_key(FAKE_SEED)).hexdigest()[:16]


def _plugin_zip_bytes(*, extra_names: list[str] | None = None) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("skills/bpmsoft-dev/SKILL.md", "# skill\n")
        zf.writestr("manifest.json", "{}")
        for name in (extra_names or []):
            zf.writestr(name, "evil")
    return b"PK" + buf.getvalue()[2:]  # оставить ZIP-магию как есть (PK уже первая)


def _plugin_blob(**kw) -> bytes:
    data = _plugin_zip_bytes(**kw)
    assert data[:2] == b"PK"
    return data


def _skills_meta(version: str, blob: bytes, *, filename: str | None = None,
                 signed: bool = True) -> dict:
    return {
        "version": version,
        "filename": filename or f"bpmkit-skills-{version}.plugin",
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "published_at": "2026-09-24T10:00:00Z",
        "signed": signed,
        "sig_key_id": KEY_ID,
        "is_latest": True,
    }


def _skills_sidecar(filename: str, blob: bytes, *, seed: bytes = FAKE_SEED,
                    kind="skills") -> dict:
    digest = hashlib.sha256(blob).hexdigest()
    raw = _sign(seed, bytes.fromhex(digest))
    sc = {
        "format": sigmod.SIG_FORMAT,
        "artifact": filename,
        "size": len(blob),
        "sha256": digest,
        "signed_at": "2026-09-24T10:00:00Z",
        "key_id": hashlib.sha256(_public_key(seed)).hexdigest()[:16],
        "signature": base64.b64encode(raw).decode("ascii"),
    }
    if kind is not None:
        sc["kind"] = kind
    return sc


@pytest.fixture(autouse=True)
def _no_real_runtime_marker(monkeypatch):
    """Гермет: `_current_skills_version`/`_app_dir_from_ctx` не имеют права
    подсмотреть РЕАЛЬНЫЙ `mcp_runtime.json` этой машины (`releases.read_runtime_marker`
    читает `bpmkit_config_dir()` напрямую, её не переопределяет фикстура `env`
    ниже) — по умолчанию маркера нет, тесты, которым он нужен, переопределяют явно."""
    monkeypatch.setattr(skills_channel, "read_runtime_marker", lambda: None)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(skills_channel, "bpmkit_config_dir", lambda: appdata)
    state = CompanionState(tmp_path / "companion-state.json")
    binary = tmp_path / "app" / "bin" / "bpmkit.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"mcp binary")
    ctx = FakeCtx(workdir=str(appdata / "companion"), binary_path=str(binary),
                 package_root=str(tmp_path / "app"))
    return state, ctx, tmp_path


# ======================================================================================
# check_skills -- дёшево
# ======================================================================================
def test_check_skills_does_not_download(env):
    state, ctx, _tmp = env
    blob = _plugin_blob()
    client = FakeClient(_skills_meta("1.1.230", blob), blob)

    result = skills_channel.check_skills(client, state, ctx, version="1.1.230")

    assert client.file_calls == []
    assert result["latest"] == "1.1.230"


def test_check_skills_not_configured_is_typed_skip(env):
    state, ctx, _tmp = env
    blob = _plugin_blob()
    client = FakeClient(_skills_meta("1.1.230", blob), blob)
    client.meta_error = ChannelError("нет скиллов", kind="http_error",
                                      http_status=404, detail="skills not configured")

    result = skills_channel.check_skills(client, state, ctx, version="1.1.230")
    assert result["available"] is False
    assert result["reason"] == "skills_not_available"


# ======================================================================================
# stage_skills -- kind обязателен
# ======================================================================================
def test_stage_skills_writes_verified_file(env):
    state, ctx, _tmp = env
    blob = _plugin_blob()
    filename = "bpmkit-skills-1.1.230.plugin"
    meta = _skills_meta("1.1.230", blob, filename=filename)
    sidecar = _skills_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)

    result = skills_channel.stage_skills(client, state, ctx, version="1.1.230")

    assert result["version"] == "1.1.230"
    assert Path(result["path"]).read_bytes() == blob
    assert skills_channel.staged_skills_info(state)["filename"] == filename


def test_stage_skills_rejects_wrong_kind(env):
    state, ctx, _tmp = env
    blob = _plugin_blob()
    filename = "bpmkit-skills-1.1.231.plugin"
    meta = _skills_meta("1.1.231", blob, filename=filename)
    sidecar = _skills_sidecar(filename, blob, kind="hub")
    client = FakeClient(meta, blob, sidecar=sidecar)

    with pytest.raises(ChannelError) as excinfo:
        skills_channel.stage_skills(client, state, ctx, version="1.1.231")
    assert excinfo.value.kind == "artifact_kind_mismatch"


def test_stage_skills_rejects_non_zip_content(env):
    state, ctx, _tmp = env
    blob = b"NOTZIPDATA" * 100
    filename = "bpmkit-skills-1.1.232.plugin"
    meta = _skills_meta("1.1.232", blob, filename=filename)
    sidecar = _skills_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)

    with pytest.raises(ChannelError) as excinfo:
        skills_channel.stage_skills(client, state, ctx, version="1.1.232")
    assert excinfo.value.kind == "artifact_type_mismatch"


# ======================================================================================
# zip-slip
# ======================================================================================
def test_safe_extract_rejects_path_traversal(tmp_path):
    archive = tmp_path / "evil.plugin"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../evil.txt", "pwned")
    dest = tmp_path / "dist"

    with pytest.raises(ChannelError) as excinfo:
        with zipfile.ZipFile(archive) as zf:
            skills_channel._safe_extract(zf, dest)
    assert excinfo.value.kind == "artifact_type_mismatch"
    assert not (tmp_path / "evil.txt").exists()


def test_safe_extract_rejects_absolute_path(tmp_path):
    archive = tmp_path / "evil2.plugin"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("/etc/passwd", "pwned")
    dest = tmp_path / "dist2"

    with pytest.raises(ChannelError):
        with zipfile.ZipFile(archive) as zf:
            skills_channel._safe_extract(zf, dest)


# ======================================================================================
# apply_skills -- CLI по каждому клиенту, один отказ не блокирует остальных
# ======================================================================================
def test_apply_skills_calls_cli_per_client_and_writes_marker(env, monkeypatch):
    state, ctx, tmp = env
    # Гермет: `_app_dir_from_ctx` не имеет права подсмотреть РЕАЛЬНЫЙ
    # `mcp_runtime.json` этой машины -- фолбэк на `ctx.package_root` детерминирован.
    monkeypatch.setattr(skills_channel, "read_runtime_marker", lambda: None)
    blob = _plugin_blob()
    filename = "bpmkit-skills-2.0.0.plugin"
    meta = _skills_meta("2.0.0", blob, filename=filename)
    sidecar = _skills_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)
    skills_channel.stage_skills(client, state, ctx, version="2.0.0")

    install_config = Path(ctx.package_root) / "install_config.json"
    install_config.write_text(json.dumps({"skills_installed": ["cursor", "vscode"]}),
                              encoding="utf-8")

    ctx.cli = ["python", "-m", "bpmkit_cli"]
    calls = []

    class _Proc:
        def __init__(self, ok):
            self.stdout = json.dumps({"ok": ok, "client": "x"})
            self.stderr = ""

    def _fake_run_console(cmd, **kwargs):
        calls.append(list(cmd))
        client_name = cmd[cmd.index("skills-install") + 1]
        return _Proc(ok=(client_name != "vscode"))

    monkeypatch.setattr(skills_channel, "run_console", _fake_run_console)

    result = skills_channel.apply_skills(state, ctx)

    assert len(calls) == 2
    assert any("cursor" in c for c in calls)
    assert any("vscode" in c for c in calls)
    assert result["hosts"] == ["cursor"]  # vscode отказал, в маркер не попал
    marker = skills_channel.read_installed_marker()
    assert marker["version"] == "2.0.0"
    assert marker["hosts"] == ["cursor"]
    # Один отказавший клиент не должен ронять весь вызов исключением.
    assert result["error"] is None


def test_apply_skills_all_failed_marks_error_without_raising(env, monkeypatch):
    state, ctx, tmp = env
    monkeypatch.setattr(skills_channel, "read_runtime_marker", lambda: None)
    blob = _plugin_blob()
    filename = "bpmkit-skills-2.0.1.plugin"
    meta = _skills_meta("2.0.1", blob, filename=filename)
    sidecar = _skills_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)
    skills_channel.stage_skills(client, state, ctx, version="2.0.1")

    install_config = Path(ctx.package_root) / "install_config.json"
    install_config.write_text(json.dumps({"skills_installed": ["cursor"]}), encoding="utf-8")
    ctx.cli = ["python", "-m", "bpmkit_cli"]

    class _Proc:
        stdout = json.dumps({"ok": False, "error": "boom"})
        stderr = ""

    monkeypatch.setattr(skills_channel, "run_console", lambda cmd, **kw: _Proc())

    result = skills_channel.apply_skills(state, ctx)
    assert result["error"] is not None
    assert result["error"]["kind"] == "skills_apply_failed"


def test_apply_skills_without_staged_is_nothing_staged(env):
    state, ctx, _tmp = env
    with pytest.raises(ChannelError) as excinfo:
        skills_channel.apply_skills(state, ctx)
    assert excinfo.value.kind == "nothing_staged"


# ======================================================================================
# install_summary_lite -- НЕ запускает CLI
# ======================================================================================
def test_install_summary_lite_does_not_invoke_cli(env, monkeypatch):
    _state, ctx, _tmp = env

    def _boom(*a, **k):
        raise AssertionError("install_summary_lite не должен запускать процессы")

    monkeypatch.setattr(skills_channel, "run_console", _boom)
    monkeypatch.setattr(skills_channel, "read_runtime_marker",
                        lambda: {"binary": ctx.binary_path})

    result = skills_channel.install_summary_lite()
    assert "app_dir" in result
    assert "plugin_dir" in result
    assert isinstance(result["clients"], list)
