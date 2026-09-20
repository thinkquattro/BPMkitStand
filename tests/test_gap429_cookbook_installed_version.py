"""GAP-429 -- `cookbook.installed_version` брала профильную копию БЕЗУСЛОВНО.

СИМПТОМ (тот же дефект, что PR #837 чинил в `_cookbook_report` dev-репо). Порядок
поиска был «профиль, затем поставка», и ПЕРВАЯ найденная копия объявлялась
установленной. После установки НОВОЙ поставки в `{app}\\docs` ложится свежий
документ, а в профиле остаётся редакция, доставленная каналом раньше -- канал
считал установленной СТАРУЮ: сравнивал бэкенд не с тем, что есть на машине, и
называл пользователю версию, которой у него уже нет.

ФИКС. Сравниваются ОБЕ копии по версии ВНУТРИ файла (`<поставка>-<sha8>`,
посегментно целыми числами), побеждает НОВЕЙШАЯ; при равном номере поставки --
более свежий файл; при неразбираемых версиях порядок прежний (профиль первым),
поэтому согласованность с ярлыком и `self_check` (GAP-361) сохраняется.

Плюс `check` честно предупреждает, когда свежая копия лежит в ПОСТАВКЕ, а ярлык
открывает профильную: иначе отчёт и документ перед глазами пользователя снова
расходятся.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from standkit_companion import cookbook


def _html(version: str) -> bytes:
    return (
        '<!doctype html><html><head><meta charset="UTF-8">'
        '<meta name="bpmkit-cookbook-version" content="{}">'
        "</head><body>тело</body></html>"
    ).format(version).encode("utf-8")


@dataclass
class FakeCtx:
    binary_path: str = ""


@pytest.fixture()
def paths(tmp_path):
    """(ctx, config_dir, положить-профиль, положить-поставку)."""
    binary = tmp_path / "app" / "server" / "bpmkit.exe"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"MZ binar")
    ctx = FakeCtx(binary_path=str(binary))
    config_dir = tmp_path / "profile"

    def put_profile(version, mtime=None, raw=None):
        path = cookbook.cookbook_path(config_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if raw is not None else _html(version))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def put_shipped(version, mtime=None, raw=None):
        path = Path(ctx.binary_path).parent.parent / "docs" / cookbook.COOKBOOK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if raw is not None else _html(version))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    return ctx, config_dir, put_profile, put_shipped


# --- сравнение версий -----------------------------------------------------------


@pytest.mark.parametrize("version,expected", [
    ("1.1.149-bb912a67", (1, 1, 149)),
    ("0.12.8", (0, 12, 8)),
    ("2-abc", (2,)),
    ("dev-snapshot", None),
    ("", None),
    (None, None),
])
def test_version_order_key(version, expected):
    assert cookbook._version_order_key(version) == expected


def test_order_key_is_numeric_not_lexicographic():
    """`1.1.9` НЕ новее `1.1.149` -- строковое сравнение здесь дало бы обратное."""
    assert cookbook._version_order_key("1.1.149-x") > cookbook._version_order_key("1.1.9-x")


# --- собственно регресс ---------------------------------------------------------


def test_newer_shipped_copy_wins_over_stale_profile(paths):
    """РЕГРЕСС GAP-429: в профиле старая доставленная копия, в поставке -- новая."""
    ctx, config_dir, put_profile, put_shipped = paths
    put_profile("1.1.100-aaaaaaaa")
    put_shipped("1.1.149-bbbbbbbb")
    assert cookbook.installed_version(ctx, config_dir) == "1.1.149-bbbbbbbb"


def test_newer_profile_copy_still_wins(paths):
    """Штатный случай (канал доставил свежее поставки) не сломан."""
    ctx, config_dir, put_profile, put_shipped = paths
    put_profile("1.1.149-bbbbbbbb")
    put_shipped("1.1.100-aaaaaaaa")
    assert cookbook.installed_version(ctx, config_dir) == "1.1.149-bbbbbbbb"


def test_same_delivery_version_falls_back_to_file_time(paths):
    """Один номер поставки, разный sha8 содержимого -- решает время файла."""
    ctx, config_dir, put_profile, put_shipped = paths
    now = time.time()
    put_profile("1.1.149-old00000", mtime=now - 3600)
    put_shipped("1.1.149-new11111", mtime=now)
    assert cookbook.installed_version(ctx, config_dir) == "1.1.149-new11111"


def test_unparsable_versions_keep_search_order(paths):
    """Сравнивать нечем -> порядок прежний (профиль первым), GAP-361 не нарушен."""
    ctx, config_dir, put_profile, put_shipped = paths
    # Алфавит версии (_VERSION_VALUE_RE) -- ASCII, поэтому «неразбираемая» здесь
    # значит «без числового префикса», а не «кириллица».
    put_profile("dev-profile")
    put_shipped("dev-shipped")
    assert cookbook.installed_version(ctx, config_dir) == "dev-profile"


def test_parsable_version_beats_unparsable(paths):
    ctx, config_dir, put_profile, put_shipped = paths
    put_profile("dev-profile")
    put_shipped("1.1.149-bbbbbbbb")
    assert cookbook.installed_version(ctx, config_dir) == "1.1.149-bbbbbbbb"


def test_single_copy_and_no_copy(paths):
    ctx, config_dir, put_profile, _put_shipped = paths
    assert cookbook.installed_version(ctx, config_dir) is None
    put_profile("1.1.149-bbbbbbbb")
    assert cookbook.installed_version(ctx, config_dir) == "1.1.149-bbbbbbbb"


def test_document_without_meta_tag_is_not_a_version(paths):
    ctx, config_dir, put_profile, put_shipped = paths
    put_profile("x", raw="<html><head></head><body>no meta</body></html>".encode("utf-8"))
    put_shipped("1.1.149-bbbbbbbb")
    assert cookbook.installed_version(ctx, config_dir) == "1.1.149-bbbbbbbb"


# --- диагностика ----------------------------------------------------------------


def test_installed_copies_lists_both_in_search_order(paths):
    ctx, config_dir, put_profile, put_shipped = paths
    put_profile("1.1.100-aaaaaaaa")
    put_shipped("1.1.149-bbbbbbbb")
    copies = cookbook.installed_copies(ctx, config_dir)
    assert [c["origin"] for c in copies] == ["профиль", "поставка"]
    assert [c["version"] for c in copies] == ["1.1.100-aaaaaaaa", "1.1.149-bbbbbbbb"]


def test_stale_profile_copy_detected(paths):
    ctx, config_dir, put_profile, put_shipped = paths
    put_profile("1.1.100-aaaaaaaa")
    put_shipped("1.1.149-bbbbbbbb")
    stale = cookbook._stale_profile_copy(ctx, config_dir)
    assert stale == {"profile": "1.1.100-aaaaaaaa", "shipped": "1.1.149-bbbbbbbb"}


def test_stale_profile_copy_none_when_profile_is_fresh(paths):
    ctx, config_dir, put_profile, put_shipped = paths
    put_profile("1.1.149-bbbbbbbb")
    put_shipped("1.1.100-aaaaaaaa")
    assert cookbook._stale_profile_copy(ctx, config_dir) is None
