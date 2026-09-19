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


# --- GAP-278: визуальные дефекты диспетчера ----------------------------------
#
# Приёмка гэпа визуальная (скриншоты в двух темах), и заменить её тестом
# нельзя. Но ровно те элементы, ОТСУТСТВИЕ которых и было жалобой владельца,
# проверяются статически — чтобы правка не откатилась молча при следующем
# редактировании разметки.


def _read_web(name: str) -> str:
    return (WEB_DIR / name).read_text(encoding="utf-8")


def test_header_uses_full_logo_not_favicon():
    """
    П.1: в шапке стоял favicon.svg (значок «k»), то есть продукт представлялся
    обрезанным значком. Должен быть полный логотип — парой под светлую и
    тёмную тему.
    """
    html = _read_web("index.html")
    # Именно <header>, а не всё до </header>: в <head> стоит <link rel="icon">
    # на тот же favicon, и он там законен — во вкладке значок и должен быть.
    header = html.split("<header", 1)[1].split("</header>", 1)[0]

    assert "bpmkit-logo.svg" in header
    assert "bpmkit-logo-dark.svg" in header
    assert "favicon.svg" not in header


def test_header_separates_product_from_section():
    html = _read_web("index.html")
    header = html.split("<header", 1)[1].split("</header>", 1)[0]
    assert "brand-sep" in header
    # Название раздела без повтора имени продукта — бренд теперь рисует логотип.
    assert "<h1>Диспетчер стендов</h1>" in header


def test_brand_logo_switches_by_theme_like_about_logo():
    """Переключение темы — CSS-правилами, той же механикой, что у .about-logo-*."""
    css = _read_web("style.css")
    assert '[data-theme="dark"] .brand-logo-light' in css
    assert '[data-theme="dark"] .brand-logo-dark' in css
    assert '[data-theme="auto"] .brand-logo-dark' in css


def test_table_toolbar_replaces_loose_button_row():
    """
    П.2: «Обновить» и «Зарегистрировать стенд» висели отдельной строкой над
    таблицей. Теперь это заголовок таблицы: счётчик слева, возраст данных и
    действия справа.
    """
    html = _read_web("index.html")
    assert 'id="stands-count"' in html
    assert "panel-toolbar-right" in html
    # Ручное обновление осталось, но стало иконкой, а не главной кнопкой экрана.
    assert 'id="refresh-stands-btn"' in html
    assert "icon-btn-refresh" in html
    assert ">+ Стенд<" in html


def test_empty_registry_offers_first_stand():
    """П.2: пустой реестр показывал пустую таблицу с шапкой колонок."""
    html = _read_web("index.html")
    assert 'id="stands-empty"' in html
    assert 'id="register-first-stand-btn"' in html

    app = _read_web("app.js")
    # Приглашение и таблица переключаются по числу стендов.
    assert 'getElementById("stands-empty")' in app


def test_about_answers_about_updates_not_edition():
    """
    П.3: строка «Редакция: с каналом обновлений» — перевод внутреннего
    edition=companion. Пользователь спрашивает про обновления, а не про
    редакцию, и ему нужна ссылка туда, где это чинится.
    """
    html = _read_web("index.html")
    # Проверяем ПОДПИСЬ строки, а не любое вхождение слова: объяснение, почему
    # строка переименована, живёт в комментарии рядом и упоминает старое имя.
    assert '<div class="lic-k">Редакция</div>' not in html
    assert '<div class="lic-k">Обновления</div>' in html
    assert 'id="about-license-link"' in html

    app = _read_web("app.js")
    assert "renderAboutUpdates" in app
    # Старый текст не ПРИСВАИВАЕТСЯ элементу (в комментарии рядом он остаётся:
    # там объясняется, что именно было заменено и почему).
    assert 'edition.textContent' not in app
    assert "не подключены — нет лицензии" in app


def test_snapshot_age_is_always_visible():
    """
    П.2: возраст снапшота показывался ТОЛЬКО при устаревании вдвое, поэтому
    признака «список живой» не было вовсе — отсюда и впечатление, что без
    кнопки «Обновить» таблица мёртвая.
    """
    app = _read_web("app.js")
    assert "обновлено ${formatAge(age)} назад" in app
    assert "stands-age-stale" in app
