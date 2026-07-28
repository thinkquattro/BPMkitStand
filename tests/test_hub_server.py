"""
Тесты standkit_hub.server: security-модель (сессионный токен/cookie,
double-submit + Origin-проверка на мутациях, fail-closed bind) и API
(/api/stands агрегация, start/stop/restart через lifecycle, секреты не
логируются, санитайзинг статики).

Сервер поднимается в daemon-потоке на свободном порту на время теста (тот же
лёгкий паттерн, что и tests/test_agent_server_integration.py) — без реального
браузера/дисплея.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import standkit_hub.client as hub_client_module
import standkit_hub.server as server_module
from standkit.registry import Registry
from standkit_hub.config import HubConfig
from standkit_hub.security import InsecureBindError, generate_session_token
from standkit_hub.server import create_hub_server


@pytest.fixture(autouse=True)
def _close_hub_servers(monkeypatch):
    """
    Закрывает все хабы, поднятые тестом.

    Раньше тесты оставляли серверы жить до конца сессии, и это сходило с рук:
    без фонового опроса «забытый» хаб просто держал сокет. С появлением
    поллера (standkit_hub/poller.py) он продолжает тикать в своём потоке и
    дёргать модульные функции — а тесты подменяют их через monkeypatch
    глобально. В результате чужой поллер вызывал подменённую функцию уже в
    следующем тесте, и тот падал в зависимости от порядка запуска.

    Порядок уборки важен:

    1. поллеру говорим остановиться с КОРОТКИМ таймаутом — иначе каждый тест
       платил бы до 2 секунд ожидания потока, застрявшего на сетевой пробе
       (поток daemon, процесс он не удержит);
    2. ``shutdown()`` — в отдельном потоке с ограничением: на сервере, где
       ``serve_forever`` не запускали, он заблокировался бы навсегда;
    3. ``server_close()`` — закрывает сокет. Если сделать его до shutdown,
       ``serve_forever`` падает с WinError 10038 «операция на объекте, не
       являющемся сокетом» и pytest сыплет предупреждениями.
    """
    created: list = []
    original = create_hub_server

    def _tracking(*args, **kwargs):
        httpd = original(*args, **kwargs)
        created.append(httpd)
        return httpd

    monkeypatch.setattr(sys.modules[__name__], "create_hub_server", _tracking)
    monkeypatch.setattr(server_module, "create_hub_server", _tracking)
    yield
    for httpd in created:
        poller = getattr(httpd, "status_poller", None)
        if poller is not None:
            try:
                poller.stop(timeout=0.2)
            except Exception:  # noqa: BLE001 - уборка не должна ронять тест
                pass
        stopper = threading.Thread(target=httpd.shutdown, daemon=True)
        stopper.start()
        stopper.join(timeout=1.0)
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001 - уборка не должна ронять тест
            pass


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _write_registry(tmp_path, *, stand_name="demo"):
    from standkit.models import Stand

    registry_path = tmp_path / "projects.json"
    registry = Registry(
        path=registry_path,
        default=stand_name,
        stands={stand_name: Stand(name=stand_name, stand_dir=str(tmp_path / stand_name))},
    )
    registry.save()
    return registry_path


def _write_config(tmp_path, *, registry_path):
    config_path = tmp_path / "standkit-hub.json"
    cfg = HubConfig(registry_path=str(registry_path))
    cfg.save(config_path)
    return config_path


def _start_hub(tmp_path, *, stand_name="demo"):
    registry_path = _write_registry(tmp_path, stand_name=stand_name)
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()

    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)

    return f"http://127.0.0.1:{port}", session_token, config_path, registry_path, httpd


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"хаб не поднялся на порту {port} за {timeout}s")


def _request(
    base_url: str,
    path: str,
    *,
    token: str | None = None,
    cookie_token: str | None = None,
    method: str = "GET",
    body: dict | None = None,
    origin: str | None = None,
):
    req = urllib.request.Request(base_url + path, method=method)
    if token is not None:
        req.add_header("X-Standkit-Token", token)
    if cookie_token is not None:
        req.add_header("Cookie", f"standkit_session={cookie_token}")
    if origin is not None:
        req.add_header("Origin", origin)
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    def _maybe_json(payload: str, headers) -> dict:
        content_type = headers.get("Content-Type", "")
        if not payload or "application/json" not in content_type:
            return {}
        return json.loads(payload)

    try:
        with urllib.request.urlopen(req, data=data, timeout=3.0) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, _maybe_json(payload, resp.headers), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        return exc.code, _maybe_json(payload, exc.headers), dict(exc.headers)


# --- аутентификация: GET /api/* ---


def test_get_api_without_token_is_unauthorized(tmp_path):
    base_url, _, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/stands")
    assert status == 401


def test_get_api_with_wrong_token_is_unauthorized(tmp_path):
    base_url, _, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/stands", token="wrong")
    assert status == 401


def test_get_api_with_header_token_is_ok(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stands", token=token)
    assert status == 200
    assert "stands" in body


def test_get_api_with_cookie_token_is_ok(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stands", cookie_token=token)
    assert status == 200
    assert "stands" in body


# --- root: установка cookie по токену в query ---


def test_root_with_valid_query_token_sets_cookie_and_injects_token(tmp_path):
    # По ссылке /?t=<token> хаб отдаёт index НАПРЯМУЮ (без редиректа — иначе токен
    # теряется до загрузки JS), ставит session-cookie и инжектит токен в <meta>,
    # чтобы фронтенд мог класть X-Standkit-Token в мутации.
    base_url, token, *_ = _start_hub(tmp_path)
    try:
        resp = urllib.request.urlopen(f"{base_url}/?t={token}", timeout=3.0)
        status = resp.status
        headers = dict(resp.headers)
        body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status = exc.code
        headers = dict(exc.headers)
        body = exc.read().decode("utf-8")

    assert status == 200
    assert "standkit_session=" + token in headers.get("Set-Cookie", "")
    assert "HttpOnly" in headers.get("Set-Cookie", "")
    assert "SameSite=Strict" in headers.get("Set-Cookie", "")
    assert headers.get("Location") is None
    # токен инжектнут в страницу, плейсхолдер заменён
    assert token in body
    assert "__STANDKIT_TOKEN__" not in body


def test_root_without_token_does_not_leak_token(tmp_path):
    # Неаутентифицированный GET "/" отдаёт index, но БЕЗ токена (плейсхолдер пуст).
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/")
    assert status == 200
    assert token not in (body or "")
    assert "__STANDKIT_TOKEN__" not in (body or "")


def test_root_without_token_serves_index_without_auth(tmp_path):
    base_url, *_ = _start_hub(tmp_path)
    status, _, headers = _request(base_url, "/")
    assert status == 200


# --- мутации: double-submit + Origin ---


def test_post_stand_action_without_header_token_is_forbidden(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    # Только cookie — без X-Standkit-Token заголовка мутация должна отклоняться.
    status, _, _ = _request(
        base_url, "/api/stand/demo/start", cookie_token=token, method="POST", origin=base_url
    )
    assert status == 403


def test_post_stand_action_with_wrong_origin_is_forbidden(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(
        base_url,
        "/api/stand/demo/start",
        token=token,
        method="POST",
        origin="http://evil.example.com",
    )
    assert status == 403


def test_post_stand_action_with_correct_header_and_origin_succeeds(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)

    started = {}

    def _fake_start(stand):
        started["name"] = stand.name

    monkeypatch.setattr(hub_client_module.lifecycle, "start", _fake_start)

    status, body, _ = _request(
        base_url, "/api/stand/demo/start", token=token, method="POST", origin=base_url
    )

    assert status == 200
    assert body.get("ok") is True
    assert started["name"] == "demo"


def test_post_stand_action_dispatches_stop_and_restart(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)

    calls = []
    # stop/restart принимают force= (усыновление стенда, поднятого вне
    # диспетчера) — подменяющие лямбды обязаны повторять сигнатуру.
    monkeypatch.setattr(
        hub_client_module.lifecycle,
        "stop",
        lambda stand, force=False: calls.append(("stop", stand.name, force)),
    )
    monkeypatch.setattr(
        hub_client_module.lifecycle,
        "restart",
        lambda stand, force=False: calls.append(("restart", stand.name, force)),
    )

    status1, _, _ = _request(base_url, "/api/stand/demo/stop", token=token, method="POST", origin=base_url)
    status2, _, _ = _request(base_url, "/api/stand/demo/restart", token=token, method="POST", origin=base_url)

    assert status1 == 200
    assert status2 == 200
    # Без ?force=1 усыновление не запрашивается — force доезжает как False.
    assert ("stop", "demo", False) in calls
    assert ("restart", "demo", False) in calls


def test_unknown_stand_action_returns_404(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(
        base_url, "/api/stand/does-not-exist/start", token=token, method="POST", origin=base_url
    )
    assert status == 404


def test_invalid_stand_name_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(
        base_url, "/api/stand/..%2f..%2fetc/status", token=token
    )
    assert status in (400, 404)


# --- API: /api/stands агрегация ---


def test_api_stands_aggregates_registry_entries(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stands", token=token)
    assert status == 200
    names = [s["name"] for s in body["stands"]]
    assert names == ["demo"]
    assert body["stands"][0]["transport"] == "local"
    assert "status" in body["stands"][0]


# --- API: секреты не логируются/не возвращаются ---


def test_secret_post_never_returns_value(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)

    captured = {}

    def _fake_set_secret(ref, value):
        captured["ref"] = ref
        captured["value"] = value

    monkeypatch.setattr(server_module, "set_secret", _fake_set_secret)

    status, body, _ = _request(
        base_url,
        "/api/secret/standkit:demo:agent-token",
        token=token,
        method="POST",
        origin=base_url,
        body={"value": "super-secret-value"},
    )

    assert status == 200
    assert captured["value"] == "super-secret-value"
    assert "super-secret-value" not in json.dumps(body)
    assert body == {"ok": True, "ref": "standkit:demo:agent-token"}


def test_secret_get_returns_only_has_secret_flag(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)
    monkeypatch.setattr(server_module, "has_secret", lambda ref: True)

    status, body, _ = _request(base_url, "/api/secret/standkit:demo:agent-token", token=token)

    assert status == 200
    assert body == {"ref": "standkit:demo:agent-token", "has_secret": True}


# --- API: настройки ---


def test_settings_get_and_post_roundtrip(tmp_path):
    base_url, token, config_path, *_ = _start_hub(tmp_path)

    status, body, _ = _request(base_url, "/api/settings", token=token)
    assert status == 200
    assert body["refresh_interval_sec"] == 10

    status, body, _ = _request(
        base_url,
        "/api/settings",
        token=token,
        method="POST",
        origin=base_url,
        body={"refresh_interval_sec": 42},
    )
    assert status == 200
    assert body["refresh_interval_sec"] == 42

    reloaded = HubConfig.load(config_path)
    assert reloaded.refresh_interval_sec == 42


def test_settings_get_includes_defaults_for_placeholders(tmp_path):
    """
    GET /api/settings отдаёт справочный блок ``defaults`` — форма показывает
    эти значения в placeholder'ах, чтобы пустое поле не выглядело как «ничего
    не подставится».
    """
    base_url, token, *_ = _start_hub(tmp_path)

    status, body, _ = _request(base_url, "/api/settings", token=token)
    assert status == 200

    defaults = body.get("defaults")
    assert isinstance(defaults, dict)
    assert defaults["agent_host"] == "127.0.0.1"
    assert defaults["agent_port"] == 8765
    assert defaults["refresh_interval_sec"] == 10
    assert defaults["lockout_max_failures"] == 5
    # Секретов в дефолтах нет и быть не может — только ссылки на них.
    assert defaults["token_ref"] == ""


def test_settings_post_ignores_defaults_echoed_back(tmp_path):
    """
    ``defaults`` — поле ответа, а не конфига. Если форма вернёт его в POST,
    оно должно быть молча отброшено, а не сохранено и не привести к 500.
    """
    base_url, token, config_path, *_ = _start_hub(tmp_path)

    status, body, _ = _request(
        base_url,
        "/api/settings",
        token=token,
        method="POST",
        origin=base_url,
        body={"refresh_interval_sec": 7, "defaults": {"agent_port": 1}},
    )
    assert status == 200
    assert body["refresh_interval_sec"] == 7

    raw = json.loads(config_path.read_text(encoding="utf-8"))
    assert "defaults" not in raw
    assert HubConfig.load(config_path).agent_port == 8765


# --- API: версия (для модалки "О программе") ---


def test_api_version_returns_version_with_token(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/version", token=token)
    assert status == 200
    assert body["name"] == "BPMkitStand"
    assert "version" in body and body["version"]


def test_api_version_without_token_is_unauthorized(tmp_path):
    base_url, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/version")
    assert status == 401


# --- статика: санитайзинг путей ---


def test_static_traversal_is_rejected(tmp_path):
    import http.client

    base_url, *_ = _start_hub(tmp_path)
    host_port = base_url.replace("http://", "")
    host, port = host_port.split(":")

    # Реальный path-traversal (не percent-encoded) — отправляем сырым
    # http.client, чтобы клиентская библиотека не "нормализовала" URL за нас.
    conn = http.client.HTTPConnection(host, int(port), timeout=3.0)
    try:
        conn.request("GET", "/static/../../../../etc/passwd")
        resp = conn.getresponse()
        status = resp.status
        resp.read()
    finally:
        conn.close()

    assert status == 404


def test_static_known_file_is_served(tmp_path):
    base_url, *_ = _start_hub(tmp_path)
    status, _, headers = _request(base_url, "/static/style.css")
    assert status == 200
    assert "text/css" in headers.get("Content-Type", "")


def test_cookbook_is_served_as_static_and_self_contained(tmp_path):
    """
    Кукбук (`/static/cookbook.html`) — точка входа кнопки «Справка» в шапке
    дашборда и ссылки в модалке «О программе». Тест держит три вещи:
    файл вообще доехал до пакета (package-data `web/*`), отдаётся как HTML и
    остаётся самодостаточным — без внешних ссылок на CDN и без обращений к
    `/static/*`, чтобы его можно было открыть с диска при остановленном хабе.
    """
    base_url, *_ = _start_hub(tmp_path)
    status, _, headers = _request(base_url, "/static/cookbook.html")
    assert status == 200
    assert "text/html" in headers.get("Content-Type", "")

    # _request отдаёт тело только для JSON — содержимое читаем с диска.
    text = (Path(server_module.__file__).parent / "web" / "cookbook.html").read_text("utf-8")
    assert "BPMkitStand" in text
    for forbidden in ('src="/static/', 'href="/static/', "<script src=", "<link rel=\"stylesheet\""):
        assert forbidden not in text, f"кукбук перестал быть самодостаточным: {forbidden}"


def test_static_missing_file_is_404(tmp_path):
    base_url, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/static/does-not-exist.js")
    assert status == 404


# --- fail-closed bind ---


def test_create_hub_server_rejects_non_loopback_without_insecure(tmp_path):
    config_path = _write_config(tmp_path, registry_path=tmp_path / "projects.json")
    with pytest.raises(InsecureBindError):
        create_hub_server(
            "0.0.0.0",
            0,
            config_path=config_path,
            session_token=generate_session_token(),
        )


def test_create_hub_server_allows_non_loopback_with_insecure(tmp_path):
    config_path = _write_config(tmp_path, registry_path=tmp_path / "projects.json")
    httpd = create_hub_server(
        "0.0.0.0",
        0,
        config_path=config_path,
        session_token=generate_session_token(),
        insecure=True,
    )
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()


def test_create_hub_server_allows_loopback_without_insecure(tmp_path):
    config_path = _write_config(tmp_path, registry_path=tmp_path / "projects.json")
    httpd = create_hub_server(
        "127.0.0.1",
        0,
        config_path=config_path,
        session_token=generate_session_token(),
    )
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()


def test_unsupported_method_returns_405(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/stands", token=token, method="PUT")
    assert status == 405


# --- API: /api/stands обогащённый payload (http/db/redis/process значения) ---


def _write_registry_with_extra(tmp_path, *, stand_name="demo", extra=None, db_name="", db_host="", db_port=0):
    from standkit.models import Stand

    registry_path = tmp_path / "projects.json"
    registry = Registry(
        path=registry_path,
        default=stand_name,
        stands={
            stand_name: Stand(
                name=stand_name,
                stand_dir=str(tmp_path / stand_name),
                db_name=db_name,
                db_host=db_host,
                db_port=db_port,
                extra=extra or {},
            )
        },
    )
    registry.save()
    return registry_path


def test_api_stands_enriched_payload_shape(tmp_path):
    docs_dir = tmp_path / "project_docs"
    logs_dir = docs_dir / "logs"
    logs_dir.mkdir(parents=True)
    registry_path = _write_registry_with_extra(
        tmp_path,
        extra={"docs_folder": str(docs_dir), "redis_db": 3},
        db_name="mydb",
    )
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    status, body, _ = _request(base_url, "/api/stands", token=session_token)
    assert status == 200
    stand = body["stands"][0]

    assert stand["http"]["url"] == "http://127.0.0.1:5000"
    assert "state" in stand["http"]

    assert stand["db"]["name"] == "mydb"
    assert "state" in stand["db"]

    assert stand["redis"]["number"] == 3
    assert "state" in stand["redis"]

    assert "state" in stand["process"]
    assert stand["process"]["transport"] == "local"
    assert stand["process"]["logs_path"] == str(logs_dir)


def test_api_stands_redis_number_null_when_not_in_registry(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stands", token=token)
    assert status == 200
    stand = body["stands"][0]
    # Redis-параметры не хранятся в реестре по умолчанию — это ожидаемое
    # отсутствие данных, а не ошибка.
    assert stand["redis"]["number"] is None


# --- API: /api/stand/{name}/state (два источника: stand/bpmkit) ---


def test_api_stand_state_default_source_is_stand(tmp_path):
    # source не передан явно — дефолт "stand" (<stand_dir>/logs), а не bpmkit.
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stand/demo/state", token=token)
    assert status == 200
    assert body["available"] is False
    assert body["source"] == "stand"
    assert "Стенд" in body["text"]


def test_api_stand_state_stand_source_returns_tail_of_primary_log(tmp_path):
    stand_dir = tmp_path / "demo"
    logs_dir = stand_dir / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "stand.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
    registry_path = _write_registry_with_extra(tmp_path)
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    status, body, _ = _request(base_url, "/api/stand/demo/state?source=stand", token=session_token)
    assert status == 200
    assert body["available"] is True
    assert body["source"] == "stand"
    assert "line1" in body["text"]
    assert body["file"] == "stand.log"


def test_api_stand_state_bpmkit_source_unavailable_when_docs_folder_not_set(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stand/demo/state?source=bpmkit", token=token)
    assert status == 200
    assert body["available"] is False
    assert body["source"] == "bpmkit"
    assert "BPMkit" in body["text"]


def test_api_stand_state_bpmkit_source_returns_tail_of_primary_log(tmp_path):
    docs_dir = tmp_path / "project_docs"
    logs_dir = docs_dir / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "stand.log").write_text("line1\nline2\nline3\n", encoding="utf-8")
    registry_path = _write_registry_with_extra(tmp_path, extra={"docs_folder": str(docs_dir)})
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    status, body, _ = _request(base_url, "/api/stand/demo/state?source=bpmkit", token=session_token)
    assert status == 200
    assert body["available"] is True
    assert body["source"] == "bpmkit"
    assert "line1" in body["text"]
    assert body["file"] == "stand.log"


def test_api_stand_state_invalid_source_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/stand/demo/state?source=nope", token=token)
    assert status == 400


def test_api_stand_state_returns_only_current_session(tmp_path):
    # Лог содержит НЕСКОЛЬКО прошлых запусков (каждый со своим "=== START
    # pid=…") плюс текущий — панель "Текущее состояние" должна вернуть
    # ТОЛЬКО последнюю сессию (см. standkit.logs.extract_current_session),
    # а не весь хвост со всей историей запусков.
    stand_dir = tmp_path / "demo"
    logs_dir = stand_dir / "logs"
    logs_dir.mkdir(parents=True)
    log_text = (
        "=== START pid=100 ts=2026-01-01T00:00:00 ===\n"
        "Application starting\n"
        "старый запуск, строка A\n"
        "=== START pid=200 ts=2026-01-01T01:00:00 ===\n"
        "Application starting\n"
        "Application started\n"
        "текущая сессия, строка A\n"
        "текущая сессия, строка B\n"
    )
    (logs_dir / "stand.log").write_text(log_text, encoding="utf-8")
    registry_path = _write_registry_with_extra(tmp_path)
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    status, body, _ = _request(base_url, "/api/stand/demo/state?source=stand", token=session_token)
    assert status == 200
    assert body["available"] is True
    assert body["text"].startswith("=== START pid=200")
    assert "старый запуск" not in body["text"]
    assert "текущая сессия, строка A" in body["text"]
    assert "текущая сессия, строка B" in body["text"]


# --- API: /api/stand/{name}/logs/open-folder (единственный оставшийся суб-путь логов) ---


def test_api_stand_logs_list_and_file_removed(tmp_path):
    # Просмотр отдельных файлов лога из UI убран — эндпоинты list/file больше
    # не существуют (панель "Текущее состояние" показывает только консоль
    # выбранного стенда, единственное действие с логами — открыть папку).
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/stand/demo/logs/list", token=token)
    assert status == 404
    status, _, _ = _request(base_url, "/api/stand/demo/logs/file?name=a.log", token=token)
    assert status == 404


def test_api_stand_logs_open_folder_requires_mutation_auth(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    # Только токен, без Origin — мутация должна отклоняться (та же модель, что и /start).
    status, _, _ = _request(base_url, "/api/stand/demo/logs/open-folder", token=token, method="POST")
    assert status == 403


def test_api_stand_logs_open_folder_calls_subprocess(tmp_path, monkeypatch):
    docs_dir = tmp_path / "project_docs"
    logs_dir = docs_dir / "logs"
    logs_dir.mkdir(parents=True)
    registry_path = _write_registry_with_extra(tmp_path, extra={"docs_folder": str(docs_dir)})
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    import standkit_hub.logs_browser as logs_browser_module

    # ВАЖНО: подменяем именно open_folder, а не subprocess.Popen.
    # Popen используется только в POSIX-ветке open_folder; на Windows код
    # идёт через ShellExecuteW/os.startfile (см. _open_folder_windows), и
    # мок Popen туда не достаёт — тест открывал НАСТОЯЩЕЕ окно Проводника
    # на машине разработчика, по одному на каждый прогон.
    opened: list = []

    def _fake_open_folder(path):
        opened.append(Path(path))
        return logs_browser_module.OpenFolderResult(ok=True, message="")

    monkeypatch.setattr(server_module.logs_browser, "open_folder", _fake_open_folder)

    status, body, _ = _request(
        base_url,
        "/api/stand/demo/logs/open-folder?source=bpmkit",
        token=session_token,
        method="POST",
        origin=base_url,
    )
    assert status == 200
    assert body["ok"] is True
    # Открывали именно каталог логов проекта, а не что-то ещё.
    assert opened == [logs_dir]


def test_api_stand_logs_open_folder_invalid_source_returns_400(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(
        base_url,
        "/api/stand/demo/logs/open-folder?source=nope",
        token=token,
        method="POST",
        origin=base_url,
    )
    assert status == 400


# --- API: /api/stand/{name}/start возвращает pid при успехе ---


def test_post_stand_start_returns_pid_in_response(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)

    monkeypatch.setattr(hub_client_module.lifecycle, "start", lambda stand: 4242)

    status, body, _ = _request(
        base_url, "/api/stand/demo/start", token=token, method="POST", origin=base_url
    )

    assert status == 200
    assert body.get("ok") is True
    assert body.get("pid") == 4242


def test_post_stand_restart_returns_pid_in_response(tmp_path, monkeypatch):
    base_url, token, *_ = _start_hub(tmp_path)

    monkeypatch.setattr(hub_client_module.lifecycle, "restart", lambda stand, force=False: 4343)

    status, body, _ = _request(
        base_url, "/api/stand/demo/restart", token=token, method="POST", origin=base_url
    )

    assert status == 200
    assert body.get("ok") is True
    assert body.get("pid") == 4343


def test_post_stand_action_lifecycle_error_returns_400_with_message(tmp_path, monkeypatch):
    from standkit.lifecycle import LifecycleError

    base_url, token, *_ = _start_hub(tmp_path)

    def _fake_start(stand):
        raise LifecycleError("dotnet не найден в PATH: 'dotnet'")

    monkeypatch.setattr(hub_client_module.lifecycle, "start", _fake_start)

    status, body, _ = _request(
        base_url, "/api/stand/demo/start", token=token, method="POST", origin=base_url
    )

    assert status == 400
    assert "dotnet" in body.get("error", "")


def test_api_mcp_version_endpoint_removed(tmp_path):
    # Карточка MCP/Companion убрана из UI — эндпоинт больше не существует.
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/mcp/version", token=token)
    assert status == 404


# --- API: /api/stand/{name}/redis-clear (кнопка "Очистить Redis") ---


def test_redis_clear_without_redis_db_returns_400(tmp_path):
    # Реестр demo-стенда из _write_registry не содержит extra["redis_db"] —
    # номер БД Redis НИКОГДА не угадывается, только явный отказ с понятным текстом.
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(
        base_url, "/api/stand/demo/redis-clear", token=token, method="POST", origin=base_url
    )
    assert status == 400
    assert "redis_db" in body.get("error", "")


def test_redis_clear_requires_mutation_auth(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    # Только токен, без Origin — мутация должна отклоняться (та же модель, что и /start).
    status, _, _ = _request(base_url, "/api/stand/demo/redis-clear", token=token, method="POST")
    assert status == 403


def test_redis_clear_unknown_stand_returns_404(tmp_path):
    base_url, token, *_ = _start_hub(tmp_path)
    status, _, _ = _request(
        base_url,
        "/api/stand/does-not-exist/redis-clear",
        token=token,
        method="POST",
        origin=base_url,
    )
    assert status == 404


def test_redis_clear_with_redis_db_calls_flush_db_and_returns_200(tmp_path, monkeypatch):
    registry_path = _write_registry_with_extra(tmp_path, extra={"redis_db": 7})
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    calls = {}

    def _fake_flush_db(host, port_, db, **kwargs):
        calls["args"] = (host, port_, db)
        from standkit_hub.redis_min import RedisClearResult

        return RedisClearResult(True, f"Redis db={db} очищен ({host}:{port_})")

    monkeypatch.setattr(server_module.redis_min, "flush_db", _fake_flush_db)

    status, body, _ = _request(
        base_url, "/api/stand/demo/redis-clear", token=session_token, method="POST", origin=base_url
    )

    assert status == 200
    assert body["ok"] is True
    assert calls["args"] == ("127.0.0.1", 6379, 7)


def test_redis_clear_propagates_failure_from_redis_min(tmp_path, monkeypatch):
    registry_path = _write_registry_with_extra(tmp_path, extra={"redis_db": 7})
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    def _fake_flush_db(host, port_, db, **kwargs):
        from standkit_hub.redis_min import RedisClearResult

        return RedisClearResult(False, "не удалось подключиться к Redis")

    monkeypatch.setattr(server_module.redis_min, "flush_db", _fake_flush_db)

    status, body, _ = _request(
        base_url, "/api/stand/demo/redis-clear", token=session_token, method="POST", origin=base_url
    )

    assert status == 502
    assert body["ok"] is False
    assert "не удалось подключиться" in body.get("error", "")


def test_api_stands_logs_bpmkit_available_true_when_docs_folder_logs_exists(tmp_path):
    docs_dir = tmp_path / "project_docs"
    logs_dir = docs_dir / "logs"
    logs_dir.mkdir(parents=True)
    registry_path = _write_registry_with_extra(tmp_path, extra={"docs_folder": str(docs_dir)})
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    status, body, _ = _request(base_url, "/api/stands", token=session_token)
    assert status == 200
    stand = body["stands"][0]
    assert stand["logs"]["bpmkit_available"] is True


def test_api_stands_logs_bpmkit_available_false_when_docs_folder_not_set(tmp_path):
    # demo-стенд из _write_registry не задаёт extra["docs_folder"].
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/stands", token=token)
    assert status == 200
    stand = body["stands"][0]
    assert stand["logs"]["bpmkit_available"] is False


def test_api_stands_logs_bpmkit_available_false_when_docs_folder_does_not_exist(tmp_path):
    registry_path = _write_registry_with_extra(
        tmp_path, extra={"docs_folder": str(tmp_path / "no-such-dir")}
    )
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    status, body, _ = _request(base_url, "/api/stands", token=session_token)
    assert status == 200
    assert body["stands"][0]["logs"]["bpmkit_available"] is False


def test_api_stands_logs_bpmkit_available_false_when_docs_folder_set_but_logs_subdir_missing(tmp_path):
    # docs_folder существует, но подпапка logs внутри него не создана.
    docs_dir = tmp_path / "project_docs"
    docs_dir.mkdir()
    registry_path = _write_registry_with_extra(tmp_path, extra={"docs_folder": str(docs_dir)})
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    status, body, _ = _request(base_url, "/api/stands", token=session_token)
    assert status == 200
    assert body["stands"][0]["logs"]["bpmkit_available"] is False


def test_api_stands_redis_number_falls_back_to_stand_config(tmp_path, monkeypatch):
    # Реестр не содержит redis_db — но _redis_connect_params (через
    # server_module.redis_min) резолвит db из конфига стенда.
    base_url, token, config_path, registry_path, httpd = _start_hub(tmp_path)

    monkeypatch.setattr(
        server_module.redis_min,
        "resolve_redis_from_stand_config",
        lambda stand_dir: {"host": "10.0.0.9", "port": 6400, "db": 11},
    )

    status, body, _ = _request(base_url, "/api/stands", token=token)
    assert status == 200
    assert body["stands"][0]["redis"]["number"] == 11


def test_api_stands_redis_number_registry_takes_priority_over_config(tmp_path, monkeypatch):
    """
    Реестр в приоритете: если в нём есть ``redis_db``, конфиг стенда читаться
    не должен вовсе.

    Проверяется прямым вызовом ``_redis_number``, а не через HTTP. Утверждение
    «функция НЕ вызывалась» плохо уживается с фоновым опросом: monkeypatch
    подменяет функцию глобально, а поллеры хабов из соседних тестов тикают в
    своих потоках и дёргают её же — тест падал в зависимости от порядка и
    тайминга. HTTP-слой для этого поля покрыт соседним тестом
    (``..._falls_back_to_stand_config``).
    """
    registry_path = _write_registry_with_extra(tmp_path, extra={"redis_db": 3})
    registry = Registry.load(registry_path)
    stand = registry.get(registry.names()[0])

    called = {}

    def _fake_resolve(stand_dir):
        called["invoked"] = True
        return {"host": "10.0.0.9", "port": 6400, "db": 99}

    monkeypatch.setattr(server_module.redis_min, "resolve_redis_from_stand_config", _fake_resolve)

    assert server_module._redis_number(stand) == 3
    assert "invoked" not in called


def test_redis_clear_falls_back_to_stand_config_when_registry_has_no_redis_db(tmp_path, monkeypatch):
    base_url, token, config_path, registry_path, httpd = _start_hub(tmp_path)

    monkeypatch.setattr(
        server_module.redis_min,
        "resolve_redis_from_stand_config",
        lambda stand_dir: {"host": "10.0.0.9", "port": 6400, "db": 11},
    )

    calls = {}

    def _fake_flush_db(host, port_, db, **kwargs):
        calls["args"] = (host, port_, db)
        from standkit_hub.redis_min import RedisClearResult

        return RedisClearResult(True, "ok")

    monkeypatch.setattr(server_module.redis_min, "flush_db", _fake_flush_db)

    status, body, _ = _request(
        base_url, "/api/stand/demo/redis-clear", token=token, method="POST", origin=base_url
    )

    assert status == 200
    assert calls["args"] == ("10.0.0.9", 6400, 11)


def test_redis_clear_uses_custom_host_and_port_from_extra(tmp_path, monkeypatch):
    registry_path = _write_registry_with_extra(
        tmp_path, extra={"redis_db": 2, "redis_host": "10.0.0.5", "redis_port": 6380}
    )
    config_path = _write_config(tmp_path, registry_path=registry_path)
    session_token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=session_token)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"

    calls = {}

    def _fake_flush_db(host, port_, db, **kwargs):
        calls["args"] = (host, port_, db)
        from standkit_hub.redis_min import RedisClearResult

        return RedisClearResult(True, "ok")

    monkeypatch.setattr(server_module.redis_min, "flush_db", _fake_flush_db)

    status, _, _ = _request(
        base_url, "/api/stand/demo/redis-clear", token=session_token, method="POST", origin=base_url
    )

    assert status == 200
    assert calls["args"] == ("10.0.0.5", 6380, 2)
