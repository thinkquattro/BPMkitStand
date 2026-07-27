"""
Тесты «быстрого прогрева» дашборда: снапшот без проб (``?probe=0``), фоновый
поллер с кэшем состояния, кэш конфига/реестра, валидаторы кэша статики,
фиксированный порт с откатом и тема, приезжающая из конфига.

Реальная сеть здесь не используется: health-пробы и федеративный клиент
подменяются monkeypatch'ем, единственные настоящие сокеты — сам хаб на
loopback (тот же приём, что в tests/test_hub_server.py).
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import standkit_hub.client as hub_client_module
import standkit_hub.server as server_module
from standkit.models import ProbeState, Stand, StandStatus
from standkit.registry import Registry
from standkit_hub.config import HubConfig, normalize_theme
from standkit_hub.poller import StatusPoller, StatusSnapshot
from standkit_hub.security import generate_session_token
from standkit_hub.server import DEFAULT_HUB_PORT, bind_hub_server, create_hub_server


# --------------------------------------------------------------------------
# Вспомогательное
# --------------------------------------------------------------------------


def _write_registry(tmp_path, names=("alpha", "beta", "gamma")):
    registry_path = tmp_path / "projects.json"
    registry = Registry(
        path=registry_path,
        default=names[0],
        stands={
            name: Stand(name=name, stand_dir=str(tmp_path / name), stand_port=5000 + idx)
            for idx, name in enumerate(names)
        },
    )
    registry.save()
    return registry_path


def _write_config(tmp_path, registry_path, **kwargs):
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path), **kwargs).save(config_path)
    return config_path


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"хаб не поднялся на порту {port} за {timeout}s")


def _get(base_url: str, path: str, *, token: str, headers: dict | None = None):
    req = urllib.request.Request(base_url + path, method="GET")
    req.add_header("X-Standkit-Token", token)
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


# --------------------------------------------------------------------------
# ?probe=0 — слепок реестра без единой сетевой пробы
# --------------------------------------------------------------------------


def test_probe_zero_returns_registry_without_calling_probes(tmp_path, monkeypatch):
    """
    Главная гарантия быстрой первой отрисовки: при ``?probe=0`` не должно
    произойти НИ ОДНОЙ пробы, сколько бы недоступных стендов ни было в реестре.
    """
    registry_path = _write_registry(tmp_path)
    config_path = _write_config(tmp_path, registry_path)

    calls = []

    def _never(self):
        calls.append("status_all")
        raise AssertionError("при probe=0 пробы выполняться не должны")

    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", _never)

    snapshot = server_module.build_snapshot(config_path, probe=False)
    payload = snapshot.to_payload()

    assert calls == []
    assert payload["probed"] is False
    assert [s["name"] for s in payload["stands"]] == ["alpha", "beta", "gamma"]


def test_probe_one_calls_probes_and_marks_snapshot_probed(tmp_path, monkeypatch):
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path)

    monkeypatch.setattr(
        hub_client_module.FederatedClient,
        "status_all",
        lambda self: {"alpha": StandStatus(name="alpha", process=ProbeState.OK)},
    )

    payload = server_module.build_snapshot(config_path, probe=True).to_payload()
    assert payload["probed"] is True


def test_api_stands_probe_zero_is_accepted_and_probe_two_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        hub_client_module.FederatedClient,
        "status_all",
        lambda self: {},
    )
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path)
    token = generate_session_token()

    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"
    try:
        status, _, _ = _get(base_url, "/api/stands?probe=0", token=token)
        assert status == 200

        # Неизвестное значение — честный 400, а не молчаливая трактовка как 1.
        status, _, _ = _get(base_url, "/api/stands?probe=2", token=token)
        assert status == 400
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# Параллельный опрос: порядок результатов — по реестру, а не по завершению
# --------------------------------------------------------------------------


def test_status_all_preserves_registry_order_despite_uneven_probe_time(tmp_path, monkeypatch):
    """
    Пробы идут в пуле потоков, поэтому завершаются в произвольном порядке.
    Порядок в ответе обязан остаться порядком реестра, иначе строки таблицы
    будут прыгать между обновлениями.
    """
    registry_path = _write_registry(tmp_path, names=("alpha", "beta", "gamma"))
    registry = Registry.load(registry_path)

    # Первый стенд «отвечает» дольше всех — при наивной сборке результатов
    # по мере готовности он уехал бы в конец.
    delays = {"alpha": 0.15, "beta": 0.05, "gamma": 0.0}

    def _fake_check(stand, **kwargs):
        time.sleep(delays[stand.name])
        return StandStatus(name=stand.name, process=ProbeState.OK)

    monkeypatch.setattr(hub_client_module.health, "check_stand", _fake_check)

    started = time.monotonic()
    result = hub_client_module.FederatedClient(registry).status_all()
    elapsed = time.monotonic() - started

    assert list(result.keys()) == ["alpha", "beta", "gamma"]
    # Последовательно это заняло бы 0.20 с; в пуле — примерно самый долгий.
    assert elapsed < 0.19


# --------------------------------------------------------------------------
# Кэш конфига и реестра: не перечитывать JSON на каждый запрос
# --------------------------------------------------------------------------


def test_config_and_registry_are_cached_until_file_changes(tmp_path, monkeypatch):
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path)

    reads = []
    original_load = HubConfig.load

    def _counting_load(cls_path=None, *args, **kwargs):
        reads.append(str(cls_path))
        return original_load(cls_path, *args, **kwargs)

    monkeypatch.setattr(server_module.HubConfig, "load", staticmethod(_counting_load))

    server_module._load_config(config_path)
    server_module._load_config(config_path)
    server_module._load_config(config_path)
    assert len(reads) == 1, "конфиг должен читаться с диска один раз, пока файл не изменился"

    # Файл изменился — кэш обязан это заметить (реестр правят и снаружи:
    # руками, через MCP BPMkit, при регистрации стенда).
    time.sleep(0.01)
    HubConfig(registry_path=str(registry_path), refresh_interval_sec=33).save(config_path)
    fresh = server_module._load_config(config_path)
    assert fresh.refresh_interval_sec == 33
    assert len(reads) == 2


# --------------------------------------------------------------------------
# Фоновый поллер
# --------------------------------------------------------------------------


def test_poller_publishes_snapshot_and_bumps_version():
    builds = []

    def _build():
        builds.append(1)
        return StatusSnapshot(stands=[{"name": "alpha"}], probed=True, generated_at=time.time())

    poller = StatusPoller(build=_build, interval=lambda: 0.05)
    poller.start()
    try:
        deadline = time.monotonic() + 3.0
        while poller.snapshot() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert poller.snapshot() is not None
        first_version = poller.version

        deadline = time.monotonic() + 3.0
        while poller.version == first_version and time.monotonic() < deadline:
            time.sleep(0.01)
        assert poller.version > first_version, "второй тик должен опубликовать новую версию"
    finally:
        poller.stop()

    assert poller.is_stopping()
    assert len(builds) >= 2


def test_poller_survives_build_failure():
    """
    Сборка снапшота может упасть (недоступен реестр, битый конфиг). Поток
    поллера обязан это пережить: демон, который умер от одной ошибки, хуже,
    чем отсутствие демона.
    """
    calls = {"n": 0}

    def _build():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("реестр временно недоступен")
        return StatusSnapshot(stands=[], probed=True, generated_at=time.time())

    poller = StatusPoller(build=_build, interval=lambda: 0.02)
    poller.start()
    try:
        deadline = time.monotonic() + 3.0
        while calls["n"] < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert calls["n"] >= 2, "после ошибки поллер должен продолжить работу"
    finally:
        poller.stop()


def test_server_close_stops_poller(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path)

    httpd = create_hub_server(
        "127.0.0.1", 0, config_path=config_path, session_token=generate_session_token()
    )
    poller = getattr(httpd, "status_poller", None)
    assert poller is not None, "хаб должен поднимать фоновый поллер"

    httpd.server_close()
    assert poller.is_stopping(), "server_close обязан останавливать фоновый поток"


def test_stale_snapshot_is_not_served_after_registry_changes(tmp_path, monkeypatch):
    """
    Регрессия: снапшот поллера собран ДО изменения реестра, и /api/stands
    отдавал устаревший состав стендов — зарегистрированный стенд не появлялся
    в списке до следующего тика. Реестр правят и мимо хаба (руками, из MCP
    BPMkit), поэтому проверяем отпечаток источников, а не только возраст.
    """
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path)

    before = server_module.build_snapshot(config_path, probe=True)
    assert [s["name"] for s in before.stands] == ["alpha"]

    # Стенд добавили в реестр после сборки снапшота.
    time.sleep(0.01)
    _write_registry(tmp_path, names=("alpha", "delta"))

    assert before.sources != server_module._snapshot_sources(config_path), (
        "изменение реестра обязано менять отпечаток источников"
    )

    after = server_module.build_snapshot(config_path, probe=False)
    assert [s["name"] for s in after.stands] == ["alpha", "delta"]


def test_snapshot_sources_stable_when_files_untouched(tmp_path):
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path)
    first = server_module._snapshot_sources(config_path)
    assert first == server_module._snapshot_sources(config_path)
    assert first, "отпечаток существующих файлов не должен быть пустым"


def test_snapshot_payload_reports_age(tmp_path):
    snapshot = StatusSnapshot(stands=[], probed=True, generated_at=time.time() - 5.0)
    payload = snapshot.to_payload()
    assert payload["age_sec"] >= 5.0
    assert payload["generated_at"] > 0


# --------------------------------------------------------------------------
# Фиксированный порт с откатом на эфемерный
# --------------------------------------------------------------------------


def test_bind_falls_back_to_ephemeral_when_port_is_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path)

    # Занимаем конкретный порт «чужим» сокетом и просим хаб именно его.
    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    busy_port = squatter.getsockname()[1]

    reported = []
    try:
        httpd = bind_hub_server(
            "127.0.0.1",
            busy_port,
            config_path=config_path,
            session_token=generate_session_token(),
            poll=False,
            on_fallback=lambda port, exc: reported.append(port),
        )
    finally:
        squatter.close()

    try:
        actual = httpd.server_address[1]
        assert actual != busy_port, "занятый порт должен приводить к откату, а не к падению"
        assert actual > 0
        assert reported == [busy_port], "вызывающий должен узнать, какой порт был занят"
    finally:
        httpd.server_close()


def test_default_hub_port_is_fixed():
    """
    Значение важно само по себе: origin (схема+хост+порт) — ключ localStorage
    и браузерного кэша, поэтому порт не должен «плавать» между запусками.
    """
    assert DEFAULT_HUB_PORT == 8770


# --------------------------------------------------------------------------
# Тема из конфига
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("light", "light"),
        ("DARK", "dark"),
        ("  auto  ", "auto"),
        ("розовая", "auto"),
        (None, "auto"),
        (42, "auto"),
    ],
)
def test_normalize_theme(raw, expected):
    assert normalize_theme(raw) == expected


def test_theme_survives_config_roundtrip(tmp_path):
    path = tmp_path / "hub.json"
    HubConfig(theme="light").save(path)
    assert HubConfig.load(path).theme == "light"


def test_index_html_carries_theme_from_config(tmp_path, monkeypatch):
    """
    Тема подставляется в data-theme прямо при отдаче index.html — иначе тёмная
    тема успевает мигнуть до выполнения JS.
    """
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path, theme="light")
    token = generate_session_token()

    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    try:
        status, body, headers = _get(f"http://127.0.0.1:{port}", f"/?t={token}", token=token)
        assert status == 200
        assert 'data-theme="light"' in body
        # У страницы с сессионным токеном в <meta> кэша быть не должно.
        assert "no-store" in headers.get("Cache-Control", "")
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# Валидаторы кэша статики
# --------------------------------------------------------------------------


def test_static_is_cacheable_and_returns_304_on_if_none_match(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    registry_path = _write_registry(tmp_path, names=("alpha",))
    config_path = _write_config(tmp_path, registry_path)
    token = generate_session_token()

    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    base_url = f"http://127.0.0.1:{port}"
    try:
        status, body, headers = _get(base_url, "/static/app.js", token=token)
        assert status == 200
        assert body
        etag = headers.get("ETag")
        assert etag, "статика должна отдаваться с ETag — иначе кэш браузера бесполезен"
        assert headers.get("Cache-Control")

        status, body, _ = _get(
            base_url, "/static/app.js", token=token, headers={"If-None-Match": etag}
        )
        assert status == 304
        assert body == ""

        # Чужой ETag — полноценный ответ, а не 304.
        status, body, _ = _get(
            base_url, "/static/app.js", token=token, headers={"If-None-Match": '"deadbeef-1"'}
        )
        assert status == 200
        assert body
    finally:
        httpd.shutdown()
        httpd.server_close()
