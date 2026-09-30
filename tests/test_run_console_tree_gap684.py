# -*- coding: utf-8 -*-
"""GAP-684: запуск CLI лицензии не оставляет процессов и не наследует stdin.

Баг Курова 30.09.2026: после зависшей записи ключа в диспетчере остались процессы.
`subprocess.run(timeout=...)` убивает только прямого потомка, а у сборки CLI это загрузчик:
рабочий процесс -- его ребёнок. `run_console_tree` по таймауту убивает ВСЁ дерево.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from standkit import platform as platform_module


class _FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = b""


def test_run_console_defaults_stdin_to_devnull(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: captured.update(kw) or _FakeCompleted())
    platform_module.run_console(["x"])
    assert captured["stdin"] is subprocess.DEVNULL


def test_run_console_keeps_explicit_stdin_and_input(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: captured.update(kw) or _FakeCompleted())
    platform_module.run_console(["x"], input="data")
    assert "stdin" not in captured and captured["input"] == "data"
    captured.clear()
    platform_module.run_console(["x"], stdin=subprocess.PIPE)
    assert captured["stdin"] is subprocess.PIPE


def test_run_console_tree_returns_output_like_subprocess_run():
    res = platform_module.run_console_tree(
        [sys.executable, "-c", "print('ok')"], timeout=30,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert res.returncode == 0
    assert res.stdout.strip() == "ok"


def test_run_console_tree_does_not_inherit_stdin():
    res = platform_module.run_console_tree(
        [sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=30,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert res.stdout.strip() == "''"


def test_run_console_tree_kills_whole_tree_on_timeout(tmp_path):
    """Потомок родителя (внук теста) не должен пережить таймаут."""
    pid_file = tmp_path / "child.pid"
    child = ("import os,time,pathlib;"
             f"pathlib.Path(r'{pid_file}').write_text(str(os.getpid()));time.sleep(120)")
    parent = ("import subprocess,sys,time;"
              f"subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(120)")
    started = time.time()
    with pytest.raises(subprocess.TimeoutExpired):
        platform_module.run_console_tree([sys.executable, "-c", parent], timeout=3,
                                         capture_output=True)
    assert time.time() - started < 30
    deadline = time.time() + 10
    while not pid_file.exists() and time.time() < deadline:
        time.sleep(0.2)
    assert pid_file.exists(), "внук не успел стартовать -- тест невалиден"
    child_pid = int(pid_file.read_text())
    deadline = time.time() + 10
    alive = True
    while time.time() < deadline:
        alive = platform_module.is_alive(child_pid)
        if not alive:
            break
        time.sleep(0.3)
    assert not alive, "потомок пережил таймаут -- дерево не убито"
