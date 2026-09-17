# -*- coding: utf-8 -*-
"""
Тесты `standkit_hub.instance` (GAP-311 Б1/M13):
  - атомарность `write_state`/`clear_state` (M13);
  - файл-запрос штатной остановки (`write_stop_request`/`read_stop_request`/
    `discard_stop_request`) и весь путь `stop_running_instance` — штатная
    остановка по запросу, эскалация до `platform.stop(..., tree=False)`,
    защита `_same_process_as_recorded` от убийства переиспользованного pid
    (Б1).

Запуск: python -m pytest tests/test_hub_instance.py -q
"""
from __future__ import annotations

import json
import os

from standkit_hub import instance


# --- M13: write_state атомарен, не оставляет .tmp-мусора ------------------------


def _state(pid=4242, started_at=1000.0):
    return instance.HubInstanceState(
        pid=pid, host="127.0.0.1", port=8770, elevated=False, started_at=started_at
    )


def test_write_state_produces_no_leftover_tmp_files(tmp_path):
    path = instance.state_path(tmp_path)
    instance.write_state(path, _state())

    entries = list(tmp_path.iterdir())
    assert entries == [path]  # никаких *.tmp рядом


def test_write_state_content_is_valid_and_complete(tmp_path):
    path = instance.state_path(tmp_path)
    instance.write_state(path, _state(pid=999))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["pid"] == 999


def test_write_state_uses_random_suffixed_tmp_name_not_predictable(tmp_path, monkeypatch):
    """Предсказуемое имя (``.tmp<pid>``) в общедоступном run_dir — цель для
    подмены (см. В7) — суффикс должен быть случайным (`secrets.token_hex`),
    и два подряд идущих временных файла должны иметь РАЗНЫЕ имена."""
    import re

    seen_tmp_names = []
    orig_open = instance.os.open

    def spy_open(path_str, *args, **kwargs):
        seen_tmp_names.append(path_str)
        return orig_open(path_str, *args, **kwargs)

    monkeypatch.setattr(instance.os, "open", spy_open)
    path = instance.state_path(tmp_path)
    instance.write_state(path, _state())
    instance.write_state(path, _state())

    assert len(seen_tmp_names) == 2
    for tmp_name in seen_tmp_names:
        assert re.search(r"\.[0-9a-f]{16}\.tmp$", tmp_name), tmp_name
    assert seen_tmp_names[0] != seen_tmp_names[1]  # случайный суффикс, не фиксированный по pid


# --- M13: clear_state удаляет только если файл читается как валидный JSON с нашим pid


def test_clear_state_removes_matching_pid(tmp_path):
    path = instance.state_path(tmp_path)
    instance.write_state(path, _state(pid=111))

    instance.clear_state(path, pid=111)

    assert not path.exists()


def test_clear_state_keeps_file_with_different_pid(tmp_path):
    path = instance.state_path(tmp_path)
    instance.write_state(path, _state(pid=111))

    instance.clear_state(path, pid=222)

    assert path.exists()


def test_clear_state_keeps_unreadable_file(tmp_path):
    """M13: файл есть, но не парсится как валидный JSON нашего pid — НЕ
    удаляем (раньше удаляли всё, что не читалось как «чужой pid» явно)."""
    path = instance.state_path(tmp_path)
    path.write_text("не json вовсе", encoding="utf-8")

    instance.clear_state(path, pid=111)

    assert path.exists()


def test_clear_state_without_pid_always_removes(tmp_path):
    path = instance.state_path(tmp_path)
    instance.write_state(path, _state(pid=111))

    instance.clear_state(path)  # pid=None — безусловное удаление (как раньше)

    assert not path.exists()


def test_clear_state_missing_file_does_not_raise(tmp_path):
    path = instance.state_path(tmp_path)
    instance.clear_state(path, pid=111)  # файла нет вовсе — не бросает


# --- Б1: файл-запрос остановки — write/read/discard ------------------------------


def test_stop_request_round_trip(tmp_path):
    path = instance.stop_request_path(tmp_path)
    instance.write_stop_request(path, target_pid=555, requester_pid=777)

    data = instance.read_stop_request(path)

    assert data["target_pid"] == 555
    assert data["requester_pid"] == 777
    assert "at" in data


def test_read_stop_request_missing_file_returns_none(tmp_path):
    path = instance.stop_request_path(tmp_path)
    assert instance.read_stop_request(path) is None


def test_read_stop_request_corrupt_file_returns_none_without_raising(tmp_path):
    path = instance.stop_request_path(tmp_path)
    path.write_text("{не json", encoding="utf-8")
    assert instance.read_stop_request(path) is None


def test_discard_stop_request_missing_file_does_not_raise(tmp_path):
    path = instance.stop_request_path(tmp_path)
    instance.discard_stop_request(path)  # не бросает


# --- Б1: stop_running_instance — штатная остановка по запросу ------------------


def test_stop_running_instance_succeeds_via_stop_request_without_hard_kill(tmp_path, monkeypatch):
    """Работающий хаб реагирует на файл-запрос (его watcher останавливает
    HTTP-сервер сам) — `platform.stop` вовсе не вызывается: дерево процессов
    (живые kestrel-стенды, локальный агент) не трогается."""
    state = _state(pid=4242)
    stop_calls = []
    monkeypatch.setattr(instance, "wait_for_exit", lambda pid, timeout: True)
    monkeypatch.setattr(instance, "is_alive", lambda pid: False)
    monkeypatch.setattr(instance, "stop", lambda *a, **kw: stop_calls.append((a, kw)) or True)

    ok, reason = instance.stop_running_instance(state, run_dir=tmp_path, requester_pid=os.getpid())

    assert ok is True
    assert reason == ""
    assert stop_calls == []  # жёсткое убийство НЕ вызывалось
    assert not instance.stop_request_path(tmp_path).exists()  # файл-запрос убран


def test_stop_running_instance_writes_stop_request_file_with_target_pid(tmp_path, monkeypatch):
    seen = {}
    orig_write = instance.write_stop_request

    def spy_write(path, *, target_pid, requester_pid=None, now=None):
        seen["target_pid"] = target_pid
        seen["requester_pid"] = requester_pid
        return orig_write(path, target_pid=target_pid, requester_pid=requester_pid, now=now)

    monkeypatch.setattr(instance, "write_stop_request", spy_write)
    monkeypatch.setattr(instance, "wait_for_exit", lambda pid, timeout: True)

    state = _state(pid=9999)
    instance.stop_running_instance(state, run_dir=tmp_path, requester_pid=123)

    assert seen["target_pid"] == 9999
    assert seen["requester_pid"] == 123


# --- Б1/Н1: эскалация до platform.stop(..., tree=False), но ТОЛЬКО после
# подтверждения личности процесса заново у ОС (не по файлу состояния) -----


def test_stop_running_instance_escalates_to_tree_false_hard_kill(tmp_path):
    """Процесс не отреагировал на файл-запрос вовремя, но жив, и его личность
    подтверждена НЕЗАВИСИМО от файла состояния (время создания процесса
    совпадает с ``started_at`` в пределах допуска) — эскалация до
    `platform.stop`, ОБЯЗАТЕЛЬНО с `tree=False` (Б1: дерево не трогаем даже
    на этом пути — иначе смысла в файле-запросе не было бы)."""
    state = _state(pid=4321, started_at=5000.0)
    instance.write_state(instance.state_path(tmp_path), state)

    calls = []

    def fake_stop(pid, *, timeout, tree):
        calls.append({"pid": pid, "timeout": timeout, "tree": tree})
        return True

    import unittest.mock as mock
    with mock.patch.object(instance, "wait_for_exit", lambda pid, timeout: False), \
         mock.patch.object(instance, "is_alive", lambda pid: True), \
         mock.patch.object(instance, "process_create_time", lambda pid: 5000.5), \
         mock.patch.object(instance, "stop", fake_stop):
        ok, reason = instance.stop_running_instance(state, run_dir=tmp_path, stop_request_timeout=0.01)

    assert ok is True
    assert reason == ""
    assert len(calls) == 1
    assert calls[0]["pid"] == 4321
    assert calls[0]["tree"] is False


def test_stop_running_instance_refuses_to_kill_when_identity_not_confirmed(tmp_path):
    """Н1 — воспроизведение живого бага ревью: pid жив, файл состояния
    остался НЕТРОНУТЫМ (не переписан и не удалён — иначе сработал бы путь
    M2), но время создания процесса НЕ совпадает с ``started_at``, и
    командная строка/образ не похожи на диспетчер (посторонний процесс,
    ОС отдала ему тот же pid) — жёсткое убийство НЕ вызывается вовсе, и
    возвращается понятная причина отказа (M4)."""
    state = _state(pid=4321, started_at=5000.0)
    instance.write_state(instance.state_path(tmp_path), state)  # файл НЕ трогаем дальше

    calls = []

    import unittest.mock as mock
    with mock.patch.object(instance, "wait_for_exit", lambda pid, timeout: False), \
         mock.patch.object(instance, "is_alive", lambda pid: True), \
         mock.patch.object(instance, "process_create_time", lambda pid: 999999.0), \
         mock.patch.object(instance, "_process_looks_like_hub", lambda pid: False), \
         mock.patch.object(instance, "stop", lambda *a, **kw: calls.append((a, kw)) or True):
        ok, reason = instance.stop_running_instance(state, run_dir=tmp_path, stop_request_timeout=0.01)

    assert calls == []  # НЕ убили чужой процесс
    assert ok is False
    assert "не удалось подтвердить" in reason
    assert "4321" in reason


def test_stop_running_instance_confirms_via_cmdline_when_create_time_unavailable(tmp_path):
    """Время создания процесса недоступно (напр. macOS/чужая учётка), но
    командная строка/образ содержат "standkit_hub"/"bpmkit-hub" — этого
    достаточно для подтверждения (Н1: любая ИЗ ДВУХ проверок, не обе сразу)."""
    state = _state(pid=4321, started_at=5000.0)
    instance.write_state(instance.state_path(tmp_path), state)

    calls = []

    def fake_stop(pid, *, timeout, tree):
        calls.append(pid)
        return True

    import unittest.mock as mock
    with mock.patch.object(instance, "wait_for_exit", lambda pid, timeout: False), \
         mock.patch.object(instance, "is_alive", lambda pid: True), \
         mock.patch.object(instance, "process_create_time", lambda pid: None), \
         mock.patch.object(instance, "_process_looks_like_hub", lambda pid: True), \
         mock.patch.object(instance, "stop", fake_stop):
        ok, reason = instance.stop_running_instance(state, run_dir=tmp_path, stop_request_timeout=0.01)

    assert ok is True
    assert reason == ""
    assert calls == [4321]


def test_stop_running_instance_returns_true_when_process_already_gone_after_timeout(tmp_path):
    """Стоп-запрос не подтверждён (`wait_for_exit` вернул False — таймаут),
    но к моменту проверки процесс уже мёртв сам по себе (штатное завершение
    чуть позже таймаута) — успех, без эскалации до `platform.stop`."""
    state = _state(pid=4321)
    calls = []

    import unittest.mock as mock
    with mock.patch.object(instance, "wait_for_exit", lambda pid, timeout: False), \
         mock.patch.object(instance, "is_alive", lambda pid: False), \
         mock.patch.object(instance, "stop", lambda *a, **kw: calls.append((a, kw)) or True):
        ok, reason = instance.stop_running_instance(state, run_dir=tmp_path, stop_request_timeout=0.01)

    assert ok is True
    assert reason == ""
    assert calls == []


def test_stop_running_instance_returns_false_when_hard_kill_raises(tmp_path):
    state = _state(pid=4321, started_at=5000.0)
    instance.write_state(instance.state_path(tmp_path), state)

    def fake_stop(pid, *, timeout, tree):
        raise instance.ProcessError("не удалось убить")

    import unittest.mock as mock
    with mock.patch.object(instance, "wait_for_exit", lambda pid, timeout: False), \
         mock.patch.object(instance, "is_alive", lambda pid: True), \
         mock.patch.object(instance, "process_create_time", lambda pid: 5000.2), \
         mock.patch.object(instance, "stop", fake_stop):
        ok, reason = instance.stop_running_instance(state, run_dir=tmp_path, stop_request_timeout=0.01)

    assert ok is False
    assert "не удалось убить" in reason


# --- M2: файл состояния уже очищен самим процессом — не провал, ждём дольше ----


def test_stop_running_instance_waits_longer_when_state_already_cleared(tmp_path):
    state = _state(pid=4321)
    # Файл НЕ пишем вовсе (симулирует clear_state, вызванный самим процессом
    # ДО того, как он реально завершился) — stop_request_path тоже не существует.
    wait_calls = []

    def fake_wait_for_exit(pid, timeout):
        wait_calls.append(timeout)
        return len(wait_calls) == 2  # первый (stop_request_timeout) — неудача, второй (hard_timeout) — успех

    import unittest.mock as mock
    with mock.patch.object(instance, "wait_for_exit", fake_wait_for_exit), \
         mock.patch.object(instance, "is_alive", lambda pid: True):
        ok, reason = instance.stop_running_instance(
            state, run_dir=tmp_path, stop_request_timeout=0.01, hard_timeout=0.01
        )

    assert ok is True
    assert reason == ""
    assert wait_calls == [0.01, 0.01]


def test_stop_running_instance_fails_when_state_cleared_but_process_never_exits(tmp_path):
    state = _state(pid=4321)

    import unittest.mock as mock
    with mock.patch.object(instance, "wait_for_exit", lambda pid, timeout: False), \
         mock.patch.object(instance, "is_alive", lambda pid: True):
        ok, reason = instance.stop_running_instance(
            state, run_dir=tmp_path, stop_request_timeout=0.01, hard_timeout=0.01
        )

    assert ok is False
    assert "не завершился" in reason


# --- Н1: _same_process_as_recorded — подтверждение НЕЗАВИСИМО от файла состояния ---


def test_same_process_as_recorded_confirms_via_create_time_within_tolerance(monkeypatch):
    state = _state(pid=1, started_at=1000.0)
    monkeypatch.setattr(instance, "is_alive", lambda pid: True)
    monkeypatch.setattr(instance, "process_create_time", lambda pid: 1000.0 + instance._PROCESS_IDENTITY_TOLERANCE_SEC - 0.1)

    confirmed, reason = instance._same_process_as_recorded(state)

    assert confirmed is True
    assert reason == ""


def test_same_process_as_recorded_rejects_when_create_time_differs_beyond_tolerance(monkeypatch):
    state = _state(pid=1, started_at=1000.0)
    monkeypatch.setattr(instance, "is_alive", lambda pid: True)
    monkeypatch.setattr(instance, "process_create_time", lambda pid: 999999.0)
    monkeypatch.setattr(instance, "_process_looks_like_hub", lambda pid: False)

    confirmed, reason = instance._same_process_as_recorded(state)

    assert confirmed is False
    assert "не удалось подтвердить" in reason


def test_same_process_as_recorded_confirms_via_cmdline_marker(monkeypatch):
    state = _state(pid=1, started_at=1000.0)
    monkeypatch.setattr(instance, "is_alive", lambda pid: True)
    monkeypatch.setattr(instance, "process_create_time", lambda pid: None)
    monkeypatch.setattr(instance, "_process_looks_like_hub", lambda pid: True)

    confirmed, reason = instance._same_process_as_recorded(state)

    assert confirmed is True


def test_same_process_as_recorded_false_when_process_already_dead(monkeypatch):
    state = _state(pid=1, started_at=1000.0)
    monkeypatch.setattr(instance, "is_alive", lambda pid: False)

    confirmed, reason = instance._same_process_as_recorded(state)

    assert confirmed is False
    assert "уже не выполняется" in reason


# --- Н1: интеграционный тест против РЕАЛЬНОГО постороннего процесса -----------


def test_stop_running_instance_does_not_kill_a_real_unrelated_process(tmp_path):
    """Прямое воспроизведение сценария из ревью: посторонний процесс (`sleep`)
    получает pid, файл состояния (подложенный, как будто оставшийся от давно
    завершившегося хаба) указывает НА ЭТОТ ЖЕ pid, но с несовпадающим
    `started_at` и `process_create_time` — процесс НЕ убит."""
    import subprocess
    import sys as _sys
    import time as _time

    # Раньше здесь стоял ["sleep", "60"] — на Windows такой команды нет вовсе,
    # и тест падал FileNotFoundError [WinError 2], не проверив НИЧЕГО (живьём
    # 17.09.2026, хост издателя). Берём заведомо присутствующий посторонний
    # процесс — тот же интерпретатор, которым запущен сам прогон.
    proc = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        state = instance.HubInstanceState(
            pid=proc.pid,
            host="127.0.0.1",
            port=8770,
            elevated=False,
            started_at=1.0,  # заведомо не время создания sleep-процесса
            process_create_time=1.0,
        )
        instance.write_state(instance.state_path(tmp_path), state)

        ok, reason = instance.stop_running_instance(
            state, run_dir=tmp_path, stop_request_timeout=0.05, hard_timeout=0.05
        )

        assert ok is False
        assert "не удалось подтвердить" in reason
        _time.sleep(0.05)
        assert proc.poll() is None  # процесс жив — НЕ убит
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_create_time_mismatch_is_not_overridden_by_cmdline_marker(monkeypatch):
    """Повторное ревью GAP-311, М-А: известное и несовпавшее время создания —
    отказ, даже если в командной строке есть маркер хаба."""
    from standkit_hub import instance as inst

    state = inst.HubInstanceState(pid=4242, host="127.0.0.1", port=8770, started_at=1000.0)
    state.process_create_time = 1000.0
    monkeypatch.setattr(inst, "is_alive", lambda pid: True)
    monkeypatch.setattr(inst, "process_create_time", lambda pid: 5000.0)
    monkeypatch.setattr(inst, "_process_looks_like_hub", lambda pid: True)
    ok, reason = inst._same_process_as_recorded(state)
    assert ok is False
    assert "время создания" in reason


def test_cmdline_marker_used_when_create_time_unknown(monkeypatch):
    from standkit_hub import instance as inst

    state = inst.HubInstanceState(pid=4242, host="127.0.0.1", port=8770, started_at=1000.0)
    monkeypatch.setattr(inst, "is_alive", lambda pid: True)
    monkeypatch.setattr(inst, "process_create_time", lambda pid: None)
    monkeypatch.setattr(inst, "_process_looks_like_hub", lambda pid: True)
    assert inst._same_process_as_recorded(state) == (True, "")


def test_console_script_names_are_hub_markers():
    from standkit_hub import instance as inst

    for name in ("standkit-hub", "standkit-gui", "standkit_hub", "bpmkit-hub"):
        assert name in inst._HUB_PROCESS_IDENTITY_MARKERS
