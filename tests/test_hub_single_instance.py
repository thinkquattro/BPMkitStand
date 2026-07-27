"""
Тесты single-instance защиты хаба (standkit_hub.server).

ЗАЧЕМ ЭТО ЕСТЬ. Ярлык на рабочем столе запускает диспетчер через
``pythonw.exe`` — без консоли, окна нет. Закрытие браузера НЕ останавливает
процесс: сервер живёт в ``serve_forever``, idle-shutdown у хаба нет. Поэтому
повторный клик по ярлыку раньше поднимал второй экземпляр (порт 8770 занят →
откат на эфемерный), и над одним ``projects.json`` начинали работать два
фоновых поллера, а окно открывалось на другом origin со своей копией
localStorage. Сообщение об откате уходило в stderr, которого при ``pythonw``
никто не видит.

Проверяем обе ветки развилки: порт занял НАШ хаб (второй не поднимаем) и порт
занял чужой сервис (прежний откат на эфемерный сохраняется).
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from standkit_hub.config import HubConfig
from standkit_hub.security import generate_session_token
from standkit_hub.server import (
    HUB_IDENTITY_HEADER,
    HubAlreadyRunning,
    bind_hub_server,
    create_hub_server,
    probe_hub_instance,
)


def _write_config(tmp_path):
    registry_path = tmp_path / "projects.json"
    registry_path.write_text('{"projects": {}}', encoding="utf-8")
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path)).save(config_path)
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


def _shutdown(httpd) -> None:
    poller = getattr(httpd, "status_poller", None)
    if poller is not None:
        try:
            poller.stop(timeout=0.2)
        except TypeError:
            poller.stop()
    threading.Thread(target=httpd.shutdown, daemon=True).start()
    httpd.server_close()


@pytest.fixture()
def running_hub(tmp_path):
    """Живой хаб на эфемерном порту — имитация «забытого» процесса от ярлыка."""
    config_path = _write_config(tmp_path)
    httpd = create_hub_server(
        "127.0.0.1", 0, config_path=config_path, session_token=generate_session_token(), poll=False
    )
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _wait_for_port(port)
    try:
        yield port, config_path
    finally:
        _shutdown(httpd)


def test_probe_recognizes_running_hub(running_hub):
    """Опознание идёт БЕЗ токена — у второго процесса его нет и быть не может."""
    port, _ = running_hub
    assert probe_hub_instance("127.0.0.1", port) is not None


def test_probe_returns_none_for_foreign_service(tmp_path):
    """Чужой сокет без заголовка-опознавателя — это не наш хаб."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept_and_close():
        try:
            conn, _ = srv.accept()
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_accept_and_close, daemon=True).start()
    try:
        assert probe_hub_instance("127.0.0.1", port, timeout=1.0) is None
    finally:
        srv.close()


def test_probe_returns_none_when_nothing_listens(tmp_path):
    """Свободный порт: соединение отвергается — консервативный ответ «не наш»."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
    assert probe_hub_instance("127.0.0.1", free_port, timeout=1.0) is None


def test_identity_header_present_without_auth(running_hub):
    """
    Заголовок должен быть и на 401 — иначе опознать работающий экземпляр без
    токена невозможно, а токен чужого процесса нам недоступен.
    """
    import http.client

    port, _ = running_hub
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    conn.request("GET", "/api/stands")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 401
    assert resp.getheader(HUB_IDENTITY_HEADER)
    conn.close()


def test_bind_refuses_second_instance_on_busy_hub_port(running_hub, tmp_path):
    """Главный сценарий: повторный клик по ярлыку не плодит второй поллер."""
    port, config_path = running_hub
    with pytest.raises(HubAlreadyRunning) as excinfo:
        bind_hub_server(
            "127.0.0.1",
            port,
            config_path=config_path,
            session_token=generate_session_token(),
        )
    exc = excinfo.value
    assert exc.port == port
    # CLI открывает браузер именно по этому URL — без токена, его подставить
    # неоткуда; работающий экземпляр узнаёт браузер по сессионной cookie.
    assert exc.url == f"http://127.0.0.1:{port}/"


def test_bind_falls_back_when_port_taken_by_foreign_service(tmp_path):
    """Чужой сервис на порту — прежнее поведение: откат на эфемерный."""
    config_path = _write_config(tmp_path)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    busy_port = srv.getsockname()[1]

    fallbacks: list = []
    httpd = None
    try:
        httpd = bind_hub_server(
            "127.0.0.1",
            busy_port,
            config_path=config_path,
            session_token=generate_session_token(),
            poll=False,
            on_fallback=lambda requested, exc: fallbacks.append(requested),
        )
        assert httpd.server_address[1] != busy_port
        assert fallbacks == [busy_port]
    finally:
        if httpd is not None:
            httpd.server_close()
        srv.close()


def test_single_instance_false_restores_previous_behaviour(running_hub, tmp_path):
    """
    Явный отказ от защиты — прежнее поведение, без ``HubAlreadyRunning``.

    Конкретный порт здесь НЕ проверяем: он платформозависим. На Windows
    ``SO_REUSEADDR`` даёт занять уже слушаемый порт (bind проходит, это и есть
    то самое тихое раздвоение, ради которого сделана проверка до bind'а), на
    POSIX прилетает EADDRINUSE и срабатывает откат на эфемерный. Значимо лишь
    то, что сервер поднялся и исключения не было.
    """
    port, config_path = running_hub
    httpd = bind_hub_server(
        "127.0.0.1",
        port,
        config_path=config_path,
        session_token=generate_session_token(),
        poll=False,
        single_instance=False,
    )
    try:
        assert httpd.server_address[1] > 0
    finally:
        httpd.server_close()
