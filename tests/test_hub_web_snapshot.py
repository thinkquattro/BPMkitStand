"""
Статика хаба переживает пропажу каталога ``web/`` у работающего процесса.

Инцидент 29.10.2026: в окне диспетчера вместо страницы -- JSON
``{"error": "index.html не найден в пакете хаба"}``. Диспетчер был exe-сборкой
(PyInstaller onefile): ``web/`` лежит во временном ``_MEIxxxx``, и постороннюю уборку
``_MEI*`` процесс не пережил -- каталог вынесло из-под работающего хаба (процесс жив,
порт слушает, ``/`` отвечает 500). Хаб держит снимок ``web/`` в памяти и отдаёт его только
когда файла на диске уже нет.

Второй блок -- упаковка: каждый файл ``standkit_hub/web`` обязан попадать в wheel/sdist
(``package-data`` + ``MANIFEST.in``), иначе pip-установка получит ту же ошибку.
"""

from __future__ import annotations

import fnmatch
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

import standkit_hub.server as server_module
from standkit.models import Stand
from standkit.registry import Registry
from standkit_hub.config import HubConfig
from standkit_hub.security import generate_session_token
from standkit_hub.server import create_hub_server

PKG_WEB = Path(server_module.__file__).parent / "web"
REPO = Path(server_module.__file__).parent.parent


def _start(tmp_path, web_dir):
    registry_path = tmp_path / "projects.json"
    Registry(
        path=registry_path,
        default="alpha",
        stands={"alpha": Stand(name="alpha", stand_dir=str(tmp_path / "alpha"))},
    ).save()
    config_path = tmp_path / "standkit-hub.json"
    HubConfig(registry_path=str(registry_path)).save(config_path)
    token = generate_session_token()
    httpd = create_hub_server(
        "127.0.0.1", 0, config_path=config_path, session_token=token, web_dir=web_dir)
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


def _get(base, path, *, token=None):
    req = urllib.request.Request(base + path, method="GET")
    if token:
        req.add_header("X-Standkit-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            return resp.status, resp.read().decode("utf-8"), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


@pytest.fixture()
def hub_with_copy(tmp_path):
    web = tmp_path / "web_copy"
    shutil.copytree(PKG_WEB, web)
    base, token, httpd = _start(tmp_path, web)
    try:
        yield base, token, web
    finally:
        httpd.shutdown()
        httpd.server_close()


class TestWebDirVanishes:
    def test_index_and_static_served_from_disk_while_files_exist(self, hub_with_copy):
        base, token, _web = hub_with_copy
        code, body, _ = _get(base, "/?token=" + token)
        assert code in (200, 302)
        code, body, _ = _get(base, "/static/style.css")
        assert code == 200 and body

    def test_disk_wins_over_snapshot(self, hub_with_copy):
        """Файл на диске изменили (pip -U на ходу) -- отдаётся новый, не снимок."""
        base, _token, web = hub_with_copy
        (web / "style.css").write_text("/* fresh */", encoding="utf-8")
        code, body, _ = _get(base, "/static/style.css")
        assert code == 200 and body == "/* fresh */"

    def test_static_survives_removed_web_dir(self, hub_with_copy):
        base, _token, web = hub_with_copy
        expected = (PKG_WEB / "app.js").read_text(encoding="utf-8")
        shutil.rmtree(web)
        code, body, headers = _get(base, "/static/app.js")
        assert code == 200
        assert body == expected
        assert "ETag" in headers

    def test_index_survives_removed_web_dir(self, hub_with_copy):
        base, token, web = hub_with_copy
        shutil.rmtree(web)
        code, body, _ = _get(base, "/?token=" + token)
        assert code == 200, body
        assert "index.html не найден" not in body
        assert "<html" in body.lower()

    def test_unknown_and_traversal_still_404_after_removal(self, hub_with_copy):
        base, _token, web = hub_with_copy
        shutil.rmtree(web)
        assert _get(base, "/static/nope.js")[0] == 404
        assert _get(base, "/static/%2e%2e/config.py")[0] == 404

    def test_no_snapshot_and_no_file_is_still_500(self, tmp_path):
        """Пустой web/ с самого начала -- прежнее поведение (честный 500)."""
        empty = tmp_path / "empty_web"
        empty.mkdir()
        base, token, httpd = _start(tmp_path, empty)
        try:
            code, body, _ = _get(base, "/?token=" + token)
            assert code == 500
            assert "index.html не найден" in body
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestWebPackaging:
    def test_every_web_file_matches_package_data(self):
        try:
            import tomllib
        except ImportError:  # py3.10
            pytest.skip("tomllib недоступен")
        cfg = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
        globs = cfg["tool"]["setuptools"]["package-data"]["standkit_hub"]
        files = [p for p in PKG_WEB.rglob("*") if p.is_file()]
        assert any(p.name == "index.html" for p in files)
        for p in files:
            rel = p.relative_to(Path(server_module.__file__).parent).as_posix()
            assert any(fnmatch.fnmatch(rel, g) for g in globs), (
                f"{rel} не покрыт package-data {globs}: в wheel не попадёт")

    @pytest.mark.skipif(shutil.which("pip") is None and shutil.which("pip3") is None,
                        reason="нет pip")
    def test_built_wheel_contains_web(self, tmp_path):
        import subprocess
        import sys
        out = tmp_path / "wheel"
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "-q", "-w", str(out), str(REPO)],
            capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            pytest.skip("wheel не собирается в этой среде: " + proc.stderr[-300:])
        wheel = next(out.glob("standkit-*.whl"))
        names = set(zipfile.ZipFile(wheel).namelist())
        for p in PKG_WEB.rglob("*"):
            if p.is_file():
                assert "standkit_hub/web/" + p.relative_to(PKG_WEB).as_posix() in names
