"""
Тесты standkit_hub.logs_browser: резолв каталога логов стенда из ДВУХ
источников (``source="stand"`` — ``<stand_dir>/logs``, ``source="bpmkit"`` —
``<extra["docs_folder"]>/logs``, НЕ ``extra["logs_path"]``), листинг файлов,
выбор "основного" (самого свежего) лога, санитайзинг имени файла (защита от
traversal), open_folder (не должен падать при отсутствии утилиты/каталога).
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


def _stand_with_docs_folder(path) -> Stand:
    """
    Стенд с ``extra["docs_folder"]`` (источник "bpmkit" резолвится в
    ``<docs_folder>/logs``), stand_dir — фиктивный.
    """
    return Stand(name="s", stand_dir="/opt/s-not-a-real-dir", extra={"docs_folder": str(path)})


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


# --- источник "bpmkit" (<extra["docs_folder"]>/logs, НЕ extra["logs_path"]) ---


def test_resolve_logs_dir_bpmkit_none_when_docs_folder_not_set():
    stand = Stand(name="s", stand_dir="/opt/s")
    assert resolve_logs_dir(stand, source="bpmkit") is None


def test_resolve_logs_dir_bpmkit_none_when_logs_subdir_missing(tmp_path):
    # docs_folder существует, но <docs_folder>/logs не создан.
    stand = _stand_with_docs_folder(tmp_path)
    assert resolve_logs_dir(stand, source="bpmkit") is None


def test_resolve_logs_dir_bpmkit_none_when_docs_folder_missing(tmp_path):
    stand = _stand_with_docs_folder(tmp_path / "does-not-exist")
    assert resolve_logs_dir(stand, source="bpmkit") is None


def test_resolve_logs_dir_bpmkit_none_when_logs_is_a_file(tmp_path):
    (tmp_path / "logs").write_text("x", encoding="utf-8")
    stand = _stand_with_docs_folder(tmp_path)
    assert resolve_logs_dir(stand, source="bpmkit") is None


def test_resolve_logs_dir_bpmkit_ok(tmp_path):
    (tmp_path / "logs").mkdir()
    stand = _stand_with_docs_folder(tmp_path)
    assert resolve_logs_dir(stand, source="bpmkit") == tmp_path / "logs"


def test_resolve_logs_dir_unknown_source_raises(tmp_path):
    (tmp_path / "logs").mkdir()
    stand = _stand_with_docs_folder(tmp_path)
    with pytest.raises(ValueError):
        resolve_logs_dir(stand, source="something-else")


# --- raw_logs_path (для сообщений "лог недоступен") ---


def test_raw_logs_path_stand_reflects_stand_dir(tmp_path):
    stand = _stand_with_stand_dir_logs(tmp_path)
    assert raw_logs_path(stand, source="stand") == str(tmp_path / "logs")


def test_raw_logs_path_stand_none_when_stand_dir_empty():
    stand = Stand(name="s", stand_dir="")
    assert raw_logs_path(stand, source="stand") is None


def test_raw_logs_path_bpmkit_reflects_docs_folder_logs_subdir(tmp_path):
    stand = _stand_with_docs_folder(tmp_path / "somewhere")
    assert raw_logs_path(stand, source="bpmkit") == str(tmp_path / "somewhere" / "logs")


def test_raw_logs_path_bpmkit_none_when_docs_folder_not_set():
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


def test_list_log_files_recurses_into_dated_subdirs(tmp_path):
    # Стенды BPMSoft пишут логи в подпапки по датам — листинг должен их находить,
    # а имя возвращать как POSIX-путь относительно каталога логов.
    (tmp_path / "empty").mkdir()  # пустая подпапка — не даёт записей
    day = tmp_path / "2026-07-24"
    day.mkdir()
    (day / "app.log").write_text("hello", encoding="utf-8")
    (tmp_path / "top.log").write_text("top", encoding="utf-8")

    names = {f["name"] for f in list_log_files(tmp_path)}
    assert names == {"2026-07-24/app.log", "top.log"}
    by_name = {f["name"]: f for f in list_log_files(tmp_path)}
    assert by_name["2026-07-24/app.log"]["size"] == 5


def test_pick_primary_log_recurses_and_picks_newest_in_subdir(tmp_path):
    old = tmp_path / "2026-07-23"
    old.mkdir()
    (old / "a.log").write_text("old", encoding="utf-8")
    time.sleep(0.02)
    new = tmp_path / "2026-07-24"
    new.mkdir()
    (new / "b.log").write_text("new", encoding="utf-8")

    assert pick_primary_log(tmp_path) == tmp_path / "2026-07-24" / "b.log"


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


def test_sanitize_log_filename_accepts_posix_subdir_name(tmp_path):
    # Имена из list_log_files теперь могут быть путём в подпапке (POSIX-слэш) —
    # санитайзер обязан их принимать, оставаясь строго внутри logs_dir.
    day = tmp_path / "2026-07-24"
    day.mkdir()
    (day / "app.log").write_text("x", encoding="utf-8")
    result = sanitize_log_filename(tmp_path, "2026-07-24/app.log")
    assert result == day / "app.log"


def test_open_folder_false_when_path_missing(tmp_path):
    result = open_folder(tmp_path / "does-not-exist")
    assert result.ok is False


def test_open_folder_does_not_raise_on_existing_dir(tmp_path, monkeypatch):
    # Подменяем subprocess.Popen, чтобы тест не открывал реальный проводник ОС.
    # Ветка Windows (os.startfile) не выполняется на этой платформе — покрыта
    # отдельным тестом ниже (test_open_folder_uses_os_startfile_on_windows).
    import standkit_hub.logs_browser as logs_browser_module

    calls = {}

    class _FakePopen:
        def __init__(self, args):
            calls["args"] = args

    monkeypatch.setattr(logs_browser_module.subprocess, "Popen", _FakePopen)
    result = open_folder(tmp_path)
    assert result.ok is True
    assert calls["args"]


def test_open_folder_uses_os_startfile_on_windows(tmp_path, monkeypatch):
    # На Windows open_folder должен звать os.startfile (надёжнее выводит
    # окно проводника на передний план, чем subprocess-спавн explorer), а НЕ
    # subprocess.Popen. os.startfile существует только на реальной Windows —
    # на прочих платформах подставляем фейк через raising=False.
    import standkit_hub.logs_browser as logs_browser_module

    calls = {}

    def _fake_startfile(path):
        calls["path"] = path

    monkeypatch.setattr(logs_browser_module.sys, "platform", "win32")
    monkeypatch.setattr(logs_browser_module.os, "startfile", _fake_startfile, raising=False)

    def _popen_should_not_be_called(*args, **kwargs):
        raise AssertionError("subprocess.Popen не должен вызываться на Windows-ветке")

    monkeypatch.setattr(logs_browser_module.subprocess, "Popen", _popen_should_not_be_called)

    result = open_folder(tmp_path)
    assert result.ok is True
    assert calls["path"] == str(tmp_path)
