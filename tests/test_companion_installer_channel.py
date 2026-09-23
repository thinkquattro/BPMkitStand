# -*- coding: utf-8 -*-
"""Тесты канала установщика (ADR-0048, GAP-279, 23.09.2026):
`standkit_companion.releases.check_installer/stage_installer/apply_installer`.

Переиспользует `FakeClient`/`FakeCtx`/фикстуру `env` из `tests/test_companion_releases.py`
(тот же контракт `BackendClient`, тот же тестовый Ed25519-подписыватель) — заводить
вторую копию тестового клиента ради другого префикса пути было бы дублированием, которое
разойдётся с оригиналом.

Что здесь проверяется и НЕ дублируется соседним файлом:
1. `kind` сайдкара ОБЯЗАН быть `"installer"` — отсутствие/чужое значение отклоняется, БЕЗ
   послабления «подразумевается server» (в отличие от релизного канала);
2. установщик не подменяет файл, а ЗАПУСКАЕТСЯ отдельным процессом — `apply_installer`
   НЕ трогает `ctx.binary_path` вовсе;
3. раздельность состояния — `installer_staged`/`installer_partial` НЕ пересекаются с
   `staged`/`partial` релизного канала;
4. раздельность адреса — `INSTALLER_PREFIX != RELEASES_PREFIX`.
"""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from standkit_companion import releases
from standkit_companion import signature as sigmod
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from tests.test_companion_releases import FakeClient, FakeCtx, PUBKEY_B64, _blob
from tests.test_companion_signature import FAKE_SEED, OTHER_SEED, _public_key, _sign

INSTALLER = releases.INSTALLER_PREFIX
KEY_ID = hashlib.sha256(_public_key(FAKE_SEED)).hexdigest()[:16]


def _installer_meta(version: str, blob: bytes, *, filename: str | None = None,
                     signed: bool = True) -> dict:
    return {
        "version": version,
        "filename": filename or f"bpmkit-setup-{version}.exe",
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "published_at": "2026-09-23T10:00:00Z",
        "signed": signed,
        "sig_key_id": KEY_ID,
        "is_latest": True,
    }


def _installer_sidecar(filename: str, blob: bytes, *, seed: bytes = FAKE_SEED,
                       artifact: str | None = None, kind="installer") -> dict:
    digest = hashlib.sha256(blob).hexdigest()
    raw = _sign(seed, bytes.fromhex(digest))
    sc = {
        "format": sigmod.SIG_FORMAT,
        "artifact": artifact or filename,
        "size": len(blob),
        "sha256": digest,
        "signed_at": "2026-09-23T10:00:00Z",
        "key_id": hashlib.sha256(_public_key(seed)).hexdigest()[:16],
        "signature": base64.b64encode(raw).decode("ascii"),
    }
    if kind is not None:
        sc["kind"] = kind
    return sc


@pytest.fixture()
def env(tmp_path):
    binary = tmp_path / "mcp" / "bpmkit.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"stary binar 0.300.0")
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary))
    return state, ctx


def _staging(ctx) -> Path:
    return releases.companion_workdir(ctx) / releases.STAGING_DIRNAME / "installer"


# --------------------------------------------------------------------------------------
# Раздельность адреса
# --------------------------------------------------------------------------------------


def test_installer_prefix_is_separate_from_releases_prefix():
    assert releases.INSTALLER_PREFIX != releases.RELEASES_PREFIX
    assert releases.INSTALLER_PREFIX.endswith("/installer")


# --------------------------------------------------------------------------------------
# check_installer -- дёшево, без скачивания
# --------------------------------------------------------------------------------------


def test_check_installer_does_not_download(env):
    _state, ctx = env
    blob = _blob("1.5.0")
    client = FakeClient(_installer_meta("1.5.0", blob), blob)

    result = releases.check_installer(client, version="1.5.0")

    assert result["version"] == "1.5.0"
    assert client.file_calls == []


def test_check_installer_not_configured_is_typed_not_error(env):
    _state, ctx = env
    blob = _blob("1.5.0")
    client = FakeClient(_installer_meta("1.5.0", blob), blob)
    client.meta_error = ChannelError("нет установщика", kind="http_error",
                                      http_status=404, detail="installer not configured")

    with pytest.raises(ChannelError) as excinfo:
        releases.check_installer(client, version="1.5.0")
    assert excinfo.value.kind == "installer_not_available"


# --------------------------------------------------------------------------------------
# stage_installer -- kind обязателен, без послабления
# --------------------------------------------------------------------------------------


def test_stage_installer_writes_verified_file(env):
    state, ctx = env
    blob = _blob("2.0.0")
    filename = "bpmkit-setup-2.0.0.exe"
    meta = _installer_meta("2.0.0", blob, filename=filename)
    sidecar = _installer_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)

    result = releases.stage_installer(client, state, ctx, version="2.0.0")

    assert result["version"] == "2.0.0"
    assert result["filename"] == filename
    staged_path = Path(result["path"])
    assert staged_path.read_bytes() == blob
    assert releases.staged_installer_info(state)["filename"] == filename
    # Раздельность состояния — релизный слот НЕ тронут.
    assert state.releases.get("staged") is None


def test_stage_installer_rejects_missing_kind(env):
    state, ctx = env
    blob = _blob("2.0.1")
    filename = "bpmkit-setup-2.0.1.exe"
    meta = _installer_meta("2.0.1", blob, filename=filename)
    sidecar = _installer_sidecar(filename, blob, kind=None)  # поле отсутствует вовсе
    client = FakeClient(meta, blob, sidecar=sidecar)

    with pytest.raises(ChannelError) as excinfo:
        releases.stage_installer(client, state, ctx, version="2.0.1")
    assert excinfo.value.kind == "artifact_kind_mismatch"
    assert releases.staged_installer_info(state) is None


def test_stage_installer_rejects_kind_server(env):
    """Сайдкар, честно подписанный для релиза (kind=server), не годится для
    установщика — та же атака, что backend отклоняет на своей стороне."""
    state, ctx = env
    blob = _blob("2.0.2")
    filename = "bpmkit-setup-2.0.2.exe"
    meta = _installer_meta("2.0.2", blob, filename=filename)
    sidecar = _installer_sidecar(filename, blob, kind="server")
    client = FakeClient(meta, blob, sidecar=sidecar)

    with pytest.raises(ChannelError) as excinfo:
        releases.stage_installer(client, state, ctx, version="2.0.2")
    assert excinfo.value.kind == "artifact_kind_mismatch"


def test_stage_installer_rejects_release_filename_pattern(env):
    """Имя `bpmkit-2.0.3.exe` (без `setup-`) — релизный шаблон, не установщика."""
    state, ctx = env
    blob = _blob("2.0.3")
    filename = "bpmkit-2.0.3.exe"
    meta = _installer_meta("2.0.3", blob, filename=filename)
    client = FakeClient(meta, blob, sidecar=_installer_sidecar(filename, blob))

    with pytest.raises(ChannelError) as excinfo:
        releases.stage_installer(client, state, ctx, version="2.0.3")
    assert excinfo.value.kind == "artifact_kind_mismatch"
    assert client.file_calls == []  # отказ ДО скачивания


def test_stage_installer_rejects_unsigned(env):
    state, ctx = env
    blob = _blob("2.0.4")
    filename = "bpmkit-setup-2.0.4.exe"
    meta = _installer_meta("2.0.4", blob, filename=filename, signed=False)
    client = FakeClient(meta, blob, sidecar=_installer_sidecar(filename, blob))

    with pytest.raises(ChannelError) as excinfo:
        releases.stage_installer(client, state, ctx, version="2.0.4")
    assert excinfo.value.kind == "signature_not_available"
    assert client.file_calls == []


# --------------------------------------------------------------------------------------
# apply_installer -- ЗАПУСКАЕТ процесс, НЕ подменяет бинарь
# --------------------------------------------------------------------------------------


def test_apply_installer_launches_process_not_replace_binary(env, monkeypatch):
    state, ctx = env
    blob = _blob("3.0.0")
    filename = "bpmkit-setup-3.0.0.exe"
    meta = _installer_meta("3.0.0", blob, filename=filename)
    sidecar = _installer_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)
    releases.stage_installer(client, state, ctx, version="3.0.0")

    launched = {}

    class _FakeProcess:
        pid = 4242

    def _fake_popen(args, **kwargs):
        launched["args"] = args
        return _FakeProcess()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    result = releases.apply_installer(state, ctx)

    assert result["launched"] is True
    assert result["pid"] == 4242
    assert launched["args"][0].endswith(filename)
    # Бинарь MCP НЕ тронут -- установщик применяется ЗАПУСКОМ, не подменой файла.
    assert Path(ctx.binary_path).read_bytes() == b"stary binar 0.300.0"
    assert state.releases.get("current") is None


def test_apply_installer_without_staged_is_nothing_staged(env):
    state, ctx = env

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_installer(state, ctx)
    assert excinfo.value.kind == "nothing_staged"


def test_apply_installer_rechecks_kind_before_launch(env, monkeypatch):
    """Между `stage_installer` и `apply_installer` файл в стейджинге могли подменить
    (или сайдкар в состоянии — испортить) — apply обязан проверить подпись/kind ЗАНОВО,
    а не доверять флагу «уже проверено при подготовке»."""
    state, ctx = env
    blob = _blob("3.0.1")
    filename = "bpmkit-setup-3.0.1.exe"
    meta = _installer_meta("3.0.1", blob, filename=filename)
    sidecar = _installer_sidecar(filename, blob)
    client = FakeClient(meta, blob, sidecar=sidecar)
    releases.stage_installer(client, state, ctx, version="3.0.1")

    # Испортить сохранённый kind в состоянии -- имитирует подмену/повреждение.
    state.releases["installer_staged"]["sidecar"]["kind"] = "server"
    state.save()

    called = {"popen": False}
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: called.__setitem__("popen", True))

    with pytest.raises(ChannelError) as excinfo:
        releases.apply_installer(state, ctx)
    assert excinfo.value.kind == "artifact_kind_mismatch"
    assert called["popen"] is False
