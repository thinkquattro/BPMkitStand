"""
Тесты standkit_hub.logs_browser: резолв каталога логов стенда из ДВУХ
источников (``source="stand"`` — ``<stand_dir>/logs``, ``source="bpmkit"`` —
``extra["logs_path"]``), листинг файлов, выбор "основного" (самого свежего)
лога, санитайзинг имени файла (защита от traversal), open_folder (не должен
падать при отсутствии утилиты/каталога).
"""

from __future__ import annotations

import time

import pytest

from standkit.models import Stand
from standkit_hub.logs_browser import (
    list_log_files,
    open_folder,
    pick_primary_log,
    raw_logs_path,
    resolve_logs_dir,
    sanitize_log_filename,
)


def _stand_with_stand_dir_logs(stand_dir) -> Stand:
    """Стенд с реальным каталогом ``<stand_dir>/logs`` (источник "stand")."""
    return Stand(name="s", stand_dir=str(stand_dir))


def _stand_with_bpmkit_logs_path(path) -> Stand:
    """Стенд с ``extra["logs_path"]`` (источник "bpmkit"), stand_dir — фиктивный."""
    return Stand(name="s", stand_dir="/opt/s-not-a-real-dir", extra={"logs_path": str(path)})


# --- источник "stand" (<stand_dir>/logs) ---


def test_resolve_logs_dir_stand_default_source_is_stand(tmp_path):
    # source не передан явно — дефолт должен быть "stand".
    (tmp_path / "logs").mkdir()
    stand = _stand_with_stand_dir_logs(tmp_path)
    assert resolve_logs_dir(stand) == tmp_path / "logs"


def test_resolve_logs_dir_stand_none_when_stand_dir_empty():
    stand = Stand(name="s", stand_dir="")
    assert resolve_logs_dir(stand, source="stand") is None


def test_resolve_logs_dir_stand_none_when_logs_subdir_missing(tmp_path):
    stand = _stand_with_stand_dir_logs(tmp_path)  # tmp_path/logs не создан
    assert resolve_logs_dir(stand, source="stand") is None


def test_resolve_logs_dir_stand_ok(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stand = _stand_with_stand_dir_logs(tmp_path)
    assert resolve_logs_dir(stand, source="stand") == logs_dir


def test_resolve_logs_dir_stand_none_when_logs_is_a_file(tmp_path):
    (tmp_path / "logs").write_text("x", encoding="utf-8")
    stand = _stand_with_stand_dir_logs(tmp_path)
    assert resolve_logs_dir(stand, source="stand") is None


# --- источник "bpmkit" (extra["logs_path"]) ---


def test_resolve_logs_dir_bpmkit_none_when_not_set():
    stand = Stand(name="s", stand_dir="/opt/s")
    assert resolve_logs_dir(stand, source="bpmkit") is None


def test_resolve_logs_dir_bpmkit_none_when_path_missing(tmp_path):
    stand = _stand_with_bpmkit_logs_path(tmp_path / "does-not-exist")
    assert resolve_logs_dir(stand, source="bpmkit") is None


def test_resolve_logs_dir_bpmkit_none_when_path_is_a_file(tmp_path):
    f = tmp_path / "not-a-dir.txt"
    f.write_text("x", encoding="utf-8")
    stand = _stand_with_bpmkit_logs_path(f)
    assert resolve_logs_dir(stand, source="bpmkit") is None


def test_resolve_logs_dir_bpmkit_ok(tmp_path):
    stand = _stand_with_bpmkit_logs_path(tmp_path)
    assert resolve_logs_dir(stand, source="bpmkit") == tmp_path


def test_resolve_logs_dir_unknown_source_raises(tmp_path):
    stand = _stand_with_bpmkit_logs_path(tmp_path)
    with pytest.raises(ValueError):
        resolve_logs_dir(stand, source="something-else")


# --- raw_logs_path (для сообщений "лог недоступен") ---


def test_raw_logs_path_stand_reflects_stand_dir(tmp_path):
    stand = _stand_with_stand_dir_logs(tmp_path)
    assert raw_logs_path(stand, source="stand") == str(tmp_path / "logs")


def test_raw_logs_path_stand_none_when_stand_dir_empty():
    stand = Stand(name="s", stand_dir="")
    assert raw_logs_path(stand, source="stand") is None


def test_raw_logs_path_bpmkit_reflects_extra(tmp_path):
    stand = _stand_with_bpmkit_logs_path(tmp_path / "somewhere")
    assert raw_logs_path(stand, source="bpmkit") == str(tmp_path / "somewhere")


def test_raw_logs_path_bpmkit_none_when_not_set():
    stand = Stand(name="s", stand_dir="/opt/s")
    assert raw_logs_path(stand, source="bpmkit") is None


def test_raw_logs_path_unknown_source_raises():
    stand = Stand(name="s", stand_dir="/opt/s")
    with pytest.raises(ValueError):
        raw_logs_path(stand, source="nope")


# --- листинг/выбор основного лога/санитайзинг/open_folder (не зависят от источника) ---


def test_list_log_files_sorted_by_mtime_desc(tmp_path):
    old = tmp_path / "old.log"
    old.write_text("old", encoding="utf-8")
    time.sleep(0.02)
    new = tmp_path / "new.log"
    new.write_text("new", encoding="utf-8")

    files = list_log_files(tmp_path)
    names = [f["name"] for f in files]
    assert names[0] == "new.log"
    assert names[1] == "old.log"
    assert files[0]["size"] == 3


def test_list_log_files_ignores_subdirectories(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.log").write_text("a", encoding="utf-8")
    files = list_log_files(tmp_path)
    assert [f["name"] for f in files] == ["a.log"]


def test_pick_primary_log_returns_most_recent(tmp_path):
    (tmp_path / "old.log").write_text("old", encoding="utf-8")
    time.sleep(0.02)
    (tmp_path / "new.log").write_text("new", encoding="utf-8")
    primary = pick_primary_log(tmp_path)
    assert primary == tmp_path / "new.log"


def test_pick_primary_log_none_when_empty(tmp_path):
    assert pick_primary_log(tmp_path) is None


def test_sanitize_log_filename_rejects_traversal(tmp_path):
    (tmp_path / "a.log").write_text("a", encoding="utf-8")
    assert sanitize_log_filename(tmp_path, "../a.log") is None
    assert sanitize_log_filename(tmp_path, "../../etc/passwd") is None
    assert sanitize_log_filename(tmp_path, "/etc/passwd") is None
    assert sanitize_log_filename(tmp_path, "sub\\..\\a.log") is None


def test_sanitize_log_filename_rejects_missing_file(tmp_path):
    assert sanitize_log_filename(tmp_path, "does-not-exist.log") is None


def test_sanitize_log_filename_accepts_valid_name(tmp_path):
    (tmp_path / "a.log").write_text("a", encoding="utf-8")
    result = sanitize_log_filename(tmp_path, "a.log")
    assert result == tmp_path / "a.log"


def test_open_folder_false_when_path_missing(tmp_path):
    result = open_folder(tmp_path / "does-not-exist")
    assert result.ok is False


def test_open_folder_does_not_raise_on_existing_dir(tmp_path, monkeypatch):
    # Подменяем subprocess.Popen, чтобы тест не открывал реальный проводник ОС.
    import standkit_hub.logs_browser as logs_browser_module

    calls = {}

    class _FakePopen:
        def __init__(self, args):
            calls["args"] = args

    monkeypatch.setattr(logs_browser_module.subprocess, "Popen", _FakePopen)
    result = open_folder(tmp_path)
    assert result.ok is True
    assert calls["args"]
