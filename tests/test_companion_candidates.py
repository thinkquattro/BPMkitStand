"""
Тесты обратного прохода канала: `standkit_companion.candidates` (GAP-260,
GAP-248 п.2).

Свойства, ради которых модуль написан именно так (и которые здесь проверяются):

1. **сеть отсюда не трогается вовсе** — Companion просит поставку
   (`bpmkit setup outbox-flush`) разгрузить очередь, которой она владеет, а не
   читает `~/.bpmkit/outbox/` своими руками: вторая копия логики очереди в
   другом репозитории разъехалась бы молча (см. докстринг модуля);
2. **офлайн/квота/нет лицензии — НЕ ошибка по построению** (дословное
   требование строки GAP-260): системный отказ даёт `skipped`, а не `error`;
3. **отправленное до остановки — отправлено**: частичный успех не теряется в
   отчёте;
4. **попутчик не роняет несущую операцию** — ни один исход не поднимает
   исключение наружу.

Подпроцесс не запускается: `run` подменяется двойником с той же сигнатурой,
что у `candidates._default_run`.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

import pytest

from standkit_companion import candidates
from standkit_companion.state import CompanionState


@dataclass
class _Settings:
    """Минимум, который читает `context.find_cli`."""

    mcp_cli: str = ""


def _state(tmp_path) -> CompanionState:
    return CompanionState(tmp_path / "companion-state.json")


def _runner(payload=None, *, rc: int = 0, stdout: str = None, stderr: str = "",
            raises: BaseException = None, seen: list = None):
    """Двойник запуска поставки: возвращает готовый JSON-ответ подкоманды."""

    def _run(argv):
        if seen is not None:
            seen.append(list(argv))
        if raises is not None:
            raise raises
        out = stdout if stdout is not None else json.dumps(payload or {}, ensure_ascii=False)
        return rc, out, stderr

    return _run


@pytest.fixture()
def cli(monkeypatch):
    """CLI поставки «найден» — резолв пути проверяется отдельно (GAP-273)."""
    monkeypatch.setattr(candidates, "find_cli", lambda settings: ["bpmkit.exe"])


# --- штатный проход ---------------------------------------------------------


def test_flush_reports_sent_and_marks_ok(tmp_path, cli):
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner({"ok": True, "sent": 3, "failed": 0, "remaining": 0,
                     "stopped_reason": None}),
    )

    assert result["flushed"] is True
    assert result["sent"] == 3
    assert state.candidates["last_status"] == "ok"
    assert "3" in state.candidates["last_detail"]


def test_empty_queue_is_ok_not_error(tmp_path, cli):
    """Пустая очередь — норма и самый частый исход, а не повод для жёлтого."""
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner({"ok": True, "sent": 0, "failed": 0, "remaining": 0}),
    )

    assert result["reason"] == "flushed"
    assert result["flushed"] is False
    assert state.candidates["last_status"] == "ok"
    assert "пуста" in state.candidates["last_detail"]


def test_flush_passes_limit_and_json_flag(tmp_path, cli):
    """Проход ограничен: фоновый тик не превращается в выгрузку за месяц."""
    seen = []
    candidates.flush(state=_state(tmp_path), settings=_Settings(),
                     run=_runner({"ok": True, "sent": 0}, seen=seen), limit=25)

    argv = seen[0]
    assert argv[:1] == ["bpmkit.exe"]
    assert "setup" in argv and "outbox-flush" in argv
    assert "--json" in argv
    assert argv[argv.index("--limit") + 1] == "25"


# --- офлайн и прочие системные отказы: НЕ ошибка ----------------------------


@pytest.mark.parametrize("reason", sorted(candidates.SYSTEM_STOP_REASONS))
def test_system_stop_reasons_are_skipped_not_error(tmp_path, cli, reason):
    """
    Дословное требование строки GAP-260: «офлайн/квота/нет лицензии не должны
    быть ошибкой по построению». Красный цикл, который чинится сам при
    следующем тике, обесценивает индикацию.
    """
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner({"ok": True, "sent": 0, "failed": 0, "remaining": 4,
                     "stopped_reason": reason}),
    )

    assert result["reason"] == reason
    assert state.candidates["last_status"] == "skipped"
    assert state.candidates["last_status"] != "error"


def test_partial_success_before_offline_is_reported(tmp_path, cli):
    """Успевшее уехать до обрыва — уехало, и отчёт обязан это сказать."""
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner({"ok": True, "sent": 2, "failed": 0, "remaining": 5,
                     "stopped_reason": "offline"}),
    )

    assert result["sent"] == 2
    assert result["flushed"] is True
    assert state.candidates["last_status"] == "skipped"
    assert "2" in state.candidates["last_detail"]


def test_unknown_stop_reason_is_error(tmp_path, cli):
    """Незнакомая причина не попадает в «норму» по умолчанию — только известные."""
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner({"ok": True, "sent": 0, "stopped_reason": "внезапно"}),
    )

    assert result["reason"] == "внезапно"
    assert state.candidates["last_status"] == "error"


# --- отказы окружения -------------------------------------------------------


def test_missing_cli_is_skipped(tmp_path, monkeypatch):
    """Нет CLI поставки — чинит человек в настройках, канал не «сломан» (GAP-273)."""
    monkeypatch.setattr(candidates, "find_cli", lambda settings: None)
    state = _state(tmp_path)
    result = candidates.flush(state, _Settings(), run=_runner({"ok": True}))

    assert result["reason"] == "cli_not_found"
    assert state.candidates["last_status"] == "skipped"


def test_timeout_is_error_but_does_not_raise(tmp_path, cli):
    """
    Таймаут доезжает как rc=-1 (так его отдаёт `_default_run`: единая точка
    `standkit.platform.run_console` превращает любое исключение запуска в
    один вид отказа — см. GAP-138, прямой subprocess.run в пакете запрещён).
    """
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner(rc=-1, stdout="", stderr="timed out"),
    )

    assert result["reason"] == "cli_error"
    assert state.candidates["last_status"] == "error"


def test_runner_exception_does_not_escape(tmp_path, cli):
    """Попутчик обязан пережить и неожиданное исключение запуска."""
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner(raises=subprocess.TimeoutExpired(cmd="bpmkit", timeout=1)),
    )

    assert result["reason"] == "spawn_error"
    assert state.candidates["last_status"] == "error"


def test_default_run_goes_through_run_console(monkeypatch):
    """
    GAP-138: фоновый тик не имеет права мигать консольным окном. Проверяем
    ФАКТ похода через единую точку, а не наличие флага, — флаг ставит сама
    `run_console`, и её собственный тест это уже стережёт.
    """
    seen = {}

    def _fake_run_console(cmd, **kwargs):
        seen["cmd"] = list(cmd)

        class _P:
            returncode = 0
            stdout = '{"ok": true}'
            stderr = ""

        return _P()

    monkeypatch.setattr(candidates, "run_console", _fake_run_console)
    rc, out, _err = candidates._default_run(["bpmkit.exe", "setup", "outbox-flush"])

    assert rc == 0
    assert seen["cmd"][-1] == "outbox-flush"


def test_nonzero_rc_is_error_with_detail(tmp_path, cli):
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner(rc=2, stdout="", stderr="поставка сломана"),
    )

    assert result["reason"] == "cli_error"
    assert "поставка сломана" in state.candidates["last_detail"]


def test_refusal_payload_is_error(tmp_path, cli):
    """`ok: false` от подкоманды (например, модуль feedback недоступен)."""
    state = _state(tmp_path)
    result = candidates.flush(
        state, _Settings(),
        run=_runner({"ok": False, "reason": "unavailable", "detail": "нет модуля"}),
    )

    assert result["reason"] == "unavailable"
    assert state.candidates["last_status"] == "error"


def test_json_is_read_from_last_line(tmp_path, cli):
    """Предупреждение поставки перед ответом не ломает разбор."""
    noisy = "WARNING: корень пакета не резолвится\n" + json.dumps({"ok": True, "sent": 1})
    state = _state(tmp_path)
    result = candidates.flush(state, _Settings(), run=_runner(stdout=noisy))

    assert result["sent"] == 1
    assert state.candidates["last_status"] == "ok"
