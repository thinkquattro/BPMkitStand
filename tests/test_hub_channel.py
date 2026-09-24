# -*- coding: utf-8 -*-
"""Тесты канала самообновления диспетчера (kind=hub, GAP-523):
`standkit_companion.hub_channel`.

Переиспользует `FakeClient`/`FakeCtx` из `tests/test_companion_releases.py` и тестовый
подписыватель Ed25519 из `tests/test_companion_signature.py` — тот же контракт
`BackendClient`, заводить вторую копию значило бы завести расходящийся дубликат.

Что проверяется:
1. `check_hub` не качает файл (дешёвая проверка через `/meta`);
2. `check_hub_pypi` — best-effort, сетевая ошибка не поднимается как `ChannelError`;
3. `stage_hub` отказывает СРАЗУ (без единого запроса) в pip-режиме
   (`self_update_unsupported`) — тот же принцип "отказ раньше действия", что у
   `requires_installer`;
4. `kind` сайдкара ОБЯЗАН быть `"hub"` — чужой/отсутствующий кидает `artifact_kind_mismatch`;
5. `apply_self_update` запускает ПОМОЩНИКА (`spawn_hidden`) с правильными флагами
   `--apply-self-update --target ... --wait-pid ...`, НЕ подменяет файл сам и НЕ трогает
   HTTP-сервер (это ответственность `standkit_hub.server`, не канала).
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from standkit_companion import hub_channel
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from tests.test_companion_releases import FakeClient, FakeCtx
from tests.test_companion_signature import FAKE_SEED, _public_key, _sign
from standkit_companion import signature as sigmod

KEY_ID = hashlib.sha256(_public_key(FAKE_SEED)).hexdigest()[:16]


def _blob(version: str, size: int = 4096) -> bytes:
    out = bytearray(b"MZ")
    chunk = hashlib.sha256(f"hub {version}".encode("utf-8")).digest()
    while len(out) < size:
        out += chunk
        chunk = hashlib.sha256(chunk).digest()
    return bytes(out[:size])


def _hub_meta(version: str, blob: bytes, *, filename: str | None = None,
             signed: bool = True) -> dict:
    return {
        "version": version,
        "filename": filename or f"bpmkit-hub-{version}.exe",
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "published_at": "2026-09-24T10:00:00Z",
        "signed": signed,
        "sig_key_id": KEY_ID,
        "is_latest": True,
    }


def _hub_sidecar(filename: str, blob: bytes, *, seed: bytes = FAKE_SEED,
                 kind="hub") -> dict:
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


@pytest.fixture()
def env(tmp_path):
    binary = tmp_path / "hub" / "bpmkit-hub.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"stary hub 0.12.10")
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary))
    return state, ctx


@pytest.fixture(autouse=True)
def _frozen_hub(monkeypatch):
    """По умолчанию — exe-сборка (frozen); тесты pip-режима переопределяют явно."""
    monkeypatch.setattr(hub_channel, "is_frozen_hub", lambda: True)


# ======================================================================================
# check_hub -- дёшево
# ======================================================================================
def test_check_hub_does_not_download(env):
    state, _ctx = env
    blob = _blob("0.12.11")
    client = FakeClient(_hub_meta("0.12.11", blob), blob)

    result = hub_channel.check_hub(client, state, version="0.12.11")

    assert result["mode"] == "exe"
    assert client.file_calls == []


def test_check_hub_not_configured_is_typed_skip(env):
    state, _ctx = env
    blob = _blob("0.12.11")
    client = FakeClient(_hub_meta("0.12.11", blob), blob)
    client.meta_error = ChannelError("нет диспетчера", kind="http_error",
                                      http_status=404, detail="hub not configured")

    result = hub_channel.check_hub(client, state, version="0.12.11")
    assert result["available"] is False
    assert result["reason"] == "hub_not_available"


# ======================================================================================
# check_hub_pypi -- best effort
# ======================================================================================
def test_check_hub_pypi_offline_is_not_an_exception(monkeypatch):
    def _boom(*a, **k):
        raise OSError("сеть недоступна")

    monkeypatch.setattr(hub_channel, "urlopen", _boom)
    result = hub_channel.check_hub_pypi()
    assert result["mode"] == "pip"
    assert result["available"] is False
    assert result["reason"] == "offline"


# ======================================================================================
# stage_hub -- pip-режим отказывает СРАЗУ
# ======================================================================================
def test_stage_hub_pip_mode_rejects_before_any_request(env, monkeypatch):
    state, ctx = env
    monkeypatch.setattr(hub_channel, "is_frozen_hub", lambda: False)
    blob = _blob("0.12.11")
    client = FakeClient(_hub_meta("0.12.11", blob), blob)

    with pytest.raises(ChannelError) as excinfo:
        hub_channel.stage_hub(client, state, ctx, version="0.12.11")
    assert excinfo.value.kind == "self_update_unsupported"
    assert client.calls == []


def test_stage_hub_writes_verified_file(env):
    state, ctx = env
    blob = _blob("0.13.0")
    filename = "bpmkit-hub-0.13.0.exe"
    meta = _hub_meta("0.13.0", blob, filename=filename)
    sidecar = _hub_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)

    result = hub_channel.stage_hub(client, state, ctx, version="0.13.0")

    assert result["version"] == "0.13.0"
    staged_path = Path(result["path"])
    assert staged_path.read_bytes() == blob
    assert hub_channel.staged_hub_info(state)["filename"] == filename


def test_stage_hub_rejects_missing_kind(env):
    state, ctx = env
    blob = _blob("0.13.1")
    filename = "bpmkit-hub-0.13.1.exe"
    meta = _hub_meta("0.13.1", blob, filename=filename)
    sidecar = _hub_sidecar(filename, blob, kind=None)
    client = FakeClient(meta, blob, sidecar=sidecar)

    with pytest.raises(ChannelError) as excinfo:
        hub_channel.stage_hub(client, state, ctx, version="0.13.1")
    assert excinfo.value.kind == "artifact_kind_mismatch"
    assert hub_channel.staged_hub_info(state) is None


def test_stage_hub_rejects_foreign_kind(env):
    state, ctx = env
    blob = _blob("0.13.2")
    filename = "bpmkit-hub-0.13.2.exe"
    meta = _hub_meta("0.13.2", blob, filename=filename)
    sidecar = _hub_sidecar(filename, blob, kind="skills")
    client = FakeClient(meta, blob, sidecar=sidecar)

    with pytest.raises(ChannelError) as excinfo:
        hub_channel.stage_hub(client, state, ctx, version="0.13.2")
    assert excinfo.value.kind == "artifact_kind_mismatch"


def test_stage_hub_rejects_wrong_filename_pattern(env):
    state, ctx = env
    blob = _blob("0.13.3")
    filename = "bpmkit-0.13.3.exe"  # релизный шаблон, не hub
    meta = _hub_meta("0.13.3", blob, filename=filename)
    client = FakeClient(meta, blob, sidecar=_hub_sidecar(filename, blob))

    with pytest.raises(ChannelError) as excinfo:
        hub_channel.stage_hub(client, state, ctx, version="0.13.3")
    assert excinfo.value.kind == "artifact_kind_mismatch"
    assert client.file_calls == []


def test_stage_hub_rejects_unsigned(env):
    state, ctx = env
    blob = _blob("0.13.4")
    filename = "bpmkit-hub-0.13.4.exe"
    meta = _hub_meta("0.13.4", blob, filename=filename, signed=False)
    client = FakeClient(meta, blob, sidecar=_hub_sidecar(filename, blob))

    with pytest.raises(ChannelError) as excinfo:
        hub_channel.stage_hub(client, state, ctx, version="0.13.4")
    assert excinfo.value.kind == "signature_not_available"
    assert client.file_calls == []


# ======================================================================================
# apply_self_update -- запускает ПОМОЩНИКА, не подменяет файл сам
# ======================================================================================
def test_apply_self_update_spawns_helper_with_correct_flags(env, monkeypatch):
    state, ctx = env
    blob = _blob("0.14.0")
    filename = "bpmkit-hub-0.14.0.exe"
    meta = _hub_meta("0.14.0", blob, filename=filename)
    sidecar = _hub_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)
    hub_channel.stage_hub(client, state, ctx, version="0.14.0")

    spawned = {}

    def _fake_spawn_hidden(cmd, cwd, log_path):
        spawned["cmd"] = list(cmd)
        return 9999

    monkeypatch.setattr(hub_channel, "spawn_hidden", _fake_spawn_hidden)
    monkeypatch.setattr(hub_channel.sys, "executable", str(Path(ctx.binary_path)))

    result = hub_channel.apply_self_update(state, ctx)

    assert result["launched"] is True
    assert result["pid"] == 9999
    cmd = spawned["cmd"]
    assert cmd[0].endswith(filename)
    assert "--apply-self-update" in cmd
    assert "--target" in cmd
    assert "--wait-pid" in cmd
    assert cmd[cmd.index("--target") + 1] == str(Path(ctx.binary_path))
    # Файл-цель НЕ тронут этим вызовом -- подмену делает ПОМОЩНИК (второй процесс).
    assert Path(ctx.binary_path).read_bytes() == b"stary hub 0.12.10"


def test_apply_self_update_without_staged_is_nothing_staged(env):
    state, ctx = env
    with pytest.raises(ChannelError) as excinfo:
        hub_channel.apply_self_update(state, ctx)
    assert excinfo.value.kind == "nothing_staged"


def test_apply_self_update_pip_mode_rejects(env, monkeypatch):
    state, ctx = env
    monkeypatch.setattr(hub_channel, "is_frozen_hub", lambda: False)
    with pytest.raises(ChannelError) as excinfo:
        hub_channel.apply_self_update(state, ctx)
    assert excinfo.value.kind == "self_update_unsupported"


def test_apply_self_update_rechecks_kind_before_launch(env, monkeypatch):
    state, ctx = env
    blob = _blob("0.14.1")
    filename = "bpmkit-hub-0.14.1.exe"
    meta = _hub_meta("0.14.1", blob, filename=filename)
    sidecar = _hub_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)
    hub_channel.stage_hub(client, state, ctx, version="0.14.1")

    state.hub["staged"]["sidecar"]["kind"] = "skills"
    state.save()

    called = {"spawn": False}
    monkeypatch.setattr(hub_channel, "spawn_hidden",
                        lambda *a, **k: called.__setitem__("spawn", True))

    with pytest.raises(ChannelError) as excinfo:
        hub_channel.apply_self_update(state, ctx)
    assert excinfo.value.kind == "artifact_kind_mismatch"
    assert called["spawn"] is False
