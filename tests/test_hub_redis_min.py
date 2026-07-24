"""
Тесты standkit_hub.redis_min: минимальный STDLIB RESP-клиент (SELECT/FLUSHDB)
для кнопки "Очистить Redis" — сокет замокан (реальный Redis не поднимается в
тестах), проверяется формат команд, разбор ответов (``+OK``/``-ERR``) и
отсутствие падений на сетевых ошибках.

Плюс тесты ``resolve_redis_from_stand_config`` — best-effort резолвер
Redis-параметров ИЗ КОНФИГА СТЕНДА (фолбэк, когда реестр BPMkit не хранит
``extra["redis_db"]``), см. docstring модуля.
"""

from __future__ import annotations

import pytest

from standkit_hub.redis_min import _encode_command, flush_db, resolve_redis_from_stand_config


def test_encode_command_select_matches_resp_format():
    assert _encode_command("SELECT", "3") == b"*2\r\n$6\r\nSELECT\r\n$1\r\n3\r\n"


def test_encode_command_flushdb_matches_resp_format():
    assert _encode_command("FLUSHDB") == b"*1\r\n$7\r\nFLUSHDB\r\n"


class _FakeSocket:
    """
    Имитация ``socket.socket`` для теста ``flush_db``: отдаёт заранее
    заданные ответы на ``sendall`` побайтово через ``recv(1)`` (как и
    ожидает ``_read_line``), запоминает отправленные команды.
    """

    def __init__(self, replies: list[bytes]):
        self._replies = list(replies)
        self._current = b""
        self.sent: list[bytes] = []
        self.closed = False

    def settimeout(self, timeout):
        pass

    def sendall(self, data: bytes):
        self.sent.append(data)
        if self._replies:
            self._current += self._replies.pop(0)

    def recv(self, n: int) -> bytes:
        if not self._current:
            return b""
        chunk, self._current = self._current[:n], self._current[n:]
        return chunk

    def close(self):
        self.closed = True


def test_flush_db_success(monkeypatch):
    fake = _FakeSocket([b"+OK\r\n", b"+OK\r\n"])
    monkeypatch.setattr(
        "standkit_hub.redis_min.socket.create_connection", lambda addr, timeout=None: fake
    )

    result = flush_db("127.0.0.1", 6379, 7)

    assert result.ok is True
    assert "7" in result.message
    assert fake.closed is True
    assert fake.sent[0] == _encode_command("SELECT", "7")
    assert fake.sent[1] == _encode_command("FLUSHDB")


def test_flush_db_select_error_from_redis(monkeypatch):
    fake = _FakeSocket([b"-ERR invalid DB index\r\n"])
    monkeypatch.setattr(
        "standkit_hub.redis_min.socket.create_connection", lambda addr, timeout=None: fake
    )

    result = flush_db("127.0.0.1", 6379, 99)

    assert result.ok is False
    assert "SELECT" in result.message


def test_flush_db_flushdb_error_from_redis(monkeypatch):
    fake = _FakeSocket([b"+OK\r\n", b"-ERR something went wrong\r\n"])
    monkeypatch.setattr(
        "standkit_hub.redis_min.socket.create_connection", lambda addr, timeout=None: fake
    )

    result = flush_db("127.0.0.1", 6379, 7)

    assert result.ok is False
    assert "FLUSHDB" in result.message


def test_flush_db_connection_refused_returns_result_not_raises(monkeypatch):
    def _raise(addr, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("standkit_hub.redis_min.socket.create_connection", _raise)

    result = flush_db("127.0.0.1", 6379, 7)

    assert result.ok is False
    assert "127.0.0.1" in result.message


def test_flush_db_closes_socket_even_on_error(monkeypatch):
    fake = _FakeSocket([b"-ERR nope\r\n"])
    monkeypatch.setattr(
        "standkit_hub.redis_min.socket.create_connection", lambda addr, timeout=None: fake
    )

    flush_db("127.0.0.1", 6379, 7)

    assert fake.closed is True


# --- resolve_redis_from_stand_config: best-effort резолвер из конфига стенда ---


def test_resolve_redis_from_stand_config_none_when_dir_missing(tmp_path):
    assert resolve_redis_from_stand_config(str(tmp_path / "does-not-exist")) is None


def test_resolve_redis_from_stand_config_none_when_stand_dir_empty():
    assert resolve_redis_from_stand_config("") is None
    assert resolve_redis_from_stand_config(None) is None


def test_resolve_redis_from_stand_config_none_when_no_config_files(tmp_path):
    (tmp_path / "readme.txt").write_text("nothing here", encoding="utf-8")
    assert resolve_redis_from_stand_config(str(tmp_path)) is None


def test_resolve_redis_from_stand_config_connection_strings_config_add_tag(tmp_path):
    # Классический вид <connectionStrings> секции .NET-конфига.
    (tmp_path / "ConnectionStrings.config").write_text(
        """<?xml version="1.0"?>
<connectionStrings>
  <add name="db" connectionString="Data Source=.;Initial Catalog=Db" />
  <add name="redisConnectionString" connectionString="host=10.20.30.40;db=5;port=6390" />
</connectionStrings>
""",
        encoding="utf-8",
    )

    result = resolve_redis_from_stand_config(str(tmp_path))

    assert result == {"host": "10.20.30.40", "port": 6390, "db": 5}


def test_resolve_redis_from_stand_config_connection_strings_config_defaults_host_port(tmp_path):
    # Только db задан явно — host/port должны получить дефолты.
    (tmp_path / "ConnectionStrings.config").write_text(
        '<add name="redis" connectionString="db=2" />', encoding="utf-8"
    )

    result = resolve_redis_from_stand_config(str(tmp_path))

    assert result == {"host": "127.0.0.1", "port": 6379, "db": 2}


def test_resolve_redis_from_stand_config_appsettings_json_nested_object(tmp_path):
    import json

    (tmp_path / "appsettings.json").write_text(
        json.dumps({"Caching": {"Redis": {"Host": "127.0.0.1", "Port": 6379, "Db": 3}}}),
        encoding="utf-8",
    )

    result = resolve_redis_from_stand_config(str(tmp_path))

    assert result == {"host": "127.0.0.1", "port": 6379, "db": 3}


def test_resolve_redis_from_stand_config_appsettings_json_connection_string_value(tmp_path):
    import json

    (tmp_path / "appsettings.json").write_text(
        json.dumps({"ConnectionStrings": {"RedisConnectionString": "host=127.0.0.1;db=9;port=6379"}}),
        encoding="utf-8",
    )

    result = resolve_redis_from_stand_config(str(tmp_path))

    assert result == {"host": "127.0.0.1", "port": 6379, "db": 9}


def test_resolve_redis_from_stand_config_appsettings_json_without_db_returns_none(tmp_path):
    import json

    # Redis-секция есть, но без номера БД — угадывать нельзя, ждём None.
    (tmp_path / "appsettings.json").write_text(
        json.dumps({"Redis": {"Host": "127.0.0.1", "Port": 6379}}),
        encoding="utf-8",
    )

    assert resolve_redis_from_stand_config(str(tmp_path)) is None


def test_resolve_redis_from_stand_config_prefers_known_filenames_over_others(tmp_path):
    import json

    # Произвольный *.config файл предлагает свой db — но appsettings.json
    # (известное имя) должен быть проверен раньше по порядку кандидатов.
    (tmp_path / "custom.config").write_text(
        '<add name="redis" connectionString="db=99" />', encoding="utf-8"
    )
    (tmp_path / "appsettings.json").write_text(
        json.dumps({"Redis": {"Db": 1}}), encoding="utf-8"
    )

    result = resolve_redis_from_stand_config(str(tmp_path))

    assert result["db"] == 1


def test_resolve_redis_from_stand_config_generic_config_file_in_root(tmp_path):
    # Не только ConnectionStrings.config — любой *.config в корне подходит.
    (tmp_path / "Web.config").write_text(
        '<add name="redisCache" connectionString="host=192.168.1.1;db=4" />', encoding="utf-8"
    )

    result = resolve_redis_from_stand_config(str(tmp_path))

    assert result == {"host": "192.168.1.1", "port": 6379, "db": 4}


def test_resolve_redis_from_stand_config_ignores_broken_json(tmp_path):
    (tmp_path / "appsettings.json").write_text("{not valid json", encoding="utf-8")
    assert resolve_redis_from_stand_config(str(tmp_path)) is None


def test_resolve_redis_from_stand_config_ignores_unrelated_config(tmp_path):
    (tmp_path / "ConnectionStrings.config").write_text(
        '<add name="db" connectionString="Data Source=.;Initial Catalog=Db" />', encoding="utf-8"
    )
    assert resolve_redis_from_stand_config(str(tmp_path)) is None


def test_resolve_redis_from_stand_config_does_not_raise_on_huge_file(tmp_path):
    # Файл больше потолка — должен быть пропущен, а не прочитан целиком.
    big = tmp_path / "appsettings.json"
    big.write_bytes(b"{" + b" " * (3 * 1024 * 1024) + b'"Redis": {"Db": 1}}')
    assert resolve_redis_from_stand_config(str(tmp_path)) is None
