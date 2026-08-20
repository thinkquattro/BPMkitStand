"""
Тесты точки расширения редакции в хабе: ``/api/companion/*`` и ``/api/version``.

Предмет здесь — ГРАНИЦА между MIT-ядром и платным каналом обновлений
(``standkit_companion``), а не сам канал: он покрыт своими наборами
(``tests/test_companion_*.py``). Проверяется ровно то, за что отвечает хаб:

* редакция определяется НАЛИЧИЕМ пакета — и ядро обязано работать без него,
  отдавая 503 с человеческим текстом, а не 404 и не трейсбек;
* CSRF-контур мутаций распространяется на канал без послаблений (SECURITY.md
  §4.1): двойная подача токена плюс локальный ``Origin``;
* выключенный настройкой канал ОТВЕЧАЕТ статусом (200) и отказывает в
  действиях (409) — пользователь обязан видеть, почему ничего не происходит;
* типизированный отказ канала превращается в осмысленный HTTP-код, а не в 500
  на всё подряд;
* лицензионный конверт не появляется ни в одном ответе;
* фоновый планировщик канала гасится вместе с сервером.

Сервер поднимается в daemon-потоке на свободном порту — тот же лёгкий паттерн и
те же помощники, что в ``tests/test_hub_server.py`` (включая обязательную
фикстуру гашения серверов: забытый фоновый поток тикает дальше и ломает
СЛЕДУЮЩИЙ тест через подменённые monkeypatch'ем модульные функции).
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

import standkit_companion
import standkit_hub.server as server_module
from standkit.registry import Registry
from standkit_companion.errors import ChannelError
from standkit_hub.config import CompanionCycle, CompanionSettings, HubConfig
from standkit_hub.security import generate_session_token
from standkit_hub.server import create_hub_server

WEB_DIR = Path(server_module.__file__).parent / "web"

# Значение, изображающее лицензионный конверт. Ни один ответ хаба не имеет права
# содержать эту подстроку (см. test_license_envelope_never_leaks_into_responses).
FAKE_ENVELOPE = "BPMKIT-LICENSE-ENVELOPE-b1f2c3d4-НЕ-ДОЛЖЕН-УТЕЧЬ"

# Маршрут → имя действия раннера. Дублируется здесь ДОСЛОВНО и намеренно: тест
# обязан ловить переименование действия в сервере, а не соглашаться с ним,
# импортировав ту же таблицу.
ACTION_ROUTES = {
    "/api/companion/sync": "sync_patterns",
    "/api/companion/check-update": "check_update",
    "/api/companion/stage-update": "stage_update",
    "/api/companion/apply-update": "apply_update",
    "/api/companion/rollback": "rollback",
    "/api/companion/revocations": "refresh_revocations",
}


@pytest.fixture(autouse=True)
def _close_hub_servers(monkeypatch):
    """
    Закрывает все хабы, поднятые тестом (см. одноимённую фикстуру в
    tests/test_hub_server.py — обоснование порядка уборки там же).

    Здесь у неё есть вторая причина существовать: с каналом обновлений у сервера
    появился ВТОРОЙ фоновый поток. Забытый планировщик продолжает перечитывать
    конфиг и ходить в состояние на диске уже после теста.
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
        for attr in ("status_poller", "companion_runner"):
            worker = getattr(httpd, attr, None)
            if worker is None:
                continue
            try:
                worker.stop(timeout=0.2)
            except TypeError:
                # Подставной раннер принимает stop() без аргументов.
                worker.stop()
            except Exception:  # noqa: BLE001 - уборка не должна ронять тест
                pass
        stopper = threading.Thread(target=httpd.shutdown, daemon=True)
        stopper.start()
        stopper.join(timeout=1.0)
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001 - уборка не должна ронять тест
            pass


# --- подставной планировщик канала -------------------------------------------


class _StubRunner:
    """Заглушка ``CompanionRunner`` с минимальным контрактом, который знает хаб.

    Настоящий раннер ходит в сеть за лицензией и подписями — в тестах границы
    ядра это лишнее и недетерминированное. Здесь важно другое: КАКОЕ действие
    вызвал хаб, с какой версией, и что он сделал с исходом.
    """

    def __init__(self, *, result=None, error=None, status=None):
        self.calls: list = []
        self.pokes: list = []
        self.stopped = 0
        self._result = result if result is not None else {"stub": "ok"}
        self._error = error
        self._status = status if status is not None else _stub_status()

    # -- то, что зовёт хаб ------------------------------------------------------
    def run_action(self, action, *, version=None):
        self.calls.append((action, version))
        if self._error is not None:
            raise self._error
        return dict(self._result)

    def status(self):
        return json.loads(json.dumps(self._status))

    def poke(self, cycle=None):
        self.pokes.append(cycle)

    def stop(self, timeout=2.0):
        self.stopped += 1

    def is_running(self):
        return True


def _stub_status() -> dict:
    """Правдоподобная карточка статуса — той же формы, что отдаёт ``status()``."""
    return {
        "running": True,
        "edition": "companion",
        "companion_version": standkit_companion.__version__,
        "settings": CompanionSettings().to_dict(),
        "state": {
            "patterns": {"applied_count": 3, "status": "ok"},
            "releases": {"status": "ok", "restart_required": False},
            "revocations": {"revoked_count": 0, "status": "ok"},
        },
        "cycles": {},
        "context": {"ok": True, "detail": "лицензионный контекст получен", "cli": []},
        "actions": {"sync_patterns": True, "check_update": True, "stage_update": True,
                    "apply_update": False, "rollback": False, "refresh_revocations": True},
        "last_error": "",
    }


def _install_stub_runner(monkeypatch, runner: _StubRunner) -> _StubRunner:
    """Подменяет сборку планировщика — сервер поднимет заглушку вместо потока."""
    monkeypatch.setattr(server_module, "build_companion_runner",
                        lambda config_path: (runner, ""))
    return runner


def _hide_companion(monkeypatch) -> None:
    """Изображает свободную редакцию: пакета канала рядом нет."""
    monkeypatch.setattr(server_module, "_companion", None)
    monkeypatch.setattr(server_module, "_companion_runner", None)


# --- помощники поднятия хаба (те же, что в tests/test_hub_server.py) ----------


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


def _write_config(tmp_path, *, registry_path, companion: CompanionSettings = None):
    config_path = tmp_path / "standkit-hub.json"
    cfg = HubConfig(registry_path=str(registry_path))
    if companion is not None:
        cfg.companion = companion
    cfg.save(config_path)
    return config_path


def _start_hub(tmp_path, *, companion: CompanionSettings = None):
    registry_path = _write_registry(tmp_path)
    config_path = _write_config(tmp_path, registry_path=registry_path, companion=companion)
    session_token = generate_session_token()

    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path,
                              session_token=session_token)
    port = httpd.server_address[1]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _wait_for_port(port)

    return f"http://127.0.0.1:{port}", session_token, config_path, httpd


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"хаб не поднялся на порту {port} за {timeout}s")


def _request(base_url, path, *, token=None, cookie_token=None, method="GET",
             body=None, origin=None):
    """Возвращает ``(код, тело-как-dict, сырой текст тела)``.

    Сырой текст нужен проверке «конверт не утёк»: искать подстроку в
    сериализованном ответе надёжнее, чем обходить структуру, — она поймает
    утечку и в неожиданном вложенном поле.
    """
    req = urllib.request.Request(base_url + path, method=method)
    if token is not None:
        req.add_header("X-Standkit-Token", token)
    if cookie_token is not None:
        req.add_header("Cookie", f"standkit_session={cookie_token}")
    if origin is not None:
        req.add_header("Origin", origin)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req.add_header("Content-Type", "application/json")

    def _maybe_json(payload, headers):
        content_type = headers.get("Content-Type", "")
        if not payload or "application/json" not in content_type:
            return {}
        return json.loads(payload)

    try:
        with urllib.request.urlopen(req, data=data, timeout=5.0) as resp:
            payload = resp.read().decode("utf-8")
            return resp.status, _maybe_json(payload, resp.headers), payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8")
        return exc.code, _maybe_json(payload, exc.headers), payload


def _post(base_url, path, token, *, body=None):
    """Корректная мутация: заголовок токена + локальный Origin самого хаба."""
    return _request(base_url, path, token=token, method="POST", body=body,
                    origin=base_url)


# ======================================================================================
# /api/version: редакция
# ======================================================================================


def test_version_reports_companion_edition(tmp_path):
    """Пакет канала установлен — ядро обязано это показать, не сверяясь ни с чем ещё."""
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/version", token=token)

    assert status == 200
    assert body["edition"] == "companion"
    assert body["companion_version"] == standkit_companion.__version__
    # Существующие поля не тронуты — их уже читает модалка «О программе».
    assert body["name"] == "BPMkitStand"
    assert body["version"]


def test_version_reports_free_edition_without_module(tmp_path, monkeypatch):
    """Пакета нет — свободная редакция, и никакого ``companion_version``."""
    _hide_companion(monkeypatch)
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/version", token=token)

    assert status == 200
    assert body["edition"] == "free"
    assert "companion_version" not in body
    assert body["version"]


# ======================================================================================
# Авторизация и CSRF-контур
# ======================================================================================


def test_companion_status_without_token_is_unauthorized(tmp_path):
    base_url, *_ = _start_hub(tmp_path)
    status, _, _ = _request(base_url, "/api/companion/status")
    assert status == 401


@pytest.mark.parametrize("path", sorted(ACTION_ROUTES))
def test_companion_action_without_header_token_is_forbidden(tmp_path, path):
    """Cookie на мутации НЕ достаточно: нужен явный заголовок (double-submit)."""
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, path, cookie_token=token, method="POST",
                               origin=base_url)
    assert status == 403
    assert "error" in body


@pytest.mark.parametrize("path", sorted(ACTION_ROUTES))
def test_companion_action_with_foreign_origin_is_forbidden(tmp_path, path):
    """Чужой ``Origin`` — отказ даже с правильным токеном (anti-CSRF)."""
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, path, token=token, method="POST",
                               origin="http://evil.example")
    assert status == 403
    assert "error" in body


# ======================================================================================
# Свободная редакция: 503, а не 404
# ======================================================================================


def test_free_edition_status_is_503_with_human_error(tmp_path, monkeypatch):
    _hide_companion(monkeypatch)
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _request(base_url, "/api/companion/status", token=token)

    assert status == 503
    assert body["edition"] == "free"
    assert "свободная редакция" in body["error"].lower()


@pytest.mark.parametrize("path", sorted(ACTION_ROUTES))
def test_free_edition_actions_are_503_not_404(tmp_path, monkeypatch, path):
    """503 «возможности нет», а НЕ 404 «адреса нет»: разница видна пользователю."""
    _hide_companion(monkeypatch)
    base_url, token, *_ = _start_hub(tmp_path)
    status, body, _ = _post(base_url, path, token)

    assert status == 503, path
    assert body["edition"] == "free"
    assert body["error"] == server_module.COMPANION_UNAVAILABLE_MESSAGE


def test_free_edition_does_not_start_runner(tmp_path, monkeypatch):
    """В свободной редакции фонового потока канала не существует вовсе."""
    _hide_companion(monkeypatch)
    _base_url, _token, _config_path, httpd = _start_hub(tmp_path)
    assert httpd.companion_runner is None


# ======================================================================================
# Выключенный канал: статус отвечает, действия — нет
# ======================================================================================


def test_status_is_200_when_channel_disabled(tmp_path):
    """Выключенный канал обязан объяснить себя, а не отдать ошибку."""
    base_url, token, _config_path, httpd = _start_hub(
        tmp_path, companion=CompanionSettings(enabled=False))

    # Выключенный рубильник = поток не поднимается вовсе.
    assert httpd.companion_runner is None

    status, body, _ = _request(base_url, "/api/companion/status", token=token)
    assert status == 200
    assert body["enabled"] is False
    assert body["edition"] == "companion"
    # Снимок с диска: планировщика нет, значит и «работает» неоткуда взяться.
    assert body["running"] is False
    assert "cycles" in body


@pytest.mark.parametrize("path", sorted(ACTION_ROUTES))
def test_actions_are_409_when_channel_disabled(tmp_path, monkeypatch, path):
    runner = _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, *_ = _start_hub(tmp_path,
                                     companion=CompanionSettings(enabled=False))
    status, body, _ = _post(base_url, path, token)

    assert status == 409, path
    assert body["error"] == server_module.COMPANION_DISABLED_MESSAGE
    assert body["enabled"] is False
    # И, главное, до канала запрос не дошёл — выключено значит выключено.
    assert runner.calls == []


# ======================================================================================
# Маппинг маршрут → действие раннера
# ======================================================================================


@pytest.mark.parametrize("path,action", sorted(ACTION_ROUTES.items()))
def test_route_calls_expected_runner_action(tmp_path, monkeypatch, path, action):
    runner = _install_stub_runner(monkeypatch, _StubRunner(result={"done": action}))
    base_url, token, *_ = _start_hub(tmp_path)

    status, body, _ = _post(base_url, path, token)

    assert status == 200, body
    assert runner.calls == [(action, None)]
    assert body["ok"] is True
    assert body["result"] == {"done": action}
    # Свежий статус едет вместе с результатом — фронту не нужен второй запрос.
    assert body["status"]["edition"] == "companion"
    assert "actions" in body["status"]


@pytest.mark.parametrize("path,action", [
    ("/api/companion/stage-update", "stage_update"),
    ("/api/companion/rollback", "rollback"),
])
def test_version_from_body_reaches_runner(tmp_path, monkeypatch, path, action):
    runner = _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, *_ = _start_hub(tmp_path)

    status, _body, _ = _post(base_url, path, token, body={"version": "0.307.0"})

    assert status == 200
    assert runner.calls == [(action, "0.307.0")]


def test_empty_version_is_normalized_to_none(tmp_path, monkeypatch):
    """Пустая строка из формы — это «версия не выбрана», а не версия ``""``."""
    runner = _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, *_ = _start_hub(tmp_path)

    status, _body, _ = _post(base_url, "/api/companion/stage-update", token,
                             body={"version": "   "})

    assert status == 200
    assert runner.calls == [("stage_update", None)]


def test_non_string_version_is_rejected(tmp_path, monkeypatch):
    runner = _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, *_ = _start_hub(tmp_path)

    status, body, _ = _post(base_url, "/api/companion/stage-update", token,
                            body={"version": 307})

    assert status == 400
    assert "version" in body["error"]
    assert runner.calls == []


# ======================================================================================
# Классификация отказов канала
# ======================================================================================


@pytest.mark.parametrize("kind,expected", [
    ("no_license", 402),
    ("revoked", 402),
    ("expired", 402),
    ("invalid_envelope", 402),
    ("signature_invalid", 402),
    ("not_yet_valid", 402),
    ("context_unavailable", 503),
    ("nothing_staged", 409),
    ("nothing_to_rollback", 409),
    ("offline", 502),
    ("integrity_mismatch", 502),
    ("artifact_signature_invalid", 502),
    ("unknown", 502),
])
def test_companion_error_kind_maps_to_http_status(tmp_path, monkeypatch, kind, expected):
    error = ChannelError("что-то пошло не так", kind=kind, detail="подробности отказа")
    _install_stub_runner(monkeypatch, _StubRunner(error=error))
    base_url, token, *_ = _start_hub(tmp_path)

    status, body, _ = _post(base_url, "/api/companion/sync", token)

    assert status == expected, f"{kind} → {status}"
    assert body["kind"] == kind
    # Ключ error читает фронт (handleResponse в app.js) — он обязателен и
    # обязан быть человеческим текстом, а не машинным кодом.
    assert body["error"]
    assert "что-то пошло не так" in body["error"]
    assert body["detail"] == "подробности отказа"
    assert isinstance(body["retriable"], bool)
    assert isinstance(body["user_visible"], bool)


def test_error_payload_carries_channel_decision_about_retry(tmp_path, monkeypatch):
    """``retriable``/``user_visible`` берутся у канала, а не сочиняются хабом."""
    _install_stub_runner(monkeypatch, _StubRunner(
        error=ChannelError("лицензия отозвана", kind="revoked")))
    base_url, token, *_ = _start_hub(tmp_path)

    _status, body, _ = _post(base_url, "/api/companion/sync", token)

    assert body["retriable"] is False
    assert body["user_visible"] is True


def test_unexpected_exception_is_500_without_traceback(tmp_path, monkeypatch):
    _install_stub_runner(monkeypatch, _StubRunner(error=RuntimeError("диск отвалился")))
    base_url, token, *_ = _start_hub(tmp_path)

    status, body, raw = _post(base_url, "/api/companion/apply-update", token)

    assert status == 500
    assert "RuntimeError" in body["error"]
    # Трейсбек в ответ не попадает: это пути установки и чужой код в теле,
    # которое пользователь перешлёт в поддержку.
    assert "Traceback" not in raw
    assert "File \"" not in raw


# ======================================================================================
# Лицензионный конверт наружу не выходит
# ======================================================================================


def test_license_envelope_never_leaks_into_responses(tmp_path, monkeypatch):
    """Ни статус, ни результат действия, ни эхо запроса не содержат конверт.

    Проверяется поиском подстроки в СЫРОМ теле ответа: обход структуры пропустил
    бы утечку во вложенном поле, которого тест не знает. Отдельно проверяется,
    что хаб не возвращает присланное тело обратно — самый дешёвый способ
    случайно опубликовать секрет, приехавший в запросе.
    """
    runner = _StubRunner(result={"applied": 2})
    # У «раннера» конверт есть — как и у настоящего; наружу он не отдаёт его ни
    # в статусе, ни в результате.
    runner.envelope = FAKE_ENVELOPE
    _install_stub_runner(monkeypatch, runner)
    base_url, token, *_ = _start_hub(tmp_path)

    _status, _body, status_raw = _request(base_url, "/api/companion/status", token=token)
    assert FAKE_ENVELOPE not in status_raw

    code, body, action_raw = _post(base_url, "/api/companion/stage-update", token,
                                   body={"version": "0.307.0", "envelope": FAKE_ENVELOPE})
    assert code == 200
    assert FAKE_ENVELOPE not in action_raw
    assert "envelope" not in json.dumps(body, ensure_ascii=False)

    # То же самое на пути ошибки: текст отказа собирается каналом и конверта
    # не содержит, а тело запроса в ответ не попадает вовсе.
    _install_stub_runner(monkeypatch, _StubRunner(
        error=ChannelError("конверт не распознан", kind="invalid_envelope")))
    base_url2, token2, *_ = _start_hub(tmp_path / "second")
    code2, _body2, error_raw = _post(base_url2, "/api/companion/sync", token2,
                                     body={"envelope": FAKE_ENVELOPE})
    assert code2 == 402
    assert FAKE_ENVELOPE not in error_raw


def test_settings_response_never_contains_envelope(tmp_path):
    """Секция настроек канала — только политика, никаких секретов."""
    base_url, token, *_ = _start_hub(tmp_path)
    _status, body, raw = _request(base_url, "/api/settings", token=token)

    assert "companion" in body
    assert "envelope" not in raw
    assert set(body["companion"]) == {
        "enabled", "backend_url", "mcp_cli", "patterns", "releases", "revocations",
        "auto_stage_release", "require_pattern_signature",
    }


# ======================================================================================
# POST /api/settings со вложенной секцией канала
# ======================================================================================


def test_settings_post_saves_nested_companion_section(tmp_path, monkeypatch):
    runner = _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, config_path, _httpd = _start_hub(tmp_path)

    payload = {"companion": {
        "enabled": True,
        "backend_url": "https://updates.example",
        "mcp_cli": "python -m bpmkit",
        "patterns": {"enabled": True, "interval_sec": 3600},
        "releases": {"enabled": True, "interval_sec": 86400},
        "revocations": {"enabled": False, "interval_sec": 900},
        "auto_stage_release": True,
        "require_pattern_signature": True,
    }}
    status, body, _ = _post(base_url, "/api/settings", token, body=payload)

    assert status == 200
    assert body["companion"]["backend_url"] == "https://updates.example"
    assert body["companion"]["mcp_cli"] == "python -m bpmkit"
    assert body["companion"]["auto_stage_release"] is True
    assert body["companion"]["require_pattern_signature"] is True
    assert body["companion"]["releases"]["enabled"] is True
    assert body["companion"]["revocations"] == {"enabled": False, "interval_sec": 900}

    # Сохранено НА ДИСК, а не только отражено в ответе.
    saved = HubConfig.load(config_path)
    assert saved.companion.backend_url == "https://updates.example"
    assert saved.companion.patterns.interval_sec == 3600

    # Планировщику сказано не досыпать интервал: человек только что включил
    # цикл и ждёт первого прогона сейчас, а не через сутки.
    assert runner.pokes, "poke() раннера не вызван после сохранения настроек"


@pytest.mark.parametrize("cycle,too_small,minimum", [
    ("patterns", 5, 300),
    ("releases", 60, 3600),
    ("revocations", 1, 300),
])
def test_settings_post_clamps_interval_below_minimum(tmp_path, monkeypatch,
                                                     cycle, too_small, minimum):
    """Интервал ниже минимума КЛАМПИТСЯ, а не сохраняется как прислали.

    Слишком частый опрос лицензированного эндпоинта выглядит на стороне
    издателя как перебор ключа — граница обязана держаться на сервере, а не
    только в форме, которую легко обойти.
    """
    _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, config_path, _httpd = _start_hub(tmp_path)

    status, body, _ = _post(base_url, "/api/settings", token,
                            body={"companion": {cycle: {"enabled": True,
                                                        "interval_sec": too_small}}})

    assert status == 200
    assert body["companion"][cycle]["interval_sec"] == minimum
    assert getattr(HubConfig.load(config_path).companion, cycle).interval_sec == minimum


def test_settings_post_partial_companion_section_keeps_the_rest(tmp_path, monkeypatch):
    """Частичная секция не обнуляет непереданное.

    Плоский ``dict.update`` заменил бы секцию целиком, а
    ``CompanionSettings.from_dict`` достроил бы недостающее ДЕФОЛТАМИ — адрес
    бэкенда и путь к CLI исчезли бы молча, под сообщением «Настройки сохранены».
    """
    _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, config_path, _httpd = _start_hub(tmp_path, companion=CompanionSettings(
        enabled=True,
        backend_url="https://updates.example",
        mcp_cli="C:/BPMkit/bpmkit.exe",
        patterns=CompanionCycle(enabled=True, interval_sec=3600),
    ))

    status, body, _ = _post(base_url, "/api/settings", token,
                            body={"companion": {"enabled": False}})

    assert status == 200
    assert body["companion"]["enabled"] is False
    assert body["companion"]["backend_url"] == "https://updates.example"
    assert body["companion"]["mcp_cli"] == "C:/BPMkit/bpmkit.exe"
    assert body["companion"]["patterns"]["interval_sec"] == 3600
    saved = HubConfig.load(config_path)
    assert saved.companion.backend_url == "https://updates.example"
    assert saved.companion.patterns.interval_sec == 3600


def test_settings_post_without_companion_key_does_not_touch_section(tmp_path, monkeypatch):
    """Форма, не приславшая секцию канала, не имеет права её изменить."""
    _install_stub_runner(monkeypatch, _StubRunner())
    base_url, token, config_path, _httpd = _start_hub(tmp_path, companion=CompanionSettings(
        enabled=True, backend_url="https://updates.example"))

    status, body, _ = _post(base_url, "/api/settings", token,
                            body={"refresh_interval_sec": 42})

    assert status == 200
    assert body["refresh_interval_sec"] == 42
    assert body["companion"]["backend_url"] == "https://updates.example"
    assert HubConfig.load(config_path).companion.backend_url == "https://updates.example"


# ======================================================================================
# Жизненный цикл планировщика
# ======================================================================================


def test_runner_is_started_when_channel_enabled(tmp_path, monkeypatch):
    runner = _install_stub_runner(monkeypatch, _StubRunner())
    _base_url, _token, _config_path, httpd = _start_hub(tmp_path)
    assert httpd.companion_runner is runner


def test_server_close_stops_companion_runner(tmp_path, monkeypatch):
    """Забытый поток тикает дальше и ломает соседние тесты — гасим до сокета."""
    runner = _install_stub_runner(monkeypatch, _StubRunner())
    _base_url, _token, _config_path, httpd = _start_hub(tmp_path)

    httpd.shutdown()
    httpd.server_close()

    assert runner.stopped == 1
    assert httpd.companion_runner is None


def test_failed_runner_start_does_not_break_the_hub(tmp_path, monkeypatch):
    """Канал не поднялся — хаб обязан работать, а причина осесть в состоянии."""
    monkeypatch.setattr(server_module, "build_companion_runner",
                        lambda config_path: (None, "канал обновлений не запущен: тест"))
    base_url, token, _config_path, httpd = _start_hub(tmp_path)

    assert httpd.companion_runner is None
    assert "не запущен" in httpd.companion_error

    # Управление стендами не затронуто.
    status, body, _ = _request(base_url, "/api/stands", token=token)
    assert status == 200
    assert "stands" in body

    # Статус канала по-прежнему отвечает — снимком с диска.
    status, body, _ = _request(base_url, "/api/companion/status", token=token)
    assert status == 200
    assert body["running"] is False


# ======================================================================================
# Фронтенд: статические гарды вкладки «Обновления»
# ======================================================================================
#
# Браузера в наборе нет, поэтому проверяются те свойства разметки, потеря которых
# ломает вкладку молча: сама вкладка, соответствие кнопок маршрутам API,
# скрытие в компактном окне и отсутствие внешних ресурсов.


def test_companion_tab_exists_in_index_html():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'data-tab="companion"' in html, "кнопка вкладки «Обновления» потеряна"
    assert 'id="tab-companion"' in html, "панель вкладки «Обновления» потеряна"
    # Секция настроек канала — свёрнутая группа в общей форме настроек.
    assert 'id="settings-companion"' in html
    for field in ("companion_enabled", "companion_backend_url", "companion_mcp_cli",
                  "companion_patterns_interval_min", "companion_releases_interval_hours",
                  "companion_revocations_interval_min", "companion_auto_stage_release",
                  "companion_require_pattern_signature"):
        assert f'name="{field}"' in html, f"поле {field} пропало из формы настроек"


def test_companion_buttons_match_api_routes():
    """Каждая кнопка вкладки — существующее действие, и наоборот.

    Разъезд этих двух списков не виден ни одному тесту сервера: фронт просто
    получал бы 404 на нажатие, а сервер — маршрут, который никто не зовёт.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    js = (WEB_DIR / "app.js").read_text(encoding="utf-8")
    for path, action in ACTION_ROUTES.items():
        assert f'data-companion-action="{action}"' in html, f"нет кнопки для {action}"
        assert f'"{path}"' in js, f"путь {path} не известен фронту"
        assert f"{action}: \"{path}\"" in js, f"кнопка {action} не связана с {path}"


def test_companion_tab_is_hidden_in_compact_view():
    """Окно-виджет показывает только стенды: вкладка канала не должна вылезать."""
    css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    assert '[data-view="compact"] #tab-companion' in css
    assert '[data-view="compact"] .tab-btn[data-tab="companion"]' in css


def test_companion_ui_has_no_external_resources():
    """Ни CDN, ни внешних шрифтов: дашборд обязан работать в офлайн-сети."""
    for name in ("index.html", "app.js", "style.css"):
        text = (WEB_DIR / name).read_text(encoding="utf-8")
        for forbidden in ("<script src=\"http", "@import", "fonts.googleapis",
                          "cdn.jsdelivr", "unpkg.com"):
            assert forbidden not in text, f"{name}: внешний ресурс {forbidden}"
        # Стили подключаются только со своего origin.
        assert 'rel="stylesheet" href="http' not in text
