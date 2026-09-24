"""GAP-527 -- формат тега версии кукбука: генератор dev-репо пишет ГОЛЫЙ sha8.

СИМПТОМ. `tools/gen_cookbook_meta.py` (GAP-423) пишет в `bpmkit-cookbook-version`
только sha8 содержимого (`831f9a68`), а номер поставки -- отдельным тегом
`bpmkit-cookbook-for-delivery`, порядок -- тегом `bpmkit-cookbook-date`.
`installed_version` ждала `<поставка>-<sha8>` и брала из хеша ведущие цифры
(`831`) как номер поставки: выбор между копией в профиле и в `{app}\\docs`
решался случайными hex-цифрами.

ФИКС. Правило выбора -- как у `self_check` MCP-сервера (`_cookbook_pick`):
дата редакции, затем прежний числовой префикс; префикс sha8 больше не число.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import pytest

from standkit_companion import cookbook


def _html(version, date=None, delivery=None, reversed_attrs=False):
    tags = ['<meta name="bpmkit-cookbook-version" content="{}">'.format(version)]
    if delivery:
        tags.append('<meta name="bpmkit-cookbook-for-delivery" content="{}">'.format(delivery))
    if date:
        tags.append(('<meta content="{}" name="bpmkit-cookbook-date">' if reversed_attrs
                     else '<meta name="bpmkit-cookbook-date" content="{}">').format(date))
    return ('<!doctype html><html><head><meta charset="UTF-8">' + "".join(tags)
            + "</head><body>тело</body></html>").encode("utf-8")


@dataclass
class FakeCtx:
    binary_path: str = ""


@pytest.fixture()
def paths(tmp_path):
    binary = tmp_path / "app" / "server" / "bpmkit.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"MZ binar")
    ctx = FakeCtx(binary_path=str(binary))
    config_dir = tmp_path / "profile"

    def put(where, raw, mtime=None):
        path = (cookbook.cookbook_path(config_dir) if where == "profile"
                else binary.parent.parent / "docs" / cookbook.COOKBOOK_FILENAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    return ctx, config_dir, put


@pytest.mark.parametrize("version", ["831f9a68", "9f000000", "12ab34cd", "1a2b3c4d"])
def test_bare_sha8_is_not_a_delivery_number(version):
    assert cookbook._version_order_key(version) is None


def test_prefix_keys_still_parse():
    assert cookbook._version_order_key("1.1.149-bb912a67") == (1, 1, 149)
    assert cookbook._version_order_key("0.12.8") == (0, 12, 8)


def test_newer_date_wins_regardless_of_hex_digits(paths):
    """РЕГРЕСС: старая редакция `9f...` (ведущее «9») больше не бьёт новую `1a...`."""
    ctx, config_dir, put = paths
    put("profile", _html("9f000000", date="2026-09-01", delivery="1.1.200"))
    put("shipped", _html("1a2b3c4d", date="2026-09-24", delivery="1.1.224"))
    assert cookbook.installed_version(ctx, config_dir) == "1a2b3c4d"
    assert cookbook._stale_profile_copy(ctx, config_dir) == {
        "profile": "9f000000", "shipped": "1a2b3c4d"}


def test_newer_profile_by_date_wins(paths):
    ctx, config_dir, put = paths
    put("profile", _html("1a2b3c4d", date="2026-09-24"))
    put("shipped", _html("9f000000", date="2026-09-01"))
    assert cookbook.installed_version(ctx, config_dir) == "1a2b3c4d"
    assert cookbook._stale_profile_copy(ctx, config_dir) is None


def test_date_read_with_reversed_attributes(paths):
    ctx, config_dir, put = paths
    put("profile", _html("aaaaaaaa", date="2026-09-01"))
    put("shipped", _html("bbbbbbbb", date="2026-09-24", reversed_attrs=True))
    assert cookbook.installed_version(ctx, config_dir) == "bbbbbbbb"


def test_dated_copy_beats_undated(paths):
    """Как у сервера: дата есть только у одной копии -- побеждает она."""
    ctx, config_dir, put = paths
    put("profile", _html("1.1.300-aaaaaaaa"))
    put("shipped", _html("bbbbbbbb", date="2026-09-24"))
    assert cookbook.installed_version(ctx, config_dir) == "bbbbbbbb"


def test_equal_versions_keep_profile(paths):
    ctx, config_dir, put = paths
    now = time.time()
    put("profile", _html("aaaaaaaa", date="2026-09-24"), mtime=now - 3600)
    put("shipped", _html("aaaaaaaa", date="2026-09-24"), mtime=now)
    copies = cookbook.installed_copies(ctx, config_dir)
    assert [c["date"] for c in copies] == ["2026-09-24", "2026-09-24"]
    assert cookbook.installed_version(ctx, config_dir) == "aaaaaaaa"
    assert cookbook._stale_profile_copy(ctx, config_dir) is None


def test_same_date_different_sha_falls_back_to_file_time_only_for_prefixed(paths):
    """Голый sha8 и одна дата -- сравнить нечем, остаётся профиль (порядок поиска)."""
    ctx, config_dir, put = paths
    now = time.time()
    put("profile", _html("aaaaaaaa", date="2026-09-24"), mtime=now - 3600)
    put("shipped", _html("bbbbbbbb", date="2026-09-24"), mtime=now)
    assert cookbook.installed_version(ctx, config_dir) == "aaaaaaaa"


def test_bad_date_is_ignored():
    assert cookbook._DATE_VALUE_RE.match("2026-09-24")
    assert not cookbook._DATE_VALUE_RE.match("24.09.2026")
