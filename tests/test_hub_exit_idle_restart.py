# -*- coding: utf-8 -*-
"""
Д-3 / GAP-276 — штатный выход диспетчера, автовыход по простою, перезапуск
без повышения прав, детект рассинхрона версий и человеческий 404.

Что здесь принципиально проверяется отдельно от остальных наборов:

* таймер простоя — с ПОДСТАВНЫМИ часами: 30-минутный дефолт нельзя проверить
  ожиданием, а глобальная подмена ``time.monotonic`` испортила бы соседние
  тесты (её использует и поллер, и наблюдатель стоп-запроса);
* условие простоя — конъюнкция, и каждое слагаемое ломается отдельно: тест,
  проверяющий только «и то и другое сразу», пропустил бы замену И на ИЛИ;
* выход — что он НЕ трогает детей, и что мьютекс освобождается ДО завершения
  (ради этого его и нажимают, см. HubHTTPServer.request_self_shutdown).
"""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from standkit_hub import mutex as hub_mutex
from standkit_hub import server as hub_server
from standkit_hub.config import HubConfig, normalize_idle_shutdown_min
from standkit_hub.security import generate_session_token
from standkit_hub.server import _IdleShutdownWatcher, create_hub_server, on_disk_standkit_version


# --------------------------------------------------------------------------
# Нормализация настройки
# --------------------------------------------------------------------------


def test_idle_shutdown_min_default_is_30():
    assert HubConfig().idle_shutdown_min == 30


def test_idle_shutdown_zero_passes_through_as_disabled():
    # 0 — осознанное «никогда не закрывать», а не сбой разбора: оно обязано
    # доехать до конфига как есть, иначе выключить таймер станет нечем.
    assert normalize_idle_shutdown_min(0) == 0
    assert HubConfig.from_dict({"idle_shutdown_min": 0}).idle_shutdown_min == 0


@pytest.mark.parametrize("bad", ["", "тридцать", None, -5, -1, True, False, [], {}])
def test_idle_shutdown_garbage_falls_back_to_default_not_to_zero(bad):
    # Откат именно на ДЕФОЛТ: молчаливое «выключено» из-за опечатки человек
    # заметил бы только через сутки забытого процесса.
    assert normalize_idle_shutdown_min(bad) == 30


def test_idle_shutdown_survives_config_roundtrip(tmp_path):
    path = tmp_path / "standkit-hub.json"
    HubConfig(idle_shutdown_min=7).save(path)
    assert HubConfig.load(path).idle_shutdown_min == 7
    assert HubConfig.load(path).to_dict()["idle_shutdown_min"] == 7


# --------------------------------------------------------------------------
# Таймер простоя — на подставных часах
# --------------------------------------------------------------------------


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeServer:
    """Минимальный двойник HubHTTPServer для сторожа простоя: он трогает
    ровно три вещи — счётчик SSE, снапшот поллера и request_self_shutdown."""

    def __init__(self, *, sse: int = 0, stands=None, poller: bool = True) -> None:
        self._sse = sse
        self.shutdown_calls = 0
        self.status_poller = _FakePoller(stands) if poller else None

    def sse_client_count(self) -> int:
        return self._sse

    def set_sse(self, value: int) -> None:
        self._sse = value

    def request_self_shutdown(self) -> bool:
        self.shutdown_calls += 1
        return True


class _FakePoller:
    def __init__(self, stands) -> None:
        self._snapshot = None if stands is None else _FakeSnapshot(stands)

    def snapshot(self):
        return self._snapshot


class _FakeSnapshot:
    def __init__(self, stands) -> None:
        self.stands = stands


def _stand(state: str) -> dict:
    return {"name": "s", "process": {"state": state}}


def _watcher(tmp_path, server, clock, *, minutes: int = 30, desktop: bool = False):
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(run_dir=str(tmp_path / "run"), idle_shutdown_min=minutes).save(config_path)
    return _IdleShutdownWatcher(server, config_path, desktop=desktop, now=clock)


def test_idle_timer_fires_only_after_full_timeout(tmp_path):
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=[])
    watcher = _watcher(tmp_path, server, clock, minutes=30)

    watcher._tick()  # первый тик только ЗАПОМИНАЕТ момент начала простоя
    assert server.shutdown_calls == 0

    clock.advance(29 * 60)
    watcher._tick()
    assert server.shutdown_calls == 0, "29 минут — ещё не 30"

    clock.advance(2 * 60)
    watcher._tick()
    assert server.shutdown_calls == 1


def test_idle_timer_resets_when_activity_returns(tmp_path):
    # Отсчёт идёт от ПОСЛЕДНЕЙ активности, а не от старта хаба: иначе человек,
    # открывший вкладку на 29-й минуте, потерял бы диспетчер через минуту.
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=[])
    watcher = _watcher(tmp_path, server, clock, minutes=30)

    watcher._tick()
    clock.advance(29 * 60)
    server.set_sse(1)  # кто-то открыл дашборд
    watcher._tick()
    assert server.shutdown_calls == 0

    server.set_sse(0)  # и закрыл
    clock.advance(29 * 60)
    watcher._tick()   # отсчёт начался ЗАНОВО с этого момента
    clock.advance(29 * 60)
    watcher._tick()
    assert server.shutdown_calls == 0, "таймер обязан был сброситься активностью"

    clock.advance(2 * 60)
    watcher._tick()
    assert server.shutdown_calls == 1


def test_idle_timer_disabled_by_zero(tmp_path):
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=[])
    watcher = _watcher(tmp_path, server, clock, minutes=0)

    watcher._tick()
    clock.advance(10 * 60 * 60)
    watcher._tick()
    assert server.shutdown_calls == 0


def test_open_dashboard_alone_blocks_idle(tmp_path):
    # Первое слагаемое конъюнкции: стендов нет, но вкладку смотрят.
    clock = _FakeClock()
    server = _FakeServer(sse=1, stands=[])
    watcher = _watcher(tmp_path, server, clock, minutes=1)
    assert watcher.is_idle() is False

    watcher._tick()
    clock.advance(60 * 60)
    watcher._tick()
    assert server.shutdown_calls == 0


def test_running_stand_alone_blocks_idle(tmp_path):
    # Второе слагаемое: вкладку закрыли, но стенд работает — выйти сейчас
    # значит потерять управление им.
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=[_stand("running")])
    watcher = _watcher(tmp_path, server, clock, minutes=1)
    assert watcher.is_idle() is False

    watcher._tick()
    clock.advance(60 * 60)
    watcher._tick()
    assert server.shutdown_calls == 0


def test_stopped_stands_do_not_block_idle(tmp_path):
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=[_stand("stopped"), _stand("unknown")])
    watcher = _watcher(tmp_path, server, clock, minutes=1)
    assert watcher.is_idle() is True


def test_unknown_stand_state_without_poller_keeps_hub_alive(tmp_path):
    # Незнание трактуется в пользу «остаться»: ошибочно закрывшийся диспетчер
    # уносит с собой управление живыми стендами, ошибочно оставшийся — нет.
    clock = _FakeClock()
    server = _FakeServer(sse=0, poller=False)
    watcher = _watcher(tmp_path, server, clock, minutes=1)
    assert watcher.has_running_stands() is True
    assert watcher.is_idle() is False


def test_poller_without_first_snapshot_keeps_hub_alive(tmp_path):
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=None)
    watcher = _watcher(tmp_path, server, clock, minutes=1)
    assert watcher.is_idle() is False


def test_desktop_mode_ignores_sse_clients(tmp_path):
    # В режиме окна вкладки нет вовсе, признак «никто не смотрит» недостоверен —
    # условие сводится к стендам (см. docstring _IdleShutdownWatcher).
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=[])
    assert _watcher(tmp_path, server, clock, minutes=1, desktop=True).is_idle() is True

    busy = _FakeServer(sse=5, stands=[])
    assert _watcher(tmp_path, busy, clock, minutes=1, desktop=True).is_idle() is True

    with_stand = _FakeServer(sse=0, stands=[_stand("running")])
    assert _watcher(tmp_path, with_stand, clock, minutes=1, desktop=True).is_idle() is False


def test_idle_timeout_reread_from_config_without_restart(tmp_path):
    # Правка в форме настроек применяется без перезапуска хаба.
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=[])
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(run_dir=str(tmp_path / "run"), idle_shutdown_min=30).save(config_path)
    watcher = _IdleShutdownWatcher(server, config_path, now=clock)
    assert watcher.idle_timeout_sec() == 30 * 60

    hub_server.invalidate_caches()
    HubConfig(run_dir=str(tmp_path / "run"), idle_shutdown_min=1).save(config_path)
    hub_server.invalidate_caches()
    assert watcher.idle_timeout_sec() == 60


def test_broken_config_does_not_disable_the_timer(tmp_path):
    # Битый конфиг не имеет права МОЛЧА выключить автовыход — отдаём дефолт.
    clock = _FakeClock()
    server = _FakeServer(sse=0, stands=[])
    config_path = tmp_path / "standkit-hub.json"
    config_path.write_text("{не json", encoding="utf-8")
    hub_server.invalidate_caches()
    watcher = _IdleShutdownWatcher(server, config_path, now=clock)
    assert watcher.idle_timeout_sec() == 30 * 60


# --------------------------------------------------------------------------
# Счётчик SSE-клиентов
# --------------------------------------------------------------------------


def test_sse_counter_is_symmetric_and_never_negative(tmp_path):
    httpd, _, _ = _hub(tmp_path)
    try:
        assert httpd.sse_client_count() == 0
        httpd.sse_client_opened()
        httpd.sse_client_opened()
        assert httpd.sse_client_count() == 2
        httpd.sse_client_closed()
        assert httpd.sse_client_count() == 1
        httpd.sse_client_closed()
        httpd.sse_client_closed()  # лишний close (двойной finally при обрыве)
        assert httpd.sse_client_count() == 0, "отрицательный счётчик навсегда убедил бы сторожа, что клиентов нет"
    finally:
        _shutdown(httpd)


# --------------------------------------------------------------------------
# HTTP: выход, перезапуск, 404
# --------------------------------------------------------------------------


def _hub(tmp_path, *, poll=False):
    registry_path = tmp_path / "projects.json"
    registry_path.write_text('{"projects": {}}', encoding="utf-8")
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path), run_dir=str(tmp_path / "run")).save(config_path)
    hub_server.invalidate_caches()
    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token, poll=poll)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    return httpd, f"http://127.0.0.1:{port}", token


def _wait_for_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError(f"хаб не поднялся на порту {port} за {timeout}s")


def _shutdown(httpd) -> None:
    threading.Thread(target=httpd.shutdown, daemon=True).start()
    httpd.server_close()


def _request(url, *, token=None, method="GET", origin=None, build=None):
    req = urllib.request.Request(url, method=method)
    if token is not None:
        req.add_header("X-Standkit-Token", token)
        req.add_header("Cookie", f"standkit_session={token}")
    if origin is not None:
        req.add_header("Origin", origin)
    if build is not None:
        req.add_header("X-Standkit-Build", build)
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip().startswith("{") else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw.strip().startswith("{") else {})


def test_shutdown_requires_auth(tmp_path):
    httpd, base, token = _hub(tmp_path)
    try:
        status, _ = _request(f"{base}/api/hub/shutdown", method="POST")
        # 403, а не 401: у мутаций CSRF-проверка (double-submit) идёт ПЕРЕД
        # разбором токена — так же, как у всех остальных мутаций хаба.
        assert status == 403, "выход — мутация, анонимный POST обязан быть отвергнут"
        assert httpd.self_shutdown_requested is False
    finally:
        _shutdown(httpd)


def test_shutdown_rejects_foreign_origin(tmp_path):
    httpd, base, token = _hub(tmp_path)
    try:
        status, _ = _request(f"{base}/api/hub/shutdown", token=token, method="POST",
                             origin="http://evil.example")
        assert status == 403
        assert httpd.self_shutdown_requested is False
    finally:
        _shutdown(httpd)


def test_shutdown_answers_202_and_stops_the_hub(tmp_path, monkeypatch):
    httpd, base, token = _hub(tmp_path)
    stopped = threading.Event()
    monkeypatch.setattr(hub_server, "_schedule_shutdown",
                        lambda server, delay=0.0: stopped.set())
    try:
        status, data = _request(f"{base}/api/hub/shutdown", token=token, method="POST",
                                origin=base)
        # 202, а не 200: ответ обязан уйти ДО того, как сокет закроется, иначе
        # вкладка увидит обрыв и не отличит «вышли» от «упало».
        assert status == 202
        assert data == {"ok": True, "stopping": True}
        assert stopped.wait(3.0), "выход не дошёл до планирования shutdown"
        assert httpd.self_shutdown_requested is True
    finally:
        _shutdown(httpd)


def test_second_shutdown_is_409_not_a_second_stop(tmp_path, monkeypatch):
    httpd, base, token = _hub(tmp_path)
    calls = []
    monkeypatch.setattr(hub_server, "_schedule_shutdown",
                        lambda server, delay=0.0: calls.append(1))
    try:
        first, _ = _request(f"{base}/api/hub/shutdown", token=token, method="POST", origin=base)
        assert first == 202
        time.sleep(0.2)
        second, data = _request(f"{base}/api/hub/shutdown", token=token, method="POST", origin=base)
        assert second == 409
        assert "завершается" in data["error"]
        assert len(calls) == 1, "второй shutdown() по закрывающемуся серверу ничего не улучшит"
    finally:
        _shutdown(httpd)


def test_request_self_shutdown_releases_mutex_before_cleanup(tmp_path, monkeypatch):
    # Ради мьютекса кнопку «Выход» чаще всего и нажимают: пока он удерживается,
    # установщик BPMkit отказывается работать.
    httpd, base, token = _hub(tmp_path)
    order = []
    monkeypatch.setattr(hub_server, "release_hub_mutex",
                        lambda: order.append("mutex") or True)
    monkeypatch.setattr(hub_server._instance, "clear_state",
                        lambda path, pid=None: order.append("state"))
    monkeypatch.setattr(hub_server, "_schedule_shutdown",
                        lambda server, delay=0.0: order.append("shutdown"))
    httpd.instance_state_file = tmp_path / "run" / "state.json"
    try:
        assert httpd.request_self_shutdown() is True
        assert order == ["mutex", "state", "shutdown"]
        # Повторный вызов — no-op, а не вторая уборка.
        assert httpd.request_self_shutdown() is False
        assert order == ["mutex", "state", "shutdown"]
    finally:
        _shutdown(httpd)


def test_shutdown_prefers_on_stop_request_callback(tmp_path, monkeypatch):
    # desktop-режим: одного shutdown() мало — надо ещё закрыть окна pywebview,
    # и это делает тот же колбэк, что используется при перехвате порта.
    httpd, base, token = _hub(tmp_path)
    called = []
    httpd.on_stop_request = lambda: called.append("callback")
    monkeypatch.setattr(hub_server, "_schedule_shutdown",
                        lambda server, delay=0.0: called.append("schedule"))
    try:
        httpd.request_self_shutdown()
        assert called == ["callback"], "при живом колбэке прямой _schedule_shutdown не нужен"
    finally:
        httpd.on_stop_request = None
        _shutdown(httpd)


def test_restart_requires_auth_and_local_origin(tmp_path):
    httpd, base, token = _hub(tmp_path)
    try:
        status, _ = _request(f"{base}/api/hub/restart", method="POST")
        assert status == 403  # см. комментарий в test_shutdown_requires_auth
        status, _ = _request(f"{base}/api/hub/restart", token=token, method="POST",
                             origin="http://evil.example")
        assert status == 403
    finally:
        _shutdown(httpd)


def test_restart_spawns_new_process_without_uac(tmp_path, monkeypatch):
    httpd, base, token = _hub(tmp_path)
    spawned = {}
    runas_calls = []

    def _fake_spawn(cmd, cwd, log_path):
        spawned["cmd"] = list(cmd)
        return 4242

    monkeypatch.setattr(hub_server._platform, "spawn_hidden", _fake_spawn)
    monkeypatch.setattr(hub_server._elevation, "relaunch_elevated",
                        lambda *a, **kw: runas_calls.append(1))
    try:
        status, data = _request(f"{base}/api/hub/restart", token=token, method="POST", origin=base)
        assert status == 202
        assert data == {"ok": True, "restarting": True}
        assert not runas_calls, "обычный перезапуск не имеет права спрашивать UAC"
        # Переиспользованы те же параметры, что у elevated-пути.
        assert "--takeover" in spawned["cmd"]
        assert "--session-token-file" in spawned["cmd"]
        # Сверки учётной записи здесь нет: процесс заведомо порождается тем же
        # пользователем, спрашивать SID не у кого.
        assert "--initiator-sid" not in spawned["cmd"]
    finally:
        _shutdown(httpd)


def test_restart_rolls_back_state_when_spawn_fails(tmp_path, monkeypatch):
    # Без отката «requesting» застрял бы навсегда, и ВСЕ дальнейшие POST
    # получали бы 409 про перезапуск, которого уже не будет (GAP-311 M3).
    httpd, base, token = _hub(tmp_path)

    def _boom(cmd, cwd, log_path):
        raise OSError("не запустилось")

    monkeypatch.setattr(hub_server._platform, "spawn_hidden", _boom)
    try:
        status, data = _request(f"{base}/api/hub/restart", token=token, method="POST", origin=base)
        assert status == 500
        assert "не запустилось" in data["error"]
        # Откат делает `finally` обработчика — он выполняется ПОСЛЕ того, как
        # ответ ушёл в сокет, поэтому клиент может увидеть ещё не откаченное
        # состояние. Ждём коротко, а не проверяем мгновенно: гонка тут в
        # тесте, а не в коде.
        deadline = time.monotonic() + 3.0
        while httpd.restart_state is not None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert httpd.restart_state is None, "состояние обязано откатиться, иначе следующий POST — вечный 409"

        # И практическая сторона того же: следующий POST обязан снова дойти до
        # дела, а не получить 409 «перезапуск уже запрошен».
        again, _ = _request(f"{base}/api/hub/restart", token=token, method="POST", origin=base)
        assert again == 500, "повторная попытка обязана быть возможна, а не упереться в 409"
    finally:
        _shutdown(httpd)


def test_unknown_api_route_answers_human_text_not_raw_not_found(tmp_path):
    httpd, base, token = _hub(tmp_path)
    try:
        status, data = _request(f"{base}/api/no-such-route", token=token)
        assert status == 404
        assert data["error"] != "not found"
        assert "устарел" in data["error"], "сырое not found читалось как «лицензия не найдена» (11.09.2026)"
        assert data["unknown_route"] is True
        assert data["stale"] is False
    finally:
        _shutdown(httpd)


def test_unknown_api_route_names_both_versions_when_page_is_newer(tmp_path):
    httpd, base, token = _hub(tmp_path)
    try:
        status, data = _request(f"{base}/api/no-such-route", token=token, build="99.0.0")
        assert status == 404
        assert data["stale"] is True
        assert "99.0.0" in data["error"]
        assert data["hub_version"] in data["error"]
    finally:
        _shutdown(httpd)


def test_same_build_version_is_not_reported_as_skew(tmp_path):
    from standkit import __version__ as current
    httpd, base, token = _hub(tmp_path)
    try:
        status, data = _request(f"{base}/api/no-such-route", token=token, build=current)
        assert status == 404
        assert data["stale"] is False
    finally:
        _shutdown(httpd)


def test_index_carries_build_version_from_disk(tmp_path):
    httpd, base, token = _hub(tmp_path)
    try:
        req = urllib.request.Request(f"{base}/?t={token}")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            html = resp.read().decode("utf-8")
        assert "__STANDKIT_BUILD__" not in html, "плейсхолдер обязан быть подставлен"
        assert 'name="standkit-build"' in html
    finally:
        _shutdown(httpd)


def test_on_disk_version_reads_the_file_not_the_memory(tmp_path, monkeypatch):
    """Версия берётся ИЗ ФАЙЛА, и это единственное, что делает детект
    рассинхрона возможным.

    Проверяем подменой самого файла, а не сравнением с
    ``standkit.__version__``: наивное «они должны совпасть» ложно краснеет в
    ровно той ситуации, ради которой функция и написана — исходник на диске уже
    новой версии, а процесс импортировал старую (живьём 17.09.2026 при бампе
    0.11.5 → 0.12.0 под работающим pytest)."""
    fake_pkg = tmp_path / "standkit_fake"
    fake_pkg.mkdir()
    init = fake_pkg / "__init__.py"
    init.write_text('__version__ = "99.1.2"\n__all__ = ["__version__"]\n', encoding="utf-8")

    import standkit as _sk
    monkeypatch.setattr(_sk, "__file__", str(init))
    assert on_disk_standkit_version() == "99.1.2", (
        "версия обязана читаться с диска — иначе рассинхрон «файлы новые, процесс старый» "
        "невидим по определению"
    )


def test_on_disk_version_parses_the_real_package():
    # Формат настоящего файла пакета узнаётся регуляркой (защита от того, что
    # объявление __version__ перепишут в форме, которую разбор не поймёт, и
    # детект тихо выключится фолбэком).
    import re as _re
    from pathlib import Path as _Path
    import standkit as _sk
    text = _Path(_sk.__file__).read_text(encoding="utf-8")
    m = _re.search(r"""^__version__\s*=\s*['"]([^'"]+)['"]""", text, _re.MULTILINE)
    assert m is not None, "объявление __version__ перестало узнаваться — детект рассинхрона ослеп"
    assert on_disk_standkit_version() == m.group(1)


def test_on_disk_version_falls_back_quietly(monkeypatch):
    import standkit as _sk
    monkeypatch.setattr(_sk, "__file__", "/нет/такого/файла/__init__.py")
    assert on_disk_standkit_version(default="0.0.0") == "0.0.0"


# --------------------------------------------------------------------------
# Мьютекс
# --------------------------------------------------------------------------


def test_release_hub_mutex_is_idempotent_without_acquire():
    # Без предшествующего захвата (не Windows, или мьютекс не удалось создать)
    # освобождение — тихий no-op, а не исключение на пути выхода.
    assert hub_mutex.release_hub_mutex() is False
    assert hub_mutex.release_hub_mutex() is False


@pytest.mark.skipif(sys.platform != "win32", reason="именованный мьютекс есть только на Windows")
def test_acquire_then_release_hub_mutex():
    assert hub_mutex.acquire_hub_mutex() is True
    assert hub_mutex.release_hub_mutex() is True
    assert hub_mutex.release_hub_mutex() is False, "повторное освобождение — no-op"
