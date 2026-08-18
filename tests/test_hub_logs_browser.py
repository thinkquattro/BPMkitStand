"""
Тесты standkit_hub.logs_browser: резолв каталога логов стенда из ДВУХ
источников (``source="stand"`` — ``<stand_dir>/logs``, ``source="bpmkit"`` —
``<extra["docs_folder"]>/logs``, НЕ ``extra["logs_path"]``), листинг файлов,
выбор "основного" (самого свежего) лога, санитайзинг имени файла (защита от
traversal), open_folder (не должен падать при отсутствии утилиты/каталога).

Отдельный блок — GAP-006: имя каталога логов резолвится БЕЗ УЧЁТА РЕГИСТРА
("Logs"), явный ``Stand.logs_dir`` переопределяет поиск, а путь POSIX-стенда
в сообщениях не собирается разделителями хоста хаба.
"""

from __future__ import annotations

import os
import time

import pytest

from standkit.models import Stand, Transport
from standkit_hub.logs_browser import (
    list_log_files,
    logs_unavailable_reason,
    open_folder,
    pick_primary_log,
    raw_logs_path,
    resolve_logs_dir,
    sanitize_log_filename,
)

def _is_listable(path) -> bool:
    """
    Можно ли ПЕРЕЧИСЛИТЬ каталог по факту: снятое право чтения ограничивает
    только обычного пользователя на POSIX (под root его игнорирует
    CAP_DAC_OVERRIDE, на Windows chmod на каталог почти не влияет). Тесты ниже
    поэтому не помечены ``skipif`` — они формулируют инвариант, верный в обоих
    случаях, иначе на root-CI просто не исполнялись бы.
    """
    try:
        os.listdir(path)
    except OSError:
        return False
    return True


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
    # ВАЖНО: тест не должен открывать реальное окно проводника на машине
    # разработчика. Раньше подменялся только subprocess.Popen — и на Windows
    # (где идёт ветка _open_folder_windows) тест честно открывал папку.
    # Теперь глушим ОБЕ ветки: POSIX-спавн и Windows-launcher.
    import standkit_hub.logs_browser as logs_browser_module

    calls = {}

    class _FakePopen:
        def __init__(self, args):
            calls["args"] = args

    monkeypatch.setattr(logs_browser_module.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        logs_browser_module,
        "_open_folder_windows",
        lambda path: calls.__setitem__("args", [str(path)]),
    )
    result = open_folder(tmp_path)
    assert result.ok is True
    assert calls["args"]


def test_open_folder_uses_windows_launcher_on_win32(tmp_path, monkeypatch):
    # На Windows open_folder должен звать _open_folder_windows (ShellExecuteW +
    # AllowSetForegroundWindow — иначе окно проводника открывается втихую,
    # под браузером), а НЕ subprocess.Popen.
    import standkit_hub.logs_browser as logs_browser_module

    calls = {}
    monkeypatch.setattr(logs_browser_module.sys, "platform", "win32")
    monkeypatch.setattr(
        logs_browser_module,
        "_open_folder_windows",
        lambda path: calls.__setitem__("path", str(path)),
    )

    def _popen_should_not_be_called(*args, **kwargs):
        raise AssertionError("subprocess.Popen не должен вызываться на Windows-ветке")

    monkeypatch.setattr(logs_browser_module.subprocess, "Popen", _popen_should_not_be_called)

    result = open_folder(tmp_path)
    assert result.ok is True
    assert calls["path"] == str(tmp_path)


def test_bring_explorer_to_front_never_raises(tmp_path):
    # Вытаскивание окна на передний план — best-effort: на не-Windows WinAPI
    # недоступен, функция обязана тихо вернуться, а не сломать запрос.
    # timeout_s крошечный: совпадений по заголовку не будет, ждать нечего.
    import standkit_hub.logs_browser as logs_browser_module

    logs_browser_module._bring_explorer_to_front(tmp_path, timeout_s=0.01)


def test_open_folder_windows_falls_back_to_startfile(tmp_path, monkeypatch):
    # Если WinAPI недоступен (ctypes.windll нет / ShellExecuteW вернул ошибку) —
    # честный фолбэк на os.startfile, а не отказ открыть папку.
    import standkit_hub.logs_browser as logs_browser_module

    calls = {}

    def _fake_startfile(path):
        calls["path"] = path

    monkeypatch.setattr(logs_browser_module.os, "startfile", _fake_startfile, raising=False)
    # фоновый «поднять окно на передний план» в тесте не нужен
    monkeypatch.setattr(logs_browser_module, "_bring_explorer_to_front", lambda *a, **kw: None)

    import builtins

    real_import = builtins.__import__

    def _no_ctypes(name, *args, **kwargs):
        if name == "ctypes":
            raise ImportError("ctypes недоступен")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_ctypes)
    logs_browser_module._open_folder_windows(tmp_path)
    monkeypatch.undo()
    assert calls["path"] == str(tmp_path)


def test_list_log_files_since_mtime_filters_older(tmp_path):
    import os
    import time

    old = tmp_path / "old.log"
    old.write_text("o", encoding="utf-8")
    new = tmp_path / "new.log"
    new.write_text("n", encoding="utf-8")
    day_ago = time.time() - 86400
    os.utime(old, (day_ago, day_ago))

    cutoff = time.time() - 3600  # час назад
    assert [f["name"] for f in list_log_files(tmp_path, since_mtime=cutoff)] == ["new.log"]
    # без cutoff — оба
    assert {f["name"] for f in list_log_files(tmp_path)} == {"old.log", "new.log"}


def test_pick_primary_log_since_mtime_ignores_old(tmp_path):
    import os
    import time

    day = tmp_path / "2026-01-01"
    day.mkdir()
    old = day / "a.log"
    old.write_text("o", encoding="utf-8")
    day_ago = time.time() - 86400
    os.utime(old, (day_ago, day_ago))

    # только старый файл: с cutoff «за сегодня» → None; без cutoff → он.
    assert pick_primary_log(tmp_path, since_mtime=time.time() - 3600) is None
    assert pick_primary_log(tmp_path) == old


# --------------------------------------------------------------------------
# GAP-006: регистр имени каталога, явный logs_dir, разделители в сообщениях
# --------------------------------------------------------------------------


def test_resolve_logs_dir_stand_finds_capitalized_logs(tmp_path):
    # BPMSoft раскладывает логи в "Logs"; на Linux жёсткое "logs" не находило
    # каталог, на Windows (регистронезависимая ФС) — это проверка регрессии.
    logs_dir = tmp_path / "Logs"
    logs_dir.mkdir()
    stand = _stand_with_stand_dir_logs(tmp_path)
    assert resolve_logs_dir(stand, source="stand") == logs_dir


def test_resolve_logs_dir_bpmkit_finds_capitalized_logs(tmp_path):
    logs_dir = tmp_path / "Logs"
    logs_dir.mkdir()
    stand = _stand_with_docs_folder(tmp_path)
    assert resolve_logs_dir(stand, source="bpmkit") == logs_dir


def test_resolve_logs_dir_stand_explicit_logs_dir_overrides_search(tmp_path):
    (tmp_path / "Logs").mkdir()
    custom = tmp_path / "custom-logs"
    custom.mkdir()
    stand = Stand(name="s", stand_dir=str(tmp_path), logs_dir=str(custom))
    assert resolve_logs_dir(stand, source="stand") == custom


def test_resolve_logs_dir_stand_explicit_logs_dir_missing_is_none(tmp_path):
    (tmp_path / "Logs").mkdir()
    stand = Stand(name="s", stand_dir=str(tmp_path), logs_dir=str(tmp_path / "нет"))
    assert resolve_logs_dir(stand, source="stand") is None


def test_raw_logs_path_stand_uses_explicit_logs_dir(tmp_path):
    # В сообщении должен быть тот путь, который реально проверялся.
    stand = Stand(name="s", stand_dir=str(tmp_path), logs_dir="/srv/bpmsoft/logs-custom")
    assert raw_logs_path(stand, source="stand") == "/srv/bpmsoft/logs-custom"


def test_raw_logs_path_posix_stand_keeps_forward_slashes():
    # Путь удалённого (POSIX) стенда собирается на хабе, который может быть
    # под Windows: WindowsPath превращал его в \mnt\composers\... — тест
    # обязан проходить на обеих ОС, поэтому проверяем именно разделители.
    stand = Stand(
        name="remote",
        transport=Transport.AGENT,
        agent_url="https://stand-a:8765",
        stand_dir="/mnt/composers/bpmsoft/stand-a/app",
    )
    raw = raw_logs_path(stand, source="stand")
    assert raw == "/mnt/composers/bpmsoft/stand-a/app/logs"
    assert "\\" not in raw


def test_raw_logs_path_bpmkit_posix_keeps_forward_slashes():
    stand = Stand(name="remote", stand_dir="/opt/s", extra={"docs_folder": "/srv/projects/demo"})
    raw = raw_logs_path(stand, source="bpmkit")
    assert raw == "/srv/projects/demo/logs"
    assert "\\" not in raw


def test_raw_logs_path_windows_style_path_keeps_backslashes():
    # Обратный случай: путь в windows-стиле не должен «попозиксеть» и не должен
    # приезжать смесью разделителей на Linux-хабе (было: C:\BPMSoft\stand/logs).
    stand = Stand(name="s", stand_dir="C:\\BPMSoft\\stand")
    raw = raw_logs_path(stand, source="stand")
    assert raw == "C:\\BPMSoft\\stand\\logs"
    assert "/" not in raw


def test_raw_logs_path_windows_drive_only_path_uses_backslash():
    # Буква диска без обратных слэшей в исходном пути — всё равно Windows-путь.
    stand = Stand(name="s", stand_dir="D:/BPMSoft/stand")
    assert raw_logs_path(stand, source="stand") == "D:\\BPMSoft\\stand\\logs"


def test_raw_logs_path_unc_path_stays_unc():
    stand = Stand(name="s", stand_dir="\\\\srv\\share\\stand")
    raw = raw_logs_path(stand, source="stand")
    assert raw == "\\\\srv\\share\\stand\\logs"
    assert "/" not in raw


def test_raw_logs_path_bpmkit_windows_style_keeps_backslashes():
    stand = Stand(name="s", stand_dir="/opt/s", extra={"docs_folder": "C:\\projects\\demo"})
    raw = raw_logs_path(stand, source="bpmkit")
    assert raw == "C:\\projects\\demo\\logs"
    assert "/" not in raw


# --- logs_unavailable_reason: четыре ветки ---


def test_logs_unavailable_reason_path_not_set():
    stand = Stand(name="s", stand_dir="")
    assert logs_unavailable_reason(stand, "stand") == "путь не задан"


def test_logs_unavailable_reason_agent_stand_does_not_lie_about_remote_fs():
    stand = Stand(
        name="remote",
        transport=Transport.AGENT,
        agent_url="https://stand-a:8765",
        stand_dir="/mnt/composers/bpmsoft/stand-a/app",
    )
    reason = logs_unavailable_reason(stand, "stand")
    assert "каталог не найден" not in reason
    assert "на хосте стенда" in reason
    assert reason.endswith("/mnt/composers/bpmsoft/stand-a/app/logs")


def test_logs_unavailable_reason_local_stand_says_dir_not_found(tmp_path):
    stand = Stand(name="s", stand_dir=str(tmp_path))
    reason = logs_unavailable_reason(stand, "stand")
    assert reason.startswith("каталог не найден — ")
    assert str(tmp_path) in reason


def test_logs_unavailable_reason_unknown_source_raises():
    with pytest.raises(ValueError):
        logs_unavailable_reason(Stand(name="s", stand_dir="/opt/s"), "nope")


def test_logs_unavailable_reason_permission_branch_message(monkeypatch, tmp_path):
    """
    Каталог стенда есть, но перечислить его нельзя (нет права чтения), а
    подкаталог логов назван не ``logs``/``Logs`` — значит прямые проверки его
    не нашли, а листинг запрещён. «Каталог не найден» здесь было бы неверным
    диагнозом: мы просто не смогли посмотреть.
    """
    from standkit_hub import logs_browser as _lb

    monkeypatch.setattr(_lb, "scan_denied", lambda base: True)
    stand = Stand(name="s", stand_dir=str(tmp_path))
    reason = logs_unavailable_reason(stand, "stand")

    assert reason.startswith("нет прав на чтение каталога ")
    assert str(tmp_path) in reason
    assert "каталог не найден" not in reason


def test_logs_unavailable_reason_on_real_non_listable_dir(tmp_path):
    """То же на настоящем каталоге без права чтения (там, где ОС это позволяет)."""
    base = tmp_path / "stand"
    base.mkdir()
    (base / "ЖурналыСтенда").mkdir()  # написание, которого нет среди прямых проверок
    os.chmod(base, 0o311)  # -wx: пройти можно, перечислить нельзя (владельцу — тоже)
    try:
        stand = Stand(name="s", stand_dir=str(base))
        assert resolve_logs_dir(stand, "stand") is None
        reason = logs_unavailable_reason(stand, "stand")
        listable = _is_listable(base)
    finally:
        os.chmod(base, 0o755)

    if listable:  # root / Windows: перечисление доступно, каталога логов и правда нет
        assert reason.startswith("каталог не найден — ")
    else:
        assert reason.startswith("нет прав на чтение каталога ")
        assert str(base) in reason


def test_non_listable_stand_dir_with_logs_subdir_still_resolves(tmp_path):
    # Обратная сторона той же ситуации: каталог с типовым именем находится
    # даже без права листинга — до этой ветки дело не доходит.
    base = tmp_path / "stand"
    base.mkdir()
    (base / "Logs").mkdir()
    os.chmod(base, 0o311)
    try:
        assert resolve_logs_dir(Stand(name="s", stand_dir=str(base)), "stand") == base / "Logs"
    finally:
        os.chmod(base, 0o755)


def test_logs_unavailable_reason_permission_branch_not_used_for_explicit_logs_dir(tmp_path, monkeypatch):
    # При явном logs_dir перебора каталога нет вовсе — причина обязана остаться
    # прежней («каталог не найден»), даже если stand_dir нечитаем.
    from standkit_hub import logs_browser as _lb

    monkeypatch.setattr(_lb, "scan_denied", lambda base: True)
    stand = Stand(name="s", stand_dir=str(tmp_path), logs_dir=str(tmp_path / "нет-такого"))
    reason = logs_unavailable_reason(stand, "stand")
    assert reason.startswith("каталог не найден — ")
