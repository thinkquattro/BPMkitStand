# -*- coding: utf-8 -*-
"""
GAP-781 (часть сабмодуля): путь обновления «только exe» оставлял поставку
несогласованной.

`releases.apply_staged`/`releases.rollback` подменяли только бинарь MCP
(`BPMkit.exe`), не трогая соседей, которые читает сервер:

* `BPMkit.exe.sha256` — эталон суммы сборки; по нему самопроверка сервера
  (`licensing._binary_self_check_uncached`) сверяет целостность — у клиента
  ложное «контрольная сумма артефакта НЕ совпадает… переустановите»;
* `manifest.json` в корне установки (родитель `server\\`) — поле `version`
  оставалось прежним, отсюда ложный WARN self_check «версия процесса
  устарела относительно диска».

Здесь — раскладка поставки установщика на tmp-каталоге
(`<root>/manifest.json`, `<root>/server/BPMkit.exe(.sha256)`), подмена и откат,
и границы точечной правки JSON.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from standkit_companion import fsutil, releases
from standkit_companion.state import CompanionState
from tests.test_companion_releases import (  # noqa: F401 - автофикстура мьютекса
    FakeCtx,
    _client,
    _no_real_mutex_probe,
)

MANIFEST_TEXT = (
    "{\n"
    '  "manifest_version": "0.3",\n'
    '  "name": "BPMkit",\n'
    '  "version": "0.300.0",\n'
    '  "description": "Управление стендами — кириллица и \\"кавычки\\"",\n'
    '  "server": {\n'
    '    "type": "binary",\n'
    '    "version": "не трогать"\n'
    "  }\n"
    "}"
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture()
def install(tmp_path):
    """Поставка установщика: корень с manifest.json, server/ с бинарём и эталоном."""
    root = tmp_path / "BPMkit"
    server = root / "server"
    server.mkdir(parents=True)
    binary = server / "BPMkit.exe"
    old = b"MZ stary binar 0.300.0"
    binary.write_bytes(old)
    (server / "BPMkit.exe.sha256").write_text(f"{_sha(old)}  BPMkit.exe\n", encoding="utf-8")
    (root / "manifest.json").write_bytes(MANIFEST_TEXT.encode("utf-8"))
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary))
    return state, ctx, root, binary, old


def _sidecar_hex(binary: Path) -> str:
    return binary.with_name(binary.name + ".sha256").read_text(encoding="utf-8").split()[0]


def _apply(state, ctx, version):
    client, blob, _meta = _client(version)
    releases.stage(client, state, ctx)
    return releases.apply_staged(state, ctx), blob


def test_apply_staged_rewrites_sidecar_and_manifest_version(install):
    state, ctx, root, binary, _old = install
    result, blob = _apply(state, ctx, "0.307.0")

    assert binary.read_bytes() == blob
    assert _sidecar_hex(binary) == _sha(blob), "эталон суммы обязан описывать НОВЫЙ бинарь"
    sidecar_text = binary.with_name("BPMkit.exe.sha256").read_text(encoding="utf-8")
    assert sidecar_text == f"{_sha(blob)}  BPMkit.exe\n", "формат sha256sum, как у сборки"

    text = (root / "manifest.json").read_text(encoding="utf-8")
    assert text == MANIFEST_TEXT.replace('"version": "0.300.0"', '"version": "0.307.0"'), (
        "правка точечная: всё, кроме значения version верхнего уровня, байт в байт")
    assert json.loads(text)["server"]["version"] == "не трогать"

    assert result["install_sync"] == {"sidecar": "updated", "manifest": "updated"}
    assert state.releases["current"]["install_sync"] == result["install_sync"]


def test_rollback_restores_sidecar_and_manifest_to_previous(install):
    state, ctx, root, binary, old = install
    _apply(state, ctx, "0.307.0")

    result = releases.rollback(state, ctx)

    assert binary.read_bytes() == old
    assert _sidecar_hex(binary) == _sha(old)
    assert json.loads((root / "manifest.json").read_text(encoding="utf-8"))["version"] == "0.300.0"
    assert result["install_sync"] == {"sidecar": "updated", "manifest": "updated"}


def test_absent_sidecar_and_manifest_are_not_created(tmp_path):
    """Раскладка без соседей (старые тесты канала, ручная установка): ничего не
    создаём — эталон кладёт поставка, а не канал обновлений."""
    binary = tmp_path / "mcp" / "bpmkit.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ old")
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary))

    result, _blob = _apply(state, ctx, "0.307.0")

    assert not binary.with_name("bpmkit.exe.sha256").exists()
    assert not (binary.parent / "manifest.json").exists()
    assert result["install_sync"] == {"sidecar": "absent", "manifest": "absent"}


def test_manifest_next_to_binary_when_not_in_server_dir(tmp_path):
    """Бинарь не в `server\\` — корнем считается его же каталог (как `_package_root`)."""
    binary = tmp_path / "flat" / "BPMkit.exe"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"MZ old")
    (binary.parent / "manifest.json").write_text('{"version": "0.300.0"}', encoding="utf-8")
    state = CompanionState(tmp_path / "companion-state.json")
    ctx = FakeCtx(workdir=str(tmp_path / "companion"), binary_path=str(binary))

    result, _blob = _apply(state, ctx, "0.307.0")

    assert (binary.parent / "manifest.json").read_text(encoding="utf-8") == '{"version": "0.307.0"}'
    assert result["install_sync"]["manifest"] == "updated"


def test_manifest_bom_and_crlf_are_preserved(install):
    state, ctx, root, binary, _old = install
    crlf = MANIFEST_TEXT.replace("\n", "\r\n")
    (root / "manifest.json").write_bytes(b"\xef\xbb\xbf" + crlf.encode("utf-8"))

    _apply(state, ctx, "0.307.0")

    raw = (root / "manifest.json").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")
    assert raw[3:].decode("utf-8") == crlf.replace('"version": "0.300.0"', '"version": "0.307.0"')


def test_broken_manifest_does_not_fail_apply(install):
    """Лучшее старание: битый manifest.json — запись в журнал, подмена состоялась."""
    state, ctx, root, binary, _old = install
    (root / "manifest.json").write_text("{ это не json", encoding="utf-8")

    result, blob = _apply(state, ctx, "0.307.0")

    assert binary.read_bytes() == blob
    assert result["applied"] is True
    assert result["install_sync"]["sidecar"] == "updated"
    assert result["install_sync"]["manifest"].startswith("error:")
    assert (root / "manifest.json").read_text(encoding="utf-8") == "{ это не json"


def test_sidecar_write_error_does_not_fail_apply(install, monkeypatch):
    state, ctx, root, binary, _old = install
    client, blob, _meta = _client("0.307.0")
    releases.stage(client, state, ctx)

    real = fsutil.atomic_write_text

    def _boom(path, text, encoding="utf-8"):
        if str(path).endswith(".sha256"):
            raise PermissionError(13, "Access is denied")
        return real(path, text, encoding)

    monkeypatch.setattr(fsutil, "atomic_write_text", _boom)
    result = releases.apply_staged(state, ctx)

    assert binary.read_bytes() == blob
    assert result["install_sync"]["sidecar"].startswith("error:")
    assert result["install_sync"]["manifest"] == "updated"


def test_failed_replace_leaves_companions_untouched(install, monkeypatch):
    state, ctx, root, binary, old = install
    client, _blob, _meta = _client("0.307.0")
    releases.stage(client, state, ctx)
    sidecar_before = binary.with_name("BPMkit.exe.sha256").read_bytes()
    manifest_before = (root / "manifest.json").read_bytes()

    def busy(src, dst, *a, **k):
        raise PermissionError(32, "busy")

    monkeypatch.setattr(fsutil, "replace_with_retry", busy)
    with pytest.raises(releases.ChannelError):
        releases.apply_staged(state, ctx)

    assert binary.read_bytes() == old
    assert binary.with_name("BPMkit.exe.sha256").read_bytes() == sidecar_before
    assert (root / "manifest.json").read_bytes() == manifest_before


@pytest.mark.parametrize("text,expected", [
    ('{"manifest_version": "0.3", "version": "1"}', '"1"'),
    ('{"server": {"version": "x"}, "version": "2"}', '"2"'),
    ('{"a": ["version", {"version": "z"}], "version": "3"}', '"3"'),
    ('{"d": "say \\"version\\"", "version": "4"}', '"4"'),
    ('{"server": {"version": "x"}}', None),
    ('{"version": 5}', None),
])
def test_top_level_key_scanner(text, expected):
    span = releases._top_level_key_value_span(text, "version")
    if expected is None:
        assert span is None
    else:
        assert text[span[0]:span[1]] == expected
