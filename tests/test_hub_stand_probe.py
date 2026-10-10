"""
Тесты помощников формы «Зарегистрировать стенд» / «Изменить»
(``standkit_hub/stand_probe.py`` и их HTTP-обёртки в ``standkit_hub/server.py``):

* «Заполнить из конфигов» — ``probe_folder`` / ``POST /api/stands/probe-folder``
  (парсер ``ConnectionStrings.config`` и ``appsettings.json`` на фикстурах, пароли в
  ответ не попадают);
* кнопка «Тест» — ``test_connection`` / ``POST /api/stands/test-connection``
  (настоящие локальные сокеты вместо моков сети, драйверы БД — подставные модули);
* встроенный выбор папки — ``browse_dir`` / ``POST /api/fs/browse`` (ограничения путей);
* форма «Изменить» — ``POST /api/stand/update`` и ``GET /api/stand/<name>/config``;
* новые поля регистрации: ``db_user``, ``secret_ref_db``, ``engine``, перенос пароля БД
  из конфига в хранилище секретов (значение через страницу не проходит).

gap-file-reason: сквозной сценарий формы (probe → test → register → edit) разнесён
по нескольким модулям; тематические файлы test_hub_register / test_hub_server остаются
для самой регистрации и базового API.
"""

from __future__ import annotations

import json
import re
import socket
import sys
import threading
import types
from pathlib import Path

import pytest

import standkit.secrets as secrets_module
import standkit_hub.server as server_module
from standkit.registry import Registry
from standkit_hub import stand_probe
from standkit_hub.server import _REGISTER_ALLOWED_FIELDS

from tests.test_hub_server import _request, _start_hub

PG_PASSWORD = "Pg-S3cret!#1"
REDIS_PASSWORD = "r3dis-pw"


# --- фикстуры --------------------------------------------------------------


def _connection_strings(redis_port=6379, pg_port=5432, password=PG_PASSWORD, redis_password=REDIS_PASSWORD):
    pw = f"password={password};" if password else ""
    rpw = f";password={redis_password}" if redis_password else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<connectionStrings>\n"
        f'  <add name="redis" connectionString="host=127.0.0.1;db=1;port={redis_port}{rpw}" />\n'
        "  <!-- старая запись: "
        '<add name="db" connectionString="Server=old;Database=old;User ID=old;password=oldpw" /> -->\n'
        f'  <add name="db" connectionString="Server=localhost;Port={pg_port};Database=bpm_demo;'
        f'User ID=postgres;{pw}Timeout=500;MaxPoolSize=1024;" />\n'
        "</connectionStrings>\n"
    )


def _make_instance(root: Path, *, kestrel_url="http://*:5057", wsc=True, **conn_kwargs) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "ConnectionStrings.config").write_text(_connection_strings(**conn_kwargs), encoding="utf-8")
    if kestrel_url:
        (root / "appsettings.json").write_text(
            json.dumps({"Kestrel": {"Endpoints": {"Http": {"Url": kestrel_url}}}}), encoding="utf-8"
        )
    if wsc:
        wsc_dir = root / "WorkspaceConsole"
        wsc_dir.mkdir(exist_ok=True)
        (wsc_dir / "BPMSoft.Tools.WorkspaceConsole.dll").write_bytes(b"x")
    return root


class _Listener:
    """Локальный TCP-сервер: ``handler(conn)`` на каждое соединение."""

    def __init__(self, handler=None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self._handler = handler
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            try:
                if self._handler:
                    self._handler(conn)
            except Exception:  # noqa: BLE001
                pass
            finally:
                conn.close()

    def close(self):
        self.sock.close()


def _redis_handler(required_password=None):
    def handle(conn):
        data = conn.recv(4096)
        if b"AUTH" in data:
            if required_password and required_password.encode() in data:
                conn.sendall(b"+OK\r\n")
            else:
                conn.sendall(b"-ERR invalid password\r\n")
                return
            data = conn.recv(4096)
        elif required_password:
            conn.sendall(b"-NOAUTH Authentication required.\r\n")
            return
        conn.sendall(b"+PONG\r\n")

    return handle


def _free_closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def hub(tmp_path):
    base_url, token, config_path, registry_path, httpd = _start_hub(tmp_path, stand_name="existing")
    yield base_url, token, registry_path, tmp_path
    httpd.shutdown()
    httpd.server_close()


def _post(hub_tuple, path, body):
    base_url, token = hub_tuple[0], hub_tuple[1]
    return _request(base_url, path, token=token, method="POST", origin=base_url, body=body)


# --- probe_folder ----------------------------------------------------------


def test_probe_folder_reads_db_redis_site_engine_without_passwords(tmp_path):
    root = _make_instance(tmp_path / "inst")
    result = stand_probe.probe_folder(str(root))

    assert result["ok"] is True
    assert result["layout"] == "netcore"
    assert result["db"] == {
        "db_type": "postgres", "windows_auth": False, "db_host": "localhost", "db_port": 5432,
        "db_name": "bpm_demo", "db_user": "postgres", "password_in_config": True,
    }
    assert result["redis"] == {"host": "127.0.0.1", "port": 6379, "db": 1, "password_in_config": True}
    assert result["site"]["port"] == 5057 and result["site"]["scheme"] == "http"
    assert "host" not in result["site"], "wildcard-адрес не должен подменять host формы"
    assert result["engine"]["type"] == "workspaceconsole"
    assert result["engine"]["wsc_dll"].endswith("BPMSoft.Tools.WorkspaceConsole.dll")
    # Главное требование: ни одного пароля в ответе — ни в виде значения, ни в виде ключа.
    dumped = json.dumps(result, ensure_ascii=False)
    assert PG_PASSWORD not in dumped and REDIS_PASSWORD not in dumped
    assert "_password" not in dumped
    # Закомментированная старая запись db не читается.
    assert "old" not in result["db"]["db_name"]


def test_probe_folder_marks_missing_passwords(tmp_path):
    root = _make_instance(tmp_path / "inst", password="", redis_password="")
    result = stand_probe.probe_folder(str(root))
    assert result["db"]["password_in_config"] is False
    assert result["redis"]["password_in_config"] is False


def test_probe_folder_mssql_windows_auth_and_instance(tmp_path):
    root = tmp_path / "ms"
    root.mkdir()
    (root / "ConnectionStrings.config").write_text(
        '<connectionStrings>\n'
        '  <add name="dbMsSQL" connectionString="Data Source=SRV\\SQLEXPRESS;Initial Catalog=bpm;'
        'Integrated Security=SSPI;MultipleActiveResultSets=True" />\n'
        '</connectionStrings>',
        encoding="utf-8",
    )
    result = stand_probe.probe_folder(str(root))
    assert result["db"]["db_type"] == "mssql"
    assert result["db"]["db_host"] == "SRV\\SQLEXPRESS"
    assert result["db"]["db_name"] == "bpm"
    assert result["db"]["windows_auth"] is True
    assert "db_port" not in result["db"], "у MSSQL порт по умолчанию не подставляется"
    assert result["redis"] is None
    assert any("Windows" in n for n in result["notes"])


def test_probe_folder_mssql_host_with_comma_port(tmp_path):
    root = tmp_path / "ms2"
    root.mkdir()
    (root / "ConnectionStrings.config").write_text(
        '<add name="db" connectionString="Data Source=srv,14330;Initial Catalog=b;User ID=sa;Password=x" />',
        encoding="utf-8",
    )
    db = stand_probe.probe_folder(str(root))["db"]
    assert db["db_host"] == "srv" and db["db_port"] == 14330 and db["db_type"] == "mssql"


def test_probe_folder_netframework_layout_finds_config_in_webapp(tmp_path):
    root = tmp_path / "iis_inst"
    _make_instance(root / "BPMSoft.WebApp", kestrel_url=None, wsc=False)
    result = stand_probe.probe_folder(str(root))
    assert result["ok"] and result["layout"] == "netframework"
    assert result["host_kind_hint"] == "iis"
    assert result["config_dir"].endswith("BPMSoft.WebApp")
    assert result["db"]["db_name"] == "bpm_demo"
    assert result["site"] is None
    assert any("IIS" in n for n in result["notes"])


@pytest.mark.parametrize(
    "appsettings, expected",
    [
        ({"Kestrel": {"Endpoints": {"Https": {"Url": "https://*:5443"}, "Http": {"Url": "http://localhost:5001"}}}},
         {"scheme": "http", "port": 5001, "host": "localhost"}),
        ({"Kestrel": {"Endpoints": {"Https": {"Url": "https://0.0.0.0:5443"}}}}, {"scheme": "https", "port": 5443}),
        ({"Urls": "http://+:8080;https://+:8443"}, {"scheme": "http", "port": 8080}),
        ({"Kestrel": {"Endpoints": {"Http": {"Url": "http://*"}}}}, None),
    ],
)
def test_probe_folder_site_from_appsettings_variants(tmp_path, appsettings, expected):
    root = tmp_path / "s"
    root.mkdir()
    (root / "appsettings.json").write_text(json.dumps(appsettings), encoding="utf-8")
    site = stand_probe.probe_folder(str(root))["site"]
    if expected is None:
        assert site is None
    else:
        for key, value in expected.items():
            assert site[key] == value


def test_probe_folder_without_config_is_ok_with_notes(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    result = stand_probe.probe_folder(str(root))
    assert result["ok"] is True
    assert result["db"] is None and result["redis"] is None and result["engine"] is None
    assert any("ConnectionStrings.config" in n for n in result["notes"])


@pytest.mark.parametrize("bad", [None, "", "   ", "relative/path", "\\\\server\\share\\x", 42])
def test_probe_folder_rejects_bad_paths(bad):
    result = stand_probe.probe_folder(bad)
    assert result["ok"] is False and result["error"]


def test_probe_folder_missing_dir(tmp_path):
    result = stand_probe.probe_folder(str(tmp_path / "nope"))
    assert result["ok"] is False and "не найдена" in result["error"]


def test_probe_folder_broken_config_does_not_raise(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    (root / "ConnectionStrings.config").write_bytes(b"\xff\xfe\x00garbage<add name=")
    (root / "appsettings.json").write_text("{not json", encoding="utf-8")
    result = stand_probe.probe_folder(str(root))
    assert result["ok"] is True and result["db"] is None and result["site"] is None


def test_read_config_passwords_returns_values_only_for_server_use(tmp_path):
    root = _make_instance(tmp_path / "inst")
    assert stand_probe.read_config_passwords(str(root)) == {"db": PG_PASSWORD, "redis": REDIS_PASSWORD}
    assert stand_probe.read_config_passwords("relative") == {"db": "", "redis": ""}
    assert stand_probe.read_config_passwords(None) == {"db": "", "redis": ""}


# --- test_connection -------------------------------------------------------


def _by_id(result, cid):
    rows = [c for c in result["checks"] if c["id"] == cid]
    assert rows, f"нет проверки {cid}: {[c['id'] for c in result['checks']]}"
    return rows[0]


def test_connection_all_green_with_real_sockets(tmp_path):
    redis = _Listener(_redis_handler())
    pg = _Listener()
    site = _Listener()
    try:
        logs = tmp_path / "logs"
        logs.mkdir()
        result = stand_probe.test_connection({
            "transport": "local", "stand_dir": str(tmp_path), "logs_dir": str(logs),
            "stand_host": "127.0.0.1", "stand_port": site.port,
            "db_type": "postgres", "db_host": "127.0.0.1", "db_port": pg.port, "db_name": "x", "db_user": "u",
            "redis_host": "127.0.0.1", "redis_port": redis.port,
        })
    finally:
        redis.close(); pg.close(); site.close()
    assert result["ok"] is True
    assert _by_id(result, "site.port")["status"] == "ok"
    assert _by_id(result, "db.resolve")["status"] == "ok"
    assert _by_id(result, "db.port")["status"] == "ok"
    assert _by_id(result, "redis.ping")["status"] == "ok"
    assert _by_id(result, "stand_dir")["status"] == "ok"
    # без пароля вход в БД не проверяется — это skip, а не ошибка
    assert _by_id(result, "db.login")["status"] == "skip"


def test_connection_closed_db_port_is_fail_but_closed_site_is_only_warning():
    result = stand_probe.test_connection({
        "transport": "local", "stand_host": "127.0.0.1", "stand_port": _free_closed_port(),
        "db_host": "127.0.0.1", "db_port": _free_closed_port(),
    })
    assert result["ok"] is False
    assert _by_id(result, "db.port")["status"] == "fail"
    assert _by_id(result, "site.port")["status"] == "warn"


def test_connection_unresolvable_names_are_reported_before_port_check(monkeypatch):
    monkeypatch.setattr(stand_probe, "_resolve", lambda host, timeout=0: (False, "nodename nor servname"))
    result = stand_probe.test_connection({
        "db_host": "locahost", "redis_host": "locahost", "stand_host": "locahost", "stand_port": 5000,
    })
    assert result["ok"] is False
    assert _by_id(result, "db.resolve")["status"] == "fail"
    assert "locahost" in _by_id(result, "db.resolve")["message"]
    assert _by_id(result, "redis.resolve")["status"] == "fail"
    assert _by_id(result, "site.resolve")["status"] == "fail"
    assert all(c["id"] != "db.port" for c in result["checks"])


def test_connection_redis_requires_password_and_uses_config_password(tmp_path):
    root = _make_instance(tmp_path / "inst", redis_port=0)
    redis = _Listener(_redis_handler(REDIS_PASSWORD))
    try:
        params = {"redis_host": "127.0.0.1", "redis_port": redis.port, "stand_dir": str(root)}
        denied = stand_probe.test_connection(params)
        assert _by_id(denied, "redis.ping")["status"] == "fail"
        assert "пароль" in _by_id(denied, "redis.ping")["message"]
        allowed = stand_probe.test_connection({**params, "use_config_password": True})
        assert _by_id(allowed, "redis.ping")["status"] == "ok"
        assert REDIS_PASSWORD not in json.dumps(allowed, ensure_ascii=False)
    finally:
        redis.close()


def test_connection_redis_wrong_service_reply_is_fail():
    junk = _Listener(lambda conn: (conn.recv(100), conn.sendall(b"-ERR unknown\r\n")))
    try:
        result = stand_probe.test_connection({"redis_host": "127.0.0.1", "redis_port": junk.port})
    finally:
        junk.close()
    assert _by_id(result, "redis.ping")["status"] == "fail"


def _fake_psycopg2(monkeypatch, *, fail=None):
    calls = []

    class _Cur:
        def execute(self, sql):
            calls.append(sql)

        def fetchone(self):
            return (1,)

    class _Conn:
        def cursor(self):
            return _Cur()

        def close(self):
            calls.append("close")

    def connect(**kwargs):
        calls.append(kwargs)
        if fail:
            raise RuntimeError(fail.replace("{pw}", kwargs["password"]))
        return _Conn()

    monkeypatch.setitem(sys.modules, "psycopg2", types.SimpleNamespace(connect=connect))
    return calls


def test_connection_db_login_with_secret_from_store(monkeypatch):
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", True)
    secrets_module.set_secret("standkit:t:db", "from-store")
    pg = _Listener()
    calls = _fake_psycopg2(monkeypatch)
    try:
        result = stand_probe.test_connection({
            "db_host": "127.0.0.1", "db_port": pg.port, "db_name": "bpm", "db_user": "postgres",
            "secret_ref_db": "standkit:t:db",
        })
    finally:
        pg.close()
    login = _by_id(result, "db.login")
    assert login["status"] == "ok" and "хранилище секретов" in login["message"]
    assert calls[0]["password"] == "from-store" and calls[0]["user"] == "postgres" and calls[0]["dbname"] == "bpm"
    assert "from-store" not in json.dumps(result, ensure_ascii=False)


def test_connection_db_login_failure_masks_password(monkeypatch, tmp_path):
    root = _make_instance(tmp_path / "inst")
    pg = _Listener()
    _fake_psycopg2(monkeypatch, fail='FATAL: password authentication failed for "u" (password {pw})')
    try:
        result = stand_probe.test_connection({
            "db_host": "127.0.0.1", "db_port": pg.port, "db_user": "u", "stand_dir": str(root),
            "use_config_password": True,
        })
    finally:
        pg.close()
    login = _by_id(result, "db.login")
    assert login["status"] == "fail" and result["ok"] is False
    assert PG_PASSWORD not in json.dumps(result, ensure_ascii=False)
    assert "***" in login["message"]


def test_connection_db_login_without_driver_is_skip_not_fail(monkeypatch):
    for name in ("psycopg2", "psycopg"):
        monkeypatch.setitem(sys.modules, name, None)  # import → ImportError
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", True)
    secrets_module.set_secret("standkit:t:db2", "pw")
    pg = _Listener()
    try:
        result = stand_probe.test_connection({
            "db_host": "127.0.0.1", "db_port": pg.port, "db_user": "u", "secret_ref_db": "standkit:t:db2",
        })
    finally:
        pg.close()
    login = _by_id(result, "db.login")
    assert login["status"] == "warn" and "драйвера" in login["message"]
    assert result["ok"] is True


def test_connection_mssql_named_instance_without_browser_port_warns(monkeypatch):
    monkeypatch.setattr(stand_probe, "_mssql_browser_port", lambda host, inst, timeout=0: None)
    result = stand_probe.test_connection({"db_type": "mssql", "db_host": "localhost\\SQLEXPRESS"})
    port = _by_id(result, "db.port")
    assert port["status"] == "warn" and "SQLEXPRESS" in port["message"]
    assert result["ok"] is True


def test_connection_mssql_uses_instance_port_from_sql_browser(monkeypatch):
    mssql = _Listener()
    monkeypatch.setattr(stand_probe, "_mssql_browser_port", lambda host, inst, timeout=0: mssql.port)
    try:
        result = stand_probe.test_connection({"db_type": "mssql", "db_host": "localhost\\SQLEXPRESS"})
    finally:
        mssql.close()
    assert _by_id(result, "db.port")["status"] == "ok"


def test_connection_rejects_garbage_hosts_and_params():
    result = stand_probe.test_connection({"db_host": "bad host; rm -rf", "redis_host": "a b", "stand_host": "x y", "stand_port": 1})
    assert result["ok"] is False
    assert {c["status"] for c in result["checks"]} == {"fail"}
    assert stand_probe.test_connection("not a dict")["ok"] is False


def test_connection_missing_stand_dir_is_fail_for_local_but_ignored_for_http(tmp_path):
    missing = str(tmp_path / "none")
    assert stand_probe.test_connection({"transport": "local", "stand_dir": missing})["ok"] is False
    assert stand_probe.test_connection({"transport": "http", "stand_dir": missing})["ok"] is True


# --- browse_dir ------------------------------------------------------------


def test_browse_lists_only_subdirectories_sorted_case_insensitive(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "A").mkdir()
    (tmp_path / "file.txt").write_text("x")
    result = stand_probe.browse_dir(str(tmp_path))
    assert [d["name"] for d in result["dirs"]] == ["A", "b"]
    assert result["path"] == str(tmp_path) and result["roots"] is False
    assert result["parent"] == str(tmp_path.parent)


def test_browse_marks_instance_folder(tmp_path):
    _make_instance(tmp_path / "inst")
    assert stand_probe.browse_dir(str(tmp_path / "inst"))["is_instance"] is True
    assert stand_probe.browse_dir(str(tmp_path))["is_instance"] is False


def test_browse_collapses_dotdot_before_checks(tmp_path):
    (tmp_path / "a").mkdir()
    result = stand_probe.browse_dir(str(tmp_path / "a" / ".." / "a"))
    assert result["path"] == str(tmp_path / "a")


@pytest.mark.parametrize("bad", ["relative/dir", "..", "\\\\server\\share", "//server/share", "/tmp\x00x", 123])
def test_browse_rejects_relative_unc_nul_and_non_strings(bad):
    with pytest.raises(stand_probe.BrowseError):
        stand_probe.browse_dir(bad)


def test_browse_rejects_file_and_missing(tmp_path):
    f = tmp_path / "f.txt"
    f.write_text("x")
    with pytest.raises(stand_probe.BrowseError):
        stand_probe.browse_dir(str(f))
    with pytest.raises(stand_probe.BrowseError):
        stand_probe.browse_dir(str(tmp_path / "missing"))


def test_browse_empty_path_returns_roots():
    result = stand_probe.browse_dir("")
    assert result["roots"] is True and result["parent"] is None and result["dirs"]
    assert stand_probe.browse_dir(None)["roots"] is True


def test_browse_cannot_climb_above_filesystem_root():
    root = Path(sys.executable).anchor or "/"
    result = stand_probe.browse_dir(root)
    assert result["parent"] == "", "у корня родитель — экран выбора диска, а не произвольный путь"


def test_browse_truncates_huge_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(stand_probe, "BROWSE_MAX_ENTRIES", 3)
    for i in range(6):
        (tmp_path / f"d{i}").mkdir()
    result = stand_probe.browse_dir(str(tmp_path))
    assert len(result["dirs"]) == 3 and result["truncated"] is True


def test_list_roots_on_windows_enumerates_existing_drives(monkeypatch):
    monkeypatch.setattr(stand_probe, "_is_windows", lambda: True)
    monkeypatch.setattr(stand_probe.os.path, "isdir", lambda p: p in ("C:\\", "D:\\"))
    assert stand_probe.list_roots() == [{"name": "C:", "path": "C:\\"}, {"name": "D:", "path": "D:\\"}]


# --- HTTP API --------------------------------------------------------------


def test_api_probe_folder_returns_fields_and_never_a_password(hub):
    root = _make_instance(hub[3] / "inst")
    status, body, _ = _post(hub, "/api/stands/probe-folder", {"path": str(root)})
    assert status == 200 and body["db"]["db_name"] == "bpm_demo"
    assert PG_PASSWORD not in json.dumps(body, ensure_ascii=False)
    status, body, _ = _post(hub, "/api/stands/probe-folder", {"path": "relative"})
    assert status == 400 and body["error"]


@pytest.mark.parametrize("path", ["/api/stands/probe-folder", "/api/stands/test-connection", "/api/fs/browse"])
def test_api_helpers_require_session_token_and_local_origin(hub, path):
    base_url, token = hub[0], hub[1]
    assert _request(base_url, path, method="POST", origin=base_url, body={})[0] == 403  # нет токена
    assert _request(base_url, path, token=token, method="POST", origin="http://evil.example", body={})[0] == 403
    assert _request(base_url, path, token=token, method="POST", origin=base_url, body={})[0] in (200, 400)


def test_api_fs_browse_lists_and_rejects(hub):
    (hub[3] / "sub").mkdir()
    status, body, _ = _post(hub, "/api/fs/browse", {"path": str(hub[3])})
    assert status == 200 and "sub" in [d["name"] for d in body["dirs"]]
    status, body, _ = _post(hub, "/api/fs/browse", {"path": "../etc"})
    assert status == 400
    status, body, _ = _post(hub, "/api/fs/browse", {})
    assert status == 200 and body["roots"] is True


def test_api_test_connection_rejects_password_fields_and_runs_checks(hub):
    status, body, _ = _post(hub, "/api/stands/test-connection", {"db_password": "x"})
    assert status == 400 and body["fields"] == ["db_password"]
    pg = _Listener()
    try:
        # недопустимый адрес Redis отклоняется сразу (без сетевых ожиданий), порт БД открыт
        status, body, _ = _post(hub, "/api/stands/test-connection",
                                {"db_host": "127.0.0.1", "db_port": pg.port, "redis_host": "bad host"})
    finally:
        pg.close()
    assert status == 200 and body["ok"] is False
    assert _by_id(body, "db.port")["status"] == "ok"
    assert _by_id(body, "redis")["status"] == "fail"


# --- регистрация / изменение ----------------------------------------------


def test_register_accepts_db_user_secret_ref_and_engine(hub):
    engine = {"type": "workspaceconsole", "wsc_dll": "/x/wsc.dll", "workspace_name": "Default", "web_app_path": "/x", "evil": "dropped"}
    status, body, _ = _post(hub, "/api/stand/register", {
        "name": "s1", "stand_dir": str(hub[3] / "s1"), "db_host": "127.0.0.1", "db_port": "5432", "db_name": "b",
        "db_user": "postgres", "secret_ref_db": "standkit:s1:db", "engine": engine,
    })
    assert status == 200 and body == {"ok": True, "name": "s1"}
    stand = Registry.load(hub[2]).get("s1")
    assert stand.db_user == "postgres" and stand.secret_ref_db == "standkit:s1:db"
    assert stand.extra["engine"] == {k: v for k, v in engine.items() if k != "evil"}


@pytest.mark.parametrize("engine", ["workspaceconsole", {"wsc_dll": "/x"}, {"type": 5}, {"type": "x", "wsc_dll": 1}])
def test_register_rejects_malformed_engine(hub, engine):
    status, body, _ = _post(hub, "/api/stand/register", {"name": "s2", "stand_dir": str(hub[3] / "s2"), "engine": engine})
    assert status == 400 and "engine" in body["fields"]
    assert "s2" not in Registry.load(hub[2])


def test_register_rejects_bad_secret_ref(hub):
    status, body, _ = _post(hub, "/api/stand/register", {"name": "s3", "stand_dir": str(hub[3] / "s3"), "secret_ref_db": "bad ref!"})
    assert status == 400 and "secret_ref_db" in body["fields"]


def test_register_moves_config_password_into_secret_store_without_exposing_it(hub, monkeypatch):
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", True)
    root = _make_instance(hub[3] / "inst")
    status, body, _ = _post(hub, "/api/stand/register", {
        "name": "pw1", "stand_dir": str(root), "db_user": "postgres", "db_password_from_config": True,
    })
    assert status == 200 and body == {"ok": True, "name": "pw1", "secret_saved": True}
    assert PG_PASSWORD not in json.dumps(body)
    stand = Registry.load(hub[2]).get("pw1")
    assert stand.secret_ref_db == "standkit:pw1:db"
    assert secrets_module.get_secret("standkit:pw1:db") == PG_PASSWORD
    assert PG_PASSWORD not in Path(hub[2]).read_text(encoding="utf-8"), "пароль не должен попасть в реестр"


def test_register_config_password_flag_without_password_in_config_is_400_and_saves_nothing(hub, monkeypatch):
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", True)
    root = _make_instance(hub[3] / "inst2", password="")
    status, body, _ = _post(hub, "/api/stand/register", {"name": "pw2", "stand_dir": str(root), "db_password_from_config": True})
    assert status == 400 and "не найден" in body["error"]
    assert "pw2" not in Registry.load(hub[2])


def test_register_config_password_does_not_save_secret_on_name_conflict(hub, monkeypatch):
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", True)
    root = _make_instance(hub[3] / "inst3")
    status, _body, _ = _post(hub, "/api/stand/register", {"name": "existing", "stand_dir": str(root), "db_password_from_config": True})
    assert status == 409
    assert not secrets_module.has_secret("standkit:existing:db")


def test_update_changes_fields_keeps_unknown_ones_and_clears_empty(hub):
    status, _b, _ = _post(hub, "/api/stand/register", {
        "name": "u1", "stand_dir": str(hub[3] / "u1"), "stand_port": "5050", "db_host": "127.0.0.1", "db_port": "5432",
        "db_name": "b", "db_user": "old", "logs_dir": str(hub[3] / "logs"), "redis_host": "127.0.0.1", "redis_port": "6379",
        "description": "keep me", "engine": {"type": "workspaceconsole", "wsc_dll": "/x.dll"},
    })
    assert status == 200
    # поля, которых нет в форме, но есть в записи (extra, администратор) — должны пережить обновление
    registry = Registry.load(hub[2])
    stand = registry.get("u1")
    stand.extra["custom_key"] = {"nested": 1}
    stand.admin_user = "Boss"
    registry.update(stand)
    registry.save()

    status, body, _ = _post(hub, "/api/stand/update", {
        "name": "u1", "stand_dir": str(hub[3] / "u1"), "stand_port": "5051", "db_user": "new",
        "logs_dir": "", "redis_host": "", "redis_port": "", "db_port": "",
    })
    assert status == 200 and body == {"ok": True, "name": "u1", "updated": True}
    stand = Registry.load(hub[2]).get("u1")
    assert stand.stand_port == 5051 and stand.db_user == "new"
    assert stand.logs_dir == "" and stand.redis_host == "" and stand.redis_port == 0 and stand.db_port == 0
    assert stand.admin_user == "Boss" and stand.extra["custom_key"] == {"nested": 1}
    assert stand.extra["engine"]["type"] == "workspaceconsole", "engine, не пришедший в теле, остаётся"
    assert stand.db_host == "127.0.0.1" and stand.db_name == "b", "поля, которых нет в теле, не трогаются"


def test_update_can_drop_engine_and_secret_ref(hub):
    _post(hub, "/api/stand/register", {"name": "u2", "stand_dir": str(hub[3] / "u2"), "secret_ref_db": "standkit:u2:db", "engine": {"type": "ubs"}})
    status, _b, _ = _post(hub, "/api/stand/update", {"name": "u2", "stand_dir": str(hub[3] / "u2"), "secret_ref_db": "", "engine": None})
    assert status == 200
    stand = Registry.load(hub[2]).get("u2")
    assert stand.secret_ref_db is None and "engine" not in stand.extra


def test_update_unknown_stand_is_404_and_invalid_data_is_400_without_write(hub):
    status, body, _ = _post(hub, "/api/stand/update", {"name": "ghost", "stand_dir": "/x"})
    assert status == 404
    before = Path(hub[2]).read_text(encoding="utf-8")
    status, body, _ = _post(hub, "/api/stand/update", {"name": "existing", "stand_dir": ""})
    assert status == 400 and "stand_dir" in body["error"]
    assert Path(hub[2]).read_text(encoding="utf-8") == before


def test_update_rejects_password_fields(hub):
    status, body, _ = _post(hub, "/api/stand/update", {"name": "existing", "db_password": "x"})
    assert status == 400 and body["fields"] == ["db_password"]


def test_update_requires_mutation_auth(hub):
    base_url = hub[0]
    assert _request(base_url, "/api/stand/update", method="POST", origin=base_url, body={"name": "existing"})[0] == 403


def test_get_stand_config_returns_form_fields_and_secret_flag_but_no_secret(hub, monkeypatch):
    monkeypatch.setattr(secrets_module, "_HAS_KEYRING", True)
    secrets_module.set_secret("standkit:c1:db", "topsecret")
    _post(hub, "/api/stand/register", {
        "name": "c1", "stand_dir": str(hub[3] / "c1"), "db_user": "u", "secret_ref_db": "standkit:c1:db",
        "engine": {"type": "workspaceconsole", "wsc_dll": "/x.dll"},
    })
    base_url, token = hub[0], hub[1]
    status, body, _ = _request(base_url, "/api/stand/c1/config", token=token)
    assert status == 200 and body["name"] == "c1" and body["has_db_secret"] is True
    cfg = body["config"]
    assert cfg["db_user"] == "u" and cfg["secret_ref_db"] == "standkit:c1:db"
    assert cfg["engine"] == {"type": "workspaceconsole", "wsc_dll": "/x.dll"}
    assert set(cfg) <= set(_REGISTER_ALLOWED_FIELDS)
    assert "topsecret" not in json.dumps(body) and "admin_user" not in cfg
    assert _request(base_url, "/api/stand/ghost/config", token=token)[0] == 404
    assert _request(base_url, "/api/stand/c1/config")[0] == 401  # без токена/cookie


# --- статическая согласованность фронта ----------------------------------


WEB_DIR = Path(server_module.__file__).parent / "web"


def test_front_wires_new_endpoints_and_controls():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    for endpoint in ("/api/stands/probe-folder", "/api/stands/test-connection", "/api/fs/browse", "/api/stand/update", "/api/pick"):
        assert endpoint in js, endpoint
    form = html.split('<form id="register-form">', 1)[1].split("</form>", 1)[0]
    for needle in ('name="db_user"', 'name="secret_ref_db"', 'name="engine"', 'id="register-probe-btn"',
                   'id="register-test-btn"', 'data-register-pick="stand_dir"', 'data-register-pick="logs_dir"',
                   'name="db_password_from_config"', 'value="5432"', 'value="6379"'):
        assert needle in form, needle
    assert 'id="fs-browser-overlay"' in html
    assert 'data-action="edit"' in js


def test_front_never_sends_a_password_field():
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    names = re.findall(r"const _REGISTER_FIELD_NAMES = \[(.*?)\];", js, re.S)[0]
    assert "password" not in names.replace("secret_ref_db", "").replace("agent_secret_ref", "")

