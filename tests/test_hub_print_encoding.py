# -*- coding: utf-8 -*-
"""Вывод CLI диспетчера на консоли с узкой кодировкой (cp1252) не роняет процесс."""
from __future__ import annotations

import io
import sys

import pytest

from standkit_hub import __main__ as hub_main


def _cp1252_stream():
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")


def test_handover_message_survives_cp1252(monkeypatch):
    out = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _cp1252_stream())
    monkeypatch.setattr(hub_main._installed_exe, "find_installed_hub_exe",
                        lambda: "C:/BPMkit/BPMkit-hub.exe")
    monkeypatch.setattr(hub_main._platform, "spawn_hidden", lambda *a, **k: None)

    hub_main._safe_console_streams()
    rc = hub_main._hand_over_to_installed_exe(type("A", (), {"no_browser": True})())
    out.flush()

    assert rc == 0
    assert "pip uninstall standkit" in out.buffer.getvalue().decode("cp1252")


def test_control_without_safe_streams_cp1252_fails(monkeypatch):
    out = _cp1252_stream()
    monkeypatch.setattr(sys, "stdout", out)
    with pytest.raises(UnicodeEncodeError):
        print("[standkit-hub] установлен")
        out.flush()
