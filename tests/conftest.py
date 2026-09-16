# -*- coding: utf-8 -*-
"""
Общие фикстуры для всего набора тестов.

GAP-311 M7: фоновый наблюдатель файла-запроса остановки
(``standkit_hub.server._StopRequestWatcher``) стартует БЕЗУСЛОВНО при
поднятии любого ``HubHTTPServer`` (см. ``HubHTTPServer.__init__``) и
опрашивает ``run_dir`` из конфига. Конфиг БЕЗ явного ``run_dir`` (а такие
есть у многих тестов вне тематики elevation — ``test_hub_fast_paint.py``,
``test_hub_pwa_and_compact.py``, ``test_hub_pick_dialog.py`` и др.,
создающих ``HubConfig(...)`` без ``run_dir``) резолвится в РЕАЛЬНЫЙ домашний
каталог пользователя (``~/.standkit/run``, см. ``HubConfig.resolve_run_dir``).
Тестовый прогон не должен трогать реальный домашний каталог вовсе — жёсткая
изоляция на уровне ``conftest`` для ВСЕГО набора сразу, вместо правки
``run_dir=`` в каждом отдельном файле (легко забыть завести его в НОВОМ
тесте).

Тесты, уже указавшие ``run_dir`` явно в своей ``HubConfig``, не затронуты —
патч действует только когда ``self.run_dir`` пуст.
"""
from __future__ import annotations

import pytest

from standkit_hub.config import HubConfig


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
