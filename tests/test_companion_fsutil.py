# -*- coding: utf-8 -*-
"""Тесты `standkit_companion.fsutil.probe_writable` (GAP-161).

`probe_writable` — неразрушающая проба «можно ли сейчас подменить этот файл», которую
`releases.apply_staged` зовёт ДО бэкапа. Здесь проверяется контракт функции саму по себе
(файл существует и доступен на запись — тихо; файла нет — тихо; ловит и пробрасывает
`OSError`, ничего не подавляя сама), БЕЗ попытки воспроизвести настоящую блокировку файла
средствами ОС — такая блокировка платформенно-специфична (на Windows это обычно закрытая
доля `FILE_SHARE_WRITE` у процесса, держащего образ `.exe`; на POSIX обычный `open()`
второй раз чаще всего просто не блокируется), и её симуляция средствами `unittest`
неизбежно оказалась бы либо флаки, либо ложью про реальный сценарий. Сценарий «файл занят»
для `apply_staged` целиком покрыт интеграционными тестами
`tests/test_companion_releases.py` через monkeypatch самой функции — как и `probe_writable`
не должна ничего решать за вызывающего (никакой классификации `kind` внутри неё нет),
здесь и не тестируется ничего похожего на классификацию.
"""
from __future__ import annotations

import os

import pytest

from standkit_companion import fsutil


def test_writable_file_passes_silently_without_mutating_content(tmp_path):
    target = tmp_path / "bpmkit.exe"
    original = b"soderzhimoe binarya, ne trogat"
    target.write_bytes(original)

    fsutil.probe_writable(target)  # не бросает

    assert target.read_bytes() == original, (
        "проба обязана быть неразрушающей: 'r+b' без truncate, ничего не дописывается")


def test_missing_file_is_a_silent_no_op(tmp_path):
    """Файла ещё нет (первая установка без предыдущей версии) — это отдельный штатный
    случай, решает его вызывающий (`apply_staged`/`backup_copy`), не проба."""
    missing = tmp_path / "no-such-binary.exe"
    assert not missing.exists()

    fsutil.probe_writable(missing)  # не бросает


def test_directory_in_place_of_file_is_a_silent_no_op(tmp_path):
    """`is_file()` отсеивает каталог так же, как отсутствующий файл — проба не пытается
    его открыть и не падает с посторонним `IsADirectoryError`."""
    as_dir = tmp_path / "bpmkit.exe"
    as_dir.mkdir()

    fsutil.probe_writable(as_dir)  # не бросает


def test_oserror_from_open_propagates_unmodified(tmp_path, monkeypatch):
    """Функция не классифицирует и не подавляет ошибку сама — это дело вызывающего
    (`releases.apply_staged` превращает её в `ChannelError(kind='local_io')`)."""
    target = tmp_path / "bpmkit.exe"
    target.write_bytes(b"x")

    def raising_open(*args, **kwargs):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr("builtins.open", raising_open)

    with pytest.raises(PermissionError):
        fsutil.probe_writable(target)


def test_does_not_truncate_even_if_left_open_would_allow_writes(tmp_path):
    """Регресс-гард на будущее: если кто-то поменяет режим открытия на 'w+b' по ошибке,
    этот тест обязан покраснеть — 'w+b' немедленно обнуляет файл при открытии."""
    target = tmp_path / "bpmkit.exe"
    payload = os.urandom(4096)
    target.write_bytes(payload)

    fsutil.probe_writable(target)

    assert target.read_bytes() == payload
