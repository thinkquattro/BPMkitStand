"""
Тесты PWA-манифеста и компактного режима (``?view=compact``).

Оба механизма не добавляют ни одной зависимости: манифест — статический файл,
компактный режим — набор CSS-правил под атрибутом ``data-view``, который
сервер проставляет при отдаче index.html.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

import standkit_hub.client as hub_client_module
import standkit_hub.server as server_module
from standkit.models import Stand
from standkit.registry import Registry
from standkit_hub.config import HubConfig
from standkit_hub.security import generate_session_token
from standkit_hub.server import create_hub_server, normalize_view


WEB_DIR = server_module.Path(server_module.__file__).parent / "web"


def _start(tmp_path, **config_kwargs):
    registry_path = tmp_path / "projects.json"
    Registry(
        path=registry_path,
        default="alpha",
        stands={"alpha": Stand(name="alpha", stand_dir=str(tmp_path / "alpha"))},
    ).save()
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path), **config_kwargs).save(config_path)

    token = generate_session_token()
    httpd = create_hub_server("127.0.0.1", 0, config_path=config_path, session_token=token)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    return f"http://127.0.0.1:{port}", token, httpd


def _get(base_url: str, path: str, *, token: str | None = None):
    req = urllib.request.Request(base_url + path, method="GET")
    if token:
        req.add_header("X-Standkit-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


# --------------------------------------------------------------------------
# Манифест
# --------------------------------------------------------------------------


def test_manifest_file_is_valid_json_with_required_pwa_fields():
    """
    Chrome показывает «Установить приложение», только если манифест содержит
    name/short_name, start_url, display и иконку не меньше 192 px.
    """
    manifest = json.loads((WEB_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))

    assert manifest["name"]
    assert manifest["short_name"]
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    assert manifest["display"] == "standalone"

    icons = manifest["icons"]
    assert icons, "без иконок установка приложения недоступна"
    png = [i for i in icons if i.get("type") == "image/png"]
    assert png, "нужна хотя бы одна PNG-иконка — SVG принимают не все браузеры"
    assert any(i.get("sizes") == "512x512" for i in png)
    assert any(i.get("purpose") == "maskable" for i in icons)

    # Все файлы иконок должны реально лежать в пакете, иначе установка
    # молча деградирует до дефолтной иконки браузера.
    for icon in icons:
        rel = icon["src"].removeprefix("/static/")
        assert (WEB_DIR / rel).is_file(), f"иконка {icon['src']} отсутствует в web/"

    # Ярлык на компактный режим — это и есть «виджет» из бэклога.
    shortcut_urls = [s["url"] for s in manifest.get("shortcuts", [])]
    assert "/?view=compact" in shortcut_urls


def test_manifest_is_served_with_correct_content_type(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    base_url, token, httpd = _start(tmp_path)
    try:
        status, body, headers = _get(base_url, "/static/manifest.webmanifest")
        assert status == 200
        # Именно этот тип; с application/json Chrome манифест игнорирует.
        assert headers.get("Content-Type", "").startswith("application/manifest+json")
        assert json.loads(body)["scope"] == "/"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_index_links_manifest_and_icons(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    base_url, token, httpd = _start(tmp_path)
    try:
        status, body, _ = _get(base_url, f"/?t={token}")
        assert status == 200
        assert 'rel="manifest"' in body
        assert "/static/manifest.webmanifest" in body
        assert 'name="theme-color"' in body
    finally:
        httpd.shutdown()
        httpd.server_close()


# --------------------------------------------------------------------------
# Компактный режим
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("compact", "compact"),
        ("COMPACT", "compact"),
        ("  compact  ", "compact"),
        ("full", "full"),
        ("widget", "full"),
        ("", "full"),
        (None, "full"),
        (17, "full"),
    ],
)
def test_normalize_view(raw, expected):
    assert normalize_view(raw) == expected


def test_root_sets_data_view_from_query(tmp_path, monkeypatch):
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    base_url, token, httpd = _start(tmp_path)
    try:
        status, body, _ = _get(base_url, f"/?t={token}")
        assert status == 200
        assert 'data-view="full"' in body

        status, body, _ = _get(base_url, f"/?t={token}&view=compact")
        assert status == 200
        assert 'data-view="compact"' in body

        # Мусорное значение не должно ни ронять страницу, ни утекать в HTML.
        status, body, _ = _get(base_url, f"/?t={token}&view=%3Cscript%3E")
        assert status == 200
        assert 'data-view="full"' in body
        assert "<script>" not in body.split("</head>")[0]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_placeholder_is_always_replaced(tmp_path, monkeypatch):
    """
    Незаменённый плейсхолдер — заметный баг вёрстки, поэтому проверяем оба
    пути отдачи index.html: аутентифицированный и анонимный.
    """
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    base_url, token, httpd = _start(tmp_path)
    try:
        for path in (f"/?t={token}", "/", "/?view=compact"):
            status, body, _ = _get(base_url, path)
            assert status == 200
            assert "__STANDKIT_VIEW__" not in body
            assert "__STANDKIT_THEME__" not in body
            assert "__STANDKIT_TOKEN__" not in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_anonymous_root_does_not_leak_token_in_compact_mode(tmp_path, monkeypatch):
    """
    Компактный режим — обычная страница, а не обход авторизации: без токена
    он не должен отдавать сессионный токен в <meta>.
    """
    monkeypatch.setattr(hub_client_module.FederatedClient, "status_all", lambda self: {})
    base_url, token, httpd = _start(tmp_path)
    try:
        status, body, _ = _get(base_url, "/?view=compact")
        assert status == 200
        assert token not in body
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_compact_css_rules_exist():
    """
    Компактный режим держится на CSS: если правила потеряются при рефакторинге
    стилей, окно молча станет полноразмерным. Проверяем наличие якорных
    селекторов.
    """
    css = (WEB_DIR / "style.css").read_text(encoding="utf-8")
    assert '[data-view="compact"]' in css
    assert '[data-view="compact"] .state-panel' in css
    assert '[data-view="compact"] .stands-table thead' in css
