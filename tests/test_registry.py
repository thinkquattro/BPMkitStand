"""Тесты реестра: чтение образца, поле transport, add_existing, BOM-терпимость, запись без BOM."""

import json
from pathlib import Path

import pytest

from standkit.models import Stand, Transport
from standkit.registry import Registry, RegistryError

_SAMPLE = Path(__file__).resolve().parents[1] / "projects.sample.json"


def test_load_sample_registry_has_stands_with_transport():
    reg = Registry.load(_SAMPLE)
    assert "example-local" in reg
    assert "example-remote" in reg

    local = reg.get("example-local")
    assert local.transport == Transport.LOCAL

    remote = reg.get("example-remote")
    assert remote.transport == Transport.AGENT
    assert remote.agent_url == "https://example-host:8765"
    assert remote.agent_secret_ref == "standkit:example-remote:agent-token"


def test_load_missing_registry_returns_empty_registry(tmp_path):
    reg = Registry.load(tmp_path / "does_not_exist.json")
    assert len(reg) == 0
    assert reg.names() == []


def test_add_existing_and_save_roundtrip(tmp_path):
    reg_path = tmp_path / "projects.json"
    reg = Registry.load(reg_path)

    stand = Stand(name="new-stand", stand_dir=str(tmp_path / "new-stand"), transport=Transport.LOCAL)
    reg.add_existing(stand, make_default=True)
    reg.save()

    assert reg.default == "new-stand"
    assert reg_path.exists()

    # Файл записан без BOM.
    raw_bytes = reg_path.read_bytes()
    assert not raw_bytes.startswith(b"\xef\xbb\xbf")

    reloaded = Registry.load(reg_path)
    assert "new-stand" in reloaded
    assert reloaded.get("new-stand").stand_dir == str(tmp_path / "new-stand")


def test_add_existing_rejects_invalid_stand(tmp_path):
    reg = Registry.load(tmp_path / "projects.json")
    bad_stand = Stand(name="bad", stand_dir="")
    with pytest.raises(RegistryError):
        reg.add_existing(bad_stand)


def test_save_creates_missing_parent_dir(tmp_path):
    # Свежая машина: папки реестра (напр. %APPDATA%\BPMkit) ещё нет.
    # save() должен создать её сам, а не падать FileNotFoundError.
    reg_path = tmp_path / "does_not_exist_yet" / "BPMkit" / "projects.json"
    reg = Registry.load(reg_path)
    reg.add_existing(
        Stand(name="s1", stand_dir=str(tmp_path / "s1"), transport=Transport.LOCAL),
        make_default=True,
    )

    reg.save()  # не должно бросать исключение

    assert reg_path.exists()
    assert reg_path.parent.is_dir()
    assert "s1" in Registry.load(reg_path)


def test_load_tolerates_bom(tmp_path):
    reg_path = tmp_path / "projects.json"
    payload = {"default": "demo", "stands": {"demo": {"transport": "local", "stand_dir": "/opt/demo"}}}
    reg_path.write_text(json.dumps(payload), encoding="utf-8-sig")

    reg = Registry.load(reg_path)
    assert "demo" in reg
    assert reg.get_default().name == "demo"


def test_remove_updates_default(tmp_path):
    reg = Registry.load(tmp_path / "projects.json")
    reg.add_existing(Stand(name="a", stand_dir="/opt/a"), make_default=True)
    reg.add_existing(Stand(name="b", stand_dir="/opt/b"))
    reg.remove("a")

    assert "a" not in reg
    assert reg.default == "b"
