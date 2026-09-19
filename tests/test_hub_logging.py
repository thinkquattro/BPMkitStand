# -*- coding: utf-8 -*-
"""
GAP-384 — причины завершения диспетчера уходят В ФАЙЛ, а не в stderr.

ЗАЧЕМ ЭТОТ НАБОР. Инцидент 18.09.2026 20:56: хаб вышел сам, три стенда
осиротели, владелец увидел на экране только ошибку связи. Разбирать было
НЕЧЕГО: диспетчер живёт под ``pythonw.exe``, у которого stderr не подключён
никуда, а все объяснения («простой N минут», «стоп-запрос», «перехват») были
написаны через ``print(..., file=sys.stderr)``. Причина самого громкого отказа
продукта существовала ровно на время своего вывода.

Поэтому здесь проверяется не «логгер настроился», а три вещи, без которых
разбор следующего такого инцидента опять упрётся в пустоту:

* путь лога определяется БЕЗ переменных окружения тоже (дефолт обязан быть, а
  не «не смогли — молчим»);
* решение об автовыходе записывает ФАКТЫ, по которым его приняли
  (``sse_client_count``, ``has_running_stands``, ошибка снапшота), а не только
  «выхожу»;
* настройка логирования не имеет права уронить старт хаба — диспетчер без лога
  хуже диспетчера с логом, но НЕСРАВНИМО лучше не запустившегося.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import pytest

from standkit_hub import hub_logging
from standkit_hub.server import _IdleShutdownWatcher


# --------------------------------------------------------------------------
# Резолв каталога лога
# --------------------------------------------------------------------------


def test_resolve_log_dir_prefers_localappdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    resolved = hub_logging.resolve_log_dir()
    assert resolved == tmp_path / "Local" / "BPMkit" / "logs"


def test_resolve_log_dir_has_a_default_without_env(tmp_path, monkeypatch):
    # Ни одной подсказки из окружения — путь всё равно обязан быть: «не смогли
    # определить каталог» здесь означает «инцидент снова разбирать нечем».
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    resolved = hub_logging.resolve_log_dir()
    assert resolved.is_absolute()
    assert "BPMkit" in resolved.parts
    assert resolved.parts[-1] == "logs"


def test_explicit_dir_wins_over_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert hub_logging.resolve_log_dir(tmp_path / "custom") == tmp_path / "custom"


def test_log_path_is_hub_log_inside_resolved_dir(tmp_path):
    assert hub_logging.resolve_log_path(tmp_path) == tmp_path / "hub.log"


# --------------------------------------------------------------------------
# Настройка логирования
# --------------------------------------------------------------------------


def test_setup_creates_rotating_file_and_writes(tmp_path):
    path = hub_logging.setup_logging(log_dir=tmp_path, force=True)
    try:
        assert path == tmp_path / "hub.log"
        hub_logging.logger().warning("проверочная строка")
        for handler in hub_logging.logger().handlers:
            handler.flush()
        assert path.exists()
        assert "проверочная строка" in path.read_text(encoding="utf-8")
    finally:
        hub_logging.reset_logging()


def test_setup_is_rotating_and_bounded(tmp_path):
    hub_logging.setup_logging(log_dir=tmp_path, force=True)
    try:
        handlers = [h for h in hub_logging.logger().handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
        assert handlers, "лог обязан быть ротируемым: забытый процесс пишет месяцами"
        assert handlers[0].maxBytes > 0
        assert handlers[0].backupCount > 0
    finally:
        hub_logging.reset_logging()


def test_setup_never_breaks_startup_when_dir_is_unusable(tmp_path):
    # Каталог занят ФАЙЛОМ с тем же именем — mkdir обязан провалиться.
    busy = tmp_path / "logs"
    busy.write_text("не каталог", encoding="utf-8")
    try:
        assert hub_logging.setup_logging(log_dir=busy, force=True) is None
        # И логгер всё равно рабочий: вызовы ниже по коду не имеют права падать.
        hub_logging.logger().warning("после неудачной настройки")
    finally:
        hub_logging.reset_logging()


# --------------------------------------------------------------------------
# Автовыход объясняет СЕБЯ
# --------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Snapshot:
    def __init__(self, stands=None, *, probed=True, error=None) -> None:
        self.stands = stands or []
        self.probed = probed
        self.error = error


class _Poller:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


class _Server:
    def __init__(self, snapshot) -> None:
        self.status_poller = _Poller(snapshot)
        self.shutdown_calls = 0

    def sse_client_count(self) -> int:
        return 0

    def request_self_shutdown(self) -> bool:
        self.shutdown_calls += 1
        return True


def _config(tmp_path, minutes: int = 1) -> Path:
    from standkit_hub.config import HubConfig
    from standkit_hub import server as hub_server

    path = tmp_path / "standkit-hub.json"
    HubConfig(run_dir=str(tmp_path / "run"), idle_shutdown_min=minutes).save(path)
    hub_server.invalidate_caches()
    return path


def test_idle_shutdown_records_the_facts_it_decided_on(tmp_path, caplog):
    config_path = _config(tmp_path)
    server = _Server(_Snapshot(stands=[], probed=True))
    clock = _Clock()
    watcher = _IdleShutdownWatcher(server, config_path, now=clock)

    with caplog.at_level(logging.INFO, logger=hub_logging.LOGGER_NAME):
        watcher._tick()
        clock.advance(60 * 60)
        watcher._tick()

    assert server.shutdown_calls == 1
    text = "\n".join(record.getMessage() for record in caplog.records)
    # Именно ФАКТЫ, а не «выхожу»: разбор инцидента начинается с вопроса
    # «а сколько тогда было клиентов и что показывал снапшот».
    assert "sse_client_count=0" in text
    assert "has_running_stands=False" in text
    assert "snapshot_error=" in text


def test_poller_snapshot_error_is_logged(caplog):
    from standkit_hub.poller import StatusPoller

    def _boom():
        raise RuntimeError("реестр недоступен")

    poller = StatusPoller(build=_boom, interval=lambda: 2.0)
    with caplog.at_level(logging.WARNING, logger=hub_logging.LOGGER_NAME):
        snapshot = poller._safe_build()

    assert snapshot.error and "реестр недоступен" in snapshot.error
    assert snapshot.probed is False
    # Молчаливый снапшот-отказ — ровно та слепота, из-за которой хаб вышел при
    # живых стендах: ошибка обязана оставить след.
    assert any("реестр недоступен" in r.getMessage() for r in caplog.records)
