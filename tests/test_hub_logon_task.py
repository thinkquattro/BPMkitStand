"""
Автозапуск удалённого режима при входе в Windows -- задача Планировщика
(``standkit_hub.logon_task``) и маршрут ``/api/remote/logon-task``. ``schtasks`` не запускается:
runner -- подставной, с ответами в формате настоящего вывода (CSV /V /NH, XML).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import standkit_hub.logon_task as lt
import standkit_hub.remote_mode as rm
import standkit_hub.server as server_module
from standkit_hub.config import HubConfig
from standkit_hub.logon_task import (
    LogonTaskController,
    LogonTaskError,
    build_task_xml,
    parse_query_csv,
    split_command,
)
from standkit_hub.remote_mode import RemoteModeController, build_serve_argv
from tests.test_hub_server import _close_hub_servers, _request, _start_hub  # noqa: F401

CLI = ["C:/Program Files/BPMkit/server/BPMkit.exe"]
USER = "DESKTOP-1\\ivanov"
SECRET_VALUE = "s3cr3t-token-value-0123456789abcdef"

CSV_NEVER = ('"DESKTOP-1","\\BPMkit Remote MCP (serve-http)","N/A","Готово","Только интерактивный режим",'
             '"30.11.1999 0:00:00","267011","ivanov","BPMkit.exe serve-http"\n')
CSV_RAN = ('"DESKTOP-1","\\BPMkit Remote MCP (serve-http)","N/A","Выполняется","Только интерактивный режим",'
           '"24.10.2026 9:15:03","0","ivanov","BPMkit.exe serve-http"\n')


@pytest.fixture(autouse=True)
def _no_real_cli(monkeypatch):
    rm.invalidate_token_cache()
    monkeypatch.setattr(rm, "default_cli_runner", lambda argv, env=None, timeout=None: (1, "", ""))
    monkeypatch.setattr(lt, "default_runner", lambda argv, timeout=None: (1, "", "ERROR: not found"))
    yield
    rm.invalidate_token_cache()


def _cfg(tmp_path, **over) -> HubConfig:
    cfg = HubConfig(run_dir=str(tmp_path / "run"), log_dir=str(tmp_path / "logs"))
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


class _Schtasks:
    """Подставной Планировщик: одна задача, XML хранится как при /Create."""

    def __init__(self, rc_create=0):
        self.task_xml = None
        self.calls = []
        self.rc_create = rc_create
        self.csv = CSV_NEVER

    def __call__(self, argv, timeout=None):
        self.calls.append(list(argv))
        op = argv[1].lower()
        if op == "/create":
            if self.rc_create:
                return self.rc_create, "", "ERROR: Access is denied."
            raw = Path(argv[argv.index("/XML") + 1]).read_bytes()
            assert raw[:2] == b"\xff\xfe"                       # UTF-16 LE с BOM, как просит schtasks
            self.task_xml = raw[2:].decode("utf-16-le")
            return 0, "SUCCESS", ""
        if self.task_xml is None:
            return 1, "", "ERROR: The system cannot find the file specified."
        if op == "/query" and "/XML" in argv:
            return 0, self.task_xml, ""
        if op == "/query":
            return 0, self.csv, ""
        if op == "/delete":
            self.task_xml = None
            return 0, "SUCCESS", ""
        raise AssertionError(argv)


def _ctl(tmp_path, sch, *, platform="win32", token=True, cli=CLI, **cfg_over):
    cfg = _cfg(tmp_path, **cfg_over)
    remote = RemoteModeController(cfg, cli_resolver=lambda: cli, has_secret=lambda r: token)
    return LogonTaskController(cfg, remote=remote, runner=sch, platform=platform,
                               user_id=lambda: USER, xml_dir=tmp_path / "xml")


def test_enable_creates_logon_task_from_current_user_without_admin(tmp_path):
    sch = _Schtasks()
    c = _ctl(tmp_path, sch, remote_port=8800, remote_profile="cursor")
    st = c.enable()
    xml = sch.task_xml
    assert "<LogonTrigger>" in xml and f"<UserId>{USER}</UserId>" in xml
    assert "<RunLevel>LeastPrivilege</RunLevel>" in xml and "<LogonType>InteractiveToken</LogonType>" in xml
    assert "<Command>C:/Program Files/BPMkit/server/BPMkit.exe</Command>" in xml
    for part in ("serve-http", "--host 127.0.0.1", "--port 8800", "--profile cursor",
                 "--token-ref bpmsoft-mcp:remote:token"):
        assert part in xml
    assert st["present"] is True and st["up_to_date"] is True and st["never_run"] is True
    create = [a for a in sch.calls if a[1] == "/Create"][0]
    assert "/F" in create and "/RU" not in create                # текущий пользователь, не SYSTEM
    assert not list((tmp_path / "xml").glob("*.xml"))            # временный XML убран


def test_task_never_contains_token_value(tmp_path):
    # токен живёт в хранилище BPMkit; в задачу уходит только ссылка
    sch = _Schtasks()
    c = _ctl(tmp_path, sch, remote_token_ref="bpmsoft-mcp:remote:token")
    c.remote._cli_runner = lambda argv, env=None, timeout=None: (0, SECRET_VALUE, "")
    c.enable()
    assert SECRET_VALUE not in sch.task_xml
    assert all(SECRET_VALUE not in " ".join(a) for a in sch.calls)


def test_allow_query_token_flag_goes_to_arguments(tmp_path):
    sch = _Schtasks()
    _ctl(tmp_path, sch, remote_allow_query_token=True).enable()
    assert "--allow-query-token" in sch.task_xml


def test_enable_refuses_without_token(tmp_path):
    sch = _Schtasks()
    with pytest.raises(LogonTaskError, match="Токен"):
        _ctl(tmp_path, sch, token=False).enable()
    assert sch.calls == [] and sch.task_xml is None


def test_enable_refuses_without_cli_and_with_bad_settings(tmp_path):
    sch = _Schtasks()
    with pytest.raises(LogonTaskError, match="BPMkit.exe"):
        _ctl(tmp_path, sch, cli=None).enable()
    with pytest.raises(LogonTaskError, match="Порт"):
        _ctl(tmp_path, sch, remote_port=70000).enable()
    assert sch.task_xml is None


def test_enable_reports_schtasks_refusal(tmp_path):
    sch = _Schtasks(rc_create=1)
    with pytest.raises(LogonTaskError, match="Access is denied"):
        _ctl(tmp_path, sch).enable()


def test_status_absent_then_present_with_last_run(tmp_path):
    sch = _Schtasks()
    c = _ctl(tmp_path, sch)
    assert c.status() == {"supported": True, "task_name": lt.TASK_NAME, "present": False}
    c.enable()
    sch.csv = CSV_RAN
    st = c.status()
    assert st["present"] and st["never_run"] is False
    assert st["last_run"] == "24.10.2026 9:15:03" and st["last_result"] == "0"


def test_status_flags_outdated_task_after_settings_change(tmp_path):
    sch = _Schtasks()
    _ctl(tmp_path, sch, remote_port=8766).enable()
    assert _ctl(tmp_path, sch, remote_port=8766).status()["up_to_date"] is True
    assert _ctl(tmp_path, sch, remote_port=9001).status()["up_to_date"] is False
    st = _ctl(tmp_path, sch, remote_port=9001).enable()          # пересоздание с новыми настройками
    assert st["up_to_date"] is True and "--port 9001" in sch.task_xml


def test_disable_removes_task_and_is_idempotent(tmp_path):
    sch = _Schtasks()
    c = _ctl(tmp_path, sch)
    c.enable()
    assert c.disable()["present"] is False and sch.task_xml is None
    n = len(sch.calls)
    assert c.disable()["present"] is False                      # повтор -- без /Delete
    assert not any(a[1] == "/Delete" for a in sch.calls[n:])


def test_not_supported_outside_windows(tmp_path):
    sch = _Schtasks()
    c = _ctl(tmp_path, sch, platform="linux")
    assert c.status()["supported"] is False and sch.calls == []
    with pytest.raises(LogonTaskError):
        c.enable()
    with pytest.raises(LogonTaskError):
        c.disable()


def test_parse_query_csv_is_positional_and_language_independent():
    assert parse_query_csv(CSV_RAN) == {"last_run": "24.10.2026 9:15:03", "last_result": "0",
                                        "never_run": False}
    never = parse_query_csv(CSV_NEVER)
    assert never["never_run"] is True and never["last_run"] == ""
    assert parse_query_csv("") == {} and parse_query_csv("INFO: no tasks\n") == {}


def test_xml_escapes_special_characters_and_cyrillic_user():
    xml = build_task_xml("C:\\Program Files\\A&B\\BPMkit.exe", '--token-ref "a<b"', "ПК\\Иван")
    assert "A&amp;B" in xml and "a&lt;b" in xml and "ПК\\Иван" in xml
    cmd, args = split_command(["C:\\x y\\BPMkit.exe", "serve-http", "--host", "127.0.0.1"])
    assert cmd == "C:\\x y\\BPMkit.exe" and args == "serve-http --host 127.0.0.1"


def test_task_arguments_equal_serve_argv(tmp_path):
    cfg = _cfg(tmp_path, remote_port=8123)
    cmd, args = split_command(build_serve_argv(cfg, CLI))
    sch = _Schtasks()
    _ctl(tmp_path, sch, remote_port=8123).enable()
    assert f"<Arguments>{args}</Arguments>" in sch.task_xml and cmd in sch.task_xml


def test_module_runs_processes_only_through_platform_helpers():
    src = Path(lt.__file__).read_text(encoding="utf-8")
    assert "Popen(" not in src and "subprocess.run(" not in src


# --- HTTP API ---


def test_api_logon_task_auth_and_roundtrip(tmp_path, monkeypatch):
    sch = _Schtasks()
    monkeypatch.setattr(lt, "default_runner", sch)
    monkeypatch.setattr(lt.sys, "platform", "win32")
    monkeypatch.setattr(lt, "current_user_id", lambda: USER)
    monkeypatch.setattr(RemoteModeController, "_cli_or_none", lambda self: CLI)
    monkeypatch.setattr(RemoteModeController, "token_present", lambda self, fresh=False: True)
    base_url, token, *_ = _start_hub(tmp_path)
    status, _b, _ = _request(base_url, "/api/remote/logon-task")
    assert status in (401, 403)
    status, body, _ = _request(base_url, "/api/remote/logon-task", token=token)
    assert status == 200 and body["present"] is False
    status, _b, _ = _request(base_url, "/api/remote/logon-task", token=token, method="POST",
                             body={"enabled": True})
    assert status in (401, 403)                                  # мутация -- только с Origin
    status, body, _ = _request(base_url, "/api/remote/logon-task", token=token, method="POST",
                               origin=base_url, body={"enabled": True})
    assert status == 200 and body["ok"] and body["present"] and sch.task_xml
    status, body, _ = _request(base_url, "/api/remote/logon-task", token=token, method="POST",
                               origin=base_url, body={"enabled": False})
    assert status == 200 and body["present"] is False and sch.task_xml is None


def test_api_logon_task_enable_without_token_is_400(tmp_path, monkeypatch):
    sch = _Schtasks()
    monkeypatch.setattr(lt, "default_runner", sch)
    monkeypatch.setattr(lt.sys, "platform", "win32")
    monkeypatch.setattr(RemoteModeController, "_cli_or_none", lambda self: CLI)
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/remote/logon-task", token=token, method="POST",
                               origin=base_url, body={"enabled": True})
    assert status == 400 and "Токен" in body["error"] and sch.task_xml is None


def test_ui_has_logon_task_button_and_wiring():
    web = Path(server_module.__file__).parent / "web"
    html = (web / "index.html").read_text(encoding="utf-8")
    js = (web / "app.js").read_text(encoding="utf-8")
    for ident in ("mcpremote-logon-btn", "mcpremote-logon-status", "mcpremote-logon-error"):
        assert f'id="{ident}"' in html
    assert "Автозапуск при входе в Windows" in html
    assert "/api/remote/logon-task" in js and "renderLogonTask" in js
