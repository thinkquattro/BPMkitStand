# -*- coding: utf-8 -*-
"""Тесты standkit_companion.backend — транспорта канала обновлений издателя.

Почему НАСТОЯЩИЙ HTTP-сервер, а не подмена `urlopen`. Здесь проверяется поведение на
границе с сетью: точная форма заголовка авторизации, дословный `If-None-Match`, `304` как
успех, `206`/`416` докачки, редирект на чужой хост. Подменённый `urlopen` подтвердил бы
только то, что мы сами же и написали в подмену; реальный `http.server` ловит настоящие
ошибки заголовков и статусов. Тот же приём, что в tests/test_hub_server.py.

Сервер поднимается на 127.0.0.1:0 (свободный порт) в daemon-потоке и гасится фикстурой.
"""

from __future__ import annotations

import json
import socket
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from standkit_companion.backend import AUTH_SCHEME, BackendClient
from standkit_companion.errors import ChannelError, NotModified

ENVELOPE = "BPMKIT1.eyJsaWMiOiJ0ZXN0In0.c2ln"
ETAG = '"' + "a" * 64 + '"'

# Служебный ключ в таблице заголовков маршрута — «не отправлять Content-Length».
# Наружу (в ответ) он не уходит, обработчик его снимает.
NO_CONTENT_LENGTH = "_no_content_length"

# Эталонный «бинарь релиза»: важно, чтобы содержимое было узнаваемым при склейке
# докачанного хвоста с уже лежащей головой.
RELEASE_BODY = b"MZ" + bytes(range(256)) * 4


class _FakeBackend:
    """Подставной бэкенд издателя: журнал запросов + таблица маршрутов.

    Журнал (`requests`) — не украшение: два теста проверяют именно ОТСУТСТВИЕ обращения
    (запрос без конверта не должен уходить в сеть вовсе) и точный набор параметров запроса.
    """

    def __init__(self) -> None:
        self.base_url = ""
        self.requests: list = []
        self.routes: dict = {}

    def route(self, path: str, handler) -> None:
        self.routes[path] = handler

    def json_route(self, path: str, payload: dict, *, status: int = 200,
                   headers: dict | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        base = {"Content-Type": "application/json"}
        base.update(headers or {})
        self.route(path, lambda req_headers, query: (status, base, body))

    @property
    def last(self) -> dict:
        return self.requests[-1]


def _handler_factory(fake: _FakeBackend):
    class _Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 — чтобы клиент видел ровно ту семантику длины тела, что и у боевого
        # uvicorn (иначе «конец тела» определялся бы закрытием сокета и тест на пустое
        # тело потерял бы смысл).
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # тишина в выводе pytest
            pass

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("HEAD")

        def _dispatch(self, method: str) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            fake.requests.append({
                "method": method,
                "path": parsed.path,
                "query": parsed.query,
                "headers": {k.lower(): v for k, v in self.headers.items()},
            })
            route = fake.routes.get(parsed.path)
            if route is None:
                status, headers, body = 404, {"Content-Type": "application/json"}, \
                    json.dumps({"detail": "not found"}).encode("utf-8")
            else:
                status, headers, body = route(self.headers, parsed.query)

            headers = dict(headers or {})
            # Маркер маршрута «ответить БЕЗ Content-Length»: так ведёт себя отдача,
            # у которой длина неизвестна заранее, и это отдельный сценарий пустого тела.
            no_length = headers.pop(NO_CONTENT_LENGTH, None) is not None

            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, str(value))
            if status not in (204, 304) and not no_length:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if method != "HEAD" and status not in (204, 304) and body:
                self.wfile.write(body)

    return _Handler


@pytest.fixture
def backend():
    fake = _FakeBackend()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(fake))
    httpd.daemon_threads = True
    fake.base_url = f"http://127.0.0.1:{httpd.server_address[1]}"
    # poll_interval мельче дефолтных 0.5 с: иначе КАЖДЫЙ тест платит полсекунды за
    # shutdown() — на два десятка тестов это десять секунд ожидания ни на чём.
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2.0)


def _client(fake: _FakeBackend, *, envelope: str | None = ENVELOPE, **kwargs) -> BackendClient:
    return BackendClient(fake.base_url, envelope, timeout=5.0, **kwargs)


def _closed_port() -> int:
    """Порт, на котором заведомо никто не слушает (заняли и сразу отпустили)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------------------------
# 1. Заголовок авторизации
# --------------------------------------------------------------------------------------
def test_authorization_header_is_exactly_bpmkit1_envelope(backend):
    """Схему сервер сравнивает регистрозависимо и по одному пробелу — форма обязана
    совпадать дословно, иначе весь канал получает 401 на ровном месте."""
    backend.json_route("/v1/content/releases", {"latest": "0.307.0", "releases": []})

    _client(backend).get_json("/v1/content/releases")

    assert backend.last["headers"]["authorization"] == f"{AUTH_SCHEME} {ENVELOPE}"
    assert AUTH_SCHEME == "BPMKIT1", "схема авторизации бэкенда изменилась молча"


def test_public_endpoint_goes_without_authorization_header(backend):
    """`revocations.json` — публичный: конверт туда не отправляется вовсе (его читают и
    тогда, когда лицензия уже отозвана и авторизация заведомо не пройдёт)."""
    backend.json_route("/v1/content/revocations.json", {"revoked": []})

    _client(backend).get_json("/v1/content/revocations.json", authorized=False)

    assert "authorization" not in backend.last["headers"], \
        "конверт ушёл на публичный эндпоинт, где он не нужен"


# --------------------------------------------------------------------------------------
# 2. Нет конверта — нет и запроса
# --------------------------------------------------------------------------------------
def test_authorized_request_without_envelope_never_touches_network(backend):
    """Без конверта авторизованный запрос гарантированно вернёт 401 — тратить на это
    сетевой вызов и запись в лог бэкенда незачем. Отказ обязан быть ДО обращения."""
    backend.json_route("/v1/content/releases", {"latest": "0.307.0", "releases": []})
    client = _client(backend, envelope=None)

    with pytest.raises(ChannelError) as info:
        client.get_json("/v1/content/releases")

    assert info.value.kind == "no_license"
    assert backend.requests == [], "запрос ушёл в сеть, хотя авторизоваться нечем"


# --------------------------------------------------------------------------------------
# 3-5. Разбор ошибок бэкенда
# --------------------------------------------------------------------------------------
def test_401_nested_revoked_is_classified_and_not_retriable(backend):
    """401 приходит во ВЛОЖЕННОЙ форме `{"detail": {"error_code": ...}}` — разбор обязан
    доставать код оттуда, а не принимать `detail` за строку."""
    backend.json_route(
        "/v1/content/patterns/sync",
        {"detail": {"error_code": "revoked", "detail": "Лицензия отозвана."}},
        status=401,
    )

    with pytest.raises(ChannelError) as info:
        _client(backend).get_json("/v1/content/patterns/sync")

    err = info.value
    assert err.kind == "revoked"
    assert err.http_status == 401
    assert err.retriable is False, "повторять запрос с отозванной лицензией бессмысленно"
    assert err.user_visible is True, "отзыв лицензии обязан быть виден пользователю"
    assert "Лицензия отозвана." in err.detail


def test_403_feature_disabled_is_not_a_license_problem(backend):
    """Регресс на уже допущенную в проекте ошибку: `feature_disabled` — это выключенный у
    издателя ПРИЁМ, а не проблема лицензии. Пользователя таким будить нельзя."""
    backend.json_route(
        "/v1/content/patterns/sync",
        {"detail": {"error_code": "feature_disabled", "detail": "Приём выключен."}},
        status=403,
    )

    with pytest.raises(ChannelError) as info:
        _client(backend).get_json("/v1/content/patterns/sync")

    err = info.value
    assert err.kind == "feature_disabled"
    assert err.user_visible is False, "выключенный приём — не повод показывать проблему"


def test_two_404_flavours_are_distinguished(backend):
    """Оба 404 приходят в ПЛОСКОЙ форме и отличаются только строкой `detail`: «релиза ещё
    нет» — штатный пропуск тика, «подписи нет» — fail-closed отказ применять обновление."""
    backend.json_route("/v1/content/releases", {"detail": "release not configured"},
                       status=404)
    backend.json_route("/v1/content/releases/latest/signature",
                       {"detail": "signature not available"}, status=404)
    client = _client(backend)

    with pytest.raises(ChannelError) as no_release:
        client.get_json("/v1/content/releases")
    with pytest.raises(ChannelError) as no_signature:
        client.get_json("/v1/content/releases/latest/signature")

    assert no_release.value.kind == "release_not_configured"
    assert no_release.value.user_visible is False
    assert no_signature.value.kind == "signature_not_available"
    assert no_signature.value.user_visible is True
    assert no_release.value.detail != no_signature.value.detail


# --------------------------------------------------------------------------------------
# 6. ETag и 304
# --------------------------------------------------------------------------------------
def test_304_is_success_and_etag_travels_verbatim(backend):
    """Две вещи разом: `urllib` бросает HTTPError и на 304 — он обязан стать NotModified;
    ETag сервер сверяет строкой байт-в-байт, поэтому кавычки снимать нельзя."""
    seen: dict = {}

    def _route(req_headers, query):
        seen["if_none_match"] = req_headers.get("If-None-Match")
        return 304, {"ETag": ETAG}, b""

    backend.route("/v1/content/revocations.json", _route)

    with pytest.raises(NotModified):
        _client(backend).get_json("/v1/content/revocations.json",
                                  authorized=False, etag=ETAG)

    assert seen["if_none_match"] == ETAG, "ETag ушёл не дословно — сервер не узнает кэш"
    assert seen["if_none_match"].startswith('"') and seen["if_none_match"].endswith('"')


def test_head_returns_lowercase_headers(backend):
    """HEAD — дешёвая проверка «что там лежит». Ключи нормализуются в нижний регистр:
    вызывающие читают их по фиксированному имени, а прокси регистр не гарантирует."""
    backend.route("/v1/content/releases/latest", lambda h, q: (
        200,
        {"X-BPMkit-Version": "0.307.0", "X-BPMkit-SHA256": "b" * 64, "ETag": ETAG,
         "Accept-Ranges": "bytes"},
        b"",
    ))

    headers = _client(backend).head("/v1/content/releases/latest")

    assert headers["x-bpmkit-version"] == "0.307.0"
    assert headers["x-bpmkit-sha256"] == "b" * 64
    assert headers["etag"] == ETAG
    assert headers["accept-ranges"] == "bytes"
    assert backend.last["method"] == "HEAD"


# --------------------------------------------------------------------------------------
# 7-9. Скачивание
# --------------------------------------------------------------------------------------
def _release_route(req_headers, query):
    """Отдаёт эталонный «бинарь» целиком или хвостом — как настоящий бэкенд на Range."""
    rng = req_headers.get("Range")
    if not rng:
        return 200, {"Content-Type": "application/octet-stream",
                     "X-BPMkit-SHA256": "c" * 64, "X-BPMkit-Version": "0.307.0",
                     "ETag": ETAG}, RELEASE_BODY
    start = int(rng.split("=", 1)[1].split("-", 1)[0])
    tail = RELEASE_BODY[start:]
    return 206, {
        "Content-Type": "application/octet-stream",
        "Content-Range": f"bytes {start}-{len(RELEASE_BODY) - 1}/{len(RELEASE_BODY)}",
        "X-BPMkit-SHA256": "c" * 64,
        "X-BPMkit-Version": "0.307.0",
        "ETag": ETAG,
    }, tail


def test_download_full_file(tmp_path, backend):
    backend.route("/v1/content/releases/latest", _release_route)
    dest = tmp_path / "bpmkit.exe"

    result = _client(backend).download("/v1/content/releases/latest", dest,
                                       expected_size=len(RELEASE_BODY))

    assert dest.read_bytes() == RELEASE_BODY
    assert result["bytes_written"] == len(RELEASE_BODY)
    assert result["total_bytes"] == len(RELEASE_BODY)
    assert result["resumed"] is False
    assert result["status"] == 200
    assert result["sha256_header"] == "c" * 64
    assert result["version_header"] == "0.307.0"
    assert result["etag"] == ETAG


def test_download_resume_appends_instead_of_overwriting(tmp_path, backend):
    """Докачка обязана ДОПИСАТЬ хвост к уже скачанной голове: перезапись means платить
    трафиком за каждый обрыв, а это десятки мегабайт бинаря."""
    backend.route("/v1/content/releases/latest", _release_route)
    dest = tmp_path / "bpmkit.exe.part"
    head_size = 100
    dest.write_bytes(RELEASE_BODY[:head_size])

    result = _client(backend).download("/v1/content/releases/latest", dest,
                                       resume_from=head_size,
                                       expected_size=len(RELEASE_BODY))

    assert backend.last["headers"]["range"] == f"bytes={head_size}-"
    assert "," not in backend.last["headers"]["range"], \
        "мультидиапазон запрашивать нельзя: сервер молча ответит полным 200"
    assert result["status"] == 206
    assert result["resumed"] is True
    assert result["bytes_written"] == len(RELEASE_BODY) - head_size
    assert result["total_bytes"] == len(RELEASE_BODY)
    assert dest.read_bytes() == RELEASE_BODY, "файл склеен неверно — докачка испортила данные"


def test_download_416_resets_partial_file(tmp_path, backend):
    """416 означает, что наш частичный файл серверу не соответствует (релиз перевыложен).
    Не удалить его — обречь клиента вечно повторять один и тот же битый Range."""
    backend.route("/v1/content/releases/latest", lambda h, q: (
        416, {"Content-Range": f"bytes */{len(RELEASE_BODY)}"}, b""))
    dest = tmp_path / "bpmkit.exe.part"
    dest.write_bytes(b"x" * 50)

    with pytest.raises(ChannelError) as info:
        _client(backend).download("/v1/content/releases/latest", dest, resume_from=50)

    assert info.value.kind == "range_invalid"
    assert info.value.http_status == 416
    assert not dest.exists(), "недокачанный файл остался — состояние докачки не сброшено"


def test_download_empty_body_does_not_destroy_existing_file(tmp_path, backend):
    """Разгрузка отдачи (X-Accel) мимо uvicorn даёт 200 с пустым телом. Записать это
    поверх рабочего бинаря — худший исход канала; отказ обязан быть до открытия файла."""
    backend.route("/v1/content/releases/latest", lambda h, q: (
        200, {"Content-Type": "application/octet-stream"}, b""))
    dest = tmp_path / "bpmkit.exe"
    dest.write_bytes(RELEASE_BODY)

    with pytest.raises(ChannelError) as info:
        _client(backend).download("/v1/content/releases/latest", dest,
                                  expected_size=len(RELEASE_BODY))

    assert info.value.kind == "bad_response"
    assert dest.read_bytes() == RELEASE_BODY, "старый бинарь испорчен пустым ответом"


def test_download_empty_body_without_content_length_is_also_rejected(tmp_path, backend):
    """Тот же дефект, но без объявленной длины: проверка не должна опираться только на
    заголовок — тело просто не начинается."""
    backend.route("/v1/content/releases/latest", lambda h, q: (
        200, {"Content-Type": "application/octet-stream", NO_CONTENT_LENGTH: "1"}, b""))
    dest = tmp_path / "bpmkit.exe"
    dest.write_bytes(RELEASE_BODY)

    with pytest.raises(ChannelError) as info:
        _client(backend).download("/v1/content/releases/latest", dest,
                                  expected_size=len(RELEASE_BODY))

    assert info.value.kind == "bad_response"
    assert dest.read_bytes() == RELEASE_BODY


# --------------------------------------------------------------------------------------
# 10. Политика редиректов
# --------------------------------------------------------------------------------------
def test_redirect_to_foreign_host_is_blocked(backend):
    """По этому каналу едет ИСПОЛНЯЕМЫЙ код: редирект на чужой хост — готовая подмена
    бинаря (и утечка заголовка авторизации). Отказ до какого-либо чтения тела."""
    backend.route("/v1/content/releases/latest", lambda h, q: (
        302, {"Location": "http://198.51.100.7:9/evil.exe"}, b""))

    with pytest.raises(ChannelError) as info:
        _client(backend).head("/v1/content/releases/latest")

    assert info.value.kind == "blocked_by_policy"
    assert info.value.retriable is False


def test_redirect_inside_base_url_is_followed(backend):
    """Внутренний редирект разрешён: `latest` у издателя вполне может стать 302 на
    конкретную версию в пределах того же адреса."""
    backend.route("/v1/content/releases/latest/meta", lambda h, q: (
        302, {"Location": f"{backend.base_url}/v1/content/releases/0.307.0/meta"}, b""))
    backend.json_route("/v1/content/releases/0.307.0/meta",
                       {"version": "0.307.0", "size_bytes": 42, "signed": True})

    payload, _headers = _client(backend).get_json("/v1/content/releases/latest/meta")

    assert payload["version"] == "0.307.0"
    assert [r["path"] for r in backend.requests] == [
        "/v1/content/releases/latest/meta",
        "/v1/content/releases/0.307.0/meta",
    ]


# --------------------------------------------------------------------------------------
# 11-12. Транспорт и параметры
# --------------------------------------------------------------------------------------
def test_unreachable_backend_is_offline_not_crash():
    """Недоступный бэкенд — штатное состояние ноутбука, а не авария: `offline`,
    повторяемо, пользователя не будим."""
    client = BackendClient(f"http://127.0.0.1:{_closed_port()}", ENVELOPE, timeout=1.0)

    with pytest.raises(ChannelError) as info:
        client.get_json("/v1/content/releases")

    err = info.value
    assert err.kind == "offline"
    assert err.retriable is True
    assert err.user_visible is False
    assert ENVELOPE not in str(err) and ENVELOPE not in err.detail


def test_none_params_are_dropped_from_query(backend):
    """`None` означает «параметра нет» (курсора синхронизации ещё не было). Пропустить его
    в query — прислать серверу литерал `since=None` и получить 422."""
    backend.json_route("/v1/content/patterns/sync", {"items": [], "has_more": False})

    _client(backend).get_json(
        "/v1/content/patterns/sync",
        params={"since": None, "since_id": None, "mcp_version": "0.355.0", "limit": 200},
    )

    query = urllib.parse.parse_qs(backend.last["query"], keep_blank_values=True)
    assert query == {"mcp_version": ["0.355.0"], "limit": ["200"]}
    assert "since" not in backend.last["query"]


def test_trailing_slash_is_never_sent(backend):
    """FastAPI на `/path/` отвечает 307 на `/path` — лишний переход на каждом тике."""
    backend.json_route("/v1/content/releases", {"latest": "0.307.0", "releases": []})

    _client(backend).get_json("/v1/content/releases/")

    assert backend.last["path"] == "/v1/content/releases"


def test_non_json_body_is_bad_response(backend):
    """HTML-заглушка прокси вместо JSON — поломка контракта, а не «нет данных»."""
    backend.route("/v1/content/releases", lambda h, q: (
        200, {"Content-Type": "text/html"}, b"<html>gateway</html>"))

    with pytest.raises(ChannelError) as info:
        _client(backend).get_json("/v1/content/releases")

    assert info.value.kind == "bad_response"


def test_injected_opener_is_used(backend):
    """`opener` — документированная точка подмены транспорта. Без теста она однажды
    молча перестанет работать, и подменить её будет уже нечем."""
    class _Response:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self, size=-1):
            return b'{"latest": "9.9.9"}' if size == -1 else b""

        def close(self):
            pass

    class _Opener:
        def __init__(self):
            self.opened: list = []

        def open(self, req, timeout=None):
            self.opened.append(req.full_url)
            return _Response()

    opener = _Opener()
    payload, _headers = _client(backend, opener=opener).get_json("/v1/content/releases")

    assert payload == {"latest": "9.9.9"}
    assert opener.opened == [f"{backend.base_url}/v1/content/releases"]
    assert backend.requests == [], "подменённый opener всё равно сходил в сеть"


def test_client_repr_does_not_leak_envelope(backend):
    """Клиент естественно попадает в отладочный вывод — конверта там быть не должно."""
    text = repr(_client(backend))

    assert ENVELOPE not in text
    assert "authorized=True" in text
