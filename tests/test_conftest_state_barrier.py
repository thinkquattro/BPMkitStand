# -*- coding: utf-8 -*-
"""Мета-тест барьеров состояния машины из `tests/conftest.py` (GAP-414).

ЗАЧЕМ ОТДЕЛЬНЫЙ ФАЙЛ. Барьер — это код, который в нормальной жизни НИЧЕГО не
делает наблюдаемого: он просто уводит пути в песочницу. Снять его (случайно
переписав `conftest.py`, «упростив» лишнюю на вид подмену) можно без единого
красного теста — а обнаружится это только тогда, когда в боевом `hub.log`
владельца снова появятся строки pytest, то есть после следующей порчи. Поэтому
на КАЖДЫЙ барьер — своя проверка, ровно по образцу
`tests/test_conftest_secretstore_barrier.py` dev-репо (класс 58 lessons).

Проверяется не «переменная задана», а КОНЕЧНЫЙ путь, который резолвит рабочий
код: иначе барьер, закрывший переменную, но пропустивший фолбэк резолвера,
считался бы исправным.
"""
from __future__ import annotations

import os
from pathlib import Path

from standkit import registry as standkit_registry
from standkit import secrets as standkit_secrets
from standkit_hub import hub_logging

from tests.conftest import (
    ARMED_STATE_BARRIERS,
    SANDBOX_KEYRING_CLASS,
    SANDBOXED_KEYRING,
    STATE_SANDBOX_DIR,
)


def _inside_sandbox(path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(STATE_SANDBOX_DIR).resolve())
    except ValueError:
        return False
    return True


def test_sandbox_root_is_temporary():
    assert os.path.isdir(STATE_SANDBOX_DIR)
    assert "standkit-tests-state-" in os.path.basename(STATE_SANDBOX_DIR)


def test_hub_log_never_lands_in_real_localappdata():
    """ГЛАВНЫЙ барьер серии: лог диспетчера при ПУСТОМ аргументе.

    Именно этот путь (`setup_logging()` без `log_dir`) и написал строки pytest
    в боевой `hub.log` владельца.
    """
    assert _inside_sandbox(hub_logging.resolve_log_dir()), (
        "каталог лога диспетчера обязан быть в песочнице прогона, а не в профиле машины")
    assert _inside_sandbox(hub_logging.resolve_log_path())


def test_localappdata_and_appdata_are_sandboxed():
    for var in ("LOCALAPPDATA", "APPDATA"):
        assert _inside_sandbox(os.environ[var]), var
        assert os.environ[var] == ARMED_STATE_BARRIERS[var]


def test_home_is_sandboxed():
    """`~/.standkit/run` и `~/.standkit/logs` растут из `Path.home()`."""
    assert _inside_sandbox(Path.home())
    assert _inside_sandbox(Path("~").expanduser())


def test_bpmkit_config_dir_is_sandboxed():
    """`%APPDATA%\\BPMkit` — конфиг диспетчера, состояние канала, кукбук."""
    assert _inside_sandbox(standkit_registry.bpmkit_config_dir())


def test_stands_registry_is_sandboxed():
    """Реестр стендов резолвится в песочницу, а не в боевой `projects.json`."""
    assert _inside_sandbox(standkit_registry.default_registry_path())
    assert _inside_sandbox(os.environ["BPMSOFT_PROJECTS_FILE"])


def test_secretstore_is_a_memory_double():
    """Хранилище секретов подменено — `set_secret` в тесте не трогает
    OS Credential Manager пользователя."""
    if SANDBOXED_KEYRING is None:
        # Пакета `keyring` в окружении нет вовсе — утекать нечему, барьер не нужен.
        assert getattr(standkit_secrets, "keyring", None) is None
        return
    assert type(standkit_secrets.keyring).__name__ == SANDBOX_KEYRING_CLASS
    assert standkit_secrets.keyring is SANDBOXED_KEYRING


def test_license_channel_is_disarmed():
    """Ни настоящего CLI поставки, ни секретов машины в окружении прогона."""
    assert "BPMKIT_CLI" not in os.environ, (
        "найденный CLI живой установки увёл бы тест за боевым конвертом лицензии")
    leaked = [name for name in os.environ if name.startswith("STANDKIT_SECRET__")]
    assert leaked == [], f"секреты машины протекли в прогон: {leaked}"
