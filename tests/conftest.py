# -*- coding: utf-8 -*-
r"""
Общие фикстуры и БАРЬЕРЫ СОСТОЯНИЯ МАШИНЫ для всего набора тестов.

# Барьеры (GAP-414, класс 58 `docs/lessons.md` dev-репо)

**Что случилось.** В боевом `%LOCALAPPDATA%\BPMkit\logs\hub.log` владельца
нашлись строки, написанные ПРОГОНОМ ТЕСТОВ (по путям вида
`pytest-of-Admin\pytest-NN\...` внутри самих записей). Механизм ровно тот же,
что у `projects.json`, `secretstore` и файла лицензии в dev-репо: тест,
поднимающий диспетчер, зовёт `hub_logging.setup_logging()` БЕЗ явного
каталога, а тот честно резолвит `%LOCALAPPDATA%` — то есть боевой лог живой
установки. Дальше это не только грязь в чужом файле: прогон РОТИРУЕТ его,
унося настоящие записи о работе диспетчера в `.1`…`.5` и за их край.

**Правило (класс 58).** Каждый канал состояния машины закрывается барьером в
`conftest`, а не дисциплиной автора будущего теста. Автор теста «про
пик-диалог» не обязан знать, что где-то под ним резолвится `%LOCALAPPDATA%`.
Признак, что барьера не хватает: изоляция канала скопирована в `setUp`/
фикстуры НЕСКОЛЬКИХ файлов вместо ОДНОГО места здесь.

**Что закрыто ниже** (всё — НА УРОВНЕ ИМПОРТА модуля, до сбора тестов, иначе
модуль теста успеет зарезолвить путь в своём теле):

* `LOCALAPPDATA` — лог диспетчера (`hub_logging.resolve_log_dir`), ярлык
  (`shortcut.py`), каталог операций с повышением (`elevation.py`);
* `APPDATA` / `XDG_CONFIG_HOME` — `%APPDATA%\BPMkit`: конфиг диспетчера
  `standkit-hub.json`, состояние канала `companion-state.json`, кукбук
  `docs\cookbook.html`, база паттернов;
* `HOME` / `USERPROFILE` / `XDG_STATE_HOME` / `XDG_DATA_HOME` /
  `XDG_CACHE_HOME` — `Path.home()`, из которого растут `~/.standkit/run`
  (pid-файлы стендов) и `~/.standkit/logs`;
* РЕЕСТР СТЕНДОВ — `BPMSOFT_PROJECTS_FILE` уводится в песочницу: без этого
  `standkit.registry.default_registry_path()` находит боевой `projects.json`
  живой установки (и через `%APPDATA%`, и через переменную окружения машины),
  а тесты, пишущие реестр, правили бы его;
* ХРАНИЛИЩЕ СЕКРЕТОВ — `standkit.secrets` уводится на двойник в памяти
  процесса: настоящий `keyring` — это OS Credential Manager ПОЛЬЗОВАТЕЛЯ, и
  `set_secret`/`delete_secret` в тесте трогали бы его записи;
* ЛИЦЕНЗИЯ — `BPMKIT_CLI` снимается, а `STANDKIT_SECRET__*` вычищаются:
  иначе `standkit_companion.context.find_cli` находит НАСТОЯЩИЙ CLI живой
  установки, запускает его и приносит в тест боевой конверт лицензии
  (и заодно уводит прогон в сеть к бэкенду издателя).

Барьер НИЧЕГО не восстанавливает обратно в конце прогона: процесс pytest
завершается вместе с подменой, а «вернуть как было» в середине прогона
означало бы окно, в котором следующий тест снова пишет в боевой профиль.

Мета-тест барьера — `tests/test_conftest_state_barrier.py`; без него снятие
барьера в будущей правке осталось бы незамеченным ровно до следующего
испорченного `hub.log`.

# Фикстура run_dir (GAP-311 M7)

Фоновый наблюдатель файла-запроса остановки
(``standkit_hub.server._StopRequestWatcher``) стартует БЕЗУСЛОВНО при
поднятии любого ``HubHTTPServer`` и опрашивает ``run_dir`` из конфига.
Конфиг БЕЗ явного ``run_dir`` резолвится в ``~/.standkit/run``. Барьер
``HOME`` выше уже уводит и это, но фикстура остаётся: она даёт КАЖДОМУ тесту
СВОЙ каталог, а не общий на прогон.
"""
from __future__ import annotations

import os
import tempfile

import pytest

# ---------------------------------------------------------------------------
# Барьеры состояния машины — НА УРОВНЕ ИМПОРТА (см. докстринг модуля)
# ---------------------------------------------------------------------------
STATE_SANDBOX_DIR = tempfile.mkdtemp(prefix="standkit-tests-state-")

#: Имя класса-двойника хранилища секретов проверяет мета-тест барьера — по нему
#: видно, что подмена на месте (тот же приём, что у `_MemoryKeyring` dev-репо).
SANDBOX_KEYRING_CLASS = "_MemoryKeyring"


class _MemoryKeyring(object):
    """Хранилище секретов в памяти процесса с интерфейсом `keyring`, который
    использует `standkit.secrets` (`get_password`/`set_password`/`delete_password`)."""

    def __init__(self):
        self._data = {}

    def get_password(self, service, ref):
        return self._data.get((service, ref))

    def set_password(self, service, ref, value):
        self._data[(service, ref)] = value

    def delete_password(self, service, ref):
        if (service, ref) not in self._data:
            raise KeyError(ref)
        del self._data[(service, ref)]


def _sandbox_subdir(name):
    path = os.path.join(STATE_SANDBOX_DIR, name)
    os.makedirs(path, exist_ok=True)
    return path


def _arm_state_barriers():
    """Уводит все известные каналы состояния машины в песочницу. Возвращает
    словарь того, что подменено, — его читает мета-тест барьера."""
    armed = {}

    for var, sub in (("LOCALAPPDATA", "LocalAppData"),
                     ("APPDATA", "AppData"),
                     ("XDG_CONFIG_HOME", "xdg-config"),
                     ("XDG_STATE_HOME", "xdg-state"),
                     ("XDG_DATA_HOME", "xdg-data"),
                     ("XDG_CACHE_HOME", "xdg-cache")):
        os.environ[var] = _sandbox_subdir(sub)
        armed[var] = os.environ[var]

    # `Path.home()` на Windows читает USERPROFILE, на POSIX — HOME; подменяем оба,
    # чтобы барьер не зависел от того, где идёт прогон.
    home = _sandbox_subdir("home")
    for var in ("HOME", "USERPROFILE"):
        os.environ[var] = home
        armed[var] = home

    # Реестр стендов: путь в песочнице и заведомо НЕ существующий — резолвер
    # обязан не найти боевой реестр ни по переменной, ни по %APPDATA%.
    registry = os.path.join(_sandbox_subdir("BPMkit"), "projects.json")
    os.environ["BPMSOFT_PROJECTS_FILE"] = registry
    armed["BPMSOFT_PROJECTS_FILE"] = registry

    # Лицензия: никакого настоящего CLI поставки и никаких секретов машины.
    os.environ.pop("BPMKIT_CLI", None)
    for name in [k for k in os.environ if k.startswith("STANDKIT_SECRET__")]:
        os.environ.pop(name, None)

    return armed


ARMED_STATE_BARRIERS = _arm_state_barriers()


def _sandbox_secretstore():
    """Уводит `standkit.secrets` на двойник в памяти. Возвращает объект-хранилище
    либо None (модуль не импортируется в этом окружении — изолировать нечего)."""
    try:
        from standkit import secrets as _secrets
    except Exception:  # noqa: BLE001 - барьер не имеет права ронять сбор тестов
        return None
    memory = _MemoryKeyring()
    _secrets.keyring = memory
    return memory


SANDBOXED_KEYRING = _sandbox_secretstore()


# ---------------------------------------------------------------------------
# Фикстуры
# ---------------------------------------------------------------------------
from standkit_hub.config import HubConfig  # noqa: E402 - после барьеров, сознательно


@pytest.fixture(autouse=True)
def _hub_run_dir_defaults_to_tmp(tmp_path_factory, monkeypatch):
    fallback_run_dir = tmp_path_factory.mktemp("standkit-hub-run-default")
    original = HubConfig.resolve_run_dir

    def _patched(self):
        if self.run_dir:
            return original(self)
        return fallback_run_dir

    monkeypatch.setattr(HubConfig, "resolve_run_dir", _patched)
    yield
