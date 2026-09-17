"""
Тесты потока доставки кукбука пользователя: `standkit_companion.cookbook` (GAP-361).

Свойства, ради которых модуль написан именно так (и которые здесь проверяются):

1. **документ ложится в профиль, а не в поставку** — `%APPDATA%\\BPMkit\\docs`;
   запись в `{app}\\docs` невозможна при установке «для всех» (установщик:
   `PrivilegesRequired=lowest` + `/ALLUSERS`), и именно это делало GAP-361
   невоспроизводимым для части парка;
2. **fail-closed по подписи** — без сошедшегося сайдкара документ не применяется,
   прежняя копия цела ПОБАЙТНО;
3. **сравнение строковое** — версия вида `<поставка>-<sha8>`, и «то же самое»
   означает побайтово тот же документ, а не «номер не вырос»;
4. **инварианты релизного канала не тронуты** — поток кукбука не использует
   слот `staged`, не требует `MZ` и не выставляет `restart_required`.

Сеть не используется: `FakeClient` с теми же сигнатурами, подписи настоящие —
подписыватель переиспользуется из `tests/test_companion_signature.py`.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from standkit_companion import cookbook
from standkit_companion import signature as sigmod
from standkit_companion.errors import ChannelError
from standkit_companion.state import CompanionState
from tests.test_companion_signature import FAKE_SEED, OTHER_SEED, _public_key, _sign

PUBKEY_RAW = _public_key(FAKE_SEED)
PUBKEY_B64 = base64.b64encode(PUBKEY_RAW).decode("ascii")
KEY_ID = hashlib.sha256(PUBKEY_RAW).hexdigest()[:16]

PREFIX = cookbook.COOKBOOK_PREFIX


def _html(version: str, filler: str = "тело инструкции") -> bytes:
    return (
        '<!doctype html><html><head><meta charset="UTF-8">'
        '<meta name="bpmkit-cookbook-version" content="{}">'
        "</head><body>{}</body></html>"
    ).format(version, filler).encode("utf-8")


def _meta(version: str, blob: bytes, *, signed: bool = True) -> dict:
    return {
        "version": version,
        "filename": "cookbook.html",
        "size_bytes": len(blob),
        "sha256": hashlib.sha256(blob).hexdigest(),
        "published_at": "2026-09-17T10:00:00Z",
        "signed": signed,
        "sig_key_id": KEY_ID,
    }


def _sidecar(blob: bytes, *, seed: bytes = FAKE_SEED, artifact: str = "cookbook.html") -> dict:
    digest = hashlib.sha256(blob).hexdigest()
    raw = _sign(seed, bytes.fromhex(digest))
    return {
        "format": sigmod.SIG_FORMAT,
        "artifact": artifact,
        "size": len(blob),
        "sha256": digest,
        "signed_at": "2026-09-17T10:00:00Z",
        "key_id": hashlib.sha256(_public_key(seed)).hexdigest()[:16],
        "signature": base64.b64encode(raw).decode("ascii"),
    }


class FakeClient:
    def __init__(self, meta: dict, blob: bytes, *, sidecar: dict | None = None) -> None:
        self.meta = meta
        self.blob = blob
        self.sidecar = sidecar
        self.calls: list = []
        self.meta_error: BaseException | None = None

    def get_json(self, path, *, params=None, authorized=True, etag=None) -> tuple:
        self.calls.append(("GET", path))
        if path.endswith("/meta"):
            if self.meta_error is not None:
                raise self.meta_error
            return dict(self.meta), {}
        if path.endswith("/signature"):
            if self.sidecar is None:
                raise ChannelError("Бэкенд издателя ответил 404",
                                   kind="signature_not_available", http_status=404,
                                   detail="signature not available")
            return dict(self.sidecar), {}
        raise AssertionError(f"неожиданный JSON-запрос: {path}")

    def download(self, path, dest, *, authorized=True, resume_from=0, etag=None,
                 expected_size=None, chunk_size=1 << 20) -> dict:
        self.calls.append(("GET-FILE", path))
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blob)
        return {"bytes_written": len(self.blob), "total_bytes": len(self.blob),
                "resumed": False, "status": 200}

    @property
    def file_calls(self) -> list:
        return [c for c in self.calls if c[0] == "GET-FILE"]


@dataclass
class FakeCtx:
    binary_path: str = ""
    artifact_pubkey: str = PUBKEY_B64


@pytest.fixture()
def env(tmp_path):
    """Состояние + контекст + каталог профиля (`config_dir`), изолированные в tmp."""
    state = CompanionState(tmp_path / "companion-state.json")
    binary = tmp_path / "app" / "server" / "bpmkit.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"MZ binar")
    ctx = FakeCtx(binary_path=str(binary))
    return state, ctx, tmp_path / "profile"


# --- Версия из документа --------------------------------------------------------


def test_read_version_from_meta_tag(tmp_path):
    path = tmp_path / "cookbook.html"
    path.write_bytes(_html("0.420.0-a1b2c3d4"))
    assert cookbook.read_version(path) == "0.420.0-a1b2c3d4"


def test_read_version_missing_tag_is_none(tmp_path):
    path = tmp_path / "cookbook.html"
    path.write_bytes(b"<html><head></head><body>no meta</body></html>")
    assert cookbook.read_version(path) is None


def test_read_version_missing_file_is_none(tmp_path):
    assert cookbook.read_version(tmp_path / "нет-такого.html") is None


def test_installed_version_prefers_profile_over_shipped(env):
    """Порядок обязан совпадать с тем, в котором документ ищут ярлык и self_check:
    иначе канал считал бы обновление применённым, пока пользователь открывает
    старую копию — ровно симптом GAP-361."""
    _state, ctx, config_dir = env
    shipped = Path(ctx.binary_path).parent.parent / "docs" / "cookbook.html"
    shipped.parent.mkdir(parents=True, exist_ok=True)
    shipped.write_bytes(_html("0.400.0-old00000"))

    assert cookbook.installed_version(ctx, config_dir) == "0.400.0-old00000"

    profile = cookbook.cookbook_path(config_dir)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_bytes(_html("0.420.0-new11111"))

    assert cookbook.installed_version(ctx, config_dir) == "0.420.0-new11111"


# --- check ----------------------------------------------------------------------


def test_check_does_not_download(env):
    state, ctx, _dir = env
    blob = _html("0.420.0-a1b2c3d4")
    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob, sidecar=_sidecar(blob))

    result = cookbook.check(client, state, ctx)

    assert result["available"] is True
    assert client.file_calls == []


def test_check_up_to_date_when_version_matches(env):
    state, ctx, config_dir = env
    blob = _html("0.420.0-a1b2c3d4")
    profile = cookbook.cookbook_path(config_dir)
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_bytes(blob)

    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob, sidecar=_sidecar(blob))
    # `installed_version` смотрит в профиль по умолчанию — подменяем каталог.
    result = cookbook.check(client, state, ctx, config_dir=config_dir)

    assert result["available"] is False
    assert result["reason"] == "up_to_date"


def test_check_404_is_skipped_not_error(env):
    state, ctx, _dir = env
    blob = _html("0.420.0-a1b2c3d4")
    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob)
    client.meta_error = ChannelError("нет кукбука", kind="http_error", http_status=404)

    result = cookbook.check(client, state, ctx)

    assert result["available"] is False
    assert result["reason"] == "cookbook_not_configured"
    assert state.cookbook["last_status"] == "skipped"


# --- sync -----------------------------------------------------------------------


def test_sync_applies_document_into_profile(env):
    state, ctx, config_dir = env
    blob = _html("0.420.0-a1b2c3d4")
    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob, sidecar=_sidecar(blob))

    result = cookbook.sync(client, state, ctx, config_dir=config_dir)

    target = cookbook.cookbook_path(config_dir)
    assert result["applied"] is True
    assert result["version"] == "0.420.0-a1b2c3d4"
    assert target.read_bytes() == blob
    assert state.cookbook["installed"]["version"] == "0.420.0-a1b2c3d4"
    assert state.cookbook["installed"]["key_id"] == KEY_ID


def test_sync_does_not_touch_release_staging_slot(env):
    """Инвариант GAP-212/279: поток кукбука не трогает слот подготовленного
    бинаря — иначе документ вытеснял бы готовое обновление MCP."""
    state, ctx, config_dir = env
    state.releases["staged"] = {"version": "0.500.0", "path": "C:/staged/bpmkit.exe"}
    blob = _html("0.420.0-a1b2c3d4")
    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob, sidecar=_sidecar(blob))

    cookbook.sync(client, state, ctx, config_dir=config_dir)

    assert state.releases["staged"] == {"version": "0.500.0", "path": "C:/staged/bpmkit.exe"}
    assert state.releases.get("restart_required") is False


def test_sync_without_sidecar_keeps_previous_document(env):
    """Fail-closed: подписи нет -> документ не применён, прежний цел побайтно."""
    state, ctx, config_dir = env
    old = _html("0.400.0-old00000", filler="прежняя инструкция")
    target = cookbook.cookbook_path(config_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(old)

    blob = _html("0.420.0-a1b2c3d4")
    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob, sidecar=None)

    with pytest.raises(ChannelError) as exc:
        cookbook.sync(client, state, ctx, config_dir=config_dir)

    assert exc.value.kind == "signature_not_available"
    assert target.read_bytes() == old


def test_sync_unsigned_meta_does_not_download(env):
    """`signed: false` обязан заблокировать установку И не потратить трафик."""
    state, ctx, config_dir = env
    blob = _html("0.420.0-a1b2c3d4")
    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob, signed=False), blob,
                        sidecar=_sidecar(blob))

    with pytest.raises(ChannelError) as exc:
        cookbook.sync(client, state, ctx, config_dir=config_dir)

    assert exc.value.kind == "signature_not_available"
    assert client.file_calls == []


def test_sync_signature_from_wrong_key_keeps_previous(env):
    state, ctx, config_dir = env
    old = _html("0.400.0-old00000", filler="прежняя инструкция")
    target = cookbook.cookbook_path(config_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(old)

    blob = _html("0.420.0-a1b2c3d4")
    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob,
                        sidecar=_sidecar(blob, seed=OTHER_SEED))

    with pytest.raises(ChannelError):
        cookbook.sync(client, state, ctx, config_dir=config_dir)

    assert target.read_bytes() == old


def test_sync_version_inside_document_must_match_meta(env):
    """Канал обещал одну редакцию, а внутри файла другая — не применяем: иначе
    self_check и ярлык показывали бы версию, которой канал не доставлял."""
    state, ctx, config_dir = env
    blob = _html("9.9.9-mismatch")
    meta = _meta("0.420.0-a1b2c3d4", blob)
    client = FakeClient(meta, blob, sidecar=_sidecar(blob))

    with pytest.raises(ChannelError) as exc:
        cookbook.sync(client, state, ctx, config_dir=config_dir)

    assert exc.value.kind == "artifact_type_mismatch"
    assert not cookbook.cookbook_path(config_dir).exists()


def test_sync_skips_when_already_current(env):
    state, ctx, config_dir = env
    blob = _html("0.420.0-a1b2c3d4")
    target = cookbook.cookbook_path(config_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)

    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob, sidecar=_sidecar(blob))
    result = cookbook.sync(client, state, ctx, config_dir=config_dir)

    assert result["applied"] is False
    assert result["reason"] == "up_to_date"
    assert client.file_calls == []


def test_summary_exposes_cookbook_section(env):
    state, ctx, config_dir = env
    blob = _html("0.420.0-a1b2c3d4")
    client = FakeClient(_meta("0.420.0-a1b2c3d4", blob), blob, sidecar=_sidecar(blob))
    cookbook.sync(client, state, ctx, config_dir=config_dir)

    summary = state.summary()

    assert summary["cookbook"]["installed_version"] == "0.420.0-a1b2c3d4"
    assert summary["cookbook"]["status"] == "ok"
