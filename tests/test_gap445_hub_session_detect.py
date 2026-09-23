# -*- coding: utf-8 -*-
"""
Тесты GAP-445: точный детект диспетчера в СВОЁМ сеансе Windows.

Решение владельца 21.09.2026: именованный мьютекс диспетчера
(``standkit_hub.mutex.HUB_MUTEX_NAME``) остаётся сеансовым (БЕЗ префикса
``Global\\``) -- сеансовая модель НЕ меняется, честность детекта достигается
ВТОРЫМ, независимым от мьютекса источником: номером сеанса служб терминалов
(WinAPI ``ProcessIdToSessionId``), который диспетчер пишет в свой файл
состояния (``standkit_hub.instance.HubInstanceState.session_id``) рядом с
``pid``. Расхождение «порт 8770 занят, мьютекс СВОЕГО сеанса не виден» --
установщик (``bpmkit_installer.iss``, репозиторий bpmsoft-mcp) читает ЭТОТ
файл и называет пользователю сеанс/учётку, где диспетчер на самом деле
работает, вместо того чтобы молча считать путь свободным (GAP-445, симптом
аудита 16-20.09.2026).

Покрыто здесь (BPMkitStand — сторона диспетчера):
  1. ``standkit.platform.current_session_id`` -- не-Windows даёт честный
     ``None`` дёшево, без попытки грузить ctypes.WinDLL;
  2. ``_configure_session_id_winapi`` -- явные ``argtypes``/``restype`` для
     ``ProcessIdToSessionId`` (тот же класс ошибки x64-обрубания указателя,
     что чинили ``_configure_sid_winapi``/``_configure_process_time_winapi`` в
     GAP-311 Б2/Н1 -- здесь ``PDWORD`` без явного argtypes ctypes передал бы
     32-битным указателем);
  3. ``current_session_id`` -- полный путь на подменённом ``ctypes.WinDLL``
     (успех/отказ WinAPI/исключение) даёт корректный ``int``/``None``;
  4. ``HubInstanceState.session_id`` -- сериализация/десериализация,
     обратная совместимость со старыми файлами состояния (поля нет -> None);
  5. ``instance.current_state`` пишет ``session_id`` через
     ``platform.current_session_id`` для ТЕКУЩЕГО pid;
  6. ``standkit_hub.__main__._cmd_hub_stop`` -- CLI ``--hub-stop`` печатает
     ``HUB_SESSION=<id>`` ПЕРЕД остановкой (или ``HUB_SESSION=unknown``, если
     сеанс не удалось определить), честный отказ, если диспетчера нет.

Запуск: python -m pytest tests/test_gap445_hub_session_detect.py -q
"""
from __future__ import annotations

import ctypes
import types
from ctypes import wintypes

import pytest

from standkit import platform as _platform
from standkit_hub import __main__ as _hub_main
from standkit_hub import instance as _instance


# ======================================================================================
# 1. Не-Windows -- честный None без попытки грузить WinDLL
# ======================================================================================


def test_current_session_id_non_windows_returns_none_without_touching_ctypes(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: called.append(1), raising=False)
    assert _platform.current_session_id() is None
    assert not called


# ======================================================================================
# 2. _configure_session_id_winapi -- явные argtypes/restype
# ======================================================================================


class _FakeFn:
    """Заглушка WinAPI-функции — тот же приём, что в test_platform_sid.py."""

    def __init__(self, impl):
        self._impl = impl
        self.argtypes = None
        self.restype = None

    def __call__(self, *args, **kwargs):
        return self._impl(*args, **kwargs)


def test_configure_session_id_winapi_sets_explicit_argtypes():
    fn = _FakeFn(lambda *a, **kw: 1)
    kernel32 = types.SimpleNamespace(ProcessIdToSessionId=fn)
    _platform._configure_session_id_winapi(kernel32)
    assert kernel32.ProcessIdToSessionId.argtypes == [wintypes.DWORD, wintypes.PDWORD]
    assert kernel32.ProcessIdToSessionId.restype == wintypes.BOOL


# ======================================================================================
# 3. current_session_id -- полный путь на подменённом ctypes.WinDLL
# ======================================================================================


def _make_kernel32(process_id_to_session_id_impl):
    """Собирает объект-заглушку kernel32 с ``ProcessIdToSessionId`` как
    ОТДЕЛЬНЫМ вызываемым объектом (``_FakeFn``, НЕ bound-методом класса) —
    ``_configure_session_id_winapi`` проставляет ``argtypes``/``restype``
    ПРЯМО НА НЁМ (``kernel32.ProcessIdToSessionId.argtypes = ...``), а на
    bound-методе Python атрибут выставить нельзя (упало бы AttributeError,
    молча проглоченным общим ``except Exception`` в ``current_session_id`` —
    ровно тот класс маскировки ошибки, который здесь и проверяется)."""
    fn = _FakeFn(process_id_to_session_id_impl)
    return types.SimpleNamespace(ProcessIdToSessionId=fn)


def _success_impl(session_value):
    def _impl(pid, out_ptr):
        out = ctypes.cast(out_ptr, ctypes.POINTER(wintypes.DWORD))
        out[0] = wintypes.DWORD(session_value)
        return 1  # TRUE

    return _impl


def _failure_impl(pid, out_ptr):
    return 0  # FALSE — WinAPI отказала


def _raises_impl(pid, out_ptr):
    raise OSError("boom")


def test_current_session_id_win32_success(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "win32")
    fake = _make_kernel32(_success_impl(3))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: fake, raising=False)
    assert _platform.current_session_id(pid=1234) == 3


def test_current_session_id_win32_success_session_zero_is_a_real_value(monkeypatch):
    # Сеанс 0 (служба/RDP без интерактивного стола) — РЕАЛЬНЫЙ результат,
    # не должен схлопываться в None.
    monkeypatch.setattr(_platform.sys, "platform", "win32")
    fake = _make_kernel32(_success_impl(0))
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: fake, raising=False)
    assert _platform.current_session_id(pid=1234) == 0


def test_current_session_id_win32_api_failure_returns_none(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "win32")
    fake = _make_kernel32(_failure_impl)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: fake, raising=False)
    assert _platform.current_session_id(pid=1234) is None


def test_current_session_id_win32_exception_is_suppressed(monkeypatch):
    monkeypatch.setattr(_platform.sys, "platform", "win32")
    fake = _make_kernel32(_raises_impl)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: fake, raising=False)
    assert _platform.current_session_id(pid=1234) is None


def test_current_session_id_defaults_to_current_process(monkeypatch):
    import os

    monkeypatch.setattr(_platform.sys, "platform", "win32")
    seen_pid = {}

    def _impl(pid, out_ptr):
        seen_pid["pid"] = pid
        out = ctypes.cast(out_ptr, ctypes.POINTER(wintypes.DWORD))
        out[0] = wintypes.DWORD(5)
        return 1

    fake = _make_kernel32(_impl)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: fake, raising=False)
    assert _platform.current_session_id() == 5
    assert seen_pid["pid"].value == os.getpid()


# ======================================================================================
# 4. HubInstanceState.session_id -- сериализация и обратная совместимость
# ======================================================================================


def test_hub_instance_state_round_trips_session_id():
    state = _instance.HubInstanceState(
        pid=111, host="127.0.0.1", port=8770, elevated=False, session_id=2
    )
    data = state.to_dict()
    assert data["session_id"] == 2
    restored = _instance.HubInstanceState.from_dict(data)
    assert restored.session_id == 2


def test_hub_instance_state_from_dict_missing_session_id_is_none():
    # Старый файл состояния (запись до GAP-445) без этого поля.
    old = {"pid": 111, "host": "127.0.0.1", "port": 8770, "elevated": False}
    restored = _instance.HubInstanceState.from_dict(old)
    assert restored.session_id is None


def test_write_state_persists_session_id(tmp_path):
    path = _instance.state_path(tmp_path)
    state = _instance.HubInstanceState(
        pid=222, host="127.0.0.1", port=8770, elevated=False, session_id=9
    )
    _instance.write_state(path, state)
    restored = _instance.read_state(path, require_alive=False)
    assert restored is not None
    assert restored.session_id == 9


# ======================================================================================
# 5. current_state пишет session_id через platform.current_session_id
# ======================================================================================


def test_current_state_populates_session_id(monkeypatch):
    monkeypatch.setattr(_instance, "current_session_id", lambda pid: 42)
    state = _instance.current_state("127.0.0.1", 8770, elevated=False)
    assert state.session_id == 42


def test_current_state_session_id_none_when_undeterminable(monkeypatch):
    monkeypatch.setattr(_instance, "current_session_id", lambda pid: None)
    state = _instance.current_state("127.0.0.1", 8770, elevated=False)
    assert state.session_id is None


# ======================================================================================
# 6. --hub-stop (_cmd_hub_stop) -- печатает HUB_SESSION=<id> ПЕРЕД остановкой
# ======================================================================================


def test_hub_stop_no_running_instance_prints_honest_refusal(tmp_path, capsys):
    state_file = _instance.state_path(tmp_path)
    rc = _hub_main._cmd_hub_stop(state_file, tmp_path)
    assert rc == 1
    out = capsys.readouterr().out
    assert "HUB_SESSION=" not in out
    assert "не запущен" in out


def test_hub_stop_prints_session_before_stopping(monkeypatch, tmp_path, capsys):
    state = _instance.HubInstanceState(
        pid=333, host="127.0.0.1", port=8770, elevated=False, session_id=6
    )
    monkeypatch.setattr(_instance, "read_state", lambda path: state)
    calls = []

    def _fake_stop(st, *, run_dir):
        calls.append((st, run_dir))
        return True, ""

    monkeypatch.setattr(_instance, "stop_running_instance", _fake_stop)

    rc = _hub_main._cmd_hub_stop(_instance.state_path(tmp_path), tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert lines[0] == "HUB_SESSION=6"
    assert calls == [(state, tmp_path)]


def test_hub_stop_unknown_session_prints_unknown_label(monkeypatch, tmp_path, capsys):
    state = _instance.HubInstanceState(
        pid=333, host="127.0.0.1", port=8770, elevated=False, session_id=None
    )
    monkeypatch.setattr(_instance, "read_state", lambda path: state)
    monkeypatch.setattr(_instance, "stop_running_instance", lambda st, *, run_dir: (True, ""))

    rc = _hub_main._cmd_hub_stop(_instance.state_path(tmp_path), tmp_path)

    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip().splitlines()[0] == "HUB_SESSION=unknown"


def test_hub_stop_reports_failure_to_stop(monkeypatch, tmp_path, capsys):
    state = _instance.HubInstanceState(
        pid=333, host="127.0.0.1", port=8770, elevated=False, session_id=6
    )
    monkeypatch.setattr(_instance, "read_state", lambda path: state)
    monkeypatch.setattr(
        _instance, "stop_running_instance", lambda st, *, run_dir: (False, "не удалось")
    )

    rc = _hub_main._cmd_hub_stop(_instance.state_path(tmp_path), tmp_path)

    assert rc == 1
    out = capsys.readouterr().out
    assert "HUB_SESSION=6" in out
    assert "не удалось" in out
